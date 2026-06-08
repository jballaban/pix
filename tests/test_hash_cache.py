"""Tests for `pix.hash_cache` — the content-hash cache (SQLite-backed)."""

from __future__ import annotations

from pathlib import Path

from pix.hash_cache import (
    find_missing_hashes,
    read_all_cached_hashes,
    read_cached_hash,
    write_cached_hash,
)


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"some bytes")
    st = media.stat()
    write_cached_hash(
        tmp_path,
        media,
        hash_hex="deadbeef",
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
    )
    assert read_cached_hash(tmp_path, media) == "deadbeef"


def test_read_returns_none_when_no_cache_entry(tmp_path: Path) -> None:
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"")
    assert read_cached_hash(tmp_path, media) is None


def test_read_invalidates_on_size_change(tmp_path: Path) -> None:
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"original")
    st = media.stat()
    write_cached_hash(
        tmp_path, media, hash_hex="h", size=st.st_size, mtime_ns=st.st_mtime_ns
    )
    media.write_bytes(b"different size content")  # size + mtime change
    assert read_cached_hash(tmp_path, media) is None


def test_read_invalidates_on_mtime_change(tmp_path: Path) -> None:
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"abc")
    st = media.stat()
    write_cached_hash(
        tmp_path,
        media,
        hash_hex="h",
        size=st.st_size,
        mtime_ns=st.st_mtime_ns - 1,  # stored mtime doesn't match live
    )
    assert read_cached_hash(tmp_path, media) is None


def test_read_all_cached_hashes_validates_against_scanned_stamp(
    tmp_path: Path,
) -> None:
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbb")
    sa, sb = a.stat(), b.stat()
    write_cached_hash(tmp_path, a, hash_hex="ha", size=sa.st_size, mtime_ns=sa.st_mtime_ns)
    # b is cached against a stale stamp → must read as a miss.
    write_cached_hash(tmp_path, b, hash_hex="hb", size=sb.st_size + 1, mtime_ns=sb.st_mtime_ns)

    scanned = [
        (a, sa.st_size, sa.st_mtime_ns),
        (b, sb.st_size, sb.st_mtime_ns),
    ]
    result = read_all_cached_hashes(tmp_path, scanned)
    assert result[a] == "ha"
    assert result[b] is None
    assert find_missing_hashes(tmp_path, scanned) == [b]


def test_read_all_cached_hashes_batches_progress(tmp_path: Path) -> None:
    paths: list[tuple[Path, int, int]] = []
    for i in range(5):
        p = tmp_path / f"f{i}.jpg"
        p.write_bytes(b"x")
        st = p.stat()
        paths.append((p, st.st_size, st.st_mtime_ns))

    seen: list[int] = []
    read_all_cached_hashes(
        tmp_path, paths, on_batch=seen.append, batch_size=2
    )
    assert sum(seen) == 5  # every path counted exactly once
