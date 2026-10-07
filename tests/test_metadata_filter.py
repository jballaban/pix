"""Tests for `pix.metadata_filter` — the stored-metadata allowlist."""

from __future__ import annotations

from pix.metadata_filter import filter_consumed


def test_drops_noise_keeps_consumed() -> None:
    raw: dict[str, object] = {
        "SourceFile": "G:/pix/x.jpg",
        "EXIF:DateTimeOriginal": "2023:08:15 14:32:05",
        "XMP:EventOverride": "Hawaii",
        "XMP:RegionName": "Alice",  # face region — kept
        "XMP:Rating": "5",  # rating tag — kept
        "EXIF:MakerNoteCanon": "....big blob....",  # noise — dropped
        "EXIF:ThumbnailImage": "base64....",  # noise — dropped
    }
    out = filter_consumed(raw)
    assert set(out) == {
        "SourceFile",
        "EXIF:DateTimeOriginal",
        "XMP:EventOverride",
        "XMP:RegionName",
        "XMP:Rating",
    }

