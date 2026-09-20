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
    """An audience with no members must still create a sidecar. Choosing to
    show a photograph to nobody is a judgement, and it is not the same state as
    never having looked at it — which is exactly what no sidecar means."""
    decisions.write(media, Decision(audience=("private",)))

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


# --- names fold, tags do not -------------------------------------------------

def test_an_audience_folds_to_one_spelling(media: Path) -> None:
    """`Kid` and `kid` are one person, not two audiences that look identical."""
    decisions.write(media, Decision(audience=("Kid", "KID", "kid")))

    assert decisions.read(media) == Decision(audience=("kid",))


def test_unsharing_matches_whatever_case_was_typed(media: Path) -> None:
    """The one that matters: access granted and then not taken back is worse
    than never granting it."""
    decisions.apply(media, add_audience=["Family"])
    decisions.apply(media, remove_audience=["FAMILY"])

    assert not decisions.sidecar_path(media).exists()


def test_sharing_twice_in_different_case_is_one_grant(media: Path) -> None:
    decisions.apply(media, add_audience=["tv"])
    decisions.apply(media, add_audience=["TV"])

    assert decisions.read(media) == Decision(audience=("tv",))


def test_a_sidecar_written_elsewhere_still_folds(media: Path) -> None:
    """Read-side too, or a hand-edited `Kid` would be a grant nothing matches."""
    decisions.sidecar_path(media).write_text(
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:pix="http://pix.local/">'
        "<pix:Audience><rdf:Bag><rdf:li>Kid</rdf:li></rdf:Bag></pix:Audience>"
        "</rdf:Description></rdf:RDF>", encoding="utf-8")

    assert decisions.read(media) == Decision(audience=("kid",))


def test_tags_keep_their_case(media: Path) -> None:
    """A tag is a phrase someone wrote, not an identifier — folding it would
    rewrite what they typed into the permanent record."""
    decisions.write(media, Decision(tags=("Beach", "beach")))

    assert decisions.read(media) == Decision(tags=("Beach", "beach"))


# --- who is in the photograph -------------------------------------------------

def test_a_person_keeps_the_case_that_was_typed(tmp_path: Path) -> None:
    """A name is the one kind of word where case is part of it. `audience`
    folds because it is compared against logins; nobody logs in as Mum."""
    assert decisions.normalize_people(["Mom", "Dad"]) == ("Dad", "Mom")


def test_one_person_is_not_two_because_of_a_capital(tmp_path: Path) -> None:
    """*Mom* and *mom* are one person, which is the whole of what makes this
    groupable. Sorted before de-duplicating so the answer does not depend on
    the order they arrived in — and so capitals win, which is the spelling
    anybody meant."""
    assert decisions.normalize_people(["mom", "Mom"]) == ("Mom",)
    assert decisions.normalize_people(["Mom", "mom"]) == ("Mom",)


def test_people_and_audience_are_different_lists(tmp_path: Path) -> None:
    """Opposite questions that take the same kind of word: who is shown, and
    who may look. Mum being in a picture says nothing about whether it is
    shared with her."""
    media = tmp_path / "a.jpg"
    media.write_bytes(b"x")

    decisions.write(media, decisions.Decision(people=("Mom",),
                                              audience=("kids",)))
    back = decisions.read(media)

    assert back is not None
    assert back.people == ("Mom",)
    assert back.audience == ("kids",)


def test_people_round_trip_through_the_standard_field(tmp_path: Path) -> None:
    """`Iptc4xmpExt:PersonInImage` is the industry field for *who is shown*, so
    a cataloguer that has never heard of pix still reads the names."""
    media = tmp_path / "a.jpg"
    media.write_bytes(b"x")

    decisions.write(media, decisions.Decision(people=("Mom", "Dad")))

    raw = decisions.sidecar_path(media).read_text(encoding="utf-8")
    assert "Iptc4xmpExt:PersonInImage" in raw
    assert "pix:People" not in raw


def test_taking_a_person_out_matches_whatever_case_was_stored(
    tmp_path: Path
) -> None:
    """The failure this avoids is a name that can be put on and not taken off.
    `audience` avoids it by folding in storage too; a person cannot, so the
    match folds and the storage does not."""
    media = tmp_path / "a.jpg"
    media.write_bytes(b"x")
    decisions.write(media, decisions.Decision(people=("mom",)))

    _, after = decisions.change(media, remove_people=["MOM"])

    assert after.people == ()


def test_a_person_alone_is_still_a_decision(tmp_path: Path) -> None:
    """No sidecar means undecided, so saying who is in a photograph has to
    create one like any other judgement."""
    assert not decisions.Decision(people=("Mom",)).is_empty()
    assert decisions.Decision().is_empty()


def test_an_audience_of_nobody_is_not_a_decision(tmp_path: Path) -> None:
    """Pinned because the module docstring claimed the opposite for a long
    time, and a comment that describes behaviour nobody implemented is how
    working code gets "fixed".

    There is no way for the dataclass to carry it: a tuple cannot tell *set to
    nothing* from *never set*. The answer the model does have is a role with
    no members — a name like `private` is a real audience, so the file counts
    as decided, and nobody is in it.
    """
    media = tmp_path / "a.jpg"
    media.write_bytes(b"x")

    decisions.write(media, decisions.Decision(audience=()))

    assert decisions.Decision(audience=()) == decisions.Decision()
    assert not decisions.sidecar_path(media).exists()
    assert decisions.read(media) is None


def test_a_role_with_no_members_is_how_you_keep_one_to_yourself(
    tmp_path: Path
) -> None:
    """Which is the whole of what the missing feature would have bought."""
    media = tmp_path / "a.jpg"
    media.write_bytes(b"x")

    decisions.write(media, decisions.Decision(audience=("private",)))

    after = decisions.read(media)
    assert after is not None and after.audience == ("private",)
    assert not after.is_empty()
