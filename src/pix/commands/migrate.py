"""Implementation of `pix migrate`.

End-to-end flow per spec/migrate.md: cleanup → metadata cache → plan-gen →
editor → confirm → apply. Phase 3 lands editor + confirm + apply for the
non-CONVERT cases (RENAME, DELETE, TAG, RENAME+TAG). CONVERT lines are
detected and skipped with a warning; Phase 4 lands the conversion code.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer

from pix.apply import ApplyError, apply_plan
from pix.cleanup import cleanup_rename_orphans
from pix.config import Config
from pix.editor import open_in_editor, parse_kept_line_ids
from pix.metadata import (
    ExifToolFailed,
    ExifToolNotFound,
    FileMetadata,
    build_cache,
)
from pix.plan import Action, PlanLine, generate_plan, lookup_policy
from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.scan import walk_source_files


def migrate_folder(
    folder: Path,
    root_override: Path | None,
    yes: bool = False,
) -> None:
    """End-to-end migrate: plan, edit, confirm, apply.

    `yes=True` skips both the editor and the Apply? prompt — used by tests
    and non-interactive runs. The original generated plan is applied as-is.
    """
    try:
        root = resolve_root(override=root_override)
    except NoLibraryRoot as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    folder = folder.resolve()
    if not folder.is_dir():
        typer.echo(f"Error: {folder} is not a directory.", err=True)
        raise typer.Exit(code=1)

    config = Config.load(root / ".pix" / "config.yaml")

    # Recover any intermediates from a prior crashed case-only rename
    # before walking the source.
    reverted = cleanup_rename_orphans(folder)
    if reverted:
        typer.echo(
            f"Recovered {len(reverted)} rename intermediate(s) from a prior run."
        )

    source_files = walk_source_files(folder)

    _validate_extensions(source_files, config)

    try:
        cache = build_cache(folder)
    except ExifToolNotFound as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    except ExifToolFailed as e:
        typer.echo(f"Error: exiftool failed.\n{e}", err=True)
        raise typer.Exit(code=1) from e

    for path in source_files:
        if path not in cache:
            cache[path] = FileMetadata(
                path=path, raw={"SourceFile": str(path)}
            )

    run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    plan = generate_plan(
        source=folder,
        cache=cache,
        config=config,
        run_id=run_id,
    )

    runs_dir = root / ".pix" / "runs" / run_id
    runs_dir.mkdir(parents=True)
    plan_path = runs_dir / "plan.txt"
    plan_path.write_text(plan.to_text(), encoding="utf-8")

    typer.echo(f"Library root: {root}")
    typer.echo(f"Source:       {folder}")
    typer.echo(f"Plan written: {plan_path}")
    typer.echo(f"Summary: {_summarize(plan.lines)}")

    if len(plan.lines) == 0:
        typer.echo("Nothing to do.")
        return

    if not yes:
        typer.echo("")
        open_in_editor(plan_path)

    edited_text = plan_path.read_text(encoding="utf-8")
    kept_line_ids = parse_kept_line_ids(edited_text)
    kept_lines = [ln for ln in plan.lines if ln.line_id in kept_line_ids]

    if not kept_lines:
        typer.echo("Plan empty after edit; nothing to apply.")
        return

    if not yes:
        typer.echo("")
        typer.echo(f"After edit: {_summarize(kept_lines)}")
        confirmed = typer.confirm("Apply?", default=False)
        if not confirmed:
            typer.echo("Aborted; plan file left in place.")
            return

    try:
        completed, skipped = apply_plan(
            plan=plan,
            plan_path=plan_path,
            run_dir=runs_dir,
            kept_line_ids=kept_line_ids,
        )
    except ApplyError as e:
        typer.echo(f"Error: apply failed: {e}", err=True)
        raise typer.Exit(code=1) from e

    typer.echo("")
    typer.echo(
        f"Applied {completed} action(s)"
        f"{f', skipped {skipped}' if skipped else ''}."
    )


def _summarize(lines: list[PlanLine]) -> str:
    """Render `N plan line(s) — X CONVERT, Y RENAME, Z TAG, W DELETE`."""
    counts: dict[Action, int] = {a: 0 for a in Action}
    for ln in lines:
        counts[ln.action] += 1
    convert = counts[Action.CONVERT_RENAME_TAG]
    rename = counts[Action.RENAME] + counts[Action.RENAME_TAG]
    tag = counts[Action.TAG] + counts[Action.RENAME_TAG] + convert
    delete = counts[Action.DELETE]
    return (
        f"{len(lines)} plan line(s) — "
        f"{convert} CONVERT, {rename} RENAME, {tag} TAG, {delete} DELETE."
    )


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
