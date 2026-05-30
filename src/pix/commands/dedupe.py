"""Implementation of `pix dedupe <path>`.

End-to-end flow per spec/dedupe.md: resolve root → CWD check → walk
library → bulk-read metadata → check prerequisites → group by hash →
plan → editor/apply prompt → apply (move losers to data/) → sweep
empty folders.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import typer

from pix import banner, debug
from pix.cache_base import prune_orphans, remove_all
from pix.checkout import CheckoutOpen, ensure_no_open_checkout
from pix.dedupe import (
    DedupeApplyError,
    MissingHashesError,
    UnmigratedFilesError,
    apply_plan,
    generate_plan,
    serialize_plan,
)
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
from pix.organize import CwdInsideLibraryError, check_cwd_not_inside
from pix.plan import Action
from pix.progress import LiveProgress
from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.scan import walk_source_files
from pix.schema import SCHEMA_VERSION, SchemaTooNew, SchemaUpgradeRequired


def dedupe_library(path: Path, no_prompt: bool = False) -> None:
    """End-to-end dedupe: resolve, plan, edit, confirm, apply.

    `no_prompt` skips the `Apply?` confirmation and applies the generated
    plan directly (the plan is still written). Used by `pix sync`; also
    exposed as `--no-prompt`.
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
        with acquire_lock(root, "dedupe"):
            _run_dedupe(root, no_prompt=no_prompt)
    except LockHeld as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e


def _run_dedupe(root: Path, no_prompt: bool = False) -> None:
    """Dedupe body, called under the library lock."""
    run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    runs_dir = root / ".pix" / "runs" / run_id
    runs_dir.mkdir(parents=True)
    plan_log_path = runs_dir / "plan.log"

    _plog(plan_log_path, f"Library root: {root}")

    # Walk is sub-second on libraries we care about; no console
    # ticker — the next phase's progress bar comes up immediately.
    # Timing still lands in plan.log. (Same pattern as migrate.)
    t0 = time.monotonic()
    scanned = walk_source_files(root)
    _plog(
        plan_log_path,
        f"Found {len(scanned)} file(s) in "
        f"{format_duration_precise(time.monotonic() - t0)}.",
    )

    if not scanned:
        typer.echo("Library is empty; nothing to dedupe.")
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
    for p in library_files:
        if p not in cache:
            cache[p] = FileMetadata(path=p, raw={"SourceFile": str(p)})
    _plog(
        plan_log_path,
        f"Read {len(cache)} file(s) in "
        f"{format_duration_precise(time.monotonic() - t0)} "
        f"({len(hits)} cache hits, {len(misses)} from ExifTool).",
    )

    # One parallel pass over the hash cache — feeds both the prereq
    # check (which files lack a hash) and the group-by-hash pass that
    # produces the dedupe plan. Previously each phase re-read every
    # hash sequentially, doubling the syscall cost.
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
            result = generate_plan(
                library_root=root,
                cache=cache,
                hashes=hashes,
                run_id=run_id,
                run_dir=runs_dir,
                plan_log=plan_log,
            )
    except UnmigratedFilesError as e:
        typer.echo(f"Error: {e}", err=True)
        _echo_sample(e.paths)
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
    plan_text = serialize_plan(
        source=root, result=result, library_root=root
    )
    plan_path.write_text(plan_text, encoding="utf-8")

    if not result.plan.lines:
        # Terse no-op output, matching migrate/hash/organize: skip the
        # Plan-written / Summary lines when there's nothing to apply.
        typer.echo("No duplicates found; nothing to do.")
        return

    dedup_total = sum(
        1 for ln in result.plan.lines if ln.action == Action.DEDUP
    )
    merge_total = sum(
        1 for ln in result.plan.lines if ln.action == Action.MERGE
    )
    typer.echo(f"Plan written: {plan_path}")
    typer.echo(
        f"Summary: {dedup_total} DEDUP, {merge_total} MERGE across "
        f"{len(result.groups)} group(s)."
    )

    kept_line_ids = {ln.line_id for ln in result.plan.lines}
    while not no_prompt:
        typer.echo("")
        choice = prompt_apply()
        if choice == "n":
            typer.echo("Aborted; plan file left in place.")
            return
        if choice == "e":
            open_in_editor(plan_path)
            edited_text = plan_path.read_text(encoding="utf-8")
            kept_line_ids = parse_kept_line_ids(edited_text)
            kept_dedup = sum(
                1
                for ln in result.plan.lines
                if ln.line_id in kept_line_ids and ln.action == Action.DEDUP
            )
            kept_merge = sum(
                1
                for ln in result.plan.lines
                if ln.line_id in kept_line_ids and ln.action == Action.MERGE
            )
            typer.echo("")
            typer.echo(
                f"After edit: {kept_dedup} DEDUP, {kept_merge} MERGE "
                f"(of {dedup_total} / {merge_total})."
            )
            continue
        break  # 'y'

    apply_log_path = runs_dir / "apply.log"
    try:
        try:
            removed, merged, quarantined = apply_plan(
                plan=result.plan,
                kept_line_ids=kept_line_ids,
                run_dir=runs_dir,
                library_root=root,
            )
        except DedupeApplyError as e:
            typer.echo(f"Error: apply failed: {e}", err=True)
            raise typer.Exit(code=1) from e

        # Cache mutation: a removed duplicate's sidecars all go away; a
        # merged (or quarantined) keeper's .meta/.hash are now stale, so
        # drop them to be re-derived next run. Both reduce to remove_all on
        # the applied line's path. Best-effort.
        for ln in result.plan.lines:
            if ln.line_id in kept_line_ids:
                remove_all(root, ln.abs_path)

        typer.echo("")
        typer.echo(
            f"Removed {removed} duplicate(s) across "
            f"{len(result.groups)} group(s)."
        )
        if merged:
            typer.echo(f"Merged tags onto {merged} keeper(s).")

        if quarantined:
            errors_dir = root / ".pix" / "errors"
            typer.echo("")
            typer.echo(
                f"{len(quarantined)} file(s) could not be processed "
                f"and were moved to {errors_dir}",
                err=True,
            )
            raise typer.Exit(code=1)
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
    ts = datetime.now().isoformat(timespec="seconds")
    with plan_log_path.open("a", encoding="utf-8") as f:
        f.write(f"{ts} {msg}\n")
