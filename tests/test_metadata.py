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
    assert path == f
    assert isinstance(meta, FileMetadata)
    assert meta.get_str("EXIF:DateTimeOriginal") == "2023:08:15 14:32:05"
    assert meta.get_str("File:FileModifyDate") == "2024:01:01 00:00:00"
    assert meta.get_str("NotPresent") is None


def test_parse_trusts_source_file_without_stat(tmp_path: Path) -> None:
    """parse_exiftool_json no longer stats each result.

    The trust model: ExifTool emits SourceFile only for files it
    successfully read, so re-validating is wasted I/O at scale. A
    synthetic payload with a non-existent path still produces an entry.
    """
    payload = [{"SourceFile": str(tmp_path / "missing.jpg")}]
    cache = parse_exiftool_json(json.dumps(payload))
    assert len(cache) == 1
    assert (tmp_path / "missing.jpg") in cache


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
