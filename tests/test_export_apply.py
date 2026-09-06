"""Applying an export plan against a real delivery tree."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pix.export import (
    ExportAction,
    ExportLine,
    ExportPlan,
    apply_plan,
    scan_target,
)
from pix.export_manifest import Member
from pix.markers import EXPORT_TMP_SUFFIX


def _plan(target: Path, *lines: ExportLine) -> ExportPlan:
    return ExportPlan(
        distribution="general",
        target=target,
        generated_at=datetime(2026, 9, 6, 12, 0),
        lines=list(lines),
        in_sync=0,
    )


def _copy_line(source: Path, rel: str, line_id: str = "L001") -> ExportLine:
    return ExportLine(
        line_id=line_id,
        action=ExportAction.COPY,
        rel_path=rel,
        details="",
        source_path=source,
        content_hash="hash-a",
        size=source.stat().st_size if source.exists() else 0,
    )


def _all(plan: ExportPlan) -> set[str]:
    return {ln.line_id for ln in plan.lines}


def _run(plan: ExportPlan, members: dict[str, Member] | None = None):
    logged: list[str] = []
    result = apply_plan(
        plan=plan,
        kept_line_ids=_all(plan),
        members=members if members is not None else {},
        log=logged.append,
    )
    return result, logged


def test_copy_creates_folders_and_records_the_member(tmp_path: Path) -> None:
    src = tmp_path / "lib" / "a.jpg"
    src.parent.mkdir()
    src.write_bytes(b"photo")
    target = tmp_path / "delivery"

    plan = _plan(target, _copy_line(src, "2023/Hawaii/a.jpg"))
    result, _log = _run(plan)

    dest = target / "2023" / "Hawaii" / "a.jpg"
    assert dest.read_bytes() == b"photo"
    assert result.completed == 1
    member = result.members["2023/Hawaii/a.jpg"]
    assert member.source_hash == "hash-a"
    assert member.matches(dest.stat())


def test_copy_leaves_no_temp_behind(tmp_path: Path) -> None:
    src = tmp_path / "a.jpg"
    src.write_bytes(b"photo")
    target = tmp_path / "delivery"
    _run(_plan(target, _copy_line(src, "a.jpg")))
    assert list(target.rglob(f"*{EXPORT_TMP_SUFFIX}")) == []


def test_replace_overwrites_and_updates_the_member(tmp_path: Path) -> None:
    src = tmp_path / "a.jpg"
    src.write_bytes(b"new content")
    target = tmp_path / "delivery"
    (target).mkdir()
    (target / "a.jpg").write_bytes(b"old")

    line = ExportLine(
        line_id="L001",
        action=ExportAction.REPLACE,
        rel_path="a.jpg",
        details="",
        source_path=src,
        content_hash="hash-new",
        size=11,
    )
    members = {"a.jpg": Member("hash-old", 3, 1)}
    result, _log = _run(_plan(target, line), members)

    assert (target / "a.jpg").read_bytes() == b"new content"
    assert result.members["a.jpg"].source_hash == "hash-new"


def test_remove_deletes_and_prunes_empty_folders(tmp_path: Path) -> None:
    target = tmp_path / "delivery"
    (target / "2023" / "Hawaii").mkdir(parents=True)
    (target / "2023" / "Hawaii" / "a.jpg").write_bytes(b"x")

    line = ExportLine(
        line_id="L001",
        action=ExportAction.REMOVE,
        rel_path="2023/Hawaii/a.jpg",
        details="",
    )
    members = {"2023/Hawaii/a.jpg": Member("h", 1, 1)}
    result, _log = _run(_plan(target, line), members)

    assert not (target / "2023").exists()
    assert result.members == {}
    assert result.pruned_folders == 2
    assert target.exists()  # never prunes the target root itself


def test_remove_of_an_already_gone_file_succeeds(tmp_path: Path) -> None:
    target = tmp_path / "delivery"
    target.mkdir()
    line = ExportLine(
        line_id="L001", action=ExportAction.REMOVE, rel_path="gone.jpg", details=""
    )
    result, _log = _run(_plan(target, line), {"gone.jpg": Member("h", 1, 1)})
    assert result.completed == 1
    assert result.failed == 0


def test_move_relocates_within_the_target(tmp_path: Path) -> None:
    src = tmp_path / "lib" / "a.jpg"
    src.parent.mkdir()
    src.write_bytes(b"photo")
    target = tmp_path / "delivery"
    (target / "2023" / "Hawaii").mkdir(parents=True)
    (target / "2023" / "Hawaii" / "a.jpg").write_bytes(b"photo")

    line = ExportLine(
        line_id="L001",
        action=ExportAction.MOVE,
        rel_path="2023/Maui/a.jpg",
        details="",
        source_path=src,
        content_hash="hash-a",
        from_rel_path="2023/Hawaii/a.jpg",
        size=5,
    )
    members = {"2023/Hawaii/a.jpg": Member("hash-a", 5, 1)}
    result, _log = _run(_plan(target, line), members)

    assert (target / "2023" / "Maui" / "a.jpg").read_bytes() == b"photo"
    assert not (target / "2023" / "Hawaii").exists()
    assert set(result.members) == {"2023/Maui/a.jpg"}


def test_move_falls_back_to_copy_when_the_origin_vanished(
    tmp_path: Path,
) -> None:
    src = tmp_path / "lib" / "a.jpg"
    src.parent.mkdir()
    src.write_bytes(b"photo")
    target = tmp_path / "delivery"
    target.mkdir()

    line = ExportLine(
        line_id="L001",
        action=ExportAction.MOVE,
        rel_path="2023/Maui/a.jpg",
        details="",
        source_path=src,
        content_hash="hash-a",
        from_rel_path="2023/Hawaii/a.jpg",
        size=5,
    )
    result, _log = _run(
        _plan(target, line), {"2023/Hawaii/a.jpg": Member("hash-a", 5, 1)}
    )
    assert (target / "2023" / "Maui" / "a.jpg").read_bytes() == b"photo"
    assert result.failed == 0


def test_vetoed_lines_are_skipped(tmp_path: Path) -> None:
    src = tmp_path / "a.jpg"
    src.write_bytes(b"photo")
    target = tmp_path / "delivery"
    plan = _plan(
        target,
        _copy_line(src, "keep.jpg", "L001"),
        _copy_line(src, "vetoed.jpg", "L002"),
    )
    result = apply_plan(
        plan=plan, kept_line_ids={"L001"}, members={}, log=lambda _m: None
    )
    assert (target / "keep.jpg").exists()
    assert not (target / "vetoed.jpg").exists()
    assert result.completed == 1


def test_vetoed_removal_keeps_ownership(tmp_path: Path) -> None:
    # Striking a REMOVE line must leave the member in the manifest —
    # otherwise the file we deliberately kept becomes foreign next run.
    target = tmp_path / "delivery"
    target.mkdir()
    (target / "a.jpg").write_bytes(b"x")
    line = ExportLine(
        line_id="L001", action=ExportAction.REMOVE, rel_path="a.jpg", details=""
    )
    members = {"a.jpg": Member("h", 1, 1)}
    apply_plan(
        plan=_plan(target, line),
        kept_line_ids=set(),
        members=members,
        log=lambda _m: None,
    )
    assert (target / "a.jpg").exists()
    assert set(members) == {"a.jpg"}


def test_a_failed_line_is_logged_and_the_run_continues(
    tmp_path: Path
) -> None:
    good = tmp_path / "good.jpg"
    good.write_bytes(b"photo")
    missing = tmp_path / "not-there.jpg"
    target = tmp_path / "delivery"

    plan = _plan(
        target,
        _copy_line(missing, "bad.jpg", "L001"),
        _copy_line(good, "good.jpg", "L002"),
    )
    result, logged = _run(plan)

    assert result.failed == 1
    assert result.completed == 1
    assert (target / "good.jpg").exists()
    assert any("FAILED" in line for line in logged)
    assert "bad.jpg" not in result.members


def test_progress_is_reported_per_line(tmp_path: Path) -> None:
    src = tmp_path / "a.jpg"
    src.write_bytes(b"photo")
    ticks: list[int] = []
    plan = _plan(
        tmp_path / "delivery",
        _copy_line(src, "a.jpg", "L001"),
        _copy_line(src, "b.jpg", "L002"),
    )
    apply_plan(
        plan=plan,
        kept_line_ids=_all(plan),
        members={},
        log=lambda _m: None,
        on_progress=ticks.append,
    )
    assert ticks == [1, 1]


def test_applied_target_is_clean_for_the_next_scan(tmp_path: Path) -> None:
    # Round trip: what we wrote must classify as ours next run.
    src = tmp_path / "a.jpg"
    src.write_bytes(b"photo")
    target = tmp_path / "delivery"
    result, _log = _run(_plan(target, _copy_line(src, "2023/a.jpg")))

    from pix.export import classify

    present, missing, drift = classify(result.members, scan_target(target))
    assert present == {"2023/a.jpg"}
    assert not missing
    assert not drift
