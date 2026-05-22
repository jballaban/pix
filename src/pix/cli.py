"""pix CLI entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from pix import __version__
from pix.commands.init import init_library
from pix.commands.migrate import migrate_folder
from pix.commands.organize import organize_library

app: typer.Typer = typer.Typer(
    name="pix",
    help="Personal media library management at terabyte scale.",
    add_completion=False,
    no_args_is_help=True,
)


@app.callback()
def _print_version() -> None:  # pyright: ignore[reportUnusedFunction]
    """Top-level callback — fires before every subcommand.

    Prints the running version as the first line so dev and tester are
    always certain which build is executing. Doesn't fire for `pix --help`
    or `pix` with no args (typer short-circuits to the help screen).
    """
    typer.echo(f"pix {__version__}")


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
            help=(
                "Folder whose files should be normalized in place. The "
                "library is resolved by walking up from this folder, then "
                "falling back to $PIX_ROOT or CWD."
            ),
        ),
    ],
) -> None:
    """Normalize files in <folder> per the library's policy (in-place, per-file)."""
    migrate_folder(folder=folder)


@app.command("organize")
def organize(
    path: Annotated[
        Path,
        typer.Argument(
            help=(
                "Path inside (or at) the library root. The library is "
                "resolved by walking up from this path. `.` for CWD."
            ),
        ),
    ],
    template: Annotated[
        str,
        typer.Argument(
            help=(
                "Folder template, e.g. '{year}/{month}/{event}'. Tokens: "
                "{year}, {month}, {day}, {date}, {event}. Levels separated "
                "by `/`. Persisted as the active template on successful apply."
            ),
        ),
    ],
) -> None:
    """Re-shape the library to match a folder template (library-wide MOVE)."""
    organize_library(path=path, template_str=template)
