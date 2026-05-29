"""pix CLI entry point."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from pix.commands.checkout import run_checkout
from pix.commands.dedupe import dedupe_library
from pix.commands.hash import hash_library
from pix.commands.init import init_library
from pix.commands.meta import meta_file
from pix.commands.migrate import migrate_folder
from pix.commands.organize import organize_library
from pix.commands.upgrade import upgrade_library

app: typer.Typer = typer.Typer(
    name="pix",
    help="Personal media library management at terabyte scale.",
    add_completion=False,
    no_args_is_help=True,
)


def main() -> None:
    """Real entry point. Catches Ctrl-C so the user gets a clean exit
    instead of a multi-frame rich traceback.

    Unix convention: SIGINT exits with code 130. The active line was
    already logged as `Interrupted` to apply.log by the apply loop, so
    the user can tail that file to see where we stopped.
    """
    try:
        app()
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        sys.exit(130)


# The version banner now lives in each command (via `pix.banner`) so
# it can be printed as a single line that includes the resolved
# library's schema version when applicable. See `pix.__init__`.


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
        str | None,
        typer.Argument(
            help=(
                "Folder template, e.g. '{year}/{month}/{event}'. Tokens: "
                "{year}, {month}, {day}, {date}, {event}. Levels separated "
                "by `/`. Persisted as the active template on successful "
                "apply. Omit to re-apply the stored default shape."
            ),
        ),
    ] = None,
) -> None:
    """Re-shape the library to match a folder template (library-wide MOVE)."""
    organize_library(path=path, template_str=template)


@app.command("meta")
def meta(
    path: Annotated[
        Path,
        typer.Argument(
            help="File to inspect (read-only; shows date sources + tags)."
        ),
    ],
) -> None:
    """Show pix's date candidates and notable tags for one file."""
    meta_file(path)


@app.command("checkout")
def checkout(
    path: Annotated[
        Path | None,
        typer.Argument(
            help=(
                "Folder to scope the checkout to (like `pix migrate "
                "<folder>`). Resolves the library root and bounds the "
                "file set. `.` for CWD. Required when starting a checkout; "
                "omit it (and the template) for --commit / --reset / status."
            ),
        ),
    ] = None,
    template: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Folder template, e.g. '{year}/{event}'. Tokens: {year}, "
                "{month}, {day}, {date}, {event}. Required when starting."
            ),
        ),
    ] = None,
    commit: Annotated[
        bool,
        typer.Option("--commit", help="Apply the open checkout's tag edits."),
    ] = False,
    reset: Annotated[
        bool,
        typer.Option("--reset", help="Discard the open checkout."),
    ] = False,
) -> None:
    """Edit tags by shuffling a hard-link workspace (scoped to <path>)."""
    run_checkout(path, template, commit=commit, reset=reset)


@app.command("hash")
def hash_(
    path: Annotated[
        Path,
        typer.Argument(
            help=(
                "Path inside (or at) the library root. The library is "
                "resolved by walking up from this path. Hash operates on "
                "every file under the library; subfolder scope is not "
                "supported in v1."
            ),
        ),
    ],
) -> None:
    """Populate the per-file content-hash cache for every stale/missing entry."""
    hash_library(path=path)


@app.command("dedupe")
def dedupe(
    path: Annotated[
        Path,
        typer.Argument(
            help=(
                "Path inside (or at) the library root. The library is "
                "resolved by walking up from this path. `.` for CWD."
            ),
        ),
    ],
) -> None:
    """Remove duplicate files sharing the same `pix:ContentHash` (library-wide)."""
    dedupe_library(path=path)


@app.command("upgrade")
def upgrade(
    path: Annotated[
        Path,
        typer.Argument(
            help=(
                "Path inside (or at) the library root to upgrade. The "
                "library is resolved by walking up from this path."
            ),
        ),
    ],
) -> None:
    """Archive the library's prior .pix/ contents and reset to current defaults."""
    upgrade_library(path=path)
