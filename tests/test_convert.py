"""Tests for `pix.convert`.

Covers the JPEG re-encode path and the remux-only `convert_to_mp4`: a
lossless `-c copy` with an audio-only AAC fallback when MP4 can't carry the
source's audio codec. pix never re-encodes video, so there is no codec
probe / canonical-codec branch anymore.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from PIL import Image

from pix import convert
from pix.convert import ConvertFailed, convert_to_jpg, convert_to_mp4


def test_convert_to_jpg_handles_bmp(tmp_path: Path) -> None:
    """BMP is a supported convert_to_jpg source — Pillow decodes it natively."""
    src = tmp_path / "image.bmp"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(src, format="BMP")
    dst = tmp_path / "out.jpg"

    convert_to_jpg(src, dst)

    assert dst.is_file()
    with Image.open(dst) as out:
        assert out.format == "JPEG"
        assert out.size == (8, 8)


def test_convert_to_jpg_handles_paletted_bmp(tmp_path: Path) -> None:
    """A paletted (mode 'P') BMP is coerced to RGB before the JPEG encode."""
    src = tmp_path / "paletted.bmp"
    Image.new("P", (8, 8)).save(src, format="BMP")
    dst = tmp_path / "out.jpg"

    convert_to_jpg(src, dst)

    with Image.open(dst) as out:
        assert out.format == "JPEG"


class _RunRecorder:
    """Records the ffmpeg commands passed to `subprocess.run` and replays a
    scripted sequence of return codes (one per call)."""

    def __init__(self, returncodes: list[int]) -> None:
        self._returncodes = returncodes
        self.calls: list[list[str]] = []

    def __call__(
        self, cmd: list[str], *_args: Any, **_kwargs: Any
    ) -> "subprocess.CompletedProcess[str]":
        self.calls.append(cmd)
        rc = self._returncodes[len(self.calls) - 1]
        return subprocess.CompletedProcess(
            args=cmd, returncode=rc, stdout="", stderr="boom" if rc else ""
        )


def _fake_require(_name: str) -> str:
    return "ffmpeg"


def _patch(monkeypatch: pytest.MonkeyPatch, recorder: _RunRecorder) -> None:
    monkeypatch.setattr(convert, "_require_tool", _fake_require)
    monkeypatch.setattr(subprocess, "run", recorder)


def test_convert_to_mp4_remuxes_with_c_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path is a single lossless `-c copy` remux — no re-encode,
    no fallback."""
    rec = _RunRecorder([0])
    _patch(monkeypatch, rec)

    convert_to_mp4(Path("/in.mov"), Path("/out.mp4"))

    assert len(rec.calls) == 1
    cmd = rec.calls[0]
    # Whole-stream copy; never a video encoder.
    assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"
    assert "libx265" not in cmd and "hevc_nvenc" not in cmd
    assert "+faststart" in cmd


def test_convert_to_mp4_falls_back_to_aac_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `-c copy` fails (e.g. PCM audio MP4 can't carry), it retries
    copying the video bitstream while re-encoding only the audio to AAC."""
    rec = _RunRecorder([1, 0])  # first attempt fails, fallback succeeds
    _patch(monkeypatch, rec)

    convert_to_mp4(Path("/in.avi"), Path("/out.mp4"))

    assert len(rec.calls) == 2
    fallback = rec.calls[1]
    # Video still copied verbatim; only audio re-encoded.
    assert fallback[fallback.index("-c:v") + 1] == "copy"
    assert fallback[fallback.index("-c:a") + 1] == "aac"


def test_convert_to_mp4_raises_when_video_unmuxable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If even the video bitstream can't be muxed into MP4, both attempts
    fail and the caller gets a ConvertFailed to quarantine on."""
    rec = _RunRecorder([1, 1])
    _patch(monkeypatch, rec)

    with pytest.raises(ConvertFailed):
        convert_to_mp4(Path("/in.avi"), Path("/out.mp4"))

    assert len(rec.calls) == 2
