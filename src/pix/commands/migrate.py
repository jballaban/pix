"""Implementation of `pix migrate` (stub for Phase 1).

Phase 1 wires up library-root resolution and source-folder validation only.
The actual migration logic (plan generation, apply, marker handling, content
hashing, ...) lands in subsequent phases.
"""

from __future__ import annotations

from pathlib import Path

import typer

from pix.root import NoLibraryRoot, resolve as resolve_root


def migrate_folder(folder: Path, root_override: Path | None) -> None:
    """Stub: validate the library root and source folder, then exit."""
    try:
        root = resolve_root(override=root_override)
    except NoLibraryRoot as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    folder = folder.resolve()
    if not folder.is_dir():
        typer.echo(f"Error: {folder} is not a directory.", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Library root: {root}")
    typer.echo(f"Source folder: {folder}")
    typer.echo(
        "(migrate is not yet implemented; only library-root resolution is wired "
        "up at this phase.)"
    )
