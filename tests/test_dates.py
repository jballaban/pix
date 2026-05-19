from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pix.dates import (
    derive_date_auto,
    format_pix_datetime,
    parse_exiftool_datetime,
)
from pix.metadata import FileMetadata


def _meta(path: str, **fields: object) -> FileMetadata:
    return FileMetadata(path=Path(path), raw={"SourceFile": path, **fields})


def test_parse_exiftool_datetime_colon_separators() -> None:
    assert parse_exiftool_datetime("2023:08:15 14:32:05") == datetime(
        2023, 8, 15, 14, 32, 5
    )


def test_parse_exiftool_datetime_with_timezone() -> None:
    # Timezone suffix is ignored; treated as local time.
    assert parse_exiftool_datetime("2023:08:15 14:32:05+05:00") == datetime(
        2023, 8, 15, 14, 32, 5
    )


def test_parse_exiftool_datetime_with_subseconds() -> None:
    # Subseconds after the seconds field are also ignored.
    assert parse_exiftool_datetime("2023:08:15 14:32:05.123") == datetime(
        2023, 8, 15, 14, 32, 5
    )


def test_parse_exiftool_datetime_iso_separators() -> None:
    # Some tools emit ISO-style `2023-08-15T14:32:05`.
    assert parse_exiftool_datetime("2023-08-15T14:32:05") == datetime(
        2023, 8, 15, 14, 32, 5
    )


def test_parse_exiftool_datetime_returns_none_on_garbage() -> None:
    assert parse_exiftool_datetime("not a date") is None
    assert parse_exiftool_datetime("") is None


def test_format_pix_datetime() -> None:
    assert (
        format_pix_datetime(datetime(2023, 8, 15, 14, 32, 5))
        == "2023-08-15-14:32:05"
    )


def test_derive_prefers_exif_datetime_original_for_photos() -> None:
    meta = _meta(
        "F:/src/IMG_001.jpg",
        **{
            "EXIF:DateTimeOriginal": "2023:08:15 14:32:05",
            "EXIF:CreateDate": "2022:01:01 00:00:00",
            "XMP:DateCreated": "2021:01:01 00:00:00",
        },
    )
    assert derive_date_auto(meta) == datetime(2023, 8, 15, 14, 32, 5)


def test_derive_falls_through_to_xmp_when_exif_absent() -> None:
    meta = _meta(
        "F:/src/IMG_001.jpg",
        **{"XMP:DateCreated": "2021:01:01 12:34:56"},
    )
    assert derive_date_auto(meta) == datetime(2021, 1, 1, 12, 34, 56)


def test_derive_uses_quicktime_for_videos() -> None:
    meta = _meta(
        "F:/src/clip.mp4",
        **{
            "QuickTime:CreateDate": "2024:03:15 10:00:00",
            # EXIF on a video should not be consulted (video candidates only).
            "EXIF:DateTimeOriginal": "1999:01:01 00:00:00",
        },
    )
    assert derive_date_auto(meta) == datetime(2024, 3, 15, 10, 0, 0)


def test_derive_falls_back_to_filename_pattern() -> None:
    meta = _meta("F:/src/IMG_20230815_143205.jpg")
    assert derive_date_auto(meta) == datetime(2023, 8, 15, 14, 32, 5)


def test_derive_falls_back_to_canonical_filename_pattern() -> None:
    meta = _meta("F:/src/2023-08-15_143205.jpg")
    assert derive_date_auto(meta) == datetime(2023, 8, 15, 14, 32, 5)


def test_derive_handles_synology_style_no_separator() -> None:
    """`YYYYMMDDHHMMSS[NNN]` with no date-time separator (Synology bursts)."""
    meta = _meta("F:/src/20251013073558000.JPG")
    assert derive_date_auto(meta) == datetime(2025, 10, 13, 7, 35, 58)


def test_derive_handles_yyyymmdd_with_separator() -> None:
    """`IMG_YYYYMMDD_HHMMSS` (Android-style) still works."""
    meta = _meta("F:/src/IMG_20230815_143205.jpg")
    assert derive_date_auto(meta) == datetime(2023, 8, 15, 14, 32, 5)


def test_derive_falls_back_to_parent_folder() -> None:
    meta = _meta("F:/src/2023-08-15-trip/photo.jpg")
    assert derive_date_auto(meta) == datetime(2023, 8, 15, 0, 0, 0)


def test_derive_uses_mtime_as_last_resort() -> None:
    meta = _meta(
        "F:/src/unrecognized-name.jpg",
        **{"File:FileModifyDate": "2020:05:01 09:00:00"},
    )
    assert derive_date_auto(meta) == datetime(2020, 5, 1, 9, 0, 0)


def test_derive_returns_none_when_nothing_matches() -> None:
    meta = _meta("F:/src/unrecognized-name.jpg")
    assert derive_date_auto(meta) is None
