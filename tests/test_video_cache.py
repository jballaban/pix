"""Tests for `pix.video_cache` — the ffprobe profile cache."""

from __future__ import annotations

from pathlib import Path

from pix.convert import VideoProfile
from pix.video_cache import (
    cache_path_for,
    read_all_cached_profiles,
    read_cached_profile,
    write_cached_profile,
)


def _write_video(tmp_path: Path, name: str, content: bytes = b"") -> Path:
    p = tmp_path / name
    p.write_bytes(content)
    return p


def test_cache_path_mirrors_source(tmp_path: Path) -> None:
    """Cache file mirrors the media path under .pix/cache/ with `.video` suffix."""
    library_root = tmp_path / "lib"
    media = Path("G:/pix/raw/2023/foo.mp4")
    expected = (
        library_root
        / ".pix"
        / "cache"
        / "G"
        / "pix"
        / "raw"
        / "2023"
        / "foo.mp4.video"
    )
    assert cache_path_for(library_root, media) == expected


def test_round_trip(tmp_path: Path) -> None:
    """write_cached_profile + read_cached_profile preserve the triple."""
    media = _write_video(tmp_path, "v.mp4", b"abc")
    st = media.stat()
    profile = VideoProfile(codec="h264", profile="Main", pix_fmt="yuv420p")
    write_cached_profile(
        tmp_path,
        media,
        profile=profile,
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
    )
    assert read_cached_profile(tmp_path, media) == profile


def test_stale_size_invalidates(tmp_path: Path) -> None:
    """A file whose size changed since the cache entry is treated as stale."""
    media = _write_video(tmp_path, "v.mp4", b"abc")
    st = media.stat()
    profile = VideoProfile(codec="h264", profile="Main", pix_fmt="yuv420p")
    write_cached_profile(
        tmp_path, media,
        profile=profile, size=st.st_size, mtime_ns=st.st_mtime_ns,
    )
    # Rewrite with different size; mtime updates too.
    media.write_bytes(b"abcdef")
    assert read_cached_profile(tmp_path, media) is None


def test_missing_cache_returns_none(tmp_path: Path) -> None:
    media = _write_video(tmp_path, "v.mp4")
    assert read_cached_profile(tmp_path, media) is None


def test_read_all_cached_profiles_splits_hits_and_misses(
    tmp_path: Path,
) -> None:
    """Parallel read returns one entry per input path; misses are None."""
    a = _write_video(tmp_path, "a.mp4", b"a")
    b = _write_video(tmp_path, "b.mp4", b"b")
    c = _write_video(tmp_path, "c.mp4", b"c")  # left uncached

    profile_a = VideoProfile("h264", "Main", "yuv420p")
    profile_b = VideoProfile("h264", "High 4:2:2", "yuvj422p")
    sa, sb = a.stat(), b.stat()
    write_cached_profile(
        tmp_path, a,
        profile=profile_a, size=sa.st_size, mtime_ns=sa.st_mtime_ns,
    )
    write_cached_profile(
        tmp_path, b,
        profile=profile_b, size=sb.st_size, mtime_ns=sb.st_mtime_ns,
    )

    inputs = [
        (a, sa.st_size, sa.st_mtime_ns),
        (b, sb.st_size, sb.st_mtime_ns),
        (c, c.stat().st_size, c.stat().st_mtime_ns),
    ]
    result = read_all_cached_profiles(tmp_path, inputs)
    assert result[a] == profile_a
    assert result[b] == profile_b
    assert result[c] is None


def test_read_all_cached_profiles_invalidates_on_mtime_drift(
    tmp_path: Path,
) -> None:
    """A cache entry whose mtime no longer matches scandir reports as None."""
    a = _write_video(tmp_path, "a.mp4", b"old")
    sa_old = a.stat()
    profile = VideoProfile("h264", "Main", "yuv420p")
    write_cached_profile(
        tmp_path, a,
        profile=profile,
        size=sa_old.st_size,
        mtime_ns=sa_old.st_mtime_ns,
    )

    # Caller hands us an mtime that differs from what was cached
    # (simulates the walker seeing a freshly-modified file).
    result = read_all_cached_profiles(
        tmp_path,
        [(a, sa_old.st_size, sa_old.st_mtime_ns + 1)],
    )
    assert result[a] is None


def test_read_all_empty_input(tmp_path: Path) -> None:
    assert read_all_cached_profiles(tmp_path, []) == {}


def test_corrupted_cache_treated_as_miss(tmp_path: Path) -> None:
    """A garbled JSON sidecar parses as None rather than raising."""
    media = _write_video(tmp_path, "v.mp4", b"abc")
    st = media.stat()
    profile = VideoProfile("h264", "Main", "yuv420p")
    write_cached_profile(
        tmp_path, media,
        profile=profile, size=st.st_size, mtime_ns=st.st_mtime_ns,
    )
    cache_path_for(tmp_path, media).write_bytes(b"{not json")
    assert read_cached_profile(tmp_path, media) is None
