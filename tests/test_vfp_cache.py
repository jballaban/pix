"""Tests for the perceptual-fingerprint cache (pix.vfp_cache)."""

from __future__ import annotations

from pathlib import Path

from pix.vfp_cache import (
    read_cached_fingerprint,
    write_cached_fingerprint,
)
from pix.video_fingerprint import VideoFingerprint


def _lib(tmp_path: Path) -> Path:
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    return root


def _fp() -> VideoFingerprint:
    return VideoFingerprint(
        frames=(1, 2, 3, 4, 5, 6), width=1920, height=1080, duration=12.5
    )


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    root = _lib(tmp_path)
    f = (root / "clip.mp4").resolve()
    f.write_bytes(b"video-bytes")
    st = f.stat()
    write_cached_fingerprint(
        root, f, fingerprint=_fp(), size=st.st_size, mtime_ns=st.st_mtime_ns
    )
    got = read_cached_fingerprint(root, f)
    assert got == _fp()


def test_stale_on_size_change(tmp_path: Path) -> None:
    root = _lib(tmp_path)
    f = (root / "clip.mp4").resolve()
    f.write_bytes(b"video-bytes")
    st = f.stat()
    write_cached_fingerprint(
        root, f, fingerprint=_fp(), size=st.st_size, mtime_ns=st.st_mtime_ns
    )
    f.write_bytes(b"video-bytes-now-longer")  # size + mtime change
    assert read_cached_fingerprint(root, f) is None


def test_missing_returns_none(tmp_path: Path) -> None:
    root = _lib(tmp_path)
    f = (root / "clip.mp4").resolve()
    f.write_bytes(b"x")
    assert read_cached_fingerprint(root, f) is None
