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
from pix.nas.const import IMPORT_ROOT, MASTER_DIR, MASTER_SHARE
from pix.nas.folder_import import FolderImportError, run_folder_import
from pix.nas.derive import run_process
from pix.nas.device_import import ImportError_, run_device_import
from pix.nas import ledger
from pix.nas.ledger import NasUnreachable
from pix.nas.lock import Locked, ProcessLock
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


@import_app.command("device")
def import_device(
    device: Annotated[str | None, typer.Option("--device", help=(
        "Select by serial or name substring. Omit to auto-select a single "
        "known device, or be asked."
    ))] = None,
    name: Annotated[str | None, typer.Option("--name", help=(
        "Override the staging folder name. Omit to use the remembered name, "
        "or be asked once for a new device."
    ))] = None,
) -> None:
    """Pull new media off a connected phone or camera into staging."""
    banner()
    try:
        summary = run_device_import(device=device, name=name, echo=typer.echo)
    except NasUnreachable as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    except ImportError_ as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    dev = summary.device
    typer.echo(
        f"{dev.friendly or dev.model} (serial {dev.serial}): "
        f"{summary.downloaded} new, {summary.skipped} already imported, "
        f"{summary.verified} verified."
    )
    typer.echo(f"Staging: {summary.landing}")

    if summary.needs_session:
        typer.echo("", err=True)
        typer.echo(
            f"{len(summary.needs_session)} file(s) need a device reconnect - "
            "unplug, replug, and re-run.", err=True)
    if summary.failed_media or summary.failed:
        for line in (summary.failed_media + summary.failed)[:10]:
            typer.echo(f"  {line}", err=True)
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
        # For the whole run, scan included. A second one decides the same files
        # are missing and spends the same minutes making them again.
        with ProcessLock(MASTER_SHARE):
            summary = run_process(echo=typer.echo)
    except Locked as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
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

    # Rebuild the index here rather than leaving it as a step to remember.
    # `process` is the last pipeline stage and knows new derived data exists,
    # and a rebuild is correct for whatever currently exists — so even a
    # cancelled run leaves a valid index rather than a stale one.
    _reindex(quiet=True)

    if summary.cancelled:
        typer.echo("")
        typer.echo("Cancelled. Re-run `pix2 process` to continue where it left off.")
        raise typer.Exit(code=130)
    if summary.failed:
        raise typer.Exit(code=1)


@app.command("index")
def index_cmd() -> None:
    """Rebuild the app's index from the meta tier and master's sidecars.

    Disposable and never authoritative — the record is master. Rebuilt in full
    because the input is small JSON rather than media, so a rebuild that is
    always correct beats an incremental path that can drift.
    """
    banner()
    try:
        ledger.require_share()
    except NasUnreachable as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    _reindex(quiet=False)


def _reindex(*, quiet: bool) -> None:
    """Rebuild the index, reporting unless it is a trailing step of another run."""
    from pix.nas import index as ix
    from pix.nas.const import INDEX_DB

    stats = ix.build(INDEX_DB, echo=lambda m: None)
    if quiet:
        typer.echo(f"indexed {stats.files:,} file(s)")
        return
    typer.echo(
        f"{stats.files:,} file(s), {stats.events} event(s), "
        f"{stats.with_date:,} dated, {stats.with_sidecar:,} with decisions"
    )
    typer.echo(f"Index: {INDEX_DB}")
    for line in stats.skipped[:10]:
        typer.echo(f"  {line}", err=True)


@app.command("passwd")
def passwd(name: str) -> None:
    """Print a hashed credential pair for hand-editing `app/users.json`.

    Accounts are normally managed in the app, under **Accounts**. This is
    the way back in if that is not reachable — a forgotten admin password
    with the container unable to serve the page it would be changed on.
    """
    banner()
    from pix.nas.auth import hash_password

    secret = typer.prompt(f"Password for {name}", hide_input=True,
                          confirmation_prompt=True)
    typer.echo("")
    typer.echo(f"{name}:{hash_password(secret)}")


@app.command("where")
def where() -> None:
    """Print the configured paths. There is no config file — these are constants."""
    banner()
    typer.echo(f"staging : {IMPORT_ROOT}")
    typer.echo(f"master  : {MASTER_DIR}")


def main() -> None:
    """Console-script entry point."""
    app()
