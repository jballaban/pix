"""Tests for the metadata cache facade (`pix.metadata_cache`, SQLite-backed)."""

from __future__ import annotations

from pathlib import Path

from pix.metadata_cache import PerFileCache


def _new_cache(tmp_path: Path) -> PerFileCache:
    return PerFileCache.for_library(tmp_path)


def test_add_then_get_round_trip(tmp_path: Path) -> None:
    cache = _new_cache(tmp_path)
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"some image bytes")

    metadata: dict[str, object] = {
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
    cache = _new_cache(tmp_path)
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"original")
    cache.add(media, {"XMP:EventOverride": "bar"})

    media.write_bytes(b"different size")
    new_size = media.stat().st_size
    assert cache.get(media, expected_size=new_size) is None


def test_get_invalidates_on_mtime_mismatch(tmp_path: Path) -> None:
    """Unified key: a stamp mismatch on mtime alone is stale too."""
    cache = _new_cache(tmp_path)
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"abc")
    cache.add(media, {"XMP:EventOverride": "bar"})
    st = media.stat()
    assert (
        cache.get(media, expected_size=st.st_size, expected_mtime_ns=st.st_mtime_ns + 1)
        is None
    )


def test_remove_deletes_entry(tmp_path: Path) -> None:
    cache = _new_cache(tmp_path)
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"")
    cache.add(media, {"XMP:OriginalPath": "Y"})
    assert cache.get(media) is not None
    cache.remove(media)
    assert cache.get(media) is None


def test_remove_missing_is_noop(tmp_path: Path) -> None:
    cache = _new_cache(tmp_path)
    media = tmp_path / "foo.jpg"
    cache.remove(media)  # no entry, must not raise


def test_rename_moves_entry(tmp_path: Path) -> None:
    cache = _new_cache(tmp_path)
    old_media = tmp_path / "imports" / "foo.jpg"
    old_media.parent.mkdir()
    old_media.write_bytes(b"")
    cache.add(old_media, {"XMP:OriginalPath": "Y"})

    new_media = tmp_path / "2023" / "Hawaii" / "foo.jpg"
    new_media.parent.mkdir(parents=True)
    old_media.rename(new_media)
    cache.rename(old_media, new_media)

    assert cache.get(old_media) is None
    got = cache.get(new_media)
    assert got is not None and got["XMP:OriginalPath"] == "Y"


def test_rename_missing_source_is_noop(tmp_path: Path) -> None:
    cache = _new_cache(tmp_path)
    cache.rename(tmp_path / "a.jpg", tmp_path / "b.jpg")


def test_update_metadata_merges_into_cached_dict(tmp_path: Path) -> None:
    cache = _new_cache(tmp_path)
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"")
    cache.add(media, {"XMP:EventAuto": "1", "XMP:OriginalPath": "/p"})

    cache.update_metadata(
        media, {"XMP:EventOverride": "3", "XMP:EventAuto": "overwritten"}
    )

    got = cache.get(media)
    assert got is not None
    assert got["XMP:EventAuto"] == "overwritten"
    assert got["XMP:OriginalPath"] == "/p"  # unaffected
    assert got["XMP:EventOverride"] == "3"  # added


def test_update_metadata_noop_when_no_entry(tmp_path: Path) -> None:
    cache = _new_cache(tmp_path)
    media = tmp_path / "foo.jpg"
    media.write_bytes(b"")
    cache.update_metadata(media, {"X": "Y"})  # no existing entry
    assert cache.get(media) is None
