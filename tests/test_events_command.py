"""Tests for `pix events` — the unique-event lister that backs autocomplete."""

from __future__ import annotations

from pathlib import Path

import pytest

from pix.commands.events import list_events
from pix.events import EVENT_NULL, PIX_EVENT_AUTO, PIX_EVENT_OVERRIDE
from pix.metadata_cache import PerFileCache


def _lib_with_events(
    tmp_path: Path, files: dict[str, dict[str, object]]
) -> Path:
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    cache = PerFileCache.for_library(root)
    for name, md in files.items():
        f = root / name
        f.write_bytes(b"x")
        cache.add(f, {"SourceFile": str(f), **md})
    return root


def test_lists_unique_sorted_events(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _lib_with_events(
        tmp_path,
        {
            "a.jpg": {PIX_EVENT_AUTO: "Camera"},
            "b.jpg": {PIX_EVENT_AUTO: "Camera"},  # duplicate → collapsed
            "c.jpg": {PIX_EVENT_OVERRIDE: "Hawaii"},
            "d.jpg": {PIX_EVENT_AUTO: "apple"},  # case-insensitive sort
        },
    )
    list_events(root)
    assert capsys.readouterr().out.splitlines() == ["apple", "Camera", "Hawaii"]


def test_override_wins_and_blank_excluded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An override beats the auto; a force-null (blanked) event contributes
    nothing; a file with no event is ignored."""
    root = _lib_with_events(
        tmp_path,
        {
            "a.jpg": {PIX_EVENT_AUTO: "Camera", PIX_EVENT_OVERRIDE: "Trip"},
            "b.jpg": {PIX_EVENT_AUTO: "Zebra", PIX_EVENT_OVERRIDE: EVENT_NULL},
            "c.jpg": {},
        },
    )
    assert list_events(root) is None  # type: ignore[func-returns-value]
    assert capsys.readouterr().out.splitlines() == ["Trip"]


def test_no_cache_is_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    list_events(root)
    assert capsys.readouterr().out == ""


def test_no_banner_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pipe-friendly: output is pure data, no `pix <version>` banner line."""
    root = _lib_with_events(tmp_path, {"a.jpg": {PIX_EVENT_AUTO: "Camera"}})
    list_events(root)
    out = capsys.readouterr().out
    assert "pix " not in out
    assert out.splitlines() == ["Camera"]
