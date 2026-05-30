"""Tests for ExifTool write-persistence detection (`write_tags`).

`write_tags` parses ExifTool's "N image files updated" line so a write
that silently didn't persist (e.g. a truncated/damaged container, where a
hard error isn't suppressed by `-m`) raises `TagWriteFailed` instead of
being mistaken for success. We stub `execute` to avoid spawning ExifTool.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pix.exiftool_session import ExifToolSession, TagWriteFailed


class _StubSession(ExifToolSession):
    """ExifToolSession whose `execute` returns canned output and whose
    constructor spawns no subprocess."""

    def __init__(self, output: str) -> None:  # no super().__init__
        self._output = output

    def execute(self, *args: str, timeout: float = 30.0) -> str:
        return self._output


def test_write_tags_raises_when_zero_files_updated() -> None:
    stub = _StubSession(
        "    0 image files updated\n"
        "    1 files weren't updated due to errors\n"
    )
    with pytest.raises(TagWriteFailed):
        stub.write_tags(Path("broken.mp4"), {"XMP:OriginalPath": "v"})


def test_write_tags_ok_when_one_file_updated() -> None:
    stub = _StubSession("    1 image files updated\n")
    # Must not raise.
    stub.write_tags(Path("good.jpg"), {"XMP:OriginalPath": "v"})


def test_write_tags_noop_on_empty_tags() -> None:
    # No tags → no execute, no parse, no raise (even with empty output).
    stub = _StubSession("")
    stub.write_tags(Path("x.jpg"), {})
