"""The shared tag-filter grammar (`pix.tag_filter`).

Covers the bare spelling (`rating:4,5; event:x`) used by export
distributions; the braced spelling is exercised through `parse_template`
in test_organize.py.
"""

from __future__ import annotations

import pytest

from pix.tag_filter import Clause, FilterError, accepts, parse, parse_values


# --- parse_values ------------------------------------------------------------


def test_parse_values_casefolds_and_dedupes() -> None:
    assert parse_values("event", "Hawaii,hawaii,Maui") == frozenset(
        {"hawaii", "maui"}
    )


def test_parse_values_trims_whitespace() -> None:
    assert parse_values("rating", " 4 , 5 ") == frozenset({"4", "5"})


def test_parse_values_keeps_spaces_inside_a_value() -> None:
    assert parse_values("event", "beach trip") == frozenset({"beach trip"})


def test_parse_values_rejects_empty_list() -> None:
    with pytest.raises(FilterError, match="lists no values"):
        parse_values("rating", "  ")


def test_parse_values_rejects_trailing_comma() -> None:
    with pytest.raises(FilterError, match="empty value"):
        parse_values("rating", "4,5,")


def test_parse_values_rejects_negation_for_now() -> None:
    with pytest.raises(FilterError, match="negation"):
        parse_values("event", "!null")


# --- accepts -----------------------------------------------------------------


def test_accepts_is_case_insensitive() -> None:
    assert accepts(frozenset({"hawaii"}), "Hawaii")


def test_accepts_untagged_only_when_null_listed() -> None:
    assert accepts(frozenset({"null"}), None)
    assert not accepts(frozenset({"2020"}), None)


def test_accepts_rejects_unlisted_value() -> None:
    assert not accepts(frozenset({"4", "5"}), "3")


# --- parse (bare expressions) ------------------------------------------------


def test_parse_empty_matches_everything() -> None:
    f = parse("   ")
    assert f.clauses == ()
    assert f.matches({"rating": None, "event": None})


def test_parse_single_clause() -> None:
    f = parse("rating:3,4,5")
    assert f.clauses == (Clause(tag="rating", values=frozenset({"3", "4", "5"})),)
    assert f.matches({"rating": "5"})
    assert not f.matches({"rating": "2"})


def test_parse_multi_clause_is_anded() -> None:
    f = parse("rating:5; event:beach trip")
    assert len(f.clauses) == 2
    assert f.matches({"rating": "5", "event": "Beach Trip"})
    assert not f.matches({"rating": "5", "event": "Hawaii"})
    assert not f.matches({"rating": "4", "event": "Beach Trip"})


def test_parse_tag_name_is_case_insensitive() -> None:
    assert parse("RATING:5").clauses[0].tag == "rating"


def test_parse_null_selects_untagged() -> None:
    f = parse("event:null")
    assert f.matches({"event": None})
    assert not f.matches({"event": "Hawaii"})


def test_matches_ignores_tags_the_filter_does_not_mention() -> None:
    assert parse("rating:5").matches({"rating": "5", "event": None})


def test_parse_rejects_braces_with_a_pointed_message() -> None:
    with pytest.raises(FilterError, match="unbraced"):
        parse("{rating:5}")


def test_parse_rejects_clause_without_colon() -> None:
    with pytest.raises(FilterError, match="has no `:`"):
        parse("rating")


def test_parse_rejects_unknown_tag() -> None:
    with pytest.raises(FilterError, match="unknown tag"):
        parse("quarter:1")


def test_parse_rejects_date_and_time_as_filter_tags() -> None:
    # Not folder-level tokens, so not filter tags either.
    for expr in ("date:2023", "time:12"):
        with pytest.raises(FilterError, match="unknown tag"):
            parse(expr)


def test_parse_rejects_repeated_tag() -> None:
    with pytest.raises(FilterError, match="more than one clause"):
        parse("rating:5; rating:4")


def test_parse_rejects_trailing_separator() -> None:
    with pytest.raises(FilterError, match="empty clause"):
        parse("rating:5;")


def test_parse_keeps_raw_for_round_tripping() -> None:
    assert parse(" rating:5 ").raw == " rating:5 "
