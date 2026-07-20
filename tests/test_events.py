"""Tests for `derive_event_auto`."""

from __future__ import annotations

from pathlib import Path

import pytest

from pix.events import (
    EVENT_NULL,
    PIX_EVENT_AUTO,
    PIX_EVENT_OVERRIDE,
    PIX_IMPORT_ID,
    PIX_ORIGINAL_PATH,
    derive_event_auto,
    effective_event,
)
from pix.metadata import FileMetadata


def test_derive_event_auto_import_is_sticky() -> None:
    """An import file's event is pinned to the stored synthetic batch value —
    NOT re-derived from its device OriginalPath's month-bucket parent (which
    would strip to 'a'). See spec/import.md → Event (ING-2)."""
    m = FileMetadata(
        path=Path("incoming/2026-05-31_194431.jpg"),
        raw={
            "SourceFile": "incoming/2026-05-31_194431.jpg",
            PIX_IMPORT_ID: "SER1:{PUID}",
            PIX_EVENT_AUTO: "james - 20260720",
            PIX_ORIGINAL_PATH: "Internal Storage/202605_a/IMG_7399.HEIC",
        },
    )
    assert derive_event_auto(m) == "james - 20260720"


def _event_meta(auto: str | None = None, override: str | None = None) -> FileMetadata:
    raw: dict[str, object] = {"SourceFile": "x.jpg"}
    if auto is not None:
        raw[PIX_EVENT_AUTO] = auto
    if override is not None:
        raw[PIX_EVENT_OVERRIDE] = override
    return FileMetadata(path=Path("x.jpg"), raw=raw)


def test_effective_event_override_wins() -> None:
    assert effective_event(_event_meta(auto="Camera", override="Hawaii")) == "Hawaii"


def test_effective_event_falls_back_to_auto() -> None:
    assert effective_event(_event_meta(auto="Camera")) == "Camera"


def test_effective_event_null_sentinel_beats_auto() -> None:
    """EVENT_NULL is an explicit 'no event' that overrides the auto."""
    assert effective_event(_event_meta(auto="Camera", override=EVENT_NULL)) is None


def test_effective_event_none_when_nothing_set() -> None:
    assert effective_event(_event_meta()) is None


def _meta(path: str, original: str | None = None) -> FileMetadata:
    """Build a minimal FileMetadata for derivation testing.

    `derive_event_auto` only reads `path` and `OriginalPath` as strings
    via `PurePath` — no filesystem access — so tests pass synthetic
    paths directly without creating real folders.
    """
    raw: dict[str, object] = {"SourceFile": path}
    if original is not None:
        raw[PIX_ORIGINAL_PATH] = original
    return FileMetadata(path=Path(path), raw=raw)


@pytest.mark.parametrize(
    "parent_folder, expected",
    [
        # Date prefixes with various separators
        ("2023-01-Party", "Party"),
        ("2023_01_Party", "Party"),
        ("2023 01 Party", "Party"),
        ("2023.01.15 Party", "Party"),
        ("2023.01.15-Birthday", "Birthday"),
        ("2023-Party", "Party"),
        ("20230101 Party", "Party"),
        ("20230101_Party", "Party"),
        # Multi-word events
        ("2023-08-Hawaii Trip", "Hawaii Trip"),
        ("2023-08-Birthday Party", "Birthday Party"),
        # No date prefix
        ("Hawaii Trip", "Hawaii Trip"),
        ("misc", "misc"),
        ("Birthday-Party", "Birthday-Party"),
        # Trailing dates are preserved (option chosen: leading-only)
        ("Party-2023", "Party-2023"),
        ("Party 2023-01-15", "Party 2023-01-15"),
    ],
)
def test_derives_event_from_parent_folder(
    parent_folder: str, expected: str
) -> None:
    path = f"C:\\source\\{parent_folder}\\img.jpg"
    assert derive_event_auto(_meta(path)) == expected


@pytest.mark.parametrize(
    "parent_folder",
    [
        # All-date folders — nothing left after stripping
        "2023-03",
        "2023",
        "2023-08-15",
        "20230815",
        "2023.08.15",
        # All-separator (no alpha after strip)
        "---",
        "___",
        # All digits + separators, no alpha
        "12-34-56",
    ],
)
def test_no_event_when_stripping_leaves_no_alpha(
    parent_folder: str,
) -> None:
    path = f"C:\\source\\{parent_folder}\\img.jpg"
    assert derive_event_auto(_meta(path)) is None


def test_prefers_original_path_over_current_path() -> None:
    current = "C:\\library\\current-folder\\img.jpg"
    original = "C:\\source\\2023-08-Hawaii\\img.jpg"
    assert derive_event_auto(_meta(current, original=original)) == "Hawaii"


def test_falls_back_to_current_path_when_no_original() -> None:
    assert (
        derive_event_auto(_meta("C:\\source\\2023-08-Hawaii\\img.jpg"))
        == "Hawaii"
    )


def test_handles_forward_slash_original_path() -> None:
    """OriginalPath is a string and may use forward slashes (cross-platform)."""
    original = "/mnt/source/2023-08-Hawaii/img.jpg"
    current = "C:\\library\\foo.jpg"
    assert (
        derive_event_auto(_meta(current, original=original)) == "Hawaii"
    )


def test_preserves_case_and_internal_separators() -> None:
    assert (
        derive_event_auto(
            _meta("C:\\source\\2023-Birthday-Party\\img.jpg")
        )
        == "Birthday-Party"
    )
