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

from pix import __version__, banner, debug
from pix.apply import ApplyError, apply_plan
from pix.checkout import CheckoutOpen, ensure_no_open_checkout
from pix.cleanup import (
    CleanupError,
    cleanup_exiftool_tmp,
    cleanup_migrate_markers,
    cleanup_rename_orphans,
    wipe_staging,
)
from pix.cache_base import prune_orphans, relocate_all, remove_all
from pix.config import Config
from pix.convert import VideoProfile, probe_videos_parallel
from pix.duration import format_duration_precise
from pix.errors import restore_stale_errors
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
from pix.plan import (
    Action,
    Plan,
    PlanLine,
    canonical_extension,
    generate_plan,
    lookup_policy,
)
from pix.progress import LiveProgress
from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.scan import walk_source_files
from pix.schema import SCHEMA_VERSION, SchemaTooNew, SchemaUpgradeRequired
from pix.video_cache import read_all_cached_profiles, write_cached_profile


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

    try:
        ensure_no_open_checkout(root)
    except CheckoutOpen as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

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

    # Restore previously-quarantined files whose errorinfo predates the
    # running pix version — a code change since the failure means the
    # same input may now succeed. Files quarantined by THIS version are
    # left in place (retrying same code would fail again). Sidecars
    # missing `pix_version` (pre-v0.1.86) are treated as stale.
    restored, restore_skipped, kept_same_version = restore_stale_errors(root)
    if restored:
        _plog(
            plan_log_path,
            f"Restored {len(restored)} file(s) from .pix/errors/ "
            f"(written by an older pix version; will be re-attempted "
            f"this run).",
        )
        for entry in restored:
            _plog(
                plan_log_path,
                f"  restored {entry.original_path} "
                f"(prior version: {entry.sidecar_pix_version or '<unknown>'})",
            )
    if restored:
        # Mirror to the console — these files re-enter this run's plan, so
        # the user should know why work appeared for previously-failed files.
        typer.echo(
            f"Restored {len(restored)} file(s) from .pix/errors/ "
            f"(quarantined by an older pix version) — retrying this run."
        )
    if kept_same_version:
        _plog(
            plan_log_path,
            f"Left {kept_same_version} file(s) in .pix/errors/ unchanged "
            f"(same pix version that quarantined them — retrying would "
            f"hit the same failure).",
        )
        # Surface to the console too. Without this, a library whose only
        # outstanding work is a quarantined file just prints "Nothing to
        # do." with no hint the file exists or why it's being skipped.
        errors_dir = root / ".pix" / "errors"
        typer.echo(
            f"{kept_same_version} file(s) remain quarantined in {errors_dir} "
            f"after failing CONVERT under pix {__version__}. They are not "
            f"retried automatically — the same code would fail again. Check "
            f"each .errorinfo sidecar for the error; once the input is fixed "
            f"(or a newer pix ships), restore the file to its source path and "
            f"re-run to retry.",
            err=True,
        )
    if restore_skipped:
        _plog(
            plan_log_path,
            f"Could not restore {len(restore_skipped)} errorinfo entr(ies); "
            f"see details below:",
        )
        for skip in restore_skipped:
            _plog(plan_log_path, f"  {skip.entry_path}: {skip.reason}")
        # These need a human — a missing/occupied/unreadable entry won't
        # resolve itself on a re-run. Echo to the console, not just plan.log.
        typer.echo(
            f"Could not restore {len(restore_skipped)} entr(ies) from "
            f".pix/errors/ — needs attention:",
            err=True,
        )
        for skip in restore_skipped:
            typer.echo(f"  {skip.entry_path.name}: {skip.reason}", err=True)

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

    source_files = [p for p, _, _ in scanned]
    _validate_extensions(source_files, config)

    # Drop cache sidecars whose source files no longer exist under
    # the walked folder. Scoped to `folder` so we don't touch caches
    # for files outside this migrate's source. Also sweeps legacy
    # suffixes from older pix versions (currently `.cache` → renamed
    # to `.meta` in v0.1.88).
    expected_paths = {p for p in source_files}
    prune_stats = prune_orphans(
        root, expected_paths, allowed_prefix=folder
    )
    if prune_stats.orphans_removed or prune_stats.legacy_removed:
        _plog(
            plan_log_path,
            f"Pruned {prune_stats.orphans_removed} orphan cache "
            f"sidecar(s) and {prune_stats.legacy_removed} legacy "
            f"sidecar(s) from .pix/cache/.",
        )

    # Cache lookup + ExifTool reads. With the per-file cache, the
    # second pass typically has few misses; only those need ExifTool.
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

    # Windows-playability probe per spec/migrate.md → Windows playability
    # check. Probe every keep-policy mp4/m4v candidate so plan-gen knows
    # whether to route to CONVERT for re-encode. Cached at
    # <library>/.pix/cache/<...>.video keyed on (size, mtime_ns), so
    # subsequent runs over an unchanged library skip the ffprobe pass
    # entirely. Source files matching `convert_to_mp4` policy already go
    # through CONVERT and re-probe internally at apply time, so we don't
    # cache or pre-probe them here.
    video_candidates_meta = [
        (p, sz, mt) for p, sz, mt in scanned
        if lookup_policy(p.name, config.extensions) == "keep"
        and canonical_extension(p.suffix.lstrip(".")) == "mp4"
    ]
    video_profiles: dict[Path, VideoProfile | None] = {}
    if video_candidates_meta:
        t0 = time.monotonic()
        _plog(
            plan_log_path,
            f"Probing {len(video_candidates_meta)} video(s) for playability...",
        )
        # Parallel cache lookup first.
        with LiveProgress(
            total=len(video_candidates_meta)
        ) as cache_progress:
            cache_progress.begin("Loading video cache")

            def _on_cache_batch(n: int) -> None:
                cache_progress.advance(by=n)

            cached_profiles = read_all_cached_profiles(
                root, video_candidates_meta, on_batch=_on_cache_batch
            )

        # Probe only the misses; write fresh results back to the cache.
        miss_meta = [
            (p, sz, mt) for p, sz, mt in video_candidates_meta
            if cached_profiles.get(p) is None
        ]
        hits = sum(
            1 for p in cached_profiles if cached_profiles[p] is not None
        )
        fresh_profiles: dict[Path, VideoProfile | None] = {}
        if miss_meta:
            with LiveProgress(total=len(miss_meta)) as probe_progress:
                probe_progress.begin("Probing videos")

                def _on_probe_batch(n: int) -> None:
                    probe_progress.advance(by=n)

                fresh_profiles = probe_videos_parallel(
                    [p for p, _, _ in miss_meta],
                    on_batch=_on_probe_batch,
                )
            # Persist fresh probes — successful ones only (a failed
            # probe yields None; we'd re-probe on next run anyway, and
            # caching None would mask a transient ffprobe error).
            mt_by_path = {p: (sz, mt) for p, sz, mt in miss_meta}
            for p, profile in fresh_profiles.items():
                if profile is None:
                    continue
                sz, mt = mt_by_path[p]
                write_cached_profile(
                    root, p, profile=profile, size=sz, mtime_ns=mt
                )

        # Merge hits (non-None entries from cache) + fresh probes.
        video_profiles = {
            p: v for p, v in cached_profiles.items() if v is not None
        }
        video_profiles.update(fresh_profiles)
        _plog(
            plan_log_path,
            f"Probed {len(video_profiles)} video(s) in "
            f"{format_duration_precise(time.monotonic() - t0)} "
            f"({hits} cache hit(s), {len(miss_meta)} from ffprobe).",
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
            video_profiles=video_profiles,
        )
    _plog(
        plan_log_path,
        f"Plan generated in {format_duration_precise(time.monotonic() - t0)}.",
    )

    plan_path = runs_dir / "plan.txt"
    plan_path.write_text(plan.to_text(), encoding="utf-8")

    if len(plan.lines) == 0:
        typer.echo("Nothing to do.")
        return

    typer.echo(f"Plan written: {plan_path}")
    typer.echo(f"Summary: {_summarize(plan.lines)}")

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

    Walks the applied plan lines and reflects each into ALL cache
    sidecars (.meta/.hash/.video), not just .meta — a pure RENAME leaves
    bytes/size/mtime untouched, so the .hash/.video entries stay valid
    and must travel with the file instead of being orphaned and pruned:
    - DELETE / STASH: removes every sidecar (file gone).
    - CONVERT+RENAME+TAG: removes every old sidecar; the new file's
      .meta was already written inside apply (via the live ExifTool
      session); its .hash/.video are recomputed by a later pix hash /
      migrate re-probe.
    - RENAME: relocates every sidecar alongside the media rename.
    - TAG: merges the written pix:* fields into the cached .meta
      in place (the tag write changed mtime, so the .hash/.video at this
      path are now stale — left for validation to reject on next read).
    - RENAME+TAG: relocate every sidecar first (apply already moved the
      file), then update_metadata using `target_path` so cache.add
      stats the file at its current location. Calling update_metadata
      with abs_path here would silently fail the stat and leave the
      cache entry with the pre-tag size — guaranteed mismatch next run.
    """
    root = cache.library_root
    for ln in plan.lines:
        if ln.line_id not in kept_line_ids:
            continue
        if ln.action == Action.DELETE:
            remove_all(root, ln.abs_path)
        elif ln.action == Action.STASH:
            remove_all(root, ln.abs_path)
        elif ln.action == Action.CONVERT_RENAME_TAG:
            remove_all(root, ln.abs_path)  # old file gone; new .meta written in apply
        elif ln.action == Action.RENAME:
            if ln.target_path is not None:
                relocate_all(root, ln.abs_path, ln.target_path)
        elif ln.action == Action.TAG:
            if ln.pix_writes:
                cache.update_metadata(ln.abs_path, dict(ln.pix_writes))
        elif ln.action == Action.RENAME_TAG:
            if ln.target_path is not None:
                relocate_all(root, ln.abs_path, ln.target_path)
                if ln.pix_writes:
                    cache.update_metadata(
                        ln.target_path, dict(ln.pix_writes)
                    )


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
