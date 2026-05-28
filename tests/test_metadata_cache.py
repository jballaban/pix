"""Tests for the per-file metadata cache."""

from __future__ import annotations

import json
from pathlib import Path

from pix.metadata_cache import CACHE_FILE_VERSION, PerFileCache


def _new_cache(tmp_path: Path) -> PerFileCache:
    return PerFileCache.for_library(tmp_path)


def test_cache_path_mirrors_absolute_path(tmp_path: Path) -> None:
    cache = _new_cache(tmp_path)
    media = tmp_path / "foo" / "bar.jpg"
    cache_path = cache.cache_path_for(media)
    # The cache file's path mirrors the absolute path of the media,
    # with drive letter as folder name and `.meta` appended.
    assert cache_path.suffix == ".meta"
    assert cache_path.name == "bar.jpg.meta"
    # Must live under <library>/.pix/cache/
    assert (tmp_path / ".pix" / "cache") in cache_path.parents


def test_add_then_get_round_trip(tmp_path: Path) -> None:
    cache = _new_cache(tmp_path)
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"some image bytes")

    metadata = {
        "SourceFile": str(media),
        "EXIF:DateTimeOriginal": "2023:08:15 14:32:05",
    }
    cache.add(media, metadata)

    got = cache.get(media)
    assert got is not None
    assert got["SourceFile"] == str(media)
    assert got["EXIF:DateTimeOriginal"] == "2023:08:15 14:32:05"


def test_get_returns_none_when_no_cache_entry(tmp_path: Path) -> None:
    cache = _new_cache(tmp_path)
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"")
    assert cache.get(media) is None


def test_get_invalidates_on_size_mismatch(tmp_path: Path) -> None:
    """If the caller's expected size doesn't match the cache's recorded
    size, the entry is treated as stale."""
    cache = _new_cache(tmp_path)
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"original")
    cache.add(media, {"EXIF:Foo": "bar"})

    # Replace with different-sized content; caller (walker) sees the new
    # size and passes it in.
    media.write_bytes(b"different size")
    new_size = media.stat().st_size
    assert cache.get(media, expected_size=new_size) is None


def test_get_invalidates_on_version_mismatch(tmp_path: Path) -> None:
    """A cache file with the wrong format version is treated as missing."""
    cache = _new_cache(tmp_path)
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"hi")

    cache_path = cache.cache_path_for(media)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "v": CACHE_FILE_VERSION + 999,  # future version
                "size": media.stat().st_size,
                "metadata": {"X": "Y"},
            }
        ),
        encoding="utf-8",
    )
    assert cache.get(media) is None


def test_get_invalidates_on_malformed_json(tmp_path: Path) -> None:
    cache = _new_cache(tmp_path)
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"hi")
    cache_path = cache.cache_path_for(media)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("not json at all{{{{", encoding="utf-8")
    assert cache.get(media) is None


def test_remove_deletes_cache_file(tmp_path: Path) -> None:
    cache = _new_cache(tmp_path)
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"")
    cache.add(media, {"X": "Y"})

    cache_path = cache.cache_path_for(media)
    assert cache_path.is_file()
    cache.remove(media)
    assert not cache_path.is_file()


def test_remove_missing_is_noop(tmp_path: Path) -> None:
    """Removing a cache entry that doesn't exist must not raise."""
    cache = _new_cache(tmp_path)
    media = tmp_path / "foo.jpg"
    cache.remove(media)  # no entry, must not raise


def test_rename_moves_cache_file(tmp_path: Path) -> None:
    cache = _new_cache(tmp_path)
    old_media = tmp_path / "imports" / "foo.jpg"
    old_media.parent.mkdir()
    old_media.write_bytes(b"")
    cache.add(old_media, {"X": "Y"})

    # Now the media file moves (simulate organize).
    new_media = tmp_path / "2023" / "Hawaii" / "foo.jpg"
    new_media.parent.mkdir(parents=True)
    old_media.rename(new_media)
    cache.rename(old_media, new_media)

    assert not cache.cache_path_for(old_media).is_file()
    assert cache.cache_path_for(new_media).is_file()
    # And get() finds it at the new path.
    got = cache.get(new_media)
    assert got is not None
    assert got["X"] == "Y"


def test_rename_missing_source_is_noop(tmp_path: Path) -> None:
    """Renaming a cache entry that doesn't exist must not raise."""
    cache = _new_cache(tmp_path)
    cache.rename(tmp_path / "a.jpg", tmp_path / "b.jpg")


def test_update_metadata_merges_into_cached_dict(tmp_path: Path) -> None:
    cache = _new_cache(tmp_path)
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"")
    cache.add(media, {"EXIF:Foo": "1", "EXIF:Bar": "2"})

    cache.update_metadata(media, {"XMP:Baz": "3", "EXIF:Foo": "overwritten"})

    got = cache.get(media)
    assert got is not None
    assert got["EXIF:Foo"] == "overwritten"
    assert got["EXIF:Bar"] == "2"  # unaffected
    assert got["XMP:Baz"] == "3"  # added


def test_update_metadata_noop_when_no_entry(tmp_path: Path) -> None:
    cache = _new_cache(tmp_path)
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"")
    cache.update_metadata(media, {"X": "Y"})  # no existing entry
    assert cache.get(media) is None  # still empty
