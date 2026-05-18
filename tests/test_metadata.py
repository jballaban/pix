from __future__ import annotations

import json
from pathlib import Path

import pytest

from pix.metadata import ExifToolFailed, FileMetadata, parse_exiftool_json


def test_parse_empty_stdout(tmp_path: Path) -> None:
    # ExifTool may return empty stdout for empty folders.
    assert parse_exiftool_json("") == {}
    assert parse_exiftool_json("   \n  ") == {}


def test_parse_typical_output(tmp_path: Path) -> None:
    # Create a real file so the SourceFile path resolves to is_file().
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"not actually a jpg")

    payload = [
        {
            "SourceFile": str(f),
            "EXIF:DateTimeOriginal": "2023:08:15 14:32:05",
            "File:FileModifyDate": "2024:01:01 00:00:00",
        }
    ]
    cache = parse_exiftool_json(json.dumps(payload))

    assert len(cache) == 1
    [(path, meta)] = cache.items()
    assert path == f.resolve()
    assert isinstance(meta, FileMetadata)
    assert meta.get_str("EXIF:DateTimeOriginal") == "2023:08:15 14:32:05"
    assert meta.get_str("File:FileModifyDate") == "2024:01:01 00:00:00"
    assert meta.get_str("NotPresent") is None


def test_parse_skips_non_existent_source_files(tmp_path: Path) -> None:
    payload = [
        {"SourceFile": str(tmp_path / "missing.jpg")},
        # Folder entries get filtered out the same way (not a regular file).
    ]
    assert parse_exiftool_json(json.dumps(payload)) == {}


def test_parse_skips_entries_without_source_file() -> None:
    payload = [{"EXIF:DateTimeOriginal": "2023:08:15 14:32:05"}]
    assert parse_exiftool_json(json.dumps(payload)) == {}


def test_parse_rejects_non_list_top_level() -> None:
    with pytest.raises(ExifToolFailed, match="unexpected JSON"):
        parse_exiftool_json('{"SourceFile": "x"}')


def test_get_str_only_returns_strings(tmp_path: Path) -> None:
    f = tmp_path / "x.jpg"
    f.write_bytes(b"")
    payload = [
        {
            "SourceFile": str(f),
            "EXIF:FNumber": 2.8,  # number, not string
            "EXIF:DateTimeOriginal": "2023:08:15 14:32:05",
        }
    ]
    cache = parse_exiftool_json(json.dumps(payload))
    meta = next(iter(cache.values()))
    assert meta.get_str("EXIF:DateTimeOriginal") == "2023:08:15 14:32:05"
    assert meta.get_str("EXIF:FNumber") is None  # filtered out — not a str
