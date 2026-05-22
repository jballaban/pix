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

from pix import debug
from pix.apply import ApplyError, apply_plan
from pix.cleanup import (
    CleanupError,
    cleanup_migrate_markers,
    cleanup_rename_orphans,
)
from pix.config import Config
from pix.editor import open_in_editor, parse_kept_line_ids, prompt_apply
from pix.metadata import (
    ExifToolFailed,
    ExifToolNotFound,
    FileMetadata,
    build_cache,
)
from pix.plan import Action, PlanLine, generate_plan, lookup_policy
from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.scan import walk_source_files
from pix.schema import SCHEMA_VERSION, SchemaTooNew


def migrate_folder(folder: Path) -> None:
    """End-to-end migrate: plan, edit, confirm, apply.

    Per-file plan-generation reasoning streams to `<run-dir>/debug.log`
    on every run (see `pix.debug`). Constant memory; no flag.
    """
    folder = folder.resolve()
    try:
        root, schema = resolve_root(start=folder)
    except NoLibraryRoot as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    except SchemaTooNew as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    if schema.archived_from is not None:
        typer.echo(
            f"Library schema was v{schema.archived_from}; this pix expects "
            f"v{SCHEMA_VERSION}. Archived prior .pix/ contents to "
            f".pix/archive/v{schema.archived_from}/ and reset to defaults. "
            f"Inspect that folder to recover any customizations."
        )

    if not folder.is_dir():
        typer.echo(f"Error: {folder} is not a directory.", err=True)
        raise typer.Exit(code=1)

    config = Config.load(root / ".pix" / "config.yaml")

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
    # source. Order matters: rename intermediates first (they're simple
    # path-only ops); then CONVERT markers (which may need ExifTool reads).
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

    t0 = time.monotonic()
    _plog(plan_log_path, "Walking source folder...")
    source_files = walk_source_files(folder)
    _plog(
        plan_log_path,
        f"Found {len(source_files)} files in {time.monotonic() - t0:.1f}s.",
    )

    _validate_extensions(source_files, config)

    t0 = time.monotonic()
    _plog(
        plan_log_path,
        f"Reading metadata from {len(source_files)} files (one ExifTool call)...",
    )
    try:
        cache = build_cache(folder)
    except ExifToolNotFound as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    except ExifToolFailed as e:
        typer.echo(f"Error: exiftool failed.\n{e}", err=True)
        raise typer.Exit(code=1) from e
    _plog(
        plan_log_path,
        f"Read {len(cache)} files in {time.monotonic() - t0:.1f}s.",
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
        f"Plan generated in {time.monotonic() - t0:.1f}s.",
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

    try:
        completed, skipped = apply_plan(
            plan=plan,
            plan_path=plan_path,
            run_dir=runs_dir,
            kept_line_ids=kept_line_ids,
            staging_dir=staging_dir,
        )
    except ApplyError as e:
        typer.echo(f"Error: apply failed: {e}", err=True)
        raise typer.Exit(code=1) from e

    typer.echo("")
    typer.echo(
        f"Applied {completed} action(s)"
        f"{f', skipped {skipped}' if skipped else ''}."
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
