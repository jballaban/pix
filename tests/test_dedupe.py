"""Tests for `pix.dedupe` — grouping, keeper selection, plan, apply."""

from __future__ import annotations

from pathlib import Path

import pytest

from pix.dedupe import (
    DedupeApplyError,
    MissingHashesError,
    UnmigratedFilesError,
    apply_plan,
    generate_plan,
    group_by_hash,
    select_keeper,
    serialize_plan,
)
from pix.metadata import FileMetadata
from pix.plan import (
    PIX_CONTENT_HASH,
    PIX_DATE_OVERRIDE,
    PIX_EVENT_OVERRIDE,
    PIX_ORIGINAL_PATH,
    Action,
)


def _meta(path: Path, **fields: object) -> FileMetadata:
    return FileMetadata(
        path=path, raw={"SourceFile": str(path), **fields}
    )


def _migrated(path: Path, content_hash: str, **extra: object) -> FileMetadata:
    """A file that's been migrated and has a content hash."""
    return _meta(
        path,
        **{
            PIX_ORIGINAL_PATH: f"F:/source/{path.name}",
            PIX_CONTENT_HASH: content_hash,
            **extra,
        },
    )


# --- Prerequisites ----------------------------------------------------------


def test_refuses_unmigrated_files(tmp_path: Path) -> None:
    """Files without pix:OriginalPath cause refusal."""
    root = tmp_path / "lib"
    root.mkdir()
    p = root / "a.jpg"
    p.write_bytes(b"")
    cache = {p.resolve(): _meta(p)}  # no OriginalPath
    with pytest.raises(UnmigratedFilesError):
        generate_plan(
            library_root=root,
            cache=cache,
            run_id="r",
            run_dir=tmp_path / "runs",
        )


def test_refuses_missing_content_hash(tmp_path: Path) -> None:
    """Migrated files without pix:ContentHash cause refusal."""
    root = tmp_path / "lib"
    root.mkdir()
    p = root / "a.jpg"
    p.write_bytes(b"")
    cache = {
        p.resolve(): _meta(
            p, **{PIX_ORIGINAL_PATH: "F:/source/a.jpg"}
        )  # no ContentHash
    }
    with pytest.raises(MissingHashesError):
        generate_plan(
            library_root=root,
            cache=cache,
            run_id="r",
            run_dir=tmp_path / "runs",
        )


# --- Grouping ---------------------------------------------------------------


