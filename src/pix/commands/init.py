"""Implementation of `pix init`.

Creates a library root at <path> (or CWD), writes a default `config.yaml` into
`<path>/.pix/`. Refuses to nest a library inside an existing one.
"""

from __future__ import annotations

from pathlib import Path

import typer

from pix.config import DEFAULT_CONFIG_YAML

SYNC_REMINDER = """\
Reminder: exclude .pix/ from any file-sync clients (Synology Drive, OneDrive,
Dropbox, ...). .pix/runs/ accumulates full file captures from every migrate
run, which would roughly double cloud storage per run.
"""


def init_library(path: Path | None) -> None:
    """Establish a library root at `path` (or CWD)."""
    target = (path or Path.cwd()).resolve()

    pix_dir = target / ".pix"
    if pix_dir.exists():
        typer.echo(
            f"Error: {target} is already a pix library root ({pix_dir} exists).",
            err=True,
        )
        raise typer.Exit(code=1)

    for ancestor in target.parents:
        if (ancestor / ".pix").is_dir():
            typer.echo(
                f"Error: cannot init at {target}; it is nested inside library "
                f"root {ancestor}. Nested libraries are not supported.",
                err=True,
            )
            raise typer.Exit(code=1)

    target.mkdir(parents=True, exist_ok=True)
    pix_dir.mkdir()
    (pix_dir / "config.yaml").write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")

    typer.echo(f"Initialized pix library root at {target}")
    typer.echo()
    typer.echo(SYNC_REMINDER)
