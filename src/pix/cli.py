"""pix CLI entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from pix.commands.init import init_library
from pix.commands.migrate import migrate_folder

app: typer.Typer = typer.Typer(
    name="pix",
    help="Personal media library management at terabyte scale.",
    add_completion=False,
    no_args_is_help=True,
)


@app.command("init")
def init(
    path: Annotated[
        Path | None,
        typer.Argument(
            help="Path to establish as the library root. Defaults to CWD.",
        ),
    ] = None,
) -> None:
    """Establish a library root, creating .pix/ with a default config.yaml."""
    init_library(path)


@app.command("migrate")
def migrate(
    folder: Annotated[
        Path,
        typer.Argument(
            help="Folder whose files should be normalized in place.",
        ),
    ],
    root: Annotated[
        Path | None,
        typer.Option(
            "--root",
            help="Override library-root resolution.",
            envvar="PIX_ROOT",
        ),
    ] = None,
) -> None:
    """Normalize files in <folder> per the library's policy (in-place, per-file)."""
    migrate_folder(folder=folder, root_override=root)
