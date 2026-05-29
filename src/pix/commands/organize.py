"""Implementation of `pix organize <template>`.

End-to-end flow per spec/organize.md: parse template → check CWD →
walk library → bulk-read metadata → generate plan → editor/apply
prompt → apply → persist active template.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import typer

from pix import banner, debug
from pix.cache_base import prune_orphans, relocate_all
from pix.checkout import CheckoutOpen, ensure_no_open_checkout
from pix.config import Config, set_organize_template
from pix.duration import format_duration_precise
from pix.editor import open_in_editor, parse_kept_line_ids, prompt_apply
from pix.hash_cache import read_all_cached_hashes
from pix.library_lock import LockHeld, acquire as acquire_lock
from pix.metadata import (
    ExifToolFailed,
    ExifToolNotFound,
    FileMetadata,
    filter_cache_misses,
    read_metadata_batched,
)
from pix.metadata_cache import PerFileCache
from pix.progress import LiveProgress
from pix.organize import (
    CwdInsideLibraryError,
    MissingHashesError,
    OrganizeApplyError,
    OrganizeError,
    Template,
    UnmigratedFilesError,
    apply_plan,
    check_cwd_not_inside,
    generate_plan,
    parse_template,
)
from pix.plan import PlanLine
from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.scan import walk_source_files
from pix.schema import SCHEMA_VERSION, SchemaTooNew, SchemaUpgradeRequired


def organize_library(path: Path, template_str: str) -> None:
    """End-to-end organize: parse, plan, edit, confirm, apply.

    `path` is anywhere inside (or at) the library root. Resolution
    walks up from it to find the `.pix/` directory.
    """
    user_path = str(path)
    path = path.resolve()
    try:
        root = resolve_root(start=path)
    except SchemaUpgradeRequired as e:
        banner()
        typer.echo(f"{e} Run `pix upgrade {user_path}`", err=True)
        raise typer.Exit(code=1) from e
    except (NoLibraryRoot, SchemaTooNew) as e:
        banner()
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    banner(schema_version=SCHEMA_VERSION)

    try:
        ensure_no_open_checkout(root)
    except CheckoutOpen as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    try:
        check_cwd_not_inside(root)
    except CwdInsideLibraryError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    try:
        template = parse_template(template_str)
    except OrganizeError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    config_path = root / ".pix" / "config.yaml"
    Config.load(config_path)  # validates current config; parsed value not used here

    try:
        with acquire_lock(root, "organize"):
            _run_organize(root, template, template_str, config_path)
    except LockHeld as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e


def _run_organize(
    root: Path,
    template: Template,
    template_str: str,
    config_path: Path,
) -> None:
    """Organize body, called under the library lock."""
    run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    runs_dir = root / ".pix" / "runs" / run_id
    runs_dir.mkdir(parents=True)
    plan_log_path = runs_dir / "plan.log"

    _plog(plan_log_path, f"Library root: {root}")
    _plog(plan_log_path, f"Template: {template_str}")

    # Walk is sub-second on libraries we care about; no console
    # ticker — the next phase's progress bar comes up immediately.
    # Timing still lands in plan.log. (Same pattern as migrate/dedupe.)
    t0 = time.monotonic()
    scanned = walk_source_files(root)
    _plog(
        plan_log_path,
        f"Found {len(scanned)} file(s) in "
        f"{format_duration_precise(time.monotonic() - t0)}.",
    )

    if not scanned:
        typer.echo("Library is empty; nothing to organize.")
        return

    library_files = [p for p, _, _ in scanned]

    # Drop cache sidecars whose source files no longer exist anywhere
    # in the library, plus any legacy-suffix sidecars from older pix
    # versions. Library-wide walk, so no prefix scoping.
    prune_stats = prune_orphans(root, set(library_files))
    if prune_stats.orphans_removed or prune_stats.legacy_removed:
        _plog(
            plan_log_path,
            f"Pruned {prune_stats.orphans_removed} orphan cache "
            f"sidecar(s) and {prune_stats.legacy_removed} legacy "
            f"sidecar(s) from .pix/cache/.",
        )

    t0 = time.monotonic()
    meta_cache = PerFileCache.for_library(root)

    with LiveProgress(total=len(scanned)) as check_progress:
        check_progress.begin("Loading cache")

        def _on_check_batch(batch_size: int) -> None:
            check_progress.advance(by=batch_size)

        hits, misses = filter_cache_misses(
            scanned, meta_cache, on_batch=_on_check_batch
        )

    fresh: dict[Path, FileMetadata] = {}
    if misses:
        try:
            with LiveProgress(total=len(misses)) as read_progress:
                read_progress.begin("Filling missing cache")

                def _on_batch(batch_size: int) -> None:
                    read_progress.advance(by=batch_size)

                fresh = read_metadata_batched(
                    misses, cache=meta_cache, on_batch=_on_batch
                )
        except ExifToolNotFound as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1) from e
        except ExifToolFailed as e:
            typer.echo(f"Error: exiftool failed.\n{e}", err=True)
            raise typer.Exit(code=1) from e
    cache = {**hits, **fresh}
    for path in library_files:
        if path not in cache:
            cache[path] = FileMetadata(
                path=path, raw={"SourceFile": str(path)}
            )
    _plog(
        plan_log_path,
        f"Read {len(cache)} file(s) in "
        f"{format_duration_precise(time.monotonic() - t0)} "
        f"({len(hits)} cache hits, {len(misses)} from ExifTool).",
    )

    # One parallel pass over the hash cache — feeds the prereq check
    # (no_hash refusal) and the collision-resolution tiebreaker inside
    # generate_plan. Previously read_cached_hash was called twice per
    # file sequentially.
    t0 = time.monotonic()
    with LiveProgress(total=len(scanned)) as hash_progress:
        hash_progress.begin("Reading hashes")

        def _on_hash_batch(batch_size: int) -> None:
            hash_progress.advance(by=batch_size)

        hashes = read_all_cached_hashes(
            root, scanned, on_batch=_on_hash_batch
        )
    _plog(
        plan_log_path,
        f"Read {len(hashes)} hash(es) in "
        f"{format_duration_precise(time.monotonic() - t0)}.",
    )

    t0 = time.monotonic()
    _plog(plan_log_path, "Generating plan...")
    try:
        with (
            debug.writing_to(runs_dir),
            plan_log_path.open("a", encoding="utf-8") as plan_log,
        ):
            plan = generate_plan(
                library_root=root,
                template=template,
                cache=cache,
                hashes=hashes,
                run_id=run_id,
                run_dir=runs_dir,
                plan_log=plan_log,
            )
    except UnmigratedFilesError as e:
        typer.echo(f"Error: {e}", err=True)
        _echo_sample(e.paths)
        typer.echo(
            f"Run `pix migrate {root}` (or the relevant subfolder) first.",
            err=True,
        )
        raise typer.Exit(code=1) from e
    except MissingHashesError as e:
        typer.echo(f"Error: {e}", err=True)
        _echo_sample(e.paths)
        raise typer.Exit(code=1) from e
    _plog(
        plan_log_path,
        f"Plan generated in {format_duration_precise(time.monotonic() - t0)}.",
    )

    plan_path = runs_dir / "plan.txt"
    plan_path.write_text(plan.to_text(), encoding="utf-8")

    typer.echo(f"Plan written: {plan_path}")
    typer.echo(f"Template:     {template_str}")
    typer.echo(f"Summary: {_summarize(plan.lines)}")

    if len(plan.lines) == 0:
        typer.echo("Library already matches the template; nothing to do.")
        # Even a no-op run persists the active template so commit's
        # auto-trigger knows what shape the user intends.
        set_organize_template(config_path, template_str)
        return

    kept_line_ids = {ln.line_id for ln in plan.lines}
    while True:
        typer.echo("")
        choice = prompt_apply()
        if choice == "n":
            typer.echo("Aborted; plan file left in place.")
            return
        if choice == "e":
            open_in_editor(plan_path)
            edited_text = plan_path.read_text(encoding="utf-8")
            kept_line_ids = parse_kept_line_ids(edited_text)
            kept_lines = [
                ln for ln in plan.lines if ln.line_id in kept_line_ids
            ]
            typer.echo("")
            typer.echo(f"After edit: {_summarize(kept_lines)}")
            continue
        break  # 'y'

    apply_log_path = runs_dir / "apply.log"
    try:
        try:
            completed = apply_plan(
                plan=plan,
                kept_line_ids=kept_line_ids,
                run_dir=runs_dir,
                library_root=root,
            )
        except OrganizeApplyError as e:
            typer.echo(f"Error: apply failed: {e}", err=True)
            raise typer.Exit(code=1) from e

        # Cache survives organize by following every MOVE: relocate all
        # sidecars (.meta/.hash/.video) alongside the media file. A MOVE
        # leaves bytes/size/mtime untouched, so a valid hash/video entry
        # stays valid at the new path. Relocating only .meta (the old
        # behavior) orphaned .hash/.video, which the next walk pruned —
        # forcing a needless re-`pix hash` after every organize.
        # Best-effort.
        for ln in plan.lines:
            if ln.line_id in kept_line_ids and ln.target_path is not None:
                relocate_all(root, ln.abs_path, ln.target_path)

        # Persist the active template now that apply succeeded.
        set_organize_template(config_path, template_str)

        typer.echo("")
        typer.echo(f"Organized {completed} file(s).")
    finally:
        # Always emit the log path on the way out — success, error, or
        # CTRL+C. Lets the user copy-paste straight into a tail/grep.
        typer.echo(f"Log: {apply_log_path}")


def _echo_sample(paths: list[Path], limit: int = 10) -> None:
    """Print up to `limit` offending paths to stderr, then an elision note."""
    for p in paths[:limit]:
        typer.echo(f"  {p}", err=True)
    if len(paths) > limit:
        typer.echo(f"  ... and {len(paths) - limit} more", err=True)


def _plog(plan_log_path: Path, msg: str) -> None:
    """Append one timestamped line to plan.log."""
    ts = datetime.now().isoformat(timespec="seconds")
    with plan_log_path.open("a", encoding="utf-8") as f:
        f.write(f"{ts} {msg}\n")


def _summarize(lines: list[PlanLine]) -> str:
    """Render `N plan line(s) — N MOVE` (organize has only one action type)."""
    if not lines:
        return "0 plan line(s)."
    return f"{len(lines)} plan line(s) — {len(lines)} MOVE."
