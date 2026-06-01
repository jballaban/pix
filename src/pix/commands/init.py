"""Implementation of `pix init`.

Creates a library root at <path> (or CWD) with a `.pix/` directory and a
`pix.yaml` settings file. Refuses to nest a library inside an existing one.
The format policy is a property of the pix build (see config.EXTENSION_POLICY),
not written per library; the library is version-less (no schema/upgrade).
"""

from __future__ import annotations

from pathlib import Path

import typer

from pix import banner
from pix.config import CONFIG_FILENAME

SYNC_REMINDER = """\
Reminder: exclude .pix/ from any file-sync clients (Synology Drive, OneDrive,
Dropbox, ...). .pix/runs/ accumulates full file captures from every migrate
run, which would roughly double cloud storage per run.
"""

# Seed contents of pix.yaml: just a header comment. Settings (runs_dir,
# organize.template) are added on demand — by hand or by `pix organize`.
_SETTINGS_HEADER = """\
# pix per-library settings. Optional keys:
#   runs_dir: D:\\some\\path        # relocate run folders (captures) off this volume
#   organize:
#     template: '{year}/{event}/{month}'   # set automatically by `pix organize`
"""


def init_library(path: Path | None) -> None:
    """Establish a library root at `path` (or CWD)."""
    banner()
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
    (pix_dir / CONFIG_FILENAME).write_text(_SETTINGS_HEADER, encoding="utf-8")

    typer.echo(f"Initialized pix library root at {target}.")
    typer.echo()
    typer.echo(SYNC_REMINDER)
