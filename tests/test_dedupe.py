"""Tests for `pix.dedupe` — grouping, keeper selection, plan, apply."""

from __future__ import annotations

from pathlib import Path

import pytest

import pix.dedupe as dedupe_mod
from pix.dates import PIX_MERGE_DATE
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
from pix.events import PIX_EVENT_AUTO, PIX_MERGE_EVENT
from pix.metadata import FileMetadata
from pix.plan import (
    PIX_DATE_AUTO,
    PIX_DATE_OVERRIDE,
    PIX_ORIGINAL_PATH,
    Action,
)


def _meta(path: Path, **fields: object) -> FileMetadata:
    return FileMetadata(
        path=path, raw={"SourceFile": str(path), **fields}
    )


def _migrated(path: Path, **extra: object) -> FileMetadata:
    """A file that's been migrated. Hashes are registered separately via
    the `patched_hash_cache` fixture — dedupe reads them from there."""
    return _meta(
        path,
        **{PIX_ORIGINAL_PATH: f"F:/source/{path.name}", **extra},
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
            hashes={},
            run_id="r",
            run_dir=tmp_path / "runs",
        )


def test_refuses_missing_content_hash(tmp_path: Path) -> None:
    """Migrated files without a cached content hash cause refusal."""
    root = tmp_path / "lib"
    root.mkdir()
    p = root / "a.jpg"
    p.write_bytes(b"")
    cache = {p.resolve(): _migrated(p)}
    # No hash registered → MissingHashesError.
    with pytest.raises(MissingHashesError):
        generate_plan(
            library_root=root,
            cache=cache,
            hashes={},
            run_id="r",
            run_dir=tmp_path / "runs",
        )


# --- Grouping ---------------------------------------------------------------


def test_group_by_hash_yields_only_groups_of_two_or_more(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"
    b = root / "b.jpg"
    c = root / "c.jpg"  # unique
    for p in (a, b, c):
        p.write_bytes(b"")
    patched_hash_cache[a.resolve()] = "abc"
    patched_hash_cache[b.resolve()] = "abc"
    patched_hash_cache[c.resolve()] = "def"
    cache = {
        a.resolve(): _migrated(a),
        b.resolve(): _migrated(b),
        c.resolve(): _migrated(c),
    }
    groups = group_by_hash(root, cache, patched_hash_cache)
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
        (paths[0].resolve(), _migrated(paths[0])),
        (paths[1].resolve(), _migrated(paths[1])),
        (paths[2].resolve(), _migrated(paths[2])),
    ]
    assert select_keeper(root, members) == paths[1].resolve()  # a.jpg


