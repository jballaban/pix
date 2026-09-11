"""Curation decision sidecars (spec/nas-app.md §4).

The `.xmp` beside a master file is **the record** — the index is only a
projection of it — so the things that matter here are that a write survives a
round trip, that changing one field cannot erase another, and that a killed
write cannot leave a decision nobody made.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pix.markers import SIDECAR_TMP_SUFFIX
from pix.nas import decisions
from pix.nas.decisions import Decision


@pytest.fixture
def media(tmp_path: Path) -> Path:
    path = tmp_path / "master" / "init_2026" / "IMG_4471.HEIC"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"original bytes")
    return path


# --- naming ------------------------------------------------------------------

def test_the_sidecar_keeps_the_full_filename(media: Path) -> None:
    """Not `IMG_4471.xmp`: a Live Photo is `.HEIC` *and* `.MOV` in one import,
    so Lightroom's basename-replacing convention collides on the most common
    device in the library."""
    assert decisions.sidecar_path(media).name == "IMG_4471.HEIC.xmp"


def test_a_live_photo_pair_gets_two_sidecars(media: Path) -> None:
    movie = media.with_name("IMG_4471.MOV")
    assert decisions.sidecar_path(media) != decisions.sidecar_path(movie)


# --- round trip --------------------------------------------------------------

def test_a_decision_survives_a_round_trip(media: Path) -> None:
    decisions.write(media, Decision(audience=("family",), event="Banff Skiing",
                                    date_override="2015-03-15-11:52:56"))

    assert decisions.read(media) == Decision(
        audience=("family",), event="Banff Skiing",
        date_override="2015-03-15-11:52:56")


def test_no_sidecar_reads_as_no_decision(media: Path) -> None:
    """`no sidecar means unreviewed` is the whole review-state model."""
    assert decisions.read(media) is None


def test_keeping_something_private_is_still_a_decision(media: Path) -> None:
    """`private` must create a sidecar. Choosing to show a photograph to
    nobody is a judgement, and it is not the same state as never having
    looked at it — which is exactly what having no sidecar means."""
    decisions.write(media, Decision(audience=(decisions.PRIVATE,)))

    assert decisions.sidecar_path(media).is_file()
    assert decisions.read(media) == Decision(audience=("private",))


def test_clearing_everything_removes_the_sidecar(media: Path) -> None:
    """An empty sidecar would read as reviewed, which is exactly wrong."""
    decisions.write(media, Decision(audience=("family",)))
    decisions.write(media, Decision())

    assert not decisions.sidecar_path(media).exists()


def test_awkward_text_survives(media: Path) -> None:
    """Event names are free text typed by a family, not identifiers."""
    name = 'Ben & "Jo" <2015> — café'
    decisions.write(media, Decision(event=name))

    assert decisions.read(media) == Decision(event=name)


# --- standard fields ---------------------------------------------------------

def test_standard_fields_are_written_alongside(media: Path) -> None:
    """Lightroom and Bridge read the standard properties; they ignore `pix:*`."""
    decisions.write(media, Decision(event="Banff Skiing",
                                    date_override="2015-03-15-11:52:56"))
    text = decisions.sidecar_path(media).read_text(encoding="utf-8")

    assert 'Iptc4xmpExt:Event="Banff Skiing"' in text
    assert 'photoshop:DateCreated="2015-03-15T11:52:56"' in text


def test_it_is_a_real_xmp_packet(media: Path) -> None:
    decisions.write(media, Decision(audience=("family",)))
    text = decisions.sidecar_path(media).read_text(encoding="utf-8")

    assert text.startswith("<?xpacket begin=")
    assert "http://pix.local/" in text


def test_the_element_form_is_readable_too(media: Path) -> None:
    """Attributes are what pix writes; other XMP tools write child elements, and
    a sidecar edited elsewhere still has to read."""
    decisions.sidecar_path(media).write_text(
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:pix="http://pix.local/">'
        "<pix:EventOverride>Sicily</pix:EventOverride>"
        "</rdf:Description></rdf:RDF>", encoding="utf-8")

    assert decisions.read(media) == Decision(event="Sicily")


# --- partial updates ---------------------------------------------------------

def test_sharing_keeps_the_event(media: Path) -> None:
    """The regression this exists to prevent: the UI changes one field at a
    time, and a whole-record write would discard the rest."""
    decisions.apply(media, event="Banff Skiing")
    decisions.apply(media, add_audience=["family"])

    assert decisions.read(media) == Decision(audience=("family",),
                                             event="Banff Skiing")


def test_none_clears_where_omission_does_not(media: Path) -> None:
    decisions.apply(media, audience=["family"], event="Banff Skiing")

    decisions.apply(media, event=None)
    assert decisions.read(media) == Decision(audience=("family",))


def test_clearing_the_last_field_removes_the_sidecar(media: Path) -> None:
    decisions.apply(media, audience=["family"])
    decisions.apply(media, audience=[])

    assert not decisions.sidecar_path(media).exists()


def test_apply_returns_the_stored_state(media: Path) -> None:
    decisions.apply(media, event="Sicily")

    assert decisions.apply(media, add_audience=["kids"]) == Decision(
        audience=("kids",), event="Sicily")


# --- refusals ----------------------------------------------------------------

def test_an_absurd_audience_name_is_refused(media: Path) -> None:
    """Audience names are free text — anyone may be invented before they have
    a login — but a value that long is a paste accident, not a person."""
    with pytest.raises(decisions.DecisionError):
        decisions.write(media, Decision(audience=("x" * 200,)))

    assert not decisions.sidecar_path(media).exists()


def test_a_nonsense_date_is_refused(media: Path) -> None:
    with pytest.raises(decisions.DecisionError):
        decisions.write(media, Decision(date_override="last tuesday"))


def test_a_refusal_leaves_the_previous_decision_intact(media: Path) -> None:
    decisions.write(media, Decision(audience=("family",)))
    with pytest.raises(decisions.DecisionError):
        decisions.apply(media, add_audience=["y" * 200])

    assert decisions.read(media) == Decision(audience=("family",))


# --- damage ------------------------------------------------------------------

def test_an_unparseable_sidecar_reads_as_no_decision(media: Path) -> None:
    """Guessing at a damaged record would put invented values in the index. The
    file stays on disk to be looked at."""
    decisions.sidecar_path(media).write_text("<not xmp", encoding="utf-8")

    assert decisions.read(media) is None
    assert decisions.sidecar_path(media).is_file()


def test_a_write_leaves_no_temp_behind(media: Path) -> None:
    decisions.write(media, Decision(audience=("family",)))

    assert list(media.parent.glob(f"*{SIDECAR_TMP_SUFFIX}*")) == []


def test_a_failed_write_leaves_no_temp_behind(media: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """A half-written packet that parsed would be a decision nobody made."""
    def boom(*_a: object, **_k: object) -> None:
        raise OSError("share went away")

    monkeypatch.setattr(decisions.os, "replace", boom)
    with pytest.raises(OSError):
        decisions.write(media, Decision(audience=("family",)))

    assert list(media.parent.glob(f"*{SIDECAR_TMP_SUFFIX}*")) == []
    assert not decisions.sidecar_path(media).exists()


# --- tags --------------------------------------------------------------------

def test_tags_round_trip(media: Path) -> None:
    decisions.write(media, Decision(tags=("beach", "kids")))

    assert decisions.read(media) == Decision(tags=("beach", "kids"))


def test_tags_are_sorted_and_deduplicated(media: Path) -> None:
    """So the same judgement is the same bytes, and a re-write is not a change."""
    assert Decision(tags=("tv", "beach", "tv", " beach ")).tags == ("beach", "tv")


def test_blank_tags_are_dropped(media: Path) -> None:
    assert Decision(tags=("", "  ", "real")).tags == ("real",)


def test_case_is_preserved_not_folded(media: Path) -> None:
    """Folding would silently rewrite what someone typed into the permanent
    record. The fix for `Beach` vs `beach` is the UI offering what exists."""
    assert Decision(tags=("Beach", "beach")).tags == ("Beach", "beach")


def test_tags_are_written_as_the_standard_keyword_bag(media: Path) -> None:
    """`dc:subject` is what every other tool reads — no `pix:` twin to disagree."""
    decisions.write(media, Decision(tags=("beach",)))
    text = decisions.sidecar_path(media).read_text(encoding="utf-8")

    assert "dc:subject" in text
    assert "<rdf:li>beach</rdf:li>" in text


def test_adding_a_tag_keeps_the_others(media: Path) -> None:
    """The bulk gesture: tagging 200 files adds to what each already carries."""
    decisions.apply(media, tags=["beach"])
    decisions.apply(media, add_tags=["kids"])

    assert decisions.read(media) == Decision(tags=("beach", "kids"))


def test_removing_a_tag_keeps_the_others(media: Path) -> None:
    decisions.apply(media, tags=["beach", "kids", "tv"])
    decisions.apply(media, remove_tags=["kids"])

    assert decisions.read(media) == Decision(tags=("beach", "tv"))


def test_adding_a_tag_does_not_disturb_the_audience(media: Path) -> None:
    decisions.apply(media, add_audience=["family"])
    decisions.apply(media, add_tags=["beach"])

    assert decisions.read(media) == Decision(audience=("family",),
                                             tags=("beach",))


def test_removing_the_last_tag_removes_the_sidecar(media: Path) -> None:
    decisions.apply(media, tags=["beach"])
    decisions.apply(media, remove_tags=["beach"])

    assert not decisions.sidecar_path(media).exists()


def test_awkward_tag_text_survives(media: Path) -> None:
    decisions.write(media, Decision(tags=('R&D <2015>',)))

    assert decisions.read(media) == Decision(tags=('R&D <2015>',))


# --- partial dates -----------------------------------------------------------

def test_a_year_only_override_is_stored(media: Path) -> None:
    """The whole reason the grammar has holes: a scan known only by year."""
    decisions.write(media, Decision(date_override="1987-*-*-*:*:*"))

    assert decisions.read(media) == Decision(date_override="1987-*-*-*:*:*")


def test_a_partial_date_gets_no_standard_twin(media: Path) -> None:
    """`photoshop:DateCreated` has no partial form, and filling the holes to
    produce one would publish a precision the curator did not claim."""
    decisions.write(media, Decision(date_override="1987-*-*-*:*:*"))
    text = decisions.sidecar_path(media).read_text(encoding="utf-8")

    assert "photoshop:DateCreated" not in text
    assert 'pix:DateOverride="1987-*-*-*:*:*"' in text


def test_a_full_date_does_get_one(media: Path) -> None:
    decisions.write(media, Decision(date_override="2015-03-15-11:52:56"))
    text = decisions.sidecar_path(media).read_text(encoding="utf-8")

    assert 'photoshop:DateCreated="2015-03-15T11:52:56"' in text


def test_an_override_that_pins_nothing_is_refused(media: Path) -> None:
    """All-`*` is the same as no override; storing it records a decision nobody
    made, and `no sidecar means unreviewed` depends on that not happening."""
    with pytest.raises(decisions.DecisionError):
        decisions.write(media, Decision(date_override="*-*-*-*:*:*"))
