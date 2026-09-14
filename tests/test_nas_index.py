"""The app's index (spec/nas-app.md §4).

An aggregation over the derived tiers — disposable, never authoritative, and
built from the meta tier rather than the media, which is the whole reason
`process` writes that tier.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pix.nas import decisions
from pix.nas import index as ix
from pix.nas.decisions import Decision


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    meta = tmp_path / "meta"
    master = tmp_path / "master"
    meta.mkdir()
    master.mkdir()
    return {"meta": meta, "master": master, "db": tmp_path / "index.db"}


def _record(tree: dict[str, Path], folder: str, name: str,
            exif: dict[str, object], size: int = 100) -> None:
    d = tree["meta"] / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({
        "file": name, "folder": folder, "size": size, "mtime_ns": 1,
        "exif": exif,
    }), encoding="utf-8")


def _sidecar(tree: dict[str, Path], folder: str, name: str) -> None:
    d = tree["master"] / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.xmp").write_text("<x/>", encoding="utf-8")


def _build(tree: dict[str, Path]) -> ix.IndexStats:
    return ix.build(tree["db"], meta_dir=tree["meta"], master_dir=tree["master"])


def test_builds_rows_from_the_meta_tier(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg",
            {"EXIF:DateTimeOriginal": "2026:01:04 14:51:34",
             "EXIF:Model": "iPhone 17 Pro",
             "File:ImageWidth": 4032, "File:ImageHeight": 3024})

    stats = _build(tree)
    conn = ix.connect(tree["db"])
    row = conn.execute("SELECT * FROM files").fetchone()

    assert stats.files == 1
    assert row["capture_date"] == "2026:01:04 14:51:34"
    assert row["camera"] == "iPhone 17 Pro"
    assert (row["width"], row["height"]) == (4032, 3024)
    assert row["kind"] == "image"


def test_tags_are_matched_regardless_of_group(tree: dict[str, Path]) -> None:
    """ExifTool prefixes by where it found the value, which varies by format."""
    _record(tree, "init_2026", "a.jpg", {"QuickTime:Model": "Galaxy S26"})
    _build(tree)

    conn = ix.connect(tree["db"])
    assert conn.execute("SELECT camera FROM files").fetchone()["camera"] == "Galaxy S26"


def test_reads_through_to_embedded_event_tags(tree: dict[str, Path]) -> None:
    """Seeding wrote no sidecars — inherited events must simply appear (§4)."""
    _record(tree, "init_2026", "a.jpg", {"XMP:EventAuto": "Italy - Sicily"})
    _build(tree)

    conn = ix.connect(tree["db"])
    assert conn.execute("SELECT event FROM files").fetchone()["event"] == "Italy - Sicily"


def test_an_override_beats_the_auto_value(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg",
            {"XMP:EventAuto": "auto", "XMP:EventOverride": "chosen"})
    _build(tree)

    conn = ix.connect(tree["db"])
    assert conn.execute("SELECT event FROM files").fetchone()["event"] == "chosen"


def test_a_zero_capture_date_is_treated_as_absent(tree: dict[str, Path]) -> None:
    """45 files in the seeded year carry `0000` — that is not a date."""
    _record(tree, "init_2026", "a.jpg",
            {"EXIF:DateTimeOriginal": "0000:00:00 00:00:00"})
    stats = _build(tree)

    conn = ix.connect(tree["db"])
    assert conn.execute("SELECT capture_date FROM files").fetchone()[0] is None
    assert stats.with_date == 0


def test_sidecar_presence_is_recorded(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg", {})
    _record(tree, "init_2026", "b.jpg", {})
    _sidecar(tree, "init_2026", "a.jpg")

    stats = _build(tree)
    conn = ix.connect(tree["db"])
    rows = {r["name"]: r["has_sidecar"] for r in conn.execute("SELECT * FROM files")}

    assert rows == {"a.jpg": 1, "b.jpg": 0}
    assert stats.with_sidecar == 1


def test_video_and_image_are_distinguished(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg", {})
    _record(tree, "init_2026", "b.mp4", {"QuickTime:Duration": "12.5 s"})
    _build(tree)

    conn = ix.connect(tree["db"])
    rows = {r["name"]: (r["kind"], r["duration"])
            for r in conn.execute("SELECT * FROM files")}

    assert rows["a.jpg"][0] == "image"
    assert rows["b.mp4"] == ("video", 12.5)


def test_rebuild_replaces_rather_than_accumulates(tree: dict[str, Path]) -> None:
    """A full rebuild that is always correct beats an incremental one that drifts."""
    _record(tree, "init_2026", "a.jpg", {})
    _build(tree)
    _build(tree)

    conn = ix.connect(tree["db"])
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1


def test_removed_files_disappear_on_rebuild(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg", {})
    _record(tree, "init_2026", "b.jpg", {})
    _build(tree)
    (tree["meta"] / "init_2026" / "b.jpg.json").unlink()
    _build(tree)

    conn = ix.connect(tree["db"])
    assert [r["name"] for r in conn.execute("SELECT name FROM files")] == ["a.jpg"]


def test_unreadable_records_are_skipped_not_fatal(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg", {})
    (tree["meta"] / "init_2026" / "broken.json").write_text("{not json",
                                                           encoding="utf-8")
    stats = _build(tree)

    assert stats.files == 1


# --- queries -----------------------------------------------------------------

def test_events_are_ordered_largest_first(tree: dict[str, Path]) -> None:
    """The biggest 'event' is usually a phone dump needing splitting."""
    for i in range(3):
        _record(tree, "init_2026", f"big{i}.jpg", {"XMP:EventAuto": "james - 2026"})
    _record(tree, "init_2026", "small.jpg", {"XMP:EventAuto": "Italy - Sicily"})
    _build(tree)

    conn = ix.connect(tree["db"])
    rows = ix.events(conn)

    assert rows[0]["event"] == "james - 2026"
    assert rows[0]["n"] == 3
    assert rows[0]["unreviewed"] == 3


def test_untagged_files_group_under_none(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg", {})
    _build(tree)

    conn = ix.connect(tree["db"])
    assert ix.events(conn)[0]["event"] == "(none)"


def test_files_filter_by_event(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg", {"XMP:EventAuto": "Sicily"})
    _record(tree, "init_2026", "b.jpg", {"XMP:EventAuto": "Tuscany"})
    _build(tree)

    conn = ix.connect(tree["db"])
    hits = ix.files(conn, ix.Filters(event="Sicily"))
    assert [r["name"] for r in hits] == ["a.jpg"]


def test_files_sort_dated_before_undated(tree: dict[str, Path]) -> None:
    """Undated files are a work item; they should not scatter through the grid."""
    _record(tree, "init_2026", "undated.jpg", {})
    _record(tree, "init_2026", "dated.jpg",
            {"EXIF:DateTimeOriginal": "2026:01:04 14:51:34"})
    _build(tree)

    conn = ix.connect(tree["db"])
    assert [r["name"] for r in ix.files(conn)] == ["dated.jpg", "undated.jpg"]


def test_summary_counts_the_work(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg",
            {"EXIF:DateTimeOriginal": "2026:01:04 14:51:34",
             "XMP:EventAuto": "Sicily"})
    _record(tree, "init_2026", "b.jpg", {})
    _build(tree)

    conn = ix.connect(tree["db"])
    s = ix.summary(conn)

    assert s["files"] == 2
    assert s["unreviewed"] == 2
    assert s["undated"] == 1


def test_empty_meta_tier_builds_an_empty_index(tree: dict[str, Path]) -> None:
    assert _build(tree).files == 0


def test_open_ro_cannot_write(tree: dict[str, Path]) -> None:
    """The app is a reader; only `pix2 index` builds.

    `connect` creates schema and a version row, which fails on a read-only mount
    and — where the mount is writable — would let the app quietly mutate a cache
    it does not own.
    """
    import sqlite3

    _record(tree, "init_2026", "a.jpg", {})
    _build(tree)

    conn = ix.open_ro(tree["db"])
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM files")


def test_open_ro_does_not_create_a_missing_database(tmp_path: Path) -> None:
    """Opening read-only must not conjure an empty index that reads as valid."""
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        ix.open_ro(tmp_path / "nope.db").execute("SELECT 1 FROM files")


def test_build_records_when_it_ran(tree: dict[str, Path]) -> None:
    import time as _t

    _record(tree, "init_2026", "a.jpg", {})
    _build(tree)

    stamp = ix.built_at(ix.open_ro(tree["db"]))

    assert stamp is not None
    assert abs(_t.time() - stamp) < 60


# --- incremental refresh -----------------------------------------------------

def _decide(tree: dict[str, Path], folder: str, name: str,
            decision: Decision) -> None:
    d = tree["master"] / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b"original bytes")
    decisions.write(d / name, decision)


def _refresh(tree: dict[str, Path], conn: sqlite3.Connection,
             folder: str, name: str) -> bool:
    return ix.refresh(conn, folder, name, meta_dir=tree["meta"],
                      master_dir=tree["master"])


def test_refresh_picks_up_a_new_decision(tree: dict[str, Path]) -> None:
    """The point of it: tiering one photo must not cost a 62k-record rebuild."""
    _record(tree, "init_2026", "a.jpg", {})
    _build(tree)
    _decide(tree, "init_2026", "a.jpg", Decision(audience=("family",), event="Sicily"))

    conn = ix.connect(tree["db"])
    assert _refresh(tree, conn, "init_2026", "a.jpg") is True

    row = ix.one(conn, "init_2026", "a.jpg")
    assert row is not None
    assert (row["audience"], row["event"], row["has_sidecar"]) == ("family", "Sicily", 1)


def test_refresh_touches_only_its_own_row(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg", {"XMP:EventAuto": "inherited"})
    _record(tree, "init_2026", "b.jpg", {"XMP:EventAuto": "inherited"})
    _build(tree)
    _decide(tree, "init_2026", "a.jpg", Decision(audience=("family",)))

    conn = ix.connect(tree["db"])
    _refresh(tree, conn, "init_2026", "a.jpg")

    rows = {r["name"]: (r["audience"], r["event"]) for r in ix.files(conn)}
    assert rows == {"a.jpg": ("family", "inherited"), "b.jpg": (None, "inherited")}


def test_refresh_keeps_the_inherited_event_a_decision_is_silent_about(
    tree: dict[str, Path]
) -> None:
    """Read-through is per-field: sharing a photo must not hide the event it
    inherited from its embedded legacy tags."""
    _record(tree, "init_2026", "a.jpg", {"XMP:EventAuto": "Italy - Sicily"})
    _build(tree)
    _decide(tree, "init_2026", "a.jpg", Decision(audience=("kids",)))

    conn = ix.connect(tree["db"])
    _refresh(tree, conn, "init_2026", "a.jpg")

    row = ix.one(conn, "init_2026", "a.jpg")
    assert row is not None
    assert (row["audience"], row["event"]) == ("kids", "Italy - Sicily")


def test_refresh_clears_a_withdrawn_decision(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg", {})
    _decide(tree, "init_2026", "a.jpg", Decision(audience=("family",)))
    _build(tree)

    decisions.write(tree["master"] / "init_2026" / "a.jpg", Decision())
    conn = ix.connect(tree["db"])
    _refresh(tree, conn, "init_2026", "a.jpg")

    row = ix.one(conn, "init_2026", "a.jpg")
    assert row is not None
    assert (row["audience"], row["has_sidecar"]) == (None, 0)


def test_refresh_keeps_the_probed_facts(tree: dict[str, Path]) -> None:
    """A decision write must not blank the facts the row already carried."""
    _record(tree, "init_2026", "a.jpg",
            {"EXIF:DateTimeOriginal": "2026:01:04 14:51:34",
             "EXIF:Model": "iPhone 17 Pro"})
    _build(tree)
    _decide(tree, "init_2026", "a.jpg", Decision(audience=("family",)))

    conn = ix.connect(tree["db"])
    _refresh(tree, conn, "init_2026", "a.jpg")

    row = conn.execute("SELECT * FROM files").fetchone()
    assert row["capture_date"] == "2026:01:04 14:51:34"
    assert row["camera"] == "iPhone 17 Pro"


def test_refresh_reports_a_file_with_no_probed_facts(tree: dict[str, Path]) -> None:
    """No meta record means `process` has not run for it — there is no row to
    catch up, and inventing one would put a file in the index that the next
    rebuild removes."""
    _build(tree)
    _decide(tree, "init_2026", "ghost.jpg", Decision(audience=("family",)))

    conn = ix.connect(tree["db"])
    assert _refresh(tree, conn, "init_2026", "ghost.jpg") is False
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_refresh_never_adds_or_removes_rows(tree: dict[str, Path]) -> None:
    """The file set is `process`'s business; refresh only ever restates a row."""
    _record(tree, "init_2026", "a.jpg", {})
    _record(tree, "init_2026", "b.jpg", {})
    _build(tree)
    _decide(tree, "init_2026", "a.jpg", Decision(audience=("family",)))

    conn = ix.connect(tree["db"])
    _refresh(tree, conn, "init_2026", "a.jpg")

    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2