def test_keeper_lex_smallest_ignores_investment(tmp_path: Path) -> None:
    """Keeper is pure lex-smallest now — investment no longer wins it, since
    the tag merge consolidates the override onto whoever survives."""
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"  # pristine, lex-smallest → keeper
    z = root / "z.jpg"  # has an override, but no longer beats lex
    for p in (a, z):
        p.write_bytes(b"")
    members = [
        (a.resolve(), _migrated(a)),
        (
            z.resolve(),
            _migrated(z, **{PIX_DATE_OVERRIDE: "2022-*-*-*:*:*"}),
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
        (upper.resolve(), _migrated(upper)),
        (z.resolve(), _migrated(z)),
    ]
    assert select_keeper(root, members) == upper.resolve()


# --- Plan generation --------------------------------------------------------


def test_plan_no_duplicates_produces_empty_plan(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    p = root / "a.jpg"
    p.write_bytes(b"")
    patched_hash_cache[p.resolve()] = "h"
    cache = {p.resolve(): _migrated(p)}
    result = generate_plan(
        library_root=root,
        cache=cache,
        hashes=patched_hash_cache,
        run_id="r",
        run_dir=tmp_path / "runs",
    )
    assert result.plan.lines == []
    assert result.groups == ()


def test_plan_two_duplicates_produces_one_dedup_line(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"  # keeper (lex)
    b = root / "b.jpg"  # loser
    a.write_bytes(b"")
    b.write_bytes(b"")
    patched_hash_cache[a.resolve()] = "h"
    patched_hash_cache[b.resolve()] = "h"
    cache = {
        a.resolve(): _migrated(a),
        b.resolve(): _migrated(b),
    }
    run_dir = tmp_path / "runs" / "r"
    result = generate_plan(
        library_root=root,
        cache=cache,
        hashes=patched_hash_cache,
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


def test_plan_three_groups_three_lines_each_id_unique(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
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
            cache[p.resolve()] = _migrated(p)
            patched_hash_cache[p.resolve()] = h
    result = generate_plan(
        library_root=root,
        cache=cache,
        hashes=patched_hash_cache,
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


def test_serialize_plan_grouped_format(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"
    b = root / "b.jpg"
    a.write_bytes(b"")
    b.write_bytes(b"")
    patched_hash_cache[a.resolve()] = "abc123def456ghi"
    patched_hash_cache[b.resolve()] = "abc123def456ghi"
    cache = {
        a.resolve(): _migrated(a),
        b.resolve(): _migrated(b),
    }
    result = generate_plan(
        library_root=root,
        cache=cache,
        hashes=patched_hash_cache,
        run_id="2026-05-21_15-00-00",
        run_dir=tmp_path / "runs",
    )
    text = serialize_plan(source=root, result=result, library_root=root)
    assert "# Dedupe plan:" in text
    assert "# Group 1 — hash abc123def456…, 2 files" in text
    assert "# Keeper: a.jpg" in text
    assert "L001 | DEDUP" in text
    assert "hash abc123def456…" in text
    assert "# Summary: 1 DEDUP, 0 MERGE across 1 group" in text


# --- Apply ------------------------------------------------------------------


def test_apply_moves_loser_to_data_dir(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"  # keeper
    b = root / "b.jpg"  # loser
    a.write_bytes(b"keep")
    b.write_bytes(b"dup")
    patched_hash_cache[a.resolve()] = "h"
    patched_hash_cache[b.resolve()] = "h"
    cache = {
        a.resolve(): _migrated(a),
        b.resolve(): _migrated(b),
    }
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    result = generate_plan(
        library_root=root,
        cache=cache,
        hashes=patched_hash_cache,
        run_id="r",
        run_dir=run_dir,
    )
    removed, merged, quarantined = apply_plan(
        plan=result.plan,
        kept_line_ids={ln.line_id for ln in result.plan.lines},
        run_dir=run_dir,
        library_root=root,
    )
    assert (removed, merged, quarantined) == (1, 0, [])
    # Keeper survived; loser moved.
    assert a.exists() and a.read_bytes() == b"keep"
    assert not b.exists()
    captured = run_dir / "data" / "L001_b.jpg"
    assert captured.exists()
    assert captured.read_bytes() == b"dup"


def test_apply_refuses_when_capture_path_collides(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"
    b = root / "b.jpg"
    a.write_bytes(b"")
    b.write_bytes(b"")
    patched_hash_cache[a.resolve()] = "h"
    patched_hash_cache[b.resolve()] = "h"
    cache = {
        a.resolve(): _migrated(a),
        b.resolve(): _migrated(b),
    }
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    result = generate_plan(
        library_root=root,
        cache=cache,
        hashes=patched_hash_cache,
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


def test_apply_sweeps_empty_folders(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    """When a duplicate is removed and its folder becomes empty, sweep it."""
    root = tmp_path / "lib"
    (root / "imports").mkdir(parents=True)
    a = root / "a.jpg"  # keeper at root
    b = root / "imports" / "b.jpg"  # loser; its parent will go empty
    a.write_bytes(b"")
    b.write_bytes(b"")
    patched_hash_cache[a.resolve()] = "h"
    patched_hash_cache[b.resolve()] = "h"
    cache = {
        a.resolve(): _migrated(a),
        b.resolve(): _migrated(b),
    }
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    result = generate_plan(
        library_root=root,
        cache=cache,
        hashes=patched_hash_cache,
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


# --- Tag merge --------------------------------------------------------------


def _exif(path: Path, dto: str) -> FileMetadata:
    """Migrated file carrying an EXIF:DateTimeOriginal (exiftool format)."""
    return _migrated(path, **{"EXIF:DateTimeOriginal": dto})


def _with_original(path: Path, original: str, **extra: object) -> FileMetadata:
    """Migrated file with an explicit pix:OriginalPath (controls event/date
    folder derivation)."""
    return _meta(path, **{PIX_ORIGINAL_PATH: original, **extra})


def test_merge_date_takes_earliest_across_group(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"  # keeper (lex), later capture date
    b = root / "b.jpg"  # loser, earlier capture date
    for p in (a, b):
        p.write_bytes(b"")
    patched_hash_cache[a.resolve()] = "h"
    patched_hash_cache[b.resolve()] = "h"
    cache = {
        a.resolve(): _exif(a, "2023:08:16 10:00:00"),
        b.resolve(): _exif(b, "2023:08:15 09:00:00"),
    }
    groups = group_by_hash(root, cache, patched_hash_cache)
    assert len(groups) == 1
    g = groups[0]
    assert g.keeper == a.resolve()
    assert g.keeper_writes[PIX_MERGE_DATE] == "2023-08-15-09:00:00"
    assert g.keeper_writes[PIX_DATE_AUTO] == "2023-08-15-09:00:00"


def test_merge_date_skipped_when_keeper_already_earliest(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"  # keeper (lex), earlier date already
    b = root / "b.jpg"  # loser, later date
    for p in (a, b):
        p.write_bytes(b"")
    patched_hash_cache[a.resolve()] = "h"
    patched_hash_cache[b.resolve()] = "h"
    cache = {
        a.resolve(): _exif(a, "2023:08:15 09:00:00"),
        b.resolve(): _exif(b, "2023:08:16 10:00:00"),
    }
    g = group_by_hash(root, cache, patched_hash_cache)[0]
    assert PIX_MERGE_DATE not in g.keeper_writes


def test_merge_event_fills_empty_keeper(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"  # keeper; date-only folder → no event auto
    b = root / "b.jpg"  # loser; folder yields an event
    for p in (a, b):
        p.write_bytes(b"")
    patched_hash_cache[a.resolve()] = "h"
    patched_hash_cache[b.resolve()] = "h"
    cache = {
        a.resolve(): _with_original(a, "F:/2023/a.jpg"),
        b.resolve(): _with_original(b, "F:/Hawaii/b.jpg"),
    }
    g = group_by_hash(root, cache, patched_hash_cache)[0]
    assert g.keeper == a.resolve()
    assert g.keeper_writes[PIX_MERGE_EVENT] == "Hawaii"
    assert g.keeper_writes[PIX_EVENT_AUTO] == "Hawaii"


def test_merge_event_not_overwritten_when_keeper_has_one(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"  # keeper; folder yields "Birthday"
    b = root / "b.jpg"  # loser; folder yields "Hawaii"
    for p in (a, b):
        p.write_bytes(b"")
    patched_hash_cache[a.resolve()] = "h"
    patched_hash_cache[b.resolve()] = "h"
    cache = {
        a.resolve(): _with_original(a, "F:/Birthday/a.jpg"),
        b.resolve(): _with_original(b, "F:/Hawaii/b.jpg"),
    }
    g = group_by_hash(root, cache, patched_hash_cache)[0]
    assert PIX_MERGE_EVENT not in g.keeper_writes


def test_merge_override_fills_empty_keeper(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"  # keeper, no override
    b = root / "b.jpg"  # loser with a user override
    for p in (a, b):
        p.write_bytes(b"")
    patched_hash_cache[a.resolve()] = "h"
    patched_hash_cache[b.resolve()] = "h"
    cache = {
        a.resolve(): _migrated(a),
        b.resolve(): _migrated(b, **{PIX_DATE_OVERRIDE: "2020-*-*-*:*:*"}),
    }
    g = group_by_hash(root, cache, patched_hash_cache)[0]
    assert g.keeper_writes[PIX_DATE_OVERRIDE] == "2020-*-*-*:*:*"


def test_merge_override_keeps_keepers_own(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    """Fill-empty never clobbers the keeper's own override."""
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"  # keeper with its own override
    b = root / "b.jpg"  # loser with a different override
    for p in (a, b):
        p.write_bytes(b"")
    patched_hash_cache[a.resolve()] = "h"
    patched_hash_cache[b.resolve()] = "h"
    cache = {
        a.resolve(): _migrated(a, **{PIX_DATE_OVERRIDE: "2021-*-*-*:*:*"}),
        b.resolve(): _migrated(b, **{PIX_DATE_OVERRIDE: "2020-*-*-*:*:*"}),
    }
    g = group_by_hash(root, cache, patched_hash_cache)[0]
    assert PIX_DATE_OVERRIDE not in g.keeper_writes


def test_merge_override_divergence_warns_and_takes_lex_smallest(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"  # keeper, no override
    b = root / "b.jpg"  # loser override (lex-smaller contributor)
    c = root / "c.jpg"  # loser override (different value)
    for p in (a, b, c):
        p.write_bytes(b"")
    for p in (a, b, c):
        patched_hash_cache[p.resolve()] = "h"
    cache = {
        a.resolve(): _migrated(a),
        b.resolve(): _migrated(b, **{PIX_DATE_OVERRIDE: "2019-*-*-*:*:*"}),
        c.resolve(): _migrated(c, **{PIX_DATE_OVERRIDE: "2020-*-*-*:*:*"}),
    }
    g = group_by_hash(root, cache, patched_hash_cache)[0]
    assert g.keeper_writes[PIX_DATE_OVERRIDE] == "2019-*-*-*:*:*"
    assert any("date_override" in w for w in g.merge_warnings)


def test_serialize_shows_merge_line_and_warning(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"
    b = root / "b.jpg"
    c = root / "c.jpg"
    for p in (a, b, c):
        p.write_bytes(b"")
        patched_hash_cache[p.resolve()] = "h"
    cache = {
        a.resolve(): _migrated(a),
        b.resolve(): _migrated(b, **{PIX_DATE_OVERRIDE: "2019-*-*-*:*:*"}),
        c.resolve(): _migrated(c, **{PIX_DATE_OVERRIDE: "2020-*-*-*:*:*"}),
    }
    result = generate_plan(
        library_root=root,
        cache=cache,
        hashes=patched_hash_cache,
        run_id="r",
        run_dir=tmp_path / "runs",
    )
    text = serialize_plan(source=root, result=result, library_root=root)
    assert "| MERGE " in text
    assert "date_override →2019-*-*-*:*:* (merge ←b.jpg)" in text
    assert "# WARNING: date_override" in text
    assert "DEDUP, 1 MERGE across 1 group" in text


class _FakeExifSession:
    """Records merge writes instead of shelling out to ExifTool."""

    def __init__(self) -> None:
        self.writes: list[tuple[Path, dict[str, str]]] = []
        self.sidecars: list[Path] = []

    def export_xmp_sidecar(self, file: Path, sidecar_path: Path) -> None:
        sidecar_path.write_text("<xmp/>", encoding="utf-8")
        self.sidecars.append(sidecar_path)

    def write_tags(self, file: Path, tags: dict[str, str]) -> None:
        self.writes.append((file, tags))

    def close(self) -> None:
        pass


def test_apply_runs_merge_and_dedup(
    tmp_path: Path,
    patched_hash_cache: dict[Path, str | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    a = root / "a.jpg"  # keeper; empty event, gains "Hawaii"
    b = root / "b.jpg"  # loser; folder yields "Hawaii"
    a.write_bytes(b"keep")
    b.write_bytes(b"dup")
    patched_hash_cache[a.resolve()] = "h"
    patched_hash_cache[b.resolve()] = "h"
    cache = {
        a.resolve(): _with_original(a, "F:/2023/a.jpg"),
        b.resolve(): _with_original(b, "F:/Hawaii/b.jpg"),
    }
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    result = generate_plan(
        library_root=root,
        cache=cache,
        hashes=patched_hash_cache,
        run_id="r",
        run_dir=run_dir,
    )

    fake = _FakeExifSession()
    monkeypatch.setattr(dedupe_mod, "ExifToolSession", lambda: fake)

    removed, merged, quarantined = apply_plan(
        plan=result.plan,
        kept_line_ids={ln.line_id for ln in result.plan.lines},
        run_dir=run_dir,
        library_root=root,
    )
    assert (removed, merged, quarantined) == (1, 1, [])
    # Keeper survived; loser captured.
    assert a.exists() and a.read_bytes() == b"keep"
    assert not b.exists()
    # Merge wrote the consolidated event onto the keeper, and captured a
    # pre-merge sidecar.
    assert len(fake.writes) == 1
    written_file, written_tags = fake.writes[0]
    assert written_file == a.resolve()
    assert written_tags[PIX_MERGE_EVENT] == "Hawaii"
    assert len(fake.sidecars) == 1 and fake.sidecars[0].exists()


class _FailingMergeExif(_FakeExifSession):
    """Sidecar export succeeds, but the tag write reports it didn't
    persist — simulating a damaged/truncated keeper."""

    def write_tags(self, file: Path, tags: dict[str, str]) -> None:
        from pix.exiftool_session import TagWriteFailed

        raise TagWriteFailed(file, "0 image files updated")


def test_apply_quarantines_keeper_on_failed_merge_write(
    tmp_path: Path,
    patched_hash_cache: dict[Path, str | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A MERGE write that doesn't persist quarantines the keeper to
    .pix/errors/ and continues — the loser is still removed, and the
    failure is surfaced (not silently passed)."""
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    a = root / "a.jpg"  # keeper; empty event, would gain "Hawaii"
    b = root / "b.jpg"  # loser
    a.write_bytes(b"keep")
    b.write_bytes(b"dup")
    patched_hash_cache[a.resolve()] = "h"
    patched_hash_cache[b.resolve()] = "h"
    cache = {
        a.resolve(): _with_original(a, "F:/2023/a.jpg"),
        b.resolve(): _with_original(b, "F:/Hawaii/b.jpg"),
    }
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    result = generate_plan(
        library_root=root,
        cache=cache,
        hashes=patched_hash_cache,
        run_id="r",
        run_dir=run_dir,
    )

    monkeypatch.setattr(
        dedupe_mod, "ExifToolSession", lambda: _FailingMergeExif()
    )

    removed, merged, quarantined = apply_plan(
        plan=result.plan,
        kept_line_ids={ln.line_id for ln in result.plan.lines},
        run_dir=run_dir,
        library_root=root,
    )

    assert removed == 1  # loser still removed
    assert merged == 0  # merge didn't count
    assert len(quarantined) == 1
    assert not a.exists()  # keeper moved out of the library
    assert (root / ".pix" / "errors").exists()
    assert not b.exists()


def test_apply_dedup_capture_is_cross_volume_safe(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    """The loser capture into the run folder must survive a run folder relocated
    onto another volume (runs_dir): a same-volume rename fails EXDEV, so
    _apply_dedup uses safe_move (copy+delete). Regression for WinError 17 seen
    live with runs_dir=F: and library=G:."""
    import errno

    from pix.plan import PlanLine

    src = tmp_path / "loser.jpg"
    src.write_bytes(b"content")
    capture = tmp_path / "runs" / "data" / "L001_loser.jpg"
    capture.parent.mkdir(parents=True)
    ln = PlanLine(
        line_id="L001", action=Action.DEDUP, rel_path="loser.jpg",
        details="", abs_path=src, capture_path=capture,
    )

    # Simulate a cross-volume target: every rename raises EXDEV, forcing the
    # copy+delete fallback inside safe_move. (Old safe_rename would raise here.)
    def _exdev(self: Path, target: object) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(Path, "rename", _exdev)

    dedupe_mod._apply_dedup(ln)

    assert capture.read_bytes() == b"content"
    assert not src.exists()
