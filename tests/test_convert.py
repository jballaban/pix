"""Tests for `pix.convert`.

Focused on the codec-detection parsing — historical bug where ffprobe's
`csv=p=0` output included a trailing comma, silently routing every
H.264/HEVC video through the libx265 re-encode path instead of the
intended `-c copy` re-mux.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pix import convert
from pix.convert import H264_HEVC_CODECS, _probe_video_codec


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
        ("hevc\n", "hevc"),  # default=nw=1:nk=1 — what we now ask for
        ("h264\n", "h264"),
        ("hevc", "hevc"),    # no trailing newline
        ("HEVC\n", "hevc"),  # casing
        ("  hevc  \n", "hevc"),  # whitespace tolerance
        ("hevc,\n", "hevc"),  # historical csv=p=0 trailing-comma quirk
        ("hevc,extra\n", "hevc"),  # tolerate multi-field future format
    ],
)
def test_probe_video_codec_parses_output(
    monkeypatch: pytest.MonkeyPatch, stdout: str, expected: str
) -> None:
    monkeypatch.setattr(convert, "_require_tool", lambda _name: "ffprobe")
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout))
    assert _probe_video_codec(Path("/fake/video.mov")) == expected


@pytest.mark.parametrize("codec", ["hevc", "h264"])
def test_iphone_codec_strings_match_remux_set(codec: str) -> None:
    """Sanity: iPhone MOV codecs land in the re-mux fast path.

    Guards against the historical bug — if `H264_HEVC_CODECS` ever loses
    'hevc' (or someone rewrites it as 'HEVC'), iPhone videos go back to
    re-encoding silently.
    """
    assert codec in H264_HEVC_CODECS
