"""Tests for `derive_event_auto`."""

from __future__ import annotations

from pathlib import Path

import pytest

from pix.events import PIX_ORIGINAL_PATH, derive_event_auto
from pix.metadata import FileMetadata


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
