"""Tests for the `rating` tag — `pix.rating` + its organize-template wiring."""

from __future__ import annotations

from pathlib import Path

from pix.metadata import FileMetadata
from pix.organize import (
    ALLOWED_TOKENS,
    compute_values,
    parse_template,
    render_target_folder,
)
from pix.rating import XMP_RATING, effective_rating
from pix.special_folders import NULL_FOLDER


def _meta(**fields: object) -> FileMetadata:
    return FileMetadata(path=Path("/x.jpg"), raw={"SourceFile": "/x.jpg", **fields})


# --- effective_rating --------------------------------------------------------


def test_effective_rating_absent_is_none() -> None:
    assert effective_rating(_meta()) is None


def test_effective_rating_int() -> None:
    assert effective_rating(_meta(**{XMP_RATING: 5})) == "5"


def test_effective_rating_zero_is_a_value_not_none() -> None:
    # 0 is a distinct, valid rating folder; only *absence* is unrated/null.
    assert effective_rating(_meta(**{XMP_RATING: 0})) == "0"


def test_effective_rating_numeric_string() -> None:
    assert effective_rating(_meta(**{XMP_RATING: "3"})) == "3"


def test_effective_rating_float() -> None:
    assert effective_rating(_meta(**{XMP_RATING: 4.0})) == "4"


def test_effective_rating_negative_reads_unrated() -> None:
    # XMP -1 "rejected" — pix has no rejected concept, so it reads as unrated.
    assert effective_rating(_meta(**{XMP_RATING: -1})) is None


def test_effective_rating_above_five_clamps() -> None:
    assert effective_rating(_meta(**{XMP_RATING: 7})) == "5"


def test_effective_rating_junk_is_none() -> None:
    assert effective_rating(_meta(**{XMP_RATING: "Hawaii"})) is None


def test_effective_rating_bool_is_none() -> None:
    assert effective_rating(_meta(**{XMP_RATING: True})) is None


# --- organize template wiring ------------------------------------------------


def test_rating_is_an_allowed_organize_token() -> None:
    assert "rating" in ALLOWED_TOKENS
    parse_template("{event}/{rating}")  # does not raise


def test_compute_values_includes_rating() -> None:
    values = compute_values(_meta(**{XMP_RATING: 4}))
    assert values["rating"] == "4"


def test_render_rating_folder() -> None:
    t = parse_template("{rating}")
    assert render_target_folder(t, {"rating": "5"}) == "5"


def test_render_unrated_goes_to_null_folder() -> None:
    t = parse_template("{rating}")
    assert render_target_folder(t, {"rating": None}) == NULL_FOLDER
