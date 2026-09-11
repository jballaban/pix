"""`pix2` — the NAS architecture's console script (spec/nas-app.md).

A second entry point so the existing `pix` keeps working untouched until seeding
is proven. When the old architecture is amputated, this package is promoted to
`src/pix/` and `pix2` disappears.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from pix import banner
from pix.nas.const import IMPORT_ROOT, MASTER_DIR
from pix.nas.folder_import import FolderImportError, run_folder_import
from pix.nas.derive import run_process
from pix.nas.ledger import NasUnreachable
from pix.nas.upload import run_upload

app: typer.Typer = typer.Typer(
    name="pix2",
    help="Personal media archive: import, upload, process. See spec/nas-app.md.",
    add_completion=False,
    no_args_is_help=True,
)

import_app: typer.Typer = typer.Typer(
    name="import",
    help="Pull media into staging, from a device or a folder.",
    no_args_is_help=True,
)
app.add_typer(import_app, name="import")


@import_app.command("folder")
def import_folder(
    source: Annotated[Path, typer.Argument(help="Folder to import from.")],
    name: Annotated[str, typer.Option("--name", help=(
        "Staging folder name, and the prefix of the eventual master folder "
        "(e.g. --name legacy_2015 gives legacy_2015_<upload-time>)."
    ))],
) -> None:
    """Stage a folder tree for upload (SD card, shared folder, legacy library).

    Hardlinks into staging where it can, so re-running is cheap and a cancelled
    run resumes without redoing work.
    """
    banner()
    try:
        summary = run_folder_import(source, name, echo=typer.echo)
    except NasUnreachable as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    except FolderImportError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    typer.echo(
        f"{summary.name}: {summary.landed} staged "
        f"({summary.linked} linked, {summary.copied} copied, "
        f"{summary.adopted} adopted), {summary.skipped} already staged, "
        f"{summary.ignored} ignored."
    )
    typer.echo(f"Staging: {summary.staging}")

    if summary.failed:
        typer.echo(f"\n{len(summary.failed)} file(s) failed:", err=True)
        for line in summary.failed[:20]:
            typer.echo(f"  {line}", err=True)
        if len(summary.failed) > 20:
            typer.echo(f"  ... and {len(summary.failed) - 20} more", err=True)
        raise typer.Exit(code=1)


@app.command("upload")
def upload() -> None:
    """Send every pending staging folder to master, then clear what verified.

    Clearing staging is the only destructive step in the pipeline, so it happens
    per-folder and only after every file is confirmed present at master.
    """
    banner()
    try:
        summaries = run_upload(echo=typer.echo)
    except NasUnreachable as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    failed = 0
    cancelled = False
    for s in summaries:
        gb = s.bytes_copied / (1024 ** 3)
        if s.cancelled:
            cancelled = True
            state = "KEPT (cancelled)"
        elif s.staging_cleared:
            state = "cleared"
        else:
            state = "KEPT (unverified)"
        typer.echo(
            f"{s.name}: {s.copied} copied ({gb:.1f} GB), {s.skipped} already there, "
            f"{s.culled} culled -> {s.master_folder.name}; staging {state}"
        )
        for line in s.failed[:10]:
            typer.echo(f"  {line}", err=True)
        if len(s.failed) > 10:
            typer.echo(f"  ... and {len(s.failed) - 10} more", err=True)
        failed += len(s.failed)

    if cancelled:
        typer.echo("")
        typer.echo(
            "Cancelled. Staging is intact and nothing was deleted - "
            "re-run `pix2 upload` to continue into the same master folder."
        )
        raise typer.Exit(code=130)
    if failed:
        raise typer.Exit(code=1)


@app.command("process")
def process() -> None:
    """Generate missing thumbnails and previews from master.

    Resumable by construction: it makes what is missing, and missing is
    recomputed every run.
    """
    banner()
    try:
        summary = run_process(echo=typer.echo)
    except NasUnreachable as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    typer.echo(
        f"{summary.thumbs} thumbnail(s), {summary.previews} preview(s), "
        f"{summary.skipped} already done, {summary.unsupported} unsupported"
    )
    for line in summary.failed[:10]:
        typer.echo(f"  {line}", err=True)
    if len(summary.failed) > 10:
        typer.echo(f"  ... and {len(summary.failed) - 10} more", err=True)

    if summary.cancelled:
        typer.echo("")
        typer.echo("Cancelled. Re-run `pix2 process` to continue where it left off.")
        raise typer.Exit(code=130)
    if summary.failed:
        raise typer.Exit(code=1)


@app.command("where")
def where() -> None:
    """Print the configured paths. There is no config file — these are constants."""
    banner()
    typer.echo(f"staging : {IMPORT_ROOT}")
    typer.echo(f"master  : {MASTER_DIR}")


def main() -> None:
    """Console-script entry point."""
    app()
