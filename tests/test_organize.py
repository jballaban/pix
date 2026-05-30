"""Tests for `pix.organize` — template parse, render, plan-gen, apply."""

from __future__ import annotations

from pathlib import Path

import pytest

from pix.metadata import FileMetadata
from pix.organize import (
    CwdInsideLibraryError,
    MissingHashesError,
    OrganizeError,
    UnmigratedFilesError,
    apply_plan,
    check_cwd_not_inside,
    cleanup_empty_folders,
    compute_values,
    generate_plan,
    parse_template,
    render_target_folder,
    sanitize_folder_name,
)
from pix.plan import (
    PIX_DATE_AUTO,
    PIX_EVENT_AUTO,
    PIX_EVENT_OVERRIDE,
    PIX_ORIGINAL_PATH,
    Action,
)


# --- Template parsing --------------------------------------------------------


def test_parse_simple_template() -> None:
    t = parse_template("{year}/{month}/{event}")
    assert len(t.levels) == 3
    assert t.raw == "{year}/{month}/{event}"


def test_parse_template_with_literal_and_token() -> None:
    t = parse_template("{year}-archive/{event}")
    assert len(t.levels) == 2
    # First level has literal '-archive' + token year
    assert len(t.levels[0].segments) == 2


def test_parse_template_all_literal_level() -> None:
    t = parse_template("Photos/{year}")
    assert len(t.levels) == 2
    # Level 0 is just the literal "Photos"
    assert len(t.levels[0].segments) == 1


def test_parse_template_rejects_empty() -> None:
    with pytest.raises(OrganizeError, match="empty"):
        parse_template("")


def test_parse_template_rejects_consecutive_slashes() -> None:
    with pytest.raises(OrganizeError, match="empty level"):
        parse_template("{year}//{event}")


def test_parse_template_rejects_time_token() -> None:
    with pytest.raises(OrganizeError, match=r"\{time\}"):
        parse_template("{year}/{time}")


def test_parse_template_rejects_date_token() -> None:
    with pytest.raises(OrganizeError, match=r"\{date\}"):
        parse_template("{date}/{event}")


def test_parse_template_rejects_unknown_token() -> None:
    with pytest.raises(OrganizeError, match="unknown token"):
        parse_template("{year}/{quarter}")


# --- Template rendering -----------------------------------------------------


def _values(
    year: str | None = "2023",
    month: str | None = "08",
    day: str | None = "15",
    event: str | None = "Hawaii",
    date: str | None = "2023-08-15-14:32:05",
) -> dict[str, str | None]:
    return {
        "year": year,
        "month": month,
        "day": day,
        "date": date,
        "event": event,
    }


def test_render_simple() -> None:
    t = parse_template("{year}/{month}/{event}")
    assert render_target_folder(t, _values()) == "2023/08/Hawaii"


def test_render_with_literal() -> None:
    t = parse_template("{year}-archive/{event}")
    assert render_target_folder(t, _values()) == "2023-archive/Hawaii"


def test_render_per_level_null() -> None:
    t = parse_template("{year}/{event}")
    assert (
        render_target_folder(t, _values(event=None)) == "2023/(null)"
    )
    assert (
        render_target_folder(t, _values(year=None)) == "(null)/Hawaii"
    )


def test_render_collapses_trailing_null_chain() -> None:
    t = parse_template("{year}/{event}")
    assert (
        render_target_folder(t, _values(year=None, event=None)) == "(null)"
    )


def test_render_does_not_collapse_leading_or_middle_nulls() -> None:
    t = parse_template("{year}/{event}/{day}")
    assert (
        render_target_folder(t, _values(event=None, day=None))
        == "2023/(null)"
    )
    # year=null event=Hawaii day=null → null at start AND end; only the
    # trailing one is alone, so no collapse mid-path.
    assert (
        render_target_folder(t, _values(year=None, day=None))
        == "(null)/Hawaii/(null)"
    )


def test_render_sanitizes_illegal_chars() -> None:
    t = parse_template("{event}")
    assert render_target_folder(t, _values(event="Birthday/Party")) == "Birthday_Party"


def test_render_level_with_null_token_renders_whole_level_as_null() -> None:
    """`{year}-archive` with year=null → '(null)', not 'null-archive'."""
    t = parse_template("{year}-archive")
    assert render_target_folder(t, _values(year=None)) == "(null)"


def test_render_literal_null_value_does_not_collide_with_placeholder() -> None:
    """An event literally named 'null' renders to 'null/', distinct from the
    '(null)' untagged placeholder — the whole point of the bracket sentinel."""
    t = parse_template("{year}/{event}")
    assert render_target_folder(t, _values(event="null")) == "2023/null"
    assert render_target_folder(t, _values(event=None)) == "2023/(null)"