def test_group_by_hash_yields_only_groups_of_two_or_more(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"
    b = root / "b.jpg"
    c = root / "c.jpg"  # unique
    for p in (a, b, c):
        p.write_bytes(b"")
    cache = {
        a.resolve(): _migrated(a, "abc"),
        b.resolve(): _migrated(b, "abc"),
        c.resolve(): _migrated(c, "def"),
    }
    groups = group_by_hash(root, cache)
    assert len(groups) == 1
    assert groups[0].content_hash == "abc"
    assert {groups[0].keeper, *groups[0].losers} == {
        a.resolve(),
        b.resolve(),
    }


# --- Keeper selection -------------------------------------------------------


def test_keeper_lex_smallest_when_all_pristine(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    paths = [root / "z.jpg", root / "a.jpg", root / "m.jpg"]
    for p in paths:
        p.write_bytes(b"")
    members = [
        (paths[0].resolve(), _migrated(paths[0], "h")),
        (paths[1].resolve(), _migrated(paths[1], "h")),
        (paths[2].resolve(), _migrated(paths[2], "h")),
    ]
    assert select_keeper(root, members) == paths[1].resolve()  # a.jpg


def test_keeper_invested_beats_pristine(tmp_path: Path) -> None:
    """Even when a pristine file has lex-smaller path, an invested file wins."""
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"  # pristine, lex-smallest
    z = root / "z.jpg"  # invested
    for p in (a, z):
        p.write_bytes(b"")
    members = [
        (a.resolve(), _migrated(a, "h")),
        (
            z.resolve(),
            _migrated(z, "h", **{PIX_DATE_OVERRIDE: "2022-*-*-*:*:*"}),
        ),
    ]
    assert select_keeper(root, members) == z.resolve()


def test_keeper_event_override_counts_as_invested(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"
    z = root / "z.jpg"
    for p in (a, z):
        p.write_bytes(b"")
    members = [
        (a.resolve(), _migrated(a, "h")),
        (
            z.resolve(),
            _migrated(z, "h", **{PIX_EVENT_OVERRIDE: "Birthday"}),
        ),
    ]
    assert select_keeper(root, members) == z.resolve()


def test_keeper_all_wildcards_override_is_not_invested(tmp_path: Path) -> None:
    """An override that's all `*` is equivalent to absent."""
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"
    z = root / "z.jpg"
    for p in (a, z):
        p.write_bytes(b"")
    members = [
        (a.resolve(), _migrated(a, "h")),
        (
            z.resolve(),
            _migrated(z, "h", **{PIX_DATE_OVERRIDE: "*-*-*-*:*:*"}),
        ),
    ]
    # Neither is invested; pristine lex-smallest wins.
    assert select_keeper(root, members) == a.resolve()


def test_keeper_lex_smallest_among_invested(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"  # invested
    z = root / "z.jpg"  # invested
    for p in (a, z):
        p.write_bytes(b"")
    members = [
        (
            a.resolve(),
            _migrated(a, "h", **{PIX_DATE_OVERRIDE: "2022-*-*-*:*:*"}),
        ),
        (
            z.resolve(),
            _migrated(z, "h", **{PIX_DATE_OVERRIDE: "2022-*-*-*:*:*"}),
        ),
    ]
    assert select_keeper(root, members) == a.resolve()


def test_keeper_case_insensitive_lex(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    # Lowercase letter sorts after capital in ASCII; case-insensitive sort
    # should put 'A.jpg' and 'a.jpg' adjacent (essentially equivalent).
    upper = root / "A.jpg"
    z = root / "z.jpg"
    upper.write_bytes(b"")
    z.write_bytes(b"")
    members = [
        (upper.resolve(), _migrated(upper, "h")),
        (z.resolve(), _migrated(z, "h")),
    ]
    assert select_keeper(root, members) == upper.resolve()


# --- Plan generation --------------------------------------------------------


def test_plan_no_duplicates_produces_empty_plan(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    p = root / "a.jpg"
    p.write_bytes(b"")
    cache = {p.resolve(): _migrated(p, "h")}
    result = generate_plan(
        library_root=root,
        cache=cache,
        run_id="r",
        run_dir=tmp_path / "runs",
    )
    assert result.plan.lines == []
    assert result.groups == ()


def test_plan_two_duplicates_produces_one_dedup_line(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"  # keeper (lex)
    b = root / "b.jpg"  # loser
    a.write_bytes(b"")
    b.write_bytes(b"")
    cache = {
        a.resolve(): _migrated(a, "h"),
        b.resolve(): _migrated(b, "h"),
    }
    run_dir = tmp_path / "runs" / "r"
    result = generate_plan(
        library_root=root,
        cache=cache,
        run_id="r",
        run_dir=run_dir,
    )
    assert len(result.plan.lines) == 1
    line = result.plan.lines[0]
    assert line.action == Action.DEDUP
    assert line.abs_path == b.resolve()
    assert line.capture_path == run_dir / "data" / "L001_b.jpg"
    assert len(result.groups) == 1
    assert result.groups[0].keeper == a.resolve()
    assert result.groups[0].losers == (b.resolve(),)


def test_plan_three_groups_three_lines_each_id_unique(tmp_path: Path) -> None:
    """Three duplicate pairs → three DEDUP lines with L001/L002/L003."""
    root = tmp_path / "lib"
    root.mkdir()
    paths: list[Path] = []
    for i in range(6):
        p = root / f"f{i}.jpg"
        p.write_bytes(b"")
        paths.append(p)
    cache: dict[Path, FileMetadata] = {}
    # Three groups: (0,1), (2,3), (4,5) sharing distinct hashes.
    for idx, h in enumerate(["aaa", "bbb", "ccc"]):
        for p in (paths[idx * 2], paths[idx * 2 + 1]):
            cache[p.resolve()] = _migrated(p, h)
    result = generate_plan(
        library_root=root,
        cache=cache,
        run_id="r",
        run_dir=tmp_path / "runs",
    )
    assert [ln.line_id for ln in result.plan.lines] == [
        "L001",
        "L002",
        "L003",
    ]
    assert len(result.groups) == 3


# --- Serialization ----------------------------------------------------------


def test_serialize_plan_grouped_format(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"
    b = root / "b.jpg"
    a.write_bytes(b"")
    b.write_bytes(b"")
    cache = {
        a.resolve(): _migrated(a, "abc123def456ghi"),
        b.resolve(): _migrated(b, "abc123def456ghi"),
    }
    result = generate_plan(
        library_root=root,
        cache=cache,
        run_id="2026-05-21_15-00-00",
        run_dir=tmp_path / "runs",
    )
    text = serialize_plan(source=root, result=result, library_root=root)
    assert "# Dedupe plan:" in text
    assert "# Group 1 — hash abc123def456…, 2 files" in text
    assert "# Keeper: a.jpg" in text
    assert "L001 | DEDUP" in text
    assert "hash abc123def456…" in text
    assert "# Summary: 1 DEDUP across 1 group" in text


# --- Apply ------------------------------------------------------------------


def test_apply_moves_loser_to_data_dir(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"  # keeper
    b = root / "b.jpg"  # loser
    a.write_bytes(b"keep")
    b.write_bytes(b"dup")
    cache = {
        a.resolve(): _migrated(a, "h"),
        b.resolve(): _migrated(b, "h"),
    }
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    result = generate_plan(
        library_root=root,
        cache=cache,
        run_id="r",
        run_dir=run_dir,
    )
    completed = apply_plan(
        plan=result.plan,
        kept_line_ids={ln.line_id for ln in result.plan.lines},
        run_dir=run_dir,
        library_root=root,
    )
    assert completed == 1
    # Keeper survived; loser moved.
    assert a.exists() and a.read_bytes() == b"keep"
    assert not b.exists()
    captured = run_dir / "data" / "L001_b.jpg"
    assert captured.exists()
    assert captured.read_bytes() == b"dup"


def test_apply_refuses_when_capture_path_collides(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"
    b = root / "b.jpg"
    a.write_bytes(b"")
    b.write_bytes(b"")
    cache = {
        a.resolve(): _migrated(a, "h"),
        b.resolve(): _migrated(b, "h"),
    }
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    result = generate_plan(
        library_root=root,
        cache=cache,
        run_id="r",
        run_dir=run_dir,
    )
    # Pre-create the capture path so apply collides.
    (run_dir / "data").mkdir(parents=True)
    (run_dir / "data" / "L001_b.jpg").write_bytes(b"squatter")
    with pytest.raises(DedupeApplyError):
        apply_plan(
            plan=result.plan,
            kept_line_ids={ln.line_id for ln in result.plan.lines},
            run_dir=run_dir,
            library_root=root,
        )


def test_apply_sweeps_empty_folders(tmp_path: Path) -> None:
    """When a duplicate is removed and its folder becomes empty, sweep it."""
    root = tmp_path / "lib"
    (root / "imports").mkdir(parents=True)
    a = root / "a.jpg"  # keeper at root
    b = root / "imports" / "b.jpg"  # loser; its parent will go empty
    a.write_bytes(b"")
    b.write_bytes(b"")
    cache = {
        a.resolve(): _migrated(a, "h"),
        b.resolve(): _migrated(b, "h"),
    }
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    result = generate_plan(
        library_root=root,
        cache=cache,
        run_id="r",
        run_dir=run_dir,
    )
    apply_plan(
        plan=result.plan,
        kept_line_ids={ln.line_id for ln in result.plan.lines},
        run_dir=run_dir,
        library_root=root,
    )
    assert not (root / "imports").exists()
