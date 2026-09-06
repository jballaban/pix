"""The export reconcile engine: desired set, target validation, plan."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from pix.config import Distribution
from pix.export import (
    ExportAction,
    MissingHashesError,
    Source,
    adopt,
    build_plan,
    classify,
    desired_members,
    scan_target,
)
from pix.export_manifest import Member
from pix.metadata import FileMetadata
from pix.organize import parse_template
from pix.tag_filter import parse as parse_filter

LIB = Path("G:/pix")


def _meta(
    path: Path,
    *,
    year: str = "2023",
    event: str = "Hawaii",
    rating: str | None = "5",
) -> FileMetadata:
    raw: dict[str, object] = {
        "SourceFile": str(path),
        "XMP:DateAuto": f"{year}-08-15-14:32:05",
        "XMP:EventAuto": event,
    }
    if rating is not None:
        raw["XMP:Rating"] = int(rating)
    return FileMetadata(path=path, raw=raw)


def _dist(filter_expr: str = "rating:4,5") -> Distribution:
    return Distribution(
        name="general",
        path="D:/Delivery",
        template="{year}/{event}",
        filter=parse_filter(filter_expr),
    )


def _inputs(
    metas: dict[Path, FileMetadata], hashes: dict[Path, str | None]
) -> tuple[list[Path], dict[Path, FileMetadata], dict[Path, str | None], dict[Path, int]]:
    return list(metas), metas, hashes, {p: 100 for p in metas}


# --- Desired set -------------------------------------------------------------


def test_desired_members_applies_filter_and_template() -> None:
    a, b = LIB / "a.jpg", LIB / "b.jpg"
    metas = {a: _meta(a, rating="5"), b: _meta(b, rating="2")}
    files, cache, hashes, sizes = _inputs(metas, {a: "ha", b: "hb"})

    desired = desired_members(
        files, cache, hashes, sizes, _dist(), parse_template("{year}/{event}")
    )
    assert set(desired) == {"2023/Hawaii/a.jpg"}
    assert desired["2023/Hawaii/a.jpg"].content_hash == "ha"


def test_desired_members_excluded_files_do_not_get_a_filtered_folder() -> None:
    # export's rule: excluded files just don't appear (organize's
    # `(filtered)` folder is not export's behaviour).
    a = LIB / "a.jpg"
    files, cache, hashes, sizes = _inputs({a: _meta(a, rating="1")}, {a: "ha"})
    desired = desired_members(
        files, cache, hashes, sizes, _dist(), parse_template("{year}")
    )
    assert desired == {}


def test_desired_members_honours_a_filter_inside_the_template() -> None:
    a, b = LIB / "a.jpg", LIB / "b.jpg"
    metas = {a: _meta(a, year="2023"), b: _meta(b, year="2020")}
    files, cache, hashes, sizes = _inputs(metas, {a: "ha", b: "hb"})
    desired = desired_members(
        files,
        cache,
        hashes,
        sizes,
        _dist(filter_expr=""),
        parse_template("{year:2023}"),
    )
    assert set(desired) == {"2023/a.jpg"}


def test_desired_members_requires_hashes() -> None:
    a = LIB / "a.jpg"
    files, cache, hashes, sizes = _inputs({a: _meta(a)}, {a: None})
    with pytest.raises(MissingHashesError, match="pix hash"):
        desired_members(
            files, cache, hashes, sizes, _dist(), parse_template("{year}")
        )


def test_desired_members_suffixes_collisions_by_hash_order() -> None:
    # Two same-named files in different library folders flattened into one
    # export folder. Tiebreak is content hash, so the suffix doesn't churn
    # when organize moves the master around.
    a, b = LIB / "one" / "x.jpg", LIB / "two" / "x.jpg"
    metas = {a: _meta(a), b: _meta(b)}
    files, cache, hashes, sizes = _inputs(metas, {a: "hzz", b: "haa"})
    desired = desired_members(
        files, cache, hashes, sizes, _dist(), parse_template("{year}")
    )
    assert set(desired) == {"2023/x.jpg", "2023/x_001.jpg"}
    assert desired["2023/x.jpg"].content_hash == "haa"  # lowest hash first


# --- Target inspection -------------------------------------------------------


def test_scan_target_lists_files_relative(tmp_path: Path) -> None:
    (tmp_path / "2023" / "Hawaii").mkdir(parents=True)
    (tmp_path / "2023" / "Hawaii" / "a.jpg").write_bytes(b"x")
    assert set(scan_target(tmp_path)) == {"2023/Hawaii/a.jpg"}


def test_scan_target_skips_sync_client_artifacts(tmp_path: Path) -> None:
    (tmp_path / "@eaDir").mkdir()
    (tmp_path / "@eaDir" / "thumb.jpg").write_bytes(b"x")
    (tmp_path / "#recycle").mkdir()
    (tmp_path / "#recycle" / "old.jpg").write_bytes(b"x")
    (tmp_path / "desktop.ini").write_bytes(b"x")
    (tmp_path / "real.jpg").write_bytes(b"x")
    assert set(scan_target(tmp_path)) == {"real.jpg"}


def test_scan_target_missing_folder_is_empty(tmp_path: Path) -> None:
    assert scan_target(tmp_path / "nope") == {}


# --- Classification ----------------------------------------------------------


def _stat(tmp_path: Path, name: str, data: bytes = b"x") -> os.stat_result:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path.stat()


def test_classify_in_sync(tmp_path: Path) -> None:
    st = _stat(tmp_path, "a.jpg")
    members = {"a.jpg": Member("h", st.st_size, st.st_mtime_ns)}
    present, missing, drift = classify(members, {"a.jpg": st})
    assert present == {"a.jpg"}
    assert missing == set()
    assert not drift


def test_classify_missing_member(tmp_path: Path) -> None:
    st = _stat(tmp_path, "a.jpg")
    members = {"a.jpg": Member("h", st.st_size, st.st_mtime_ns)}
    present, missing, drift = classify(members, {})
    assert present == set()
    assert missing == {"a.jpg"}
    assert not drift  # gone is re-provisionable, not drift


def test_classify_modified_member_is_drift(tmp_path: Path) -> None:
    st = _stat(tmp_path, "a.jpg")
    members = {"a.jpg": Member("h", st.st_size + 5, st.st_mtime_ns)}
    _present, _missing, drift = classify(members, {"a.jpg": st})
    assert drift.modified == ["a.jpg"]


def test_classify_foreign_file_is_drift(tmp_path: Path) -> None:
    st = _stat(tmp_path, "someone-elses.jpg")
    _present, _missing, drift = classify({}, {"someone-elses.jpg": st})
    assert drift.foreign == ["someone-elses.jpg"]
    assert bool(drift) is True


# --- Adoption ----------------------------------------------------------------


def test_adopt_recovers_members_by_content_hash(tmp_path: Path) -> None:
    from pix.content_hash import compute_content_hash

    target = tmp_path / "delivery"
    target.mkdir()
    (target / "a.jpg").write_bytes(b"hello")
    real_hash = compute_content_hash(target / "a.jpg")

    actual = scan_target(target)
    desired = {"a.jpg": Source(path=LIB / "a.jpg", content_hash=real_hash, size=5)}
    manifest = adopt(target, actual, desired)
    assert set(manifest.members) == {"a.jpg"}


def test_adopt_leaves_mismatched_content_foreign(tmp_path: Path) -> None:
    target = tmp_path / "delivery"
    target.mkdir()
    (target / "a.jpg").write_bytes(b"different")
    actual = scan_target(target)
    desired = {
        "a.jpg": Source(path=LIB / "a.jpg", content_hash="nope", size=5)
    }
    assert adopt(target, actual, desired).members == {}


# --- Plan --------------------------------------------------------------------


def _plan(
    desired: dict[str, Source],
    members: dict[str, Member],
    present: set[str] | None = None,
    missing: set[str] | None = None,
):
    return build_plan(
        distribution="general",
        target=Path("D:/Delivery"),
        desired=desired,
        manifest_members=members,
        present=present if present is not None else set(members) - (missing or set()),
        missing=missing or set(),
        generated_at=datetime(2026, 9, 6, 12, 0),
    )


def _src(name: str = "a.jpg", h: str = "ha") -> Source:
    return Source(path=LIB / name, content_hash=h, size=100)


def test_plan_new_member_is_a_copy() -> None:
    plan = _plan({"2023/a.jpg": _src()}, {})
    assert [(ln.action, ln.rel_path) for ln in plan.lines] == [
        (ExportAction.COPY, "2023/a.jpg")
    ]
    assert plan.is_additive()


def test_plan_unchanged_member_is_untouched() -> None:
    plan = _plan({"2023/a.jpg": _src()}, {"2023/a.jpg": Member("ha", 1, 2)})
    assert plan.lines == []
    assert plan.in_sync == 1


def test_plan_changed_source_is_a_replace() -> None:
    plan = _plan(
        {"2023/a.jpg": _src(h="new")}, {"2023/a.jpg": Member("old", 1, 2)}
    )
    assert plan.lines[0].action is ExportAction.REPLACE
    assert not plan.is_additive()


def test_plan_dropped_member_is_a_remove() -> None:
    plan = _plan({}, {"2023/a.jpg": Member("ha", 1, 2)})
    assert plan.lines[0].action is ExportAction.REMOVE
    assert plan.lines[0].source_path is None
    assert not plan.is_additive()


def test_plan_missing_member_is_re_copied() -> None:
    plan = _plan(
        {"2023/a.jpg": _src()},
        {"2023/a.jpg": Member("ha", 1, 2)},
        present=set(),
        missing={"2023/a.jpg"},
    )
    assert plan.lines[0].action is ExportAction.COPY
    assert "missing from target" in plan.lines[0].details
    assert plan.is_additive()  # re-provisioning is additive


def test_plan_path_churn_becomes_one_move() -> None:
    # An event rename moves a member; must read as MOVE, not remove+add.
    plan = _plan({"2023/Maui/a.jpg": _src()}, {"2023/Hawaii/a.jpg": Member("ha", 1, 2)})
    assert len(plan.lines) == 1
    line = plan.lines[0]
    assert line.action is ExportAction.MOVE
    assert line.rel_path == "2023/Maui/a.jpg"
    assert line.from_rel_path == "2023/Hawaii/a.jpg"


def test_plan_ambiguous_hash_is_not_paired_into_a_move() -> None:
    # Two removals share a hash with one copy — pairing would be a guess.
    plan = _plan(
        {"2023/c.jpg": _src(h="dup")},
        {
            "2023/a.jpg": Member("dup", 1, 2),
            "2023/b.jpg": Member("dup", 1, 2),
        },
    )
    actions = sorted(ln.action.value for ln in plan.lines)
    assert actions == ["COPY", "REMOVE", "REMOVE"]


def test_plan_sorts_removals_first() -> None:
    plan = _plan(
        {"2023/new.jpg": _src("new.jpg", h="hnew")},
        {"2023/old.jpg": Member("hold", 1, 2)},
    )
    assert [ln.action for ln in plan.lines] == [
        ExportAction.REMOVE,
        ExportAction.COPY,
    ]
    assert [ln.line_id for ln in plan.lines] == ["L001", "L002"]


def test_plan_text_is_editor_parseable() -> None:
    from pix.editor import parse_kept_line_ids

    plan = _plan({"2023/a.jpg": _src()}, {"2023/b.jpg": Member("hb", 1, 2)})
    text = plan.to_text()
    assert parse_kept_line_ids(text) == {"L001", "L002"}
    assert "# Export plan: general" in text
    assert "library is never touched" in text


def test_plan_bytes_to_write_counts_copies_only() -> None:
    plan = _plan({"2023/a.jpg": _src()}, {"2023/b.jpg": Member("hb", 1, 2)})
    assert plan.bytes_to_write() == 100


# --- Extension allowlist -----------------------------------------------------


def _dist_ext(exts: set[str]) -> Distribution:
    return Distribution(
        name="general",
        path="D:/Delivery",
        template="{year}",
        filter=parse_filter(""),
        extensions=frozenset(exts),
    )


def test_desired_members_ships_only_allowed_extensions() -> None:
    photo, video, insv = LIB / "a.jpg", LIB / "b.mp4", LIB / "c.insv"
    metas = {p: _meta(p) for p in (photo, video, insv)}
    files, cache, hashes, sizes = _inputs(
        metas, {photo: "hp", video: "hv", insv: "hi"}
    )

    both = desired_members(
        files, cache, hashes, sizes, _dist_ext({"jpg", "mp4"}),
        parse_template("{year}"),
    )
    assert set(both) == {"2023/a.jpg", "2023/b.mp4"}

    photos_only = desired_members(
        files, cache, hashes, sizes, _dist_ext({"jpg"}),
        parse_template("{year}"),
    )
    assert set(photos_only) == {"2023/a.jpg"}


def test_extension_gate_is_case_insensitive() -> None:
    upper = LIB / "A.JPG"
    files, cache, hashes, sizes = _inputs({upper: _meta(upper)}, {upper: "h"})
    desired = desired_members(
        files, cache, hashes, sizes, _dist_ext({"jpg"}), parse_template("{year}")
    )
    assert set(desired) == {"2023/A.JPG"}


def test_excluded_extension_never_needs_a_hash() -> None:
    # The extension gate runs first, so an unhashed .insv can't block an
    # export that wasn't going to ship it anyway.
    insv = LIB / "c.insv"
    files, cache, hashes, sizes = _inputs({insv: _meta(insv)}, {insv: None})
    assert desired_members(
        files, cache, hashes, sizes, _dist_ext({"jpg"}), parse_template("{year}")
    ) == {}