# --- Sanitization -----------------------------------------------------------


def test_sanitize_replaces_illegal_chars() -> None:
    assert sanitize_folder_name('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"


def test_sanitize_strips_trailing_dot_and_space() -> None:
    assert sanitize_folder_name("name. ") == "name"


def test_sanitize_prefixes_reserved_names() -> None:
    assert sanitize_folder_name("CON") == "_CON"
    assert sanitize_folder_name("com1") == "_com1"


def test_sanitize_empty_result_yields_underscore() -> None:
    assert sanitize_folder_name(". .") == "_"


# --- CWD constraint ----------------------------------------------------------


def test_cwd_check_passes_at_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    check_cwd_not_inside(tmp_path)


def test_cwd_check_refuses_in_strict_subfolder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    with pytest.raises(CwdInsideLibraryError):
        check_cwd_not_inside(tmp_path)


def test_cwd_check_passes_outside_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    library = tmp_path / "library"
    library.mkdir()
    check_cwd_not_inside(library)


# --- Empty-folder cleanup ----------------------------------------------------


def test_cleanup_removes_empty_folders_bottom_up(tmp_path: Path) -> None:
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    (tmp_path / "a" / "keep").mkdir(parents=True)
    (tmp_path / "a" / "keep" / "file").write_bytes(b"")
    removed = cleanup_empty_folders(tmp_path)
    assert removed >= 2
    assert not (tmp_path / "a" / "b" / "c").exists()
    assert not (tmp_path / "a" / "b").exists()
    assert (tmp_path / "a" / "keep").exists()


def test_cleanup_skips_pix_directory(tmp_path: Path) -> None:
    (tmp_path / ".pix" / "runs").mkdir(parents=True)
    cleanup_empty_folders(tmp_path)
    assert (tmp_path / ".pix" / "runs").exists()


def test_cleanup_never_removes_library_root(tmp_path: Path) -> None:
    """Even an entirely-empty library root must survive."""
    cleanup_empty_folders(tmp_path)
    assert tmp_path.exists()


# --- Plan generation ---------------------------------------------------------


def _meta(path: Path, **fields: object) -> FileMetadata:
    return FileMetadata(
        path=path, raw={"SourceFile": str(path), **fields}
    )


def test_plan_refuses_unmigrated_files(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    p = root / "2023-08-15_143205.jpg"
    p.write_bytes(b"")
    cache = {p.resolve(): _meta(p)}  # no pix:OriginalPath
    with pytest.raises(UnmigratedFilesError):
        generate_plan(
            library_root=root,
            template=parse_template("{year}"),
            cache=cache,
            hashes={},
            run_id="test-run",
            run_dir=tmp_path / "runs",
        )


def test_plan_refuses_missing_content_hash(tmp_path: Path) -> None:
    """Files that have OriginalPath but no cached content hash cause refusal."""
    root = tmp_path / "lib"
    root.mkdir()
    p = root / "2023-08-15_143205.jpg"
    p.write_bytes(b"")
    cache = {
        p.resolve(): _meta(p, **{PIX_ORIGINAL_PATH: "F:/source/x.jpg"})
    }
    # No hash registered → MissingHashesError.
    with pytest.raises(MissingHashesError):
        generate_plan(
            library_root=root,
            template=parse_template("{year}"),
            cache=cache,
            hashes={},
            run_id="test-run",
            run_dir=tmp_path / "runs",
        )


def test_plan_moves_file_to_template_path(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    p = root / "2023-08-15_143205.jpg"
    p.write_bytes(b"")
    cache = {
        p.resolve(): _meta(
            p,
            **{
                PIX_ORIGINAL_PATH: "F:/source/2023-08-Hawaii/IMG.jpg",
                PIX_DATE_AUTO: "2023-08-15-14:32:05",
                PIX_EVENT_AUTO: "Hawaii",
            },
        )
    }
    for path in cache:
        patched_hash_cache[path] = "h"
    plan = generate_plan(
        library_root=root,
        template=parse_template("{year}/{month}/{event}"),
        cache=cache,
        hashes=patched_hash_cache,
        run_id="test-run",
        run_dir=tmp_path / "runs",
    )
    assert len(plan.lines) == 1
    line = plan.lines[0]
    assert line.action == Action.MOVE
    assert line.target_path is not None
    assert line.target_path == root / "2023" / "08" / "Hawaii" / "2023-08-15_143205.jpg"


def test_plan_idempotent_when_already_in_place(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    root = tmp_path / "lib"
    (root / "2023" / "08" / "Hawaii").mkdir(parents=True)
    p = root / "2023" / "08" / "Hawaii" / "2023-08-15_143205.jpg"
    p.write_bytes(b"")
    cache = {
        p.resolve(): _meta(
            p,
            **{
                PIX_ORIGINAL_PATH: "F:/source/x.jpg",
                PIX_DATE_AUTO: "2023-08-15-14:32:05",
                PIX_EVENT_AUTO: "Hawaii",
            },
        )
    }
    for path in cache:
        patched_hash_cache[path] = "h"
    plan = generate_plan(
        library_root=root,
        template=parse_template("{year}/{month}/{event}"),
        cache=cache,
        hashes=patched_hash_cache,
        run_id="test-run",
        run_dir=tmp_path / "runs",
    )
    assert plan.lines == []


def test_plan_drops_stale_collision_suffix(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    """A file in `imports/2023-08-15_143205_001.jpg` (collision in old folder)
    that has no peer at the target folder drops the `_001` on move."""
    root = tmp_path / "lib"
    (root / "imports").mkdir(parents=True)
    p = root / "imports" / "2023-08-15_143205_001.jpg"
    p.write_bytes(b"")
    cache = {
        p.resolve(): _meta(
            p,
            **{
                PIX_ORIGINAL_PATH: "F:/source/x.jpg",
                PIX_DATE_AUTO: "2023-08-15-14:32:05",
                PIX_EVENT_AUTO: "Hawaii",
            },
        )
    }
    for path in cache:
        patched_hash_cache[path] = "h"
    plan = generate_plan(
        library_root=root,
        template=parse_template("{year}/{event}"),
        cache=cache,
        hashes=patched_hash_cache,
        run_id="test-run",
        run_dir=tmp_path / "runs",
    )
    assert len(plan.lines) == 1
    line = plan.lines[0]
    assert line.target_filename == "2023-08-15_143205.jpg"


def test_plan_applies_collision_suffix_at_target(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    """Two files with same effective date in different sources collide at
    the target folder; second one gets `_001`."""
    root = tmp_path / "lib"
    (root / "imports-a").mkdir(parents=True)
    (root / "imports-b").mkdir(parents=True)
    a = root / "imports-a" / "2023-08-15_143205.jpg"
    b = root / "imports-b" / "2023-08-15_143205.jpg"
    a.write_bytes(b"")
    b.write_bytes(b"")
    patched_hash_cache[a.resolve()] = "aaaa"
    patched_hash_cache[b.resolve()] = "bbbb"
    cache = {
        a.resolve(): _meta(
            a,
            **{
                PIX_ORIGINAL_PATH: "F:/source/a.jpg",
                PIX_DATE_AUTO: "2023-08-15-14:32:05",
                PIX_EVENT_AUTO: "Hawaii",
            },
        ),
        b.resolve(): _meta(
            b,
            **{
                PIX_ORIGINAL_PATH: "F:/source/b.jpg",
                PIX_DATE_AUTO: "2023-08-15-14:32:05",
                PIX_EVENT_AUTO: "Hawaii",
            },
        ),
    }
    plan = generate_plan(
        library_root=root,
        template=parse_template("{year}/{event}"),
        cache=cache,
        hashes=patched_hash_cache,
        run_id="test-run",
        run_dir=tmp_path / "runs",
    )
    assert len(plan.lines) == 2
    # Sorted by content_hash ascending: aaaa keeps bare, bbbb gets _001.
    filenames = sorted(ln.target_filename for ln in plan.lines if ln.target_filename)
    assert filenames == [
        "2023-08-15_143205.jpg",
        "2023-08-15_143205_001.jpg",
    ]


def test_plan_event_override_wins_over_event_auto(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    """EventOverride determines the target folder, not EventAuto."""
    root = tmp_path / "lib"
    (root / "imports").mkdir(parents=True)
    p = root / "imports" / "2023-08-15_143205.jpg"
    p.write_bytes(b"")
    cache = {
        p.resolve(): _meta(
            p,
            **{
                PIX_ORIGINAL_PATH: "F:/source/x.jpg",
                PIX_DATE_AUTO: "2023-08-15-14:32:05",
                PIX_EVENT_AUTO: "Hawaii",
                PIX_EVENT_OVERRIDE: "Wedding",
            },
        )
    }
    for path in cache:
        patched_hash_cache[path] = "h"
    plan = generate_plan(
        library_root=root,
        template=parse_template("{event}"),
        cache=cache,
        hashes=patched_hash_cache,
        run_id="test-run",
        run_dir=tmp_path / "runs",
    )
    assert len(plan.lines) == 1
    assert plan.lines[0].target_path == root / "Wedding" / "2023-08-15_143205.jpg"


# --- Apply -------------------------------------------------------------------


def test_apply_moves_file_to_target(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    root = tmp_path / "lib"
    (root / "imports").mkdir(parents=True)
    p = root / "imports" / "2023-08-15_143205.jpg"
    p.write_bytes(b"hi")
    cache = {
        p.resolve(): _meta(
            p,
            **{
                PIX_ORIGINAL_PATH: "F:/source/x.jpg",
                PIX_DATE_AUTO: "2023-08-15-14:32:05",
                PIX_EVENT_AUTO: "Hawaii",
            },
        )
    }
    for path in cache:
        patched_hash_cache[path] = "h"
    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    plan = generate_plan(
        library_root=root,
        template=parse_template("{year}/{event}"),
        cache=cache,
        hashes=patched_hash_cache,
        run_id="test-run",
        run_dir=run_dir,
    )
    apply_plan(
        plan=plan,
        kept_line_ids={ln.line_id for ln in plan.lines},
        run_dir=run_dir,
        library_root=root,
    )
    expected = root / "2023" / "Hawaii" / "2023-08-15_143205.jpg"
    assert expected.exists()
    assert expected.read_bytes() == b"hi"
    # Empty source folder is swept.
    assert not (root / "imports").exists()


def test_apply_resolves_in_place_suffix_cycle(
    tmp_path: Path, patched_hash_cache: dict[Path, str | None]
) -> None:
    """Three files already in their target folder whose content-hash suffix
    assignment is a cyclic permutation (bare→_002→_001→bare) must all land
    correctly — the scheduler breaks the cycle with a temp name instead of
    failing with 'target already exists' (regression for the in-place
    reshuffle crash)."""
    root = tmp_path / "lib"
    folder = root / "2023" / "Hawaii"
    folder.mkdir(parents=True)
    bare = folder / "2023-08-15_143205.jpg"
    s1 = folder / "2023-08-15_143205_001.jpg"
    s2 = folder / "2023-08-15_143205_002.jpg"
    bare.write_bytes(b"C")
    s1.write_bytes(b"A")
    s2.write_bytes(b"B")

    def m(p: Path) -> FileMetadata:
        return _meta(
            p,
            **{
                PIX_ORIGINAL_PATH: f"F:/source/{p.name}",
                PIX_DATE_AUTO: "2023-08-15-14:32:05",
                PIX_EVENT_AUTO: "Hawaii",
            },
        )

    cache = {bare.resolve(): m(bare), s1.resolve(): m(s1), s2.resolve(): m(s2)}
    # Hash order (asc) → bare/_001/_002 assignment: _001-file < _002-file <
    # bare-file ⇒ _001→bare, _002→_001, bare→_002. A 3-cycle.
    patched_hash_cache[s1.resolve()] = "a"
    patched_hash_cache[s2.resolve()] = "b"
    patched_hash_cache[bare.resolve()] = "c"

    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    plan = generate_plan(
        library_root=root,
        template=parse_template("{year}/{event}"),
        cache=cache,
        hashes=patched_hash_cache,
        run_id="r",
        run_dir=run_dir,
    )
    assert len(plan.lines) == 3  # all three move (full permutation)

    apply_plan(
        plan=plan,
        kept_line_ids={ln.line_id for ln in plan.lines},
        run_dir=run_dir,
        library_root=root,
    )

    # Final occupants by content: _001-file→bare, _002-file→_001, bare→_002.
    assert bare.read_bytes() == b"A"
    assert s1.read_bytes() == b"B"
    assert s2.read_bytes() == b"C"
    # No temp files left behind.
    assert not list(folder.glob("*.__organize_tmp__"))


# --- compute_values ----------------------------------------------------------


def test_compute_values_with_date_and_event() -> None:
    meta = FileMetadata(
        path=Path("F:/x.jpg"),
        raw={
            "SourceFile": "F:/x.jpg",
            PIX_DATE_AUTO: "2023-08-15-14:32:05",
            PIX_EVENT_AUTO: "Hawaii",
        },
    )
    v = compute_values(meta)
    assert v["year"] == "2023"
    assert v["month"] == "08"
    assert v["day"] == "15"
    assert v["event"] == "Hawaii"


def test_compute_values_with_no_date_or_event() -> None:
    meta = FileMetadata(path=Path("F:/x.jpg"), raw={"SourceFile": "F:/x.jpg"})
    v = compute_values(meta)
    assert v["year"] is None
    assert v["event"] is None
