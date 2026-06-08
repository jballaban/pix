"""Tests for `pix.cache_base` — the path-mirror helpers that survive the
move to a SQLite cache (used by the errors tree and the one-time import)."""

from __future__ import annotations

from pathlib import Path

from pix.cache_base import (
    cache_path_for,
    cache_root_for,
    mirror_under,
    read_json,
    unmirror_under,
)


def test_mirror_under_folds_drive_letter(tmp_path: Path) -> None:
    root = tmp_path / "errors"
    media = Path(r"G:\pix\raw\2023\foo.jpg")
    mirrored = mirror_under(root, media)
    assert mirrored == root / "G" / "pix" / "raw" / "2023" / "foo.jpg"


def test_mirror_under_appends_suffix(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    media = Path(r"G:\pix\foo.jpg")
    assert mirror_under(root, media, ".meta").name == "foo.jpg.meta"


def test_cache_path_for_mirrors_under_legacy_tree(tmp_path: Path) -> None:
    media = tmp_path / "sub" / "foo.jpg"
    cp = cache_path_for(tmp_path, media, ".hash")
    assert cp.name == "foo.jpg.hash"
    assert cache_root_for(tmp_path) in cp.parents


def test_unmirror_round_trips_with_suffix(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    media = Path(r"G:\pix\raw\2023\foo.jpg")
    mirrored = mirror_under(root, media, ".meta")
    assert unmirror_under(root, mirrored, (".meta",)) == media


def test_unmirror_no_suffix_for_errors_tree(tmp_path: Path) -> None:
    root = tmp_path / "errors"
    media = Path(r"G:\pix\clip.mp4")
    mirrored = mirror_under(root, media)
    assert unmirror_under(root, mirrored, ("",)) == media


def test_unmirror_rejects_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    assert unmirror_under(root, tmp_path / "elsewhere" / "x.meta", (".meta",)) is None


def test_read_json_missing_or_bad(tmp_path: Path) -> None:
    assert read_json(tmp_path / "nope.meta") is None
    bad = tmp_path / "bad.meta"
    bad.write_text("not json {", encoding="utf-8")
    assert read_json(bad) is None
    good = tmp_path / "good.meta"
    good.write_text('{"k": 1}', encoding="utf-8")
    assert read_json(good) == {"k": 1}
