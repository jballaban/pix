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
    assert [r["name"] for r in ix.files(conn, event="Sicily")] == ["a.jpg"]


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
    _decide(tree, "init_2026", "a.jpg", Decision(tier="top", event="Sicily"))

    conn = ix.connect(tree["db"])
    assert _refresh(tree, conn, "init_2026", "a.jpg") is True

    row = conn.execute("SELECT * FROM files").fetchone()
    assert (row["tier"], row["event"], row["has_sidecar"]) == ("top", "Sicily", 1)


def test_refresh_touches_only_its_own_row(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg", {"XMP:EventAuto": "inherited"})
    _record(tree, "init_2026", "b.jpg", {"XMP:EventAuto": "inherited"})
    _build(tree)
    _decide(tree, "init_2026", "a.jpg", Decision(tier="top"))

    conn = ix.connect(tree["db"])
    _refresh(tree, conn, "init_2026", "a.jpg")

    rows = {r["name"]: (r["tier"], r["event"])
            for r in conn.execute("SELECT * FROM files")}
    assert rows == {"a.jpg": ("top", "inherited"), "b.jpg": (None, "inherited")}


def test_refresh_keeps_the_inherited_event_a_decision_is_silent_about(
    tree: dict[str, Path]
) -> None:
    """Read-through is per-field: tiering a photo must not hide the event it
    inherited from its embedded legacy tags."""
    _record(tree, "init_2026", "a.jpg", {"XMP:EventAuto": "Italy - Sicily"})
    _build(tree)
    _decide(tree, "init_2026", "a.jpg", Decision(tier="photo"))

    conn = ix.connect(tree["db"])
    _refresh(tree, conn, "init_2026", "a.jpg")

    row = conn.execute("SELECT * FROM files").fetchone()
    assert (row["tier"], row["event"]) == ("photo", "Italy - Sicily")


def test_refresh_clears_a_withdrawn_decision(tree: dict[str, Path]) -> None:
    _record(tree, "init_2026", "a.jpg", {})
    _decide(tree, "init_2026", "a.jpg", Decision(tier="top"))
    _build(tree)

    decisions.write(tree["master"] / "init_2026" / "a.jpg", Decision())
    conn = ix.connect(tree["db"])
    _refresh(tree, conn, "init_2026", "a.jpg")

    row = conn.execute("SELECT * FROM files").fetchone()
    assert (row["tier"], row["has_sidecar"]) == (None, 0)


def test_refresh_keeps_the_probed_facts(tree: dict[str, Path]) -> None:
    """A decision write must not blank the facts the row already carried."""
    _record(tree, "init_2026", "a.jpg",
            {"EXIF:DateTimeOriginal": "2026:01:04 14:51:34",
             "EXIF:Model": "iPhone 17 Pro"})
    _build(tree)
    _decide(tree, "init_2026", "a.jpg", Decision(tier="top"))

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
    _decide(tree, "init_2026", "ghost.jpg", Decision(tier="top"))

    conn = ix.connect(tree["db"])
    assert _refresh(tree, conn, "init_2026", "ghost.jpg") is False
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_refresh_never_adds_or_removes_rows(tree: dict[str, Path]) -> None:
    """The file set is `process`'s business; refresh only ever restates a row."""
    _record(tree, "init_2026", "a.jpg", {})
    _record(tree, "init_2026", "b.jpg", {})
    _build(tree)
    _decide(tree, "init_2026", "a.jpg", Decision(tier="top"))

    conn = ix.connect(tree["db"])
    _refresh(tree, conn, "init_2026", "a.jpg")

    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2


def test_a_rebuild_agrees_with_a_refresh(tree: dict[str, Path]) -> None:
    """The two paths read the same inputs, so they must not be able to diverge —
    otherwise a rebuild silently undoes a decision."""
    _record(tree, "init_2026", "a.jpg", {"XMP:EventAuto": "inherited"})
    _build(tree)
    _decide(tree, "init_2026", "a.jpg",
            Decision(tier="top", date_override="2015-03-15-11:52:56"))

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
