"""Implementation of `pix organize <template>`.

End-to-end flow per spec/organize.md: parse template → check CWD →
walk library → bulk-read metadata → generate plan → editor/apply
prompt → apply → persist active template.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

import typer

from pix import banner, debug
from pix.cache_base import prune_orphans
from pix.checkout import CheckoutOpen, ensure_no_open_checkout
from pix.config import Config, set_organize_template, settings_path
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
    compute_values,
    generate_plan,
    parse_template,
    render_target_folder,
)
from pix.plan import PlanLine
from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.scan import walk_source_files


def organize_library(
    path: Path, template_str: str | None, no_prompt: bool = False
) -> None:
    """End-to-end organize: parse, plan, edit, confirm, apply.

    `path` resolves the library root (walk up for `.pix/`) **and scopes the
    operation**: only files at or under `path` are (re)organized. Pointing at
    the library root organizes everything (what `pix sync` does); pointing at a
    subfolder is a fast, targeted reshape of just that subtree — useful right
    after tagging a folderful of files. A scoped run still computes
    destinations against the whole library and pulls in the files already in
    those destination folders, so collisions resolve exactly as a full run.

    `no_prompt` skips the `Apply?` confirmation and applies the generated
    plan directly (the plan is still written). Used by `pix sync`; also
    exposed as `--no-prompt`.
    """
    path = path.resolve()
    try:
        root = resolve_root(start=path)
    except NoLibraryRoot as e:
        banner()
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    banner()

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

    config_path = settings_path(root)
    config = Config.load(config_path)  # validates current settings

    # No template given → re-apply the stored default shape (the
    # `organize.template` persisted by the last successful organize).
    # See spec/organize.md → Active template persistence.
    if template_str is None:
        template_str = config.organize_template
        if template_str is None:
            typer.echo(
                "Error: no template given and none stored yet. Run "
                "`pix organize <path> <template>` once to set the default "
                "shape; after that, bare `pix organize <path>` re-applies it.",
                err=True,
            )
            raise typer.Exit(code=1)

    try:
        template = parse_template(template_str)
    except OrganizeError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    try:
        with acquire_lock(root, "organize"):
            _run_organize(
                root, path, template, template_str, config_path,
                no_prompt=no_prompt,
            )
    except LockHeld as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e


def _augment_with_destination_folders(
    root: Path,
    template: Template,
    scanned: list[tuple[Path, int, int]],
    meta_cache: PerFileCache,
) -> list[tuple[Path, int, int]]:
    """Add the files already in the folders the scoped files will move into.

    A scoped organize only walks its subtree, so it can't see a file already
    sitting at a target name — and `_do_move` aborts on an occupied target.
    Including those occupants as candidates lets the normal collision rule
    suffix the incoming file around them; the occupants are usually already
    correctly placed, so they produce no move of their own.

    Destinations are computed from the same *fresh* metadata the plan uses —
    crucially via the full read (not cache-hits only): a file tagged moments
    ago (e.g. by the context menu's `set`) has a size-stale `.meta`, so a
    hits-only read would miss it and never scan its new destination, exactly
    the case that aborts the move. The read also warms the cache, so the main
    pass below re-hits instead of re-reading. ExifTool errors here are
    swallowed — the main pass surfaces them properly.
    """
    hits, misses = filter_cache_misses(scanned, meta_cache)
    fresh: dict[Path, FileMetadata] = {}
    if misses:
        try:
            fresh = read_metadata_batched(misses, cache=meta_cache)
        except (ExifToolNotFound, ExifToolFailed):
            return scanned  # main pass surfaces the error properly
    target_folders: set[Path] = {
        root / render_target_folder(template, compute_values(meta))
        for meta in {**hits, **fresh}.values()
    }

    already = {p for p, _, _ in scanned}
    extra: list[tuple[Path, int, int]] = []
    for folder in target_folders:
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    p = Path(entry.path)
                    if p in already:
                        continue
                    already.add(p)
                    st = entry.stat()
                    extra.append((p, st.st_size, st.st_mtime_ns))
        except OSError:
            continue  # destination folder doesn't exist yet → nothing to pull
    return scanned + extra


def _run_organize(
    root: Path,
    scope: Path,
    template: Template,
    template_str: str,
    config_path: Path,
    no_prompt: bool = False,
) -> None:
    """Organize body, called under the library lock.

    `scope` bounds which files are (re)organized: the whole library when it's
    the root, or a subtree otherwise (plus the destination folders those files
    target — see `_augment_with_destination_folders`)."""
    run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    runs_dir = root / ".pix" / "runs" / run_id
    runs_dir.mkdir(parents=True)
    plan_log_path = runs_dir / "plan.log"

    scoped = scope != root
    _plog(plan_log_path, f"Library root: {root}")
    if scoped:
        _plog(plan_log_path, f"Scope: {scope}")
    _plog(plan_log_path, f"Template: {template_str}")

    # Walk is sub-second on libraries we care about; no console
    # ticker — the next phase's progress bar comes up immediately.
    # Timing still lands in plan.log. (Same pattern as migrate/dedupe.)
    t0 = time.monotonic()
    scanned = walk_source_files(scope)
    meta_cache = PerFileCache.for_library(root)
    if scoped:
        # Pull in the files already living in the folders the scoped files will
        # move into, so cross-scope canonical-name collisions resolve (suffix)
        # against them instead of aborting the move (see _do_move). Occupants
        # are normally already correctly placed → idempotent (no move line).
        scanned = _augment_with_destination_folders(
            root, template, scanned, meta_cache
        )
    _plog(
        plan_log_path,
        f"Found {len(scanned)} file(s) in "
        f"{format_duration_precise(time.monotonic() - t0)}.",
    )

    if not scanned:
        typer.echo("Nothing to organize." if scoped else "Library is empty; nothing to organize.")
        return

    library_files = [p for p, _, _ in scanned]

    # Drop cache sidecars whose source files no longer exist. Scoped to the
    # walked subtree (`allowed_prefix`) so a subfolder organize never prunes
    # entries for files elsewhere in the library; root scope prunes the lot.
    prune_stats = prune_orphans(
        root, set(library_files), allowed_prefix=scope
    )
    if prune_stats.orphans_removed or prune_stats.legacy_removed:
        _plog(
            plan_log_path,
            f"Pruned {prune_stats.orphans_removed} orphan cache "
            f"sidecar(s) and {prune_stats.legacy_removed} legacy "
            f"sidecar(s) from .pix/cache/.",
        )

    t0 = time.monotonic()
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

    if len(plan.lines) == 0:
        # Terse no-op output, matching migrate/hash: skip the
        # Plan-written / Template / Summary lines when there's nothing to
        # apply. Still persist the active template so a bare
        # `pix organize <path>` re-applies this shape.
        typer.echo("Library already matches the template; nothing to do.")
        set_organize_template(config_path, template_str)
        return

    typer.echo(f"Plan written: {plan_path}")
    typer.echo(f"Template:     {template_str}")
    typer.echo(f"Summary: {_summarize(plan.lines)}")

    kept_line_ids = {ln.line_id for ln in plan.lines}
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

        # Cache sidecars (.meta/.hash/.video) are relocated inside
        # apply_plan, per scheduled move op — so they follow the media
        # through the same vacate-before-claim ordering and cycle-breaking
        # the moves use. Doing it here in plan order instead would clobber
        # sidecars whenever a folder's `_NNN` suffixes permute, forcing a
        # needless re-hash/re-probe next run. See organize.apply_plan.

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
