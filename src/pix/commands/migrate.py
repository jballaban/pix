"""Implementation of `pix migrate`.

End-to-end flow per spec/migrate.md: cleanup → metadata cache → plan-gen →
editor → confirm → apply. Phase 3 lands editor + confirm + apply for the
non-CONVERT cases (RENAME, DELETE, TAG, RENAME+TAG). CONVERT lines are
detected and skipped with a warning; Phase 4 lands the conversion code.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import typer

from pix import banner, debug
from pix.apply import ApplyError, apply_plan
from pix.cleanup import (
    CleanupError,
    cleanup_exiftool_tmp,
    cleanup_migrate_markers,
    cleanup_rename_orphans,
    wipe_staging,
)
from pix.config import Config
from pix.duration import format_duration_precise
from pix.editor import open_in_editor, parse_kept_line_ids, prompt_apply
from pix.library_lock import LockHeld, acquire as acquire_lock
from pix.metadata import (
    ExifToolFailed,
    ExifToolNotFound,
    FileMetadata,
    filter_cache_misses,
    read_metadata_batched,
)
from pix.metadata_cache import PerFileCache
from pix.plan import Action, Plan, PlanLine, generate_plan, lookup_policy
from pix.progress import LiveProgress
from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.scan import walk_source_files
from pix.schema import SCHEMA_VERSION, SchemaTooNew, SchemaUpgradeRequired


def migrate_folder(folder: Path) -> None:
    """End-to-end migrate: plan, edit, confirm, apply.

    Per-file plan-generation reasoning streams to `<run-dir>/debug.log`
    on every run (see `pix.debug`). Constant memory; no flag.
    """
    user_path = str(folder)
    folder = folder.resolve()
    try:
        root = resolve_root(start=folder)
    except SchemaUpgradeRequired as e:
        banner()
        typer.echo(f"{e} Run `pix upgrade {user_path}`", err=True)
        raise typer.Exit(code=1) from e
    except (NoLibraryRoot, SchemaTooNew) as e:
        banner()
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    banner(schema_version=SCHEMA_VERSION)

    if not folder.is_dir():
        typer.echo(f"Error: {folder} is not a directory.", err=True)
        raise typer.Exit(code=1)

    config = Config.load(root / ".pix" / "config.yaml")

    try:
        with acquire_lock(root, "migrate"):
            _run_migrate(root, folder, config)
    except LockHeld as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e


def _run_migrate(root: Path, folder: Path, config: Config) -> None:
    """Migrate body, called under the library lock."""
    # Create the run dir up front so plan.log exists from the very
    # start of the planning phase. Plan-phase status (Library root,
    # source walk timing, bulk-read timing, etc.) all goes to plan.log
    # so the console stays quiet — only the single rewriting `\r`
    # progress line shows during plan-gen.
    run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    runs_dir = root / ".pix" / "runs" / run_id
    runs_dir.mkdir(parents=True)
    plan_log_path = runs_dir / "plan.log"
    staging_dir = root / ".pix" / "staging"

    _plog(plan_log_path, f"Library root: {root}")
    _plog(plan_log_path, f"Source: {folder}")

    # Recover any orphans from prior crashed runs before walking the
    # source. Order matters: wipe staging first (any aborted CONVERT
    # output is forfeit; the new plan will re-propose); then rename
    # intermediates (simple path-only ops); then CONVERT markers (which
    # may need ExifTool reads); then ExifTool's own _exiftool_tmp
    # leftovers from interrupted TAG writes.
    wiped = wipe_staging(staging_dir)
    if wiped:
        _plog(plan_log_path, f"Wiped {wiped} entr(ies) from staging.")
    reverted = cleanup_rename_orphans(folder)
    if reverted:
        _plog(
            plan_log_path,
            f"Recovered {len(reverted)} rename intermediate(s) from a prior run.",
        )
    try:
        marker_notes = cleanup_migrate_markers(folder)
    except CleanupError as e:
        typer.echo(f"Error: marker cleanup failed: {e}", err=True)
        raise typer.Exit(code=1) from e
    for note in marker_notes:
        _plog(plan_log_path, note)
    tmp_deleted = cleanup_exiftool_tmp(folder)
    if tmp_deleted:
        _plog(
            plan_log_path,
            f"Cleaned up {len(tmp_deleted)} *_exiftool_tmp file(s) from a "
            f"prior interrupted run.",
        )

    # Walk is sub-second on the libraries we care about (scandir-based
    # since v0.1.62), so no console ticker — the next phase's progress
    # bar comes up immediately. Timing still lands in plan.log.
    t0 = time.monotonic()
    scanned = walk_source_files(folder)
    _plog(
        plan_log_path,
        f"Found {len(scanned)} files in "
        f"{format_duration_precise(time.monotonic() - t0)}.",
    )

    source_files = [p for p, _ in scanned]
    _validate_extensions(source_files, config)

    # Cache lookup + ExifTool reads. With the per-file cache, the
    # second pass typically has few misses; only those need ExifTool.
    t0 = time.monotonic()
    meta_cache = PerFileCache.for_library(root)

    with LiveProgress(total=len(scanned)) as check_progress:
        check_progress.begin("checking cache")

        def _on_check_batch(batch_size: int) -> None:
            check_progress.advance(by=batch_size)

        hits, misses = filter_cache_misses(
            scanned, meta_cache, on_batch=_on_check_batch
        )

    fresh: dict[Path, FileMetadata] = {}
    if misses:
        try:
            with LiveProgress(total=len(misses)) as read_progress:
                read_progress.begin("reading metadata")

                def _on_batch(batch_size: int) -> None:
                    read_progress.advance(by=batch_size)

                fresh = read_metadata_batched(
                    misses,
                    cache=meta_cache,
                    on_batch=_on_batch,
                )
        except ExifToolNotFound as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1) from e
        except ExifToolFailed as e:
            typer.echo(f"Error: exiftool failed.\n{e}", err=True)
            raise typer.Exit(code=1) from e
    cache = {**hits, **fresh}
    _plog(
        plan_log_path,
        f"Read {len(cache)} files in "
        f"{format_duration_precise(time.monotonic() - t0)} "
        f"({len(hits)} cache hits, {len(misses)} from ExifTool).",
    )

    for path in source_files:
        if path not in cache:
            cache[path] = FileMetadata(
                path=path, raw={"SourceFile": str(path)}
            )

    t0 = time.monotonic()
    _plog(plan_log_path, "Generating plan...")
    with debug.writing_to(runs_dir):
        plan = generate_plan(
            source=folder,
            cache=cache,
            config=config,
            run_id=run_id,
            run_dir=runs_dir,
            staging_dir=staging_dir,
        )
    _plog(
        plan_log_path,
        f"Plan generated in {format_duration_precise(time.monotonic() - t0)}.",
    )

    plan_path = runs_dir / "plan.txt"
    plan_path.write_text(plan.to_text(), encoding="utf-8")

    typer.echo(f"Plan written: {plan_path}")
    typer.echo(f"Summary: {_summarize(plan.lines)}")

    if len(plan.lines) == 0:
        typer.echo("Nothing to do.")
        return

    # The editor pass is opt-in. By default we trust the generated plan
    # and apply directly; the user can pick `e` to review/edit and
    # re-prompt as many times as they want.
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
        break  # 'y' — apply

    apply_log_path = runs_dir / "apply.log"
    try:
        try:
            completed, convert_failures = apply_plan(
                plan=plan,
                plan_path=plan_path,
                run_dir=runs_dir,
                kept_line_ids=kept_line_ids,
                staging_dir=staging_dir,
                meta_cache=meta_cache,
            )
        except ApplyError as e:
            typer.echo(f"Error: apply failed: {e}", err=True)
            raise typer.Exit(code=1) from e

        # Skip cache updates for plan lines whose CONVERT failed — the
        # source file is still in place at its old name with its old
        # metadata, so the cache entry remains correct as-is.
        failed_ids = {ln.line_id for ln, _ in convert_failures}
        applied_ids = kept_line_ids - failed_ids
        _post_apply_cache_update(meta_cache, plan, applied_ids)

        typer.echo("")
        typer.echo(f"Applied {completed} action(s).")

        if convert_failures:
            errors_dir = root / ".pix" / "errors"
            typer.echo("")
            typer.echo(
                f"{len(convert_failures)} CONVERT line(s) failed — "
                f"sources moved to {errors_dir}:",
                err=True,
            )
            for ln, err in convert_failures:
                typer.echo(f"  {ln.abs_path}", err=True)
                typer.echo(f"    {err}", err=True)
            typer.echo("", err=True)
            typer.echo(
                f"Each entry in {errors_dir} has a .errorinfo sidecar "
                f"with original path and error. To retry: restore the "
                f"original file at its source path and re-run migrate.",
                err=True,
            )
            raise typer.Exit(code=1)
    finally:
        # Always emit the log path on the way out — success, error, or
        # CTRL+C. Lets the user copy-paste straight into a tail/grep.
        typer.echo(f"Log: {apply_log_path}")


def _post_apply_cache_update(
    cache: PerFileCache,
    plan: Plan,
    kept_line_ids: set[str],
) -> None:
    """Update the per-file metadata cache to reflect apply's mutations.

    Walks the applied plan lines and:
    - DELETE / STASH: removes the cache entry.
    - CONVERT+RENAME+TAG: removes the old entry; the new file's cache
      will be built on its next read (we don't have the post-convert
      metadata in hand here).
    - RENAME: renames the cache file alongside the media rename.
    - TAG: merges the written pix:* fields into the cached metadata.
    - RENAME+TAG: both.
    """
    for ln in plan.lines:
        if ln.line_id not in kept_line_ids:
            continue
        if ln.action == Action.DELETE:
            cache.remove(ln.abs_path)
        elif ln.action == Action.STASH:
            cache.remove(ln.abs_path)
        elif ln.action == Action.CONVERT_RENAME_TAG:
            cache.remove(ln.abs_path)  # old file gone
            # New file's cache will be (re)built on next read.
        elif ln.action == Action.RENAME:
            if ln.target_path is not None:
                cache.rename(ln.abs_path, ln.target_path)
        elif ln.action == Action.TAG:
            if ln.pix_writes:
                cache.update_metadata(ln.abs_path, dict(ln.pix_writes))
        elif ln.action == Action.RENAME_TAG:
            if ln.pix_writes:
                cache.update_metadata(ln.abs_path, dict(ln.pix_writes))
            if ln.target_path is not None:
                cache.rename(ln.abs_path, ln.target_path)


def _plog(plan_log_path: Path, msg: str) -> None:
    """Append one timestamped line to plan.log.

    Used for the phase headers and per-step timings that previously
    went to the console. Keeps the planning-phase console output to
    just the single rewriting progress line.
    """
    ts = datetime.now().isoformat(timespec="seconds")
    with plan_log_path.open("a", encoding="utf-8") as f:
        f.write(f"{ts} {msg}\n")


def _summarize(lines: list[PlanLine]) -> str:
    """Render `N plan line(s) — X CONVERT, Y RENAME, Z TAG, W DELETE`.

    Zero-count action types are omitted from the comma list.
    """
    counts: dict[Action, int] = {a: 0 for a in Action}
    for ln in lines:
        counts[ln.action] += 1
    convert = counts[Action.CONVERT_RENAME_TAG]
    rename = counts[Action.RENAME] + counts[Action.RENAME_TAG]
    tag = counts[Action.TAG] + counts[Action.RENAME_TAG] + convert
    delete = counts[Action.DELETE]

    parts: list[str] = []
    if convert:
        parts.append(f"{convert} CONVERT")
    if rename:
        parts.append(f"{rename} RENAME")
    if tag:
        parts.append(f"{tag} TAG")
    if delete:
        parts.append(f"{delete} DELETE")

    if not parts:
        return f"{len(lines)} plan line(s)."
    return f"{len(lines)} plan line(s) — " + ", ".join(parts) + "."


def _validate_extensions(
    source_files: list[Path], config: Config
) -> None:
    """Fail-fast on unknown extensions per spec/migrate.md."""
    unknown: dict[str, Path] = {}
    for path in source_files:
        if lookup_policy(path.name, config.extensions) is None:
            key = path.suffix.lower().lstrip(".") or path.name.lower()
            unknown.setdefault(key, path)

    if not unknown:
        return

    typer.echo("", err=True)
    typer.echo("Unknown file extensions found in source:", err=True)
    for key, example in sorted(unknown.items()):
        typer.echo(f"  .{key}   (e.g. {example})", err=True)
    typer.echo("", err=True)
    typer.echo(
        "Edit <library-root>/.pix/config.yaml and set an action for each, "
        "then re-run.",
        err=True,
    )
    typer.echo(
        "Available actions: keep, convert_to_jpg, convert_to_mp4, delete",
        err=True,
    )
    typer.echo("Aborted; no changes made.", err=True)
    raise typer.Exit(code=1)
