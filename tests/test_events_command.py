"""Tests for `pix info events` — the unique-event lister that backs autocomplete."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from pix.commands.events import format_range, list_events
from pix.events import EVENT_NULL, PIX_EVENT_AUTO, PIX_EVENT_OVERRIDE
from pix.metadata_cache import PerFileCache
from pix.plan import PIX_DATE_AUTO


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


def _names(out: str) -> list[str]:
    """Event names from `name<TAB>range` output lines."""
    return [line.split("\t", 1)[0] for line in out.splitlines()]


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
    assert _names(capsys.readouterr().out) == ["apple", "Camera", "Hawaii"]


def test_emits_date_range_per_event(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each event line carries the span of its files' effective dates."""
    root = _lib_with_events(
        tmp_path,
        {
            "x1.jpg": {PIX_EVENT_AUTO: "Trip", PIX_DATE_AUTO: "2023-01-01-10:00:00"},
            "x2.jpg": {PIX_EVENT_AUTO: "Trip", PIX_DATE_AUTO: "2023-01-13-18:00:00"},
            "u.jpg": {PIX_EVENT_AUTO: "Undated"},  # no date → empty range
        },
    )
    list_events(root)
    lines = capsys.readouterr().out.splitlines()
    assert "Trip\t2023-01-01..13" in lines
    assert "Undated\t" in lines  # dateless event → name, tab, empty range


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
    assert _names(capsys.readouterr().out) == ["Trip"]


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
    assert _names(out) == ["Camera"]


@pytest.mark.parametrize(
    "lo, hi, expected",
    [
        (None, None, ""),
        (datetime(2023, 1, 1), datetime(2023, 1, 1, 23), "2023-01-01"),
        (datetime(2023, 1, 1), datetime(2023, 1, 13), "2023-01-01..13"),
        (datetime(2023, 1, 1), datetime(2023, 2, 13), "2023-01-01...02-13"),
        (datetime(2023, 1, 1), datetime(2024, 2, 13), "2023-01-01...2024-02-13"),
    ],
)
def test_format_range(
    lo: datetime | None, hi: datetime | None, expected: str
) -> None:
    assert format_range(lo, hi) == expected