def test_a_rebuild_agrees_with_a_refresh(tree: dict[str, Path]) -> None:
    """The two paths read the same inputs, so they must not be able to diverge —
    otherwise a rebuild silently undoes a decision."""
    _record(tree, "init_2026", "a.jpg", {"XMP:EventAuto": "inherited"})
    _build(tree)
    _decide(tree, "init_2026", "a.jpg",
            Decision(audience=("family",), date_override="2015-03-15-11:52:56"))

    conn = ix.connect(tree["db"])
    _refresh(tree, conn, "init_2026", "a.jpg")
    after_refresh = dict(conn.execute("SELECT * FROM files").fetchone())
    conn.close()

    _build(tree)
    conn = ix.connect(tree["db"])
    assert dict(conn.execute("SELECT * FROM files").fetchone()) == after_refresh


# --- opening -----------------------------------------------------------------

def test_open_ro_refuses_to_write(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg", {})
    _build(tree)

    conn = ix.open_ro(tree["db"])
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM files")


def test_open_rw_will_not_create_an_index(tree: dict[str, Path]) -> None:
    """`rwc` would conjure an empty index, which reads as 'the archive is gone'
    rather than 'nobody built one yet'."""
    with pytest.raises(sqlite3.OperationalError):
        ix.open_rw(tree["db"]).execute("SELECT 1 FROM files")


# --- effective dates, years and bands ----------------------------------------

def test_the_year_comes_from_the_capture_date(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg",
            {"EXIF:DateTimeOriginal": "2026:01:04 14:51:34"})
    _build(tree)

    row = ix.connect(tree["db"]).execute("SELECT * FROM files").fetchone()
    assert row["year"] == "2026"
    assert row["effective_date"] == "2026-01-04-14:51:34"


def test_a_year_override_moves_only_the_year(tree: dict[str, Path]) -> None:
    """The point of a partial date: the month, day and time the file actually
    records are kept, because nobody claimed those were wrong."""
    _record(tree, "init_2026", "a.jpg",
            {"EXIF:DateTimeOriginal": "2026:01:04 14:51:34"})
    _decide(tree, "init_2026", "a.jpg", Decision(date_override="1987-*-*-*:*:*"))
    _build(tree)

    row = ix.connect(tree["db"]).execute("SELECT * FROM files").fetchone()
    assert row["year"] == "1987"
    assert row["effective_date"] == "1987-01-04-14:51:34"
    assert row["capture_date"] == "2026:01:04 14:51:34"   # the fact is untouched


def test_an_undated_file_has_no_year(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg", {})
    _build(tree)

    row = ix.connect(tree["db"]).execute("SELECT * FROM files").fetchone()
    assert (row["year"], row["effective_date"]) == (None, None)


def test_duration_parses_both_exiftool_shapes(tree: dict[str, Path]) -> None:
    """91 of the seeded year's 724 clips report `0:00:38`, and reading only
    `38.0 s` rendered them as the word "video" instead of a length."""
    _record(tree, "init_2026", "a.mp4", {"QuickTime:Duration": "0:01:38"})
    _record(tree, "init_2026", "b.mp4", {"QuickTime:Duration": "12.5 s"})
    _build(tree)

    rows = {r["name"]: r["duration"]
            for r in ix.connect(tree["db"]).execute("SELECT * FROM files")}
    assert rows == {"a.mp4": 98.0, "b.mp4": 12.5}


def test_a_short_clip_bands_as_small(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.mp4", {"QuickTime:Duration": "3.0 s"})
    _record(tree, "init_2026", "b.mp4", {"QuickTime:Duration": "0:02:00"})
    _build(tree)

    rows = {r["name"]: r["band"]
            for r in ix.connect(tree["db"]).execute("SELECT * FROM files")}
    assert rows == {"a.mp4": "small", "b.mp4": "large"}


def test_a_tiny_image_bands_as_small(tree: dict[str, Path]) -> None:
    """The other half of one question: a 50KB image is the same kind of suspect
    as a 3-second clip, so one control asks it."""
    _record(tree, "init_2026", "a.jpg", {}, size=50_000)
    _record(tree, "init_2026", "b.jpg", {}, size=3_000_000)
    _build(tree)

    rows = {r["name"]: r["band"]
            for r in ix.connect(tree["db"]).execute("SELECT * FROM files")}
    assert rows == {"a.jpg": "small", "b.jpg": "medium"}


# --- tags in the index -------------------------------------------------------

def test_tags_reach_the_index(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg", {})
    _decide(tree, "init_2026", "a.jpg", Decision(tags=("beach", "kids")))
    stats = _build(tree)

    conn = ix.connect(tree["db"])
    assert stats.tags == 2
    assert [r["name"] for r in ix.files(conn, ix.Filters(tag="beach"))] == ["a.jpg"]
    assert ix.files(conn, ix.Filters(tag="nope")) == []


def test_refreshing_removes_a_dropped_tag(tree: dict[str, Path]) -> None:
    """Delete-then-insert, or removing a tag would silently do nothing."""
    _record(tree, "init_2026", "a.jpg", {})
    _decide(tree, "init_2026", "a.jpg", Decision(tags=("beach", "kids")))
    _build(tree)

    decisions.write(tree["master"] / "init_2026" / "a.jpg",
                    Decision(tags=("kids",)))
    conn = ix.connect(tree["db"])
    _refresh(tree, conn, "init_2026", "a.jpg")

    assert ix.files(conn, ix.Filters(tag="beach")) == []
    assert len(ix.files(conn, ix.Filters(tag="kids"))) == 1


# --- filters -----------------------------------------------------------------

def test_filters_combine_with_and(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg",
            {"EXIF:DateTimeOriginal": "2026:01:04 14:51:34",
             "XMP:EventAuto": "Sicily"})
    _record(tree, "init_2026", "b.jpg",
            {"EXIF:DateTimeOriginal": "2025:01:04 14:51:34",
             "XMP:EventAuto": "Sicily"})
    _build(tree)

    conn = ix.connect(tree["db"])
    hits = ix.files(conn, ix.Filters(event="Sicily", date="2026"))
    assert [r["name"] for r in hits] == ["a.jpg"]


def test_the_new_filter_finds_undecided_files(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg", {})
    _record(tree, "init_2026", "b.jpg", {})
    _decide(tree, "init_2026", "a.jpg", Decision(audience=("private",)))
    _build(tree)

    conn = ix.connect(tree["db"])
    hits = ix.files(conn, ix.Filters(audience=ix.UNREVIEWED))
    assert [r["name"] for r in hits] == ["b.jpg"]


def test_count_ignores_the_page(tree: dict[str, Path]) -> None:
    for n in range(5):
        _record(tree, "init_2026", f"{n}.jpg", {})
    _build(tree)

    conn = ix.connect(tree["db"])
    assert len(ix.files(conn, limit=2)) == 2
    assert ix.count(conn) == 5


# --- suggestions -------------------------------------------------------------

def test_suggestions_are_most_used_first_when_nothing_is_filtered(
    tree: dict[str, Path]
) -> None:
    for n in range(3):
        _record(tree, "init_2026", f"big{n}.jpg", {"XMP:EventAuto": "Big"})
    _record(tree, "init_2026", "small.jpg", {"XMP:EventAuto": "Small"})
    _build(tree)

    got = ix.suggest(ix.connect(tree["db"]), "event")
    assert [s.value for s in got] == ["Big", "Small"]
    assert {s.scope for s in got} == {"all"}


def test_the_current_view_floats_to_the_top(tree: dict[str, Path]) -> None:
    """The whole point: looking at `tag:tv`, the events already used there come
    before the far larger ones used everywhere else."""
    for n in range(20):
        _record(tree, "init_2026", f"big{n}.jpg", {"XMP:EventAuto": "Big"})
    _record(tree, "init_2026", "tv.jpg", {"XMP:EventAuto": "Telly"})
    _decide(tree, "init_2026", "tv.jpg", Decision(tags=("tv",)))
    _build(tree)

    got = ix.suggest(ix.connect(tree["db"]), "event", ix.Filters(tag="tv"))
    assert [(s.value, s.scope) for s in got] == [("Telly", "all"), ("Big", "other")]


def test_matching_one_filter_of_two_lands_in_the_middle_band(
    tree: dict[str, Path]
) -> None:
    """`all` is every filter, `any` is one of them, `other` is neither — so a
    near miss is still offered, just below the exact ones."""
    _record(tree, "init_2026", "both.jpg",
            {"EXIF:DateTimeOriginal": "2026:01:04 14:51:34",
             "XMP:EventAuto": "Both"})
    _decide(tree, "init_2026", "both.jpg", Decision(tags=("tv",)))
    _record(tree, "init_2026", "year.jpg",
            {"EXIF:DateTimeOriginal": "2026:06:04 14:51:34",
             "XMP:EventAuto": "YearOnly"})
    _record(tree, "init_2026", "none.jpg",
            {"EXIF:DateTimeOriginal": "2001:01:04 14:51:34",
             "XMP:EventAuto": "Neither"})
    _build(tree)

    got = ix.suggest(ix.connect(tree["db"]), "event",
                     ix.Filters(tag="tv", date="2026"))
    assert [(s.value, s.scope) for s in got] == [
        ("Both", "all"), ("YearOnly", "any"), ("Neither", "other")]


def test_a_column_ignores_its_own_filter(tree: dict[str, Path]) -> None:
    """Filtering on `event:X` and reaching for an event, `X` is the one option
    nobody wants — so its own filter is excluded from the scope."""
    _record(tree, "init_2026", "a.jpg", {"XMP:EventAuto": "Here"})
    _record(tree, "init_2026", "b.jpg", {"XMP:EventAuto": "Elsewhere"})
    _build(tree)

    got = ix.suggest(ix.connect(tree["db"]), "event", ix.Filters(event="Here"))
    assert {s.scope for s in got} == {"all"}


def test_suggesting_an_unknown_column_is_refused(tree: dict[str, Path]) -> None:
    _build(tree)
    with pytest.raises(ValueError):
        ix.suggest(ix.connect(tree["db"]), "camera")


# --- schema ------------------------------------------------------------------

def test_an_old_schema_is_dropped_not_migrated(tree: dict[str, Path]) -> None:
    """The index is a cache; a migration path is machinery to maintain for
    something `pix2 index` reproduces exactly."""
    conn = ix.connect(tree["db"])
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', '1')")
    conn.execute("INSERT INTO files (folder, name) VALUES ('old', 'stale.jpg')")
    conn.commit()
    conn.close()

    conn = ix.connect(tree["db"])
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


# --- the landing page grouping -----------------------------------------------

def test_events_group_by_year_newest_first(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "new.jpg",
            {"EXIF:DateTimeOriginal": "2026:01:04 14:51:34",
             "XMP:EventAuto": "Recent"})
    _record(tree, "init_2026", "old.jpg",
            {"EXIF:DateTimeOriginal": "2001:01:04 14:51:34",
             "XMP:EventAuto": "Ancient"})
    _build(tree)

    got = [(r["year"], r["event"]) for r in ix.events(ix.connect(tree["db"]))]
    assert got == [("2026", "Recent"), ("2001", "Ancient")]


def test_undated_files_group_last(tree: dict[str, Path]) -> None:
    """`NULL = '(undated)'` is NULL and SQLite sorts NULLs first, which put the
    undated group at the top of the page instead of the bottom."""
    _record(tree, "init_2026", "dated.jpg",
            {"EXIF:DateTimeOriginal": "2001:01:04 14:51:34"})
    _record(tree, "init_2026", "undated.jpg", {})
    _build(tree)

    got = [r["year"] for r in ix.events(ix.connect(tree["db"]))]
    assert got == ["2001", ix.UNDATED]


def test_an_event_spanning_new_year_appears_under_both(
    tree: dict[str, Path]
) -> None:
    """Not a duplicate to collapse: each row links to year + event, and the two
    halves are genuinely different slices of work."""
    _record(tree, "init_2026", "dec.jpg",
            {"EXIF:DateTimeOriginal": "2025:12:31 23:00:00",
             "XMP:EventAuto": "New Year"})
    _record(tree, "init_2026", "jan.jpg",
            {"EXIF:DateTimeOriginal": "2026:01:01 01:00:00",
             "XMP:EventAuto": "New Year"})
    _build(tree)

    got = [(r["year"], r["n"]) for r in ix.events(ix.connect(tree["db"]))]
    assert got == [("2026", 1), ("2025", 1)]


def test_the_undated_group_is_reachable_as_a_filter(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg", {})
    _build(tree)

    conn = ix.connect(tree["db"])
    hits = ix.files(conn, ix.Filters(date=ix.UNDATED))
    assert [r["name"] for r in hits] == ["a.jpg"]


def test_undated_is_offered_as_a_year(tree: dict[str, Path]) -> None:
    """`show me the ones with no date` is a real piece of work, not an absence
    to hide — 374 files in the seeded year."""
    _record(tree, "init_2026", "a.jpg", {})
    _build(tree)

    got = ix.suggest(ix.connect(tree["db"]), "date")
    assert [s.value for s in got] == [ix.UNDATED]


# --- what still matches after a write ----------------------------------------

def test_matching_reports_only_the_rows_that_still_fit(
    tree: dict[str, Path]
) -> None:
    _record(tree, "init_2026", "a.jpg", {})
    _record(tree, "init_2026", "b.jpg",
            {"EXIF:DateTimeOriginal": "2026:01:04 14:51:34"})
    _build(tree)

    conn = ix.connect(tree["db"])
    stays = ix.matching(conn, ix.Filters(date=ix.UNDATED),
                        [("init_2026", "a.jpg"), ("init_2026", "b.jpg")])
    assert stays == {("init_2026", "a.jpg")}


def test_matching_with_no_filters_keeps_everything(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg", {})
    _build(tree)

    conn = ix.connect(tree["db"])
    assert ix.matching(conn, ix.Filters(), [("init_2026", "a.jpg")]) == {
        ("init_2026", "a.jpg")}


def test_matching_an_empty_selection_asks_nothing(tree: dict[str, Path]) -> None:
    _build(tree)
    assert ix.matching(ix.connect(tree["db"]), ix.Filters(), []) == set()


def test_a_name_cannot_forge_the_pair_separator(tree: dict[str, Path]) -> None:
    """Folder and name are joined on a newline, which neither can contain — so
    `a/b` and `a` + `/b` cannot be confused for one another."""
    _record(tree, "init_2026", "a.jpg", {})
    _build(tree)

    conn = ix.connect(tree["db"])
    assert ix.matching(conn, ix.Filters(), [("init", "2026/a.jpg")]) == set()


# --- proposing an event by its dates ------------------------------------------

def _dated(tree: dict[str, Path], name: str, when: str,
           event: str | None = None) -> None:
    """One file on a given day, optionally already belonging to an event."""
    exif: dict[str, object] = {"EXIF:DateTimeOriginal": when}
    if event:
        exif["XMP:EventAuto"] = event
    _record(tree, "f", name, exif)


def test_an_event_spanning_the_selection_is_proposed_first(
    tree: dict[str, Path]
) -> None:
    """The time-neighbour proposal (§8). Photographs taken on the same days as
    an event usually belong to it, and that beats every other signal there is
    for naming one — so it outranks the filter bands, which know only what is
    on screen."""
    _dated(tree, "f1.jpg", "2026:07:26 10:00:00", "France Trip")
    _dated(tree, "f2.jpg", "2026:07:30 10:00:00", "France Trip")
    _dated(tree, "o1.jpg", "2026:01:02 10:00:00", "Christmas")
    _dated(tree, "o2.jpg", "2026:01:03 10:00:00", "Christmas")
    _dated(tree, "new.jpg", "2026:07:28 10:00:00")
    _build(tree)
    conn = ix.connect(tree["db"])

    got = ix.suggest(conn, "event",
                     near=("2026-07-28-10:00:00", "2026-07-28-10:00:00"))
    by = {s.value: s.scope for s in got}

    assert by["France Trip"] == "near", by
    assert by["Christmas"] != "near", by
    assert got[0].value == "France Trip", [s.value for s in got]


def test_without_a_selection_nothing_is_near(tree: dict[str, Path]) -> None:
    """The band has to be absent rather than empty-and-first: with no selection
    there is no evidence, and proposing on none would be guessing."""
    _dated(tree, "f1.jpg", "2026:07:26 10:00:00", "France Trip")
    _build(tree)
    conn = ix.connect(tree["db"])

    assert all(s.scope != "near" for s in ix.suggest(conn, "event"))


def test_near_reaches_a_day_past_each_end(tree: dict[str, Path]) -> None:
    """Strict overlap misses by a hair — the last afternoon photographed after
    midnight, a camera an hour out — and an event proposed a day too late is no
    proposal at all."""
    _dated(tree, "f1.jpg", "2026:07:26 10:00:00", "France Trip")
    _dated(tree, "f2.jpg", "2026:07:30 10:00:00", "France Trip")
    _build(tree)
    conn = ix.connect(tree["db"])

    # The morning after it ended.
    near = ("2026-07-31-09:00:00", "2026-07-31-09:00:00")
    assert [s.scope for s in ix.suggest(conn, "event", near=near)] == ["near"]

    # A week after is not "around" anything.
    far = ("2026-08-06-09:00:00", "2026-08-06-09:00:00")
    assert all(s.scope != "near" for s in ix.suggest(conn, "event", near=far))


def test_an_undated_event_is_near_nothing(tree: dict[str, Path]) -> None:
    """MIN and MAX drop NULLs, so an event with no dates has no span. Reading
    that as a match would put the one event nobody can place at the top of
    every proposal."""
    _record(tree, "f", "u1.jpg", {"XMP:EventAuto": "Unknown"})
    _dated(tree, "f1.jpg", "2026:07:26 10:00:00", "France Trip")
    _build(tree)
    conn = ix.connect(tree["db"])

    got = {s.value: s.scope
           for s in ix.suggest(conn, "event",
                               near=("2026-07-26-10:00:00",
                                     "2026-07-26-10:00:00"))}
    assert got["France Trip"] == "near"
    assert got["Unknown"] != "near", got


def test_only_events_have_a_span(tree: dict[str, Path]) -> None:
    """A tag is not an occasion. Asking when one happened would rank keywords
    by a property they do not have."""
    _dated(tree, "f1.jpg", "2026:07:26 10:00:00", "France Trip")
    _sidecar(tree, "f", "f1.jpg")
    decisions.write(tree["master"] / "f" / "f1.jpg",
                    Decision(tags=("beach",)))
    _build(tree)
    conn = ix.connect(tree["db"])

    near = ("2026-07-26-10:00:00", "2026-07-26-10:00:00")
    assert all(s.scope != "near" for s in ix.suggest(conn, "tag", near=near))


def test_the_tightest_event_covering_the_day_is_proposed_first(
    tree: dict[str, Path]
) -> None:
    """Overlap alone is not enough. Against the real library a date in France
    matched seven events and six of them were device dumps long enough to
    overlap anything — so the band is ordered by how tightly the event fits.

    A fortnight that covers your day is a claim about your day; eight months of
    somebody's phone covers it too and says nothing."""
    _dated(tree, "t1.jpg", "2026:07:26 10:00:00", "France Trip")
    _dated(tree, "t2.jpg", "2026:08:02 10:00:00", "France Trip")
    # A phone dump either side of it, months wide — and *bigger*, so that the
    # fallback ordering (most-used first) would put it on top. Without that the
    # test passes on alphabet alone and proves nothing.
    _dated(tree, "d1.jpg", "2026:02:01 10:00:00", "alina")
    _dated(tree, "d2.jpg", "2026:11:01 10:00:00", "alina")
    _dated(tree, "d3.jpg", "2026:07:28 11:00:00", "alina")
    _build(tree)
    conn = ix.connect(tree["db"])

    near = ("2026-07-28-10:00:00", "2026-07-28-10:00:00")
    got = [s.value for s in ix.suggest(conn, "event", near=near)
           if s.scope == "near"]

    assert got == ["France Trip", "alina"], got


# --- narrowing by date --------------------------------------------------------

def _on(tree: dict[str, Path], name: str, when: str) -> None:
    _record(tree, "f", name, {"EXIF:DateTimeOriginal": when})


def test_a_date_filter_narrows_at_whatever_width_it_is_given(
    tree: dict[str, Path]
) -> None:
    """Year, month and day are the same question at three widths, and a library
    is narrowed down in exactly that order."""
    _on(tree, "a.jpg", "2026:08:30 10:00:00")
    _on(tree, "b.jpg", "2026:08:02 10:00:00")
    _on(tree, "c.jpg", "2026:01:02 10:00:00")
    _on(tree, "d.jpg", "2025:08:30 10:00:00")
    _build(tree)
    conn = ix.connect(tree["db"])

    def names(value: str) -> set[str]:
        return {r["name"] for r in ix.files(conn, ix.Filters(date=value))}

    assert names("2026") == {"a.jpg", "b.jpg", "c.jpg"}
    assert names("2026-08") == {"a.jpg", "b.jpg"}
    assert names("2026-08-30") == {"a.jpg"}
    assert names("2025") == {"d.jpg"}


def test_a_date_that_is_not_one_narrows_nothing(tree: dict[str, Path]) -> None:
    """It comes out of a URL, which people type and edit by hand. A half-typed
    date should show everything rather than 500 — and its width reaches SQL as
    a `substr` length, so it must be a number this code chose."""
    assert ix.date_prefix("2026") == "2026"
    assert ix.date_prefix("2026-08-30") == "2026-08-30"
    assert ix.date_prefix(ix.UNDATED) == ix.UNDATED
    for junk in ("2026-8", "202", "2026-08-30-11", "'; DROP TABLE files--",
                 "", "august"):
        assert ix.date_prefix(junk) is None, junk


def test_the_date_menu_drills_down(tree: dict[str, Path]) -> None:
    """Years with nothing set, that year's months inside a year, its days
    inside a month. The alternative is offering three thousand days at once to
    somebody who knows only that it was a summer."""
    _on(tree, "a.jpg", "2026:08:30 10:00:00")
    _on(tree, "b.jpg", "2026:08:02 10:00:00")
    _on(tree, "c.jpg", "2026:01:02 10:00:00")
    _on(tree, "d.jpg", "2025:08:30 10:00:00")
    _build(tree)
    conn = ix.connect(tree["db"])

    def offered(current: str | None) -> list[str]:
        return [s.value for s in ix.suggest(conn, "date",
                                            ix.Filters(date=current))]

    assert offered(None) == ["2026", "2025"], offered(None)
    assert offered("2026") == ["2026-08", "2026-01"], offered("2026")
    assert offered("2026-08") == ["2026-08-30", "2026-08-02"]


def test_a_chosen_day_offers_its_neighbours(tree: dict[str, Path]) -> None:
    """Having picked the 30th, what you want next is the 29th — not a list
    containing only the 30th."""
    _on(tree, "a.jpg", "2026:08:30 10:00:00")
    _on(tree, "b.jpg", "2026:08:02 10:00:00")
    _build(tree)
    conn = ix.connect(tree["db"])

    got = [s.value for s in ix.suggest(conn, "date",
                                       ix.Filters(date="2026-08-30"))]
    assert got == ["2026-08-30", "2026-08-02"], got


def test_undated_is_offered_beside_the_years(tree: dict[str, Path]) -> None:
    """*Show me the ones nobody could place* is a real piece of work, not an
    absence to hide — and it sorts last on its own, a bracket being below every
    digit."""
    _on(tree, "a.jpg", "2026:08:30 10:00:00")
    _record(tree, "f", "u.jpg", {})
    _build(tree)
    conn = ix.connect(tree["db"])

    assert [s.value for s in ix.suggest(conn, "date")] == ["2026", ix.UNDATED]
    assert [r["name"] for r in
            ix.files(conn, ix.Filters(date=ix.UNDATED))] == ["u.jpg"]


# --- a date with holes in it keeps them ---------------------------------------

def _partial(tree: dict[str, Path], name: str, override: str) -> None:
    """A file with no capture date and an override that pins only part of one."""
    _record(tree, "f", name, {})
    d = tree["master"] / "f"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b"x")
    decisions.write(d / name, Decision(date_override=override))


def test_a_day_that_is_not_known_is_not_invented(tree: dict[str, Path]) -> None:
    """*August 2026, day unknown* has an `effective_date` of `2026-08-01`
    because there has to be something to sort by. Grouping on that filed it
    under the first of August, beside photographs actually taken that day."""
    _dated(tree, "real.jpg", "2026:08:01 10:00:00")
    _partial(tree, "month.jpg", "2026-08-*-*:*:*")
    _build(tree)
    conn = ix.connect(tree["db"])

    by_day = {r["name"]: r["grp0"] for r in ix.files(conn, groups=["day"])}
    assert by_day["real.jpg"] == "2026-08-01"
    assert by_day["month.jpg"] is None, "invented a day it does not have"

    # But the month it does know still groups, and so does the year.
    by_month = {r["name"]: r["grp0"] for r in ix.files(conn, groups=["month"])}
    assert by_month["month.jpg"] == "2026-08"
    by_year = {r["name"]: r["grp0"] for r in ix.files(conn, groups=["year"])}
    assert by_year["month.jpg"] == "2026"


def test_grouping_never_drops_a_file(tree: dict[str, Path]) -> None:
    """A grouping is not a filter. Whatever it cannot answer for gathers in a
    section of its own, and the count still adds up."""
    _dated(tree, "real.jpg", "2026:08:01 10:00:00")
    _partial(tree, "month.jpg", "2026-08-*-*:*:*")
    _partial(tree, "year.jpg", "1987-*-*-*:*:*")
    _record(tree, "f", "none.jpg", {})
    _build(tree)
    conn = ix.connect(tree["db"])

    for group in ("day", "month", "year", "event", "camera", "kind"):
        assert len(ix.files(conn, groups=[group])) == 4, group


def test_a_filter_only_matches_what_the_date_actually_says(
    tree: dict[str, Path]
) -> None:
    """The same invention, in the other place: filtering to the first of August
    matched a file only known to be from August."""
    _dated(tree, "real.jpg", "2026:08:01 10:00:00")
    _partial(tree, "month.jpg", "2026-08-*-*:*:*")
    _build(tree)
    conn = ix.connect(tree["db"])

    def names(value: str) -> set[str]:
        return {r["name"] for r in ix.files(conn, ix.Filters(date=value))}

    assert names("2026-08-01") == {"real.jpg"}
    assert names("2026-08") == {"real.jpg", "month.jpg"}
    assert names("2026") == {"real.jpg", "month.jpg"}


def test_a_partly_known_date_is_not_undated(tree: dict[str, Path]) -> None:
    """*Undated* is no date at all, not *not to that precision*. A file known
    to be from August is not one of the ones nobody could place."""
    _partial(tree, "month.jpg", "2026-08-*-*:*:*")
    _record(tree, "f", "none.jpg", {})
    _build(tree)
    conn = ix.connect(tree["db"])

    assert {r["name"] for r in
            ix.files(conn, ix.Filters(date=ix.UNDATED))} == {"none.jpg"}


def test_the_day_list_offers_only_days_that_exist(tree: dict[str, Path]) -> None:
    """A file dated to its month has no day to offer, and offering it as
    `(undated)` would be the same lie in a different place."""
    _dated(tree, "real.jpg", "2026:08:01 10:00:00")
    _partial(tree, "month.jpg", "2026-08-*-*:*:*")
    _build(tree)
    conn = ix.connect(tree["db"])

    days = [s.value for s in ix.suggest(conn, "date",
                                        ix.Filters(date="2026-08"))]
    assert days == ["2026-08-01"], days
    # And it is still reachable one level up, which is where it belongs.
    assert "2026-08" in [s.value for s in
                         ix.suggest(conn, "date", ix.Filters(date="2026"))]


def test_an_override_on_a_real_capture_date_keeps_full_precision(
    tree: dict[str, Path]
) -> None:
    """Correcting the year of a photograph does not make its day a guess — the
    unpinned components still read off the camera."""
    _record(tree, "f", "fixed.jpg", {"EXIF:DateTimeOriginal": "2026:08:30 10:00:00"})
    d = tree["master"] / "f"
    d.mkdir(parents=True, exist_ok=True)
    (d / "fixed.jpg").write_bytes(b"x")
    decisions.write(d / "fixed.jpg", Decision(date_override="2025-*-*-*:*:*"))
    _build(tree)
    conn = ix.connect(tree["db"])

    assert [r["grp0"] for r in ix.files(conn, groups=["day"])] == ["2025-08-30"]


# --- stacks -------------------------------------------------------------------

def _stacked(tree: dict[str, Path], name: str, under: str) -> None:
    d = tree["master"] / "f"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b"x")
    decisions.write(d / name, Decision(stacked_under=under))


def test_a_stacked_file_does_not_appear_on_its_own(tree: dict[str, Path]) -> None:
    """That is what stacking is: eight takes of one photograph, one shown."""
    for i in range(4):
        _dated(tree, f"shot{i}.jpg", f"2026:08:30 10:00:0{i}")
    for i in (1, 2, 3):
        _stacked(tree, f"shot{i}.jpg", "f/shot0.jpg")
    _build(tree)
    conn = ix.connect(tree["db"])

    rows = ix.files(conn)
    assert [r["name"] for r in rows] == ["shot0.jpg"]
    assert rows[0]["behind"] == 3
    assert ix.count(conn) == 1


def test_opening_a_stack_shows_it_whole(tree: dict[str, Path]) -> None:
    """The top and everything behind it, in the ordinary grid — so everything
    the grid can do still applies to them."""
    for i in range(4):
        _dated(tree, f"shot{i}.jpg", f"2026:08:30 10:00:0{i}")
    for i in (1, 2, 3):
        _stacked(tree, f"shot{i}.jpg", "f/shot0.jpg")
    _build(tree)
    conn = ix.connect(tree["db"])

    rows = ix.files(conn, ix.Filters(within="f/shot0.jpg"))
    assert {r["name"] for r in rows} == {
        "shot0.jpg", "shot1.jpg", "shot2.jpg", "shot3.jpg"}


def test_a_stack_hides_its_files_from_every_listing(tree: dict[str, Path]) -> None:
    """Filtering and counting have to agree with the grid, or a count says one
    thing and the thumbnails another."""
    _dated(tree, "top.jpg", "2026:08:30 10:00:00")
    _dated(tree, "behind.jpg", "2026:08:30 10:00:01")
    _stacked(tree, "behind.jpg", "f/top.jpg")
    _build(tree)
    conn = ix.connect(tree["db"])

    assert ix.count(conn, ix.Filters(date="2026-08-30")) == 1
    assert [r["name"] for r in
            ix.files(conn, ix.Filters(date="2026-08-30"))] == ["top.jpg"]


def test_a_file_with_nothing_behind_it_is_not_a_stack(
    tree: dict[str, Path]
) -> None:
    """A count of one is a photograph. The badge has to know the difference."""
    _dated(tree, "alone.jpg", "2026:08:30 10:00:00")
    _build(tree)
    conn = ix.connect(tree["db"])

    assert ix.files(conn)[0]["behind"] == 0


# --- hidden means hidden everywhere -------------------------------------------

def test_a_stacked_file_never_reaches_a_filter(tree: dict[str, Path]) -> None:
    """Stack a photograph from the 1st behind one from the 2nd and the 1st is
    gone — not merely from the grid, but from the dates you can ask for. A file
    that can be filtered back into view is one that came out of its stack."""
    _dated(tree, "jan1.jpg", "2026:01:01 10:00:00", "Trip")
    _dated(tree, "jan2.jpg", "2026:01:02 10:00:00", "Trip")
    _stacked(tree, "jan1.jpg", "f/jan2.jpg")
    _build(tree)
    conn = ix.connect(tree["db"])

    assert ix.files(conn, ix.Filters(date="2026-01-01")) == []
    assert ix.count(conn, ix.Filters(date="2026-01-01")) == 0
    # And the 1st is not offered as a day to filter by at all.
    days = [s.value for s in ix.suggest(conn, "date", ix.Filters(date="2026-01"))]
    assert days == ["2026-01-02"], days


def test_a_dropdown_offers_nothing_the_grid_will_not_show(
    tree: dict[str, Path]
) -> None:
    """A tag carried only by a hidden file was offered as a filter, and
    clicking it gave an empty grid. The dropdowns took their own path to the
    library and answered with files the grid would never show."""
    _dated(tree, "shown.jpg", "2026:01:02 10:00:00")
    _dated(tree, "hidden.jpg", "2026:01:01 10:00:00")
    _sidecar(tree, "f", "hidden.jpg")
    decisions.write(tree["master"] / "f" / "hidden.jpg",
                    Decision(stacked_under="f/shown.jpg", tags=("beach",)))
    _build(tree)
    conn = ix.connect(tree["db"])

    assert [s.value for s in ix.suggest(conn, "tag")] == []
    assert [(s.value, s.n) for s in ix.suggest(conn, "date")] == [("2026", 1)]


def test_a_deleted_file_is_hidden_from_the_dropdowns_too(
    tree: dict[str, Path]
) -> None:
    """The same hole, found by asking the same question of the other state a
    file can be in. One place decides what is in the library now."""
    _dated(tree, "live.jpg", "2026:01:02 10:00:00")
    _dated(tree, "gone.jpg", "2026:01:01 10:00:00")
    _sidecar(tree, "f", "gone.jpg")
    decisions.write(tree["master"] / "f" / "gone.jpg",
                    Decision(deleted=True, tags=("beach",)))
    _build(tree)
    conn = ix.connect(tree["db"])

    assert [s.value for s in ix.suggest(conn, "tag")] == []
    assert [(s.value, s.n) for s in ix.suggest(conn, "date")] == [("2026", 1)]
    # And in the bin, it is the living one that is not offered.
    binned = ix.Filters(deleted="only")
    assert [s.value for s in ix.suggest(conn, "tag", binned)] == ["beach"]


def test_opening_a_stack_still_offers_what_is_in_it(
    tree: dict[str, Path]
) -> None:
    """Hidden is about the ordinary view, not about the stack itself."""
    _dated(tree, "top.jpg", "2026:01:02 10:00:00")
    _dated(tree, "behind.jpg", "2026:01:01 10:00:00")
    _sidecar(tree, "f", "behind.jpg")
    decisions.write(tree["master"] / "f" / "behind.jpg",
                    Decision(stacked_under="f/top.jpg", tags=("beach",)))
    _build(tree)
    conn = ix.connect(tree["db"])

    inside = ix.Filters(within="f/top.jpg")
    assert [s.value for s in ix.suggest(conn, "tag", inside)] == ["beach"]


# --- suggested stacks ---------------------------------------------------------

def _shot(tree: dict[str, Path], name: str, when: str,
          camera: str = "iPhone 17 Pro") -> None:
    _record(tree, "f", name, {"EXIF:DateTimeOriginal": when,
                              "EXIF:Model": camera})


def _rows(tree: dict[str, Path]) -> list[Any]:
    _build(tree)
    return ix.files(ix.connect(tree["db"]), limit=1000)


def test_photographs_seconds_apart_are_one_suggestion(
    tree: dict[str, Path]
) -> None:
    """Most of a burst is one photograph shot eight times."""
    _shot(tree, "a.jpg", "2026:08:30 10:00:00")
    _shot(tree, "b.jpg", "2026:08:30 10:00:01")
    _shot(tree, "c.jpg", "2026:08:30 10:02:00")     # two minutes later

    got = ix.suggestions(_rows(tree))
    assert [sorted(r["name"] for r in g) for g in got] == [["a.jpg", "b.jpg"]]


def test_a_photograph_on_its_own_is_not_a_suggestion(
    tree: dict[str, Path]
) -> None:
    """There is nothing to review about one that resembles no neighbour."""
    _shot(tree, "alone.jpg", "2026:08:30 10:00:00")

    assert ix.suggestions(_rows(tree)) == []


def test_two_cameras_at_once_are_not_one_moment(tree: dict[str, Path]) -> None:
    """Two people photographing the same thing are not eight takes of it."""
    _shot(tree, "mine.jpg", "2026:08:30 10:00:00", camera="iPhone 17 Pro")
    _shot(tree, "yours.jpg", "2026:08:30 10:00:01", camera="Pixel 9")

    assert ix.suggestions(_rows(tree)) == []


def test_the_collision_suffix_is_a_signal_of_its_own(
    tree: dict[str, Path]
) -> None:
    """Two files whose names differ only by `_001` had the same event and the
    same second — which the burst window cannot see without a camera."""
    _record(tree, "f", "G_2026-08-30_100000.jpg",
            {"EXIF:DateTimeOriginal": "2026:08:30 10:00:00"})
    _record(tree, "f", "G_2026-08-30_100000_001.jpg",
            {"EXIF:DateTimeOriginal": "2026:08:30 10:00:00"})

    got = ix.suggestions(_rows(tree))
    assert len(got) == 1 and len(got[0]) == 2


def test_a_fabricated_timestamp_is_not_evidence(tree: dict[str, Path]) -> None:
    """A generated name carries the date, so files dated only to a month all
    get the same name and collide. Sixty-eight photographs of a skiing trip
    read as one burst because none of them knew which day it happened on."""
    for i in range(4):
        name = f"G_2026-03-01_000000{'_%03d' % i if i else ''}.jpg"
        _record(tree, "f", name, {})
        _sidecar(tree, "f", name)
        decisions.write(tree["master"] / "f" / name,
                        Decision(date_override="2026-03-*-*:*:*"))

    assert ix.suggestions(_rows(tree)) == []


def test_a_file_declined_once_is_not_offered_again(
    tree: dict[str, Path]
) -> None:
    """The whole point of remembering: scrolling past the same proposal every
    time is worse than never having been offered it."""
    _shot(tree, "a.jpg", "2026:08:30 10:00:00")
    _shot(tree, "b.jpg", "2026:08:30 10:00:01")
    for name in ("a.jpg", "b.jpg"):
        _sidecar(tree, "f", name)
        decisions.write(tree["master"] / "f" / name, Decision(no_stack=True))

    assert ix.suggestions(_rows(tree)) == []


def test_a_file_already_stacked_is_not_offered(tree: dict[str, Path]) -> None:
    """It has been dealt with, by the strongest possible answer."""
    _shot(tree, "top.jpg", "2026:08:30 10:00:00")
    _shot(tree, "behind.jpg", "2026:08:30 10:00:01")
    _sidecar(tree, "f", "behind.jpg")
    decisions.write(tree["master"] / "f" / "behind.jpg",
                    Decision(stacked_under="f/top.jpg"))

    assert ix.suggestions(_rows(tree)) == []


def test_no_photograph_is_in_two_suggestions(tree: dict[str, Path]) -> None:
    """The two signals overlap almost entirely. A file in both would be
    accepted into one stack and still asked about in the other."""
    _record(tree, "f", "G_2026-08-30_100000.jpg",
            {"EXIF:DateTimeOriginal": "2026:08:30 10:00:00",
             "EXIF:Model": "iPhone 17 Pro"})
    _record(tree, "f", "G_2026-08-30_100000_001.jpg",
            {"EXIF:DateTimeOriginal": "2026:08:30 10:00:00",
             "EXIF:Model": "iPhone 17 Pro"})
    _shot(tree, "G_2026-08-30_100001.jpg", "2026:08:30 10:00:01")

    got = ix.suggestions(_rows(tree))
    seen = [r["name"] for g in got for r in g]
    assert len(seen) == len(set(seen)), seen
