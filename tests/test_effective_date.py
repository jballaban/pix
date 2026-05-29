"""Tests for null-auto date defaulting in `pix.plan.effective_date`.

A file with no `pix:DateAuto` but a `pix:DateOverride` that pins a year
gets an effective date by defaulting the unspecified components (year
anchor required). See spec/tags.md → Effective value computation.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pix.metadata import FileMetadata
from pix.plan import (
    PIX_DATE_AUTO,
    PIX_DATE_OVERRIDE,
    effective_date,
)


def _meta(**fields: object) -> FileMetadata:
    path = Path("/lib/x.jpg")
    return FileMetadata(path=path, raw={"SourceFile": str(path), **fields})


def test_null_auto_year_override_defaults_missing_parts() -> None:
    meta = _meta(**{PIX_DATE_OVERRIDE: "2008-*-*-*:*:*"})
    assert effective_date(meta) == datetime(2008, 1, 1, 0, 0, 0)


def test_null_auto_year_month_override_defaults_day_and_time() -> None:
    meta = _meta(**{PIX_DATE_OVERRIDE: "2008-12-*-*:*:*"})
    assert effective_date(meta) == datetime(2008, 12, 1, 0, 0, 0)


def test_null_auto_full_override() -> None:
    meta = _meta(**{PIX_DATE_OVERRIDE: "2008-12-31-23:59:59"})
    assert effective_date(meta) == datetime(2008, 12, 31, 23, 59, 59)


def test_null_auto_override_without_year_stays_null() -> None:
    # No year anchor → nothing to build from.
    meta = _meta(**{PIX_DATE_OVERRIDE: "*-12-25-*:*:*"})
    assert effective_date(meta) is None


def test_null_auto_no_override_is_null() -> None:
    assert effective_date(_meta()) is None


def test_present_auto_still_patched_not_defaulted() -> None:
    # When DateAuto exists, missing override fields come from auto (the
    # existing behavior), NOT from the null-auto defaults.
    meta = _meta(
        **{
            PIX_DATE_AUTO: "2010-06-15-12:30:45",
            PIX_DATE_OVERRIDE: "2008-*-*-*:*:*",
        }
    )
    assert effective_date(meta) == datetime(2008, 6, 15, 12, 30, 45)
