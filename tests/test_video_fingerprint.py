"""Tests for perceptual video fingerprinting (pix.video_fingerprint)."""

from __future__ import annotations

# pyright: reportPrivateUsage=false

import shutil
import subprocess
from typing import Any

import pytest

from pix import video_fingerprint as vf
from pix.video_fingerprint import (
    FP_BITS,
    FingerprintFailed,
    compute_fingerprint,
    fingerprint_distance,
)


def _require_ffprobe(_name: str) -> str:
    return "ffprobe"


# --- fingerprint_distance -------------------------------------------------

def test_distance_identical_is_zero() -> None:
    fp = (1, 2, 3, 4, 5, 6)
    assert fingerprint_distance(fp, fp) == 0


def test_distance_counts_differing_bits() -> None:
    # 0b1010 vs 0b0001 -> xor 0b1011 -> 3 bits; plus 0 vs 0b11 -> 2 bits
    assert fingerprint_distance((0b1010, 0), (0b0001, 0b11)) == 5


def test_distance_failed_frame_never_matches() -> None:
    assert fingerprint_distance((1, -1, 3), (1, 2, 3)) == FP_BITS + 1
    assert fingerprint_distance((1, 2, 3), (1, -1, 3)) == FP_BITS + 1


def test_distance_length_mismatch_never_matches() -> None:
    assert fingerprint_distance((1, 2), (1, 2, 3)) == FP_BITS + 1


# --- dHash bit logic ------------------------------------------------------

def _fake_frame(buf: bytes):
    class _P:
        stdout = buf
    def run(*_a: Any, **_k: Any) -> "_P":
        return _P()
    return run


def test_dhash_increasing_rows_all_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    # each row 0..8 (left < right everywhere) -> every bit 0
    buf = bytes(col for _row in range(8) for col in range(9))
    monkeypatch.setattr(vf.subprocess, "run", _fake_frame(buf))
    assert vf._dhash_at("ffmpeg", "x.mp4", 1.0) == 0


def test_dhash_decreasing_rows_all_one(monkeypatch: pytest.MonkeyPatch) -> None:
    # each row 8..0 (left > right everywhere) -> every bit 1 -> 64 ones
    buf = bytes(8 - col for _row in range(8) for col in range(9))
    monkeypatch.setattr(vf.subprocess, "run", _fake_frame(buf))
    assert vf._dhash_at("ffmpeg", "x.mp4", 1.0) == (1 << 64) - 1


def test_dhash_short_read_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vf.subprocess, "run", _fake_frame(b"\x00" * 10))
    assert vf._dhash_at("ffmpeg", "x.mp4", 1.0) == -1


# --- geometry probe parse -------------------------------------------------

def _fake_probe(stdout: str):
    class _P:
        pass
    p = _P()
    p.stdout = stdout  # type: ignore[attr-defined]
    def run(*_a: Any, **_k: Any) -> "_P":
        return p
    return run


def test_probe_geometry_parses_key_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vf, "_require", _require_ffprobe)
    monkeypatch.setattr(
        vf.subprocess, "run",
        _fake_probe("width=1920\nheight=1080\nduration=12.500\n"),
    )
    assert vf._probe_geometry("x.mp4") == (1920, 1080, 12.5)


def test_probe_geometry_raises_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vf, "_require", _require_ffprobe)
    monkeypatch.setattr(vf.subprocess, "run", _fake_probe("nonsense\n"))
    with pytest.raises(FingerprintFailed):
        vf._probe_geometry("x.mp4")


# --- real ffmpeg integration (skipped if ffmpeg/ffprobe absent) -----------

@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not on PATH",
)
def test_compute_fingerprint_real(tmp_path: Any) -> None:
    src = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=320x240:rate=30:duration=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True,
    )
    fp = compute_fingerprint(str(src))
    assert (fp.width, fp.height) == (320, 240)
    assert 1.8 <= fp.duration <= 2.2
    assert len(fp.frames) == len(vf.FRAC)
    assert all(f >= 0 for f in fp.frames)
    # a clip is identical to itself
    assert fingerprint_distance(fp.frames, fp.frames) == 0
