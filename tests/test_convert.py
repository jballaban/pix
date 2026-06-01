"""Tests for `pix.convert` codec/profile probing and canonical-codec check.

Covers `probe_video_profile` output parsing (multi-field ffprobe) and the
`is_canonical_video_codec` check (HEVC = canonical) that drives re-mux vs
re-encode.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from PIL import Image

from pix import convert
from pix.convert import (
    VideoProfile,
    convert_to_jpg,
    is_canonical_video_codec,
    probe_video_profile,
)


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


@dataclass
class _FakeCompletedProcess:
    """Drop-in for `subprocess.CompletedProcess` so we can monkeypatch run()."""

    stdout: str
    returncode: int = 0
    stderr: str = ""


def _fake_run(stdout: str, returncode: int = 0):
    """Build a `subprocess.run` replacement that always returns `stdout`."""

    def runner(*_args: Any, **_kwargs: Any) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(stdout=stdout, returncode=returncode)

    return runner


@pytest.mark.parametrize(
    "stdout,expected",
    [
        # ffprobe `default=nw=1:nk=1` with codec_name,profile,pix_fmt emits
        # one line per field, in the order requested.
        (
            "h264\nMain\nyuv420p\n",
            VideoProfile(codec="h264", profile="Main", pix_fmt="yuv420p"),
        ),
        (
            "h264\nHigh 4:2:2\nyuvj422p\n",
            VideoProfile(
                codec="h264", profile="High 4:2:2", pix_fmt="yuvj422p"
            ),
        ),
        (
            "hevc\nMain\nyuv420p\n",
            VideoProfile(codec="hevc", profile="Main", pix_fmt="yuv420p"),
        ),
        # Casing: codec + pix_fmt lowercased, profile preserved.
        (
            "H264\nHigh\nYUV420P\n",
            VideoProfile(codec="h264", profile="High", pix_fmt="yuv420p"),
        ),
        # Tolerate missing tail values (some old containers don't expose
        # profile or pix_fmt) — they pad to "" and the codec check
        # will route to re-encode.
        (
            "mpeg2video\n\n\n",
            VideoProfile(codec="mpeg2video", profile="", pix_fmt=""),
        ),
        # Trailing-comma tolerance kept from the v0.1.56 fix.
        (
            "hevc,\nMain\nyuv420p\n",
            VideoProfile(codec="hevc", profile="Main", pix_fmt="yuv420p"),
        ),
    ],
)
def test_probe_video_profile_parses_output(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    expected: VideoProfile,
) -> None:
    def _fake_require(_name: str) -> str:
        return "ffprobe"

    monkeypatch.setattr(convert, "_require_tool", _fake_require)
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout))
    assert probe_video_profile(Path("/fake/video.mov")) == expected


@pytest.mark.parametrize(
    "profile,canonical",
    [
        # HEVC is the canonical codec — re-mux only, any profile/pix_fmt.
        (VideoProfile("hevc", "Main", "yuv420p"), True),
        (VideoProfile("hevc", "Main 10", "yuv420p10le"), True),
        (VideoProfile("hevc", "Rext", "yuv422p"), True),
        # Everything else re-encodes to HEVC — including formerly
        # "Windows-playable" H.264 4:2:0 (now still re-encoded for efficiency).
        (VideoProfile("h264", "Main", "yuv420p"), False),
        (VideoProfile("h264", "High", "yuvj420p"), False),
        (VideoProfile("h264", "High 4:2:2", "yuvj422p"), False),
        (VideoProfile("h264", "High 10", "yuv420p10le"), False),
        (VideoProfile("mpeg2video", "Main", "yuv420p"), False),
        (VideoProfile("mpeg4", "Simple Profile", "yuv420p"), False),
        (VideoProfile("vp9", "Profile 0", "yuv420p"), False),
        (VideoProfile("av1", "Main", "yuv420p"), False),
        # Unknown / probe-incomplete.
        (VideoProfile("", "", ""), False),
    ],
)
def test_is_canonical_video_codec(
    profile: VideoProfile, canonical: bool
) -> None:
    assert is_canonical_video_codec(profile) is canonical
