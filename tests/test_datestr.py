"""Partial date overrides — a date with holes in it (spec/tags.md).

The point of the grammar is that *this scan is from 1987* can be recorded
without inventing a month, a day and a time. Fabricating that precision is not
harmless: a made-up `1987-01-01 00:00:00` is indistinguishable from a real one a
year later, and the archive is the thing that has to still be trustworthy then.
"""

from __future__ import annotations

from datetime import datetime

from pix import datestr


# --- the grammar -------------------------------------------------------------

def test_a_full_timestamp_is_valid() -> None:
    assert datestr.valid("2015-03-15-11:52:56")


def test_holes_are_valid() -> None:
    assert datestr.valid("1987-*-*-*:*:*")
    assert datestr.valid("*-03-*-*:*:*")


def test_prose_is_not() -> None:
    assert not datestr.valid("last tuesday")
    assert not datestr.valid("1987")
    assert not datestr.valid("2015-3-15-11:52:56")   # unpadded


def test_all_holes_pins_nothing() -> None:
    """Equivalent to no override, so it must never be stored as a decision."""
    assert not datestr.pins_anything("*-*-*-*:*:*")
    assert datestr.pins_anything("1987-*-*-*:*:*")
    assert not datestr.pins_anything(None)


# --- composing ---------------------------------------------------------------

def test_compose_pads_and_fills() -> None:
    assert datestr.compose(year=1987, month=3) == "1987-03-*-*:*:*"


def test_compose_of_nothing_is_nothing() -> None:
    """Not an all-`*` string: that would create a sidecar recording no decision."""
    assert datestr.compose() is None
    assert datestr.compose(year="", month="") is None


# --- applying ----------------------------------------------------------------

def test_only_the_pinned_components_move() -> None:
    auto = datetime(2023, 8, 15, 14, 32, 5)

    assert datestr.effective(auto, "*-03-*-*:*:*") == datetime(2023, 3, 15, 14, 32, 5)
    assert datestr.effective(auto, "2020-*-01-*:*:*") == datetime(2020, 8, 1, 14, 32, 5)


def test_no_override_leaves_the_capture_date() -> None:
    auto = datetime(2023, 8, 15, 14, 32, 5)

    assert datestr.effective(auto, None) == auto
    assert datestr.effective(auto, "*-*-*-*:*:*") == auto


def test_a_year_anchors_an_undated_file() -> None:
    """A scan with no EXIF at all still lands in 1987, at the year's start."""
    assert datestr.effective(None, "1987-*-*-*:*:*") == datetime(1987, 1, 1)


def test_without_a_year_an_undated_file_stays_undated() -> None:
    """There is nothing for a bare month to be relative to, and picking a year
    would be inventing the very thing the override exists to avoid."""
    assert datestr.effective(None, "*-03-*-*:*:*") is None


def test_an_impossible_date_is_refused_not_rounded() -> None:
    auto = datetime(2023, 1, 31, 12, 0, 0)

    assert datestr.effective(auto, "*-02-*-*:*:*") is None


# --- the two string shapes ExifTool emits ------------------------------------

def test_both_exiftool_separators_parse() -> None:
    assert datestr.parse_exiftool("2026:01:04 14:51:34") == datetime(
        2026, 1, 4, 14, 51, 34)
    assert datestr.parse_exiftool("2026-01-04T14:51:34") == datetime(
        2026, 1, 4, 14, 51, 34)


def test_a_zero_date_is_junk_not_year_zero() -> None:
    """45 files in the seeded year carry it."""
    assert datestr.parse_exiftool("0000:00:00 00:00:00") is None


def test_a_trailing_timezone_is_dropped() -> None:
    """pix treats every timestamp as naive local time (spec/tags.md)."""
    assert datestr.parse_exiftool("2026:01:04 14:51:34+05:00") == datetime(
        2026, 1, 4, 14, 51, 34)


def test_round_trip_through_the_pix_format() -> None:
    moment = datetime(2015, 3, 15, 11, 52, 56)
    assert datestr.parse_pix(datestr.format_pix(moment)) == moment
