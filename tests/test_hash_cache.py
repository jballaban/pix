"""Tests for `pix.hash_cache` — the per-file content-hash cache."""

from __future__ import annotations

import json
from pathlib import Path

from pix.hash_cache import (
    cache_path_for,
    read_cached_hash,
    write_cached_hash,
)


def test_cache_path_mirrors_absolute_path(tmp_path: Path) -> None:
    media = tmp_path / "sub" / "foo.jpg"
    cp = cache_path_for(tmp_path, media)
    assert cp.suffix == ".hash"
    assert cp.name == "foo.jpg.hash"
    assert (tmp_path / ".pix" / "cache") in cp.parents


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
        tmp_path,
        media,
        hash_hex="h",
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
    )
    # Mutate size — cache entry now stale.
    media.write_bytes(b"different size content")
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


def test_read_returns_none_on_malformed_json(tmp_path: Path) -> None:
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"")
    cp = cache_path_for(tmp_path, media)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text("not valid json {", encoding="utf-8")
    assert read_cached_hash(tmp_path, media) is None


def test_write_records_all_expected_fields(tmp_path: Path) -> None:
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"x")
    write_cached_hash(
        tmp_path, media, hash_hex="deadbeef", size=1, mtime_ns=42
    )
    cp = cache_path_for(tmp_path, media)
    payload = json.loads(cp.read_text(encoding="utf-8"))
    assert payload["size"] == 1
    assert payload["mtime_ns"] == 42
    assert payload["hash"] == "deadbeef"
    assert "computed_at" in payload


def test_write_atomic_no_tmp_left_behind(tmp_path: Path) -> None:
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"x")
    write_cached_hash(tmp_path, media, hash_hex="h", size=1, mtime_ns=1)
    cp = cache_path_for(tmp_path, media)
    tmp_sibling = cp.parent / (cp.name + ".tmp")
    assert cp.is_file()
    assert not tmp_sibling.exists()
