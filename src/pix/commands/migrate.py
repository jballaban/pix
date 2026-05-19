"""Implementation of `pix migrate` (Phase 2: plan generation only).

The full flow (cleanup pass → run folder → metadata cache → plan → editor →
confirm → apply) is specified in spec/migrate.md. This file implements
through the plan-generation step; editor open and apply land in Phase 3.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer

from pix.config import Config
from pix.metadata import (
    ExifToolFailed,
    ExifToolNotFound,
    FileMetadata,
    build_cache,
)
from pix.plan import Action, generate_plan, lookup_policy
from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.scan import walk_source_files


def migrate_folder(folder: Path, root_override: Path | None) -> None:
    """Generate a plan for migrating `folder` against the resolved library root.

    Does not apply. Writes plan.txt under `<library-root>/.pix/runs/<run-id>/`
    and prints the path + a summary.
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

    # ExifTool only returns entries for files it recognizes (images, videos,
    # PDFs, ...). Files marked with `delete` policy like Thumbs.db or
    # .DS_Store are skipped. Fill in bare entries so every source file gets
    # considered during plan generation.
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

    counts = plan.counts()
    convert = counts[Action.CONVERT_RENAME_TAG]
    rename = counts[Action.RENAME] + counts[Action.RENAME_TAG]
    tag = counts[Action.TAG] + counts[Action.RENAME_TAG] + convert
    delete = counts[Action.DELETE]

    typer.echo(f"Library root: {root}")
    typer.echo(f"Source:       {folder}")
    typer.echo(f"Plan written: {plan_path}")
    typer.echo(
        f"Summary: {len(plan.lines)} plan line(s) — "
        f"{convert} CONVERT, {rename} RENAME, {tag} TAG, {delete} DELETE."
    )
    typer.echo("")
    typer.echo(
        "Phase 2 generates plans only; review with your editor. Apply lands "
        "in Phase 3."
    )


def _validate_extensions(
    source_files: list[Path], config: Config
) -> None:
    """Fail-fast on unknown extensions per spec/migrate.md."""
    unknown: dict[str, Path] = {}  # ext (or filename) -> first example
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
