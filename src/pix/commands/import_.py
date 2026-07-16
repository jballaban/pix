"""Implementation of `pix import` (see spec/import.md).

Pulls new photos/videos off a connected phone (USB / WPD) and lands them,
verified, under `.pix/local/import/<device>/`. Device→disk only: nothing
consumes the landed files yet (the migrate ingest seam is deferred — see
spec/import.md → Ingestion seam). Runs under the library write lock.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer

from pix import banner
from pix.config import Config, settings_path
from pix.duration import format_size
from pix.importer import ImportError_, run_import
from pix.library_lock import LockHeld, acquire as acquire_lock
from pix.root import NoLibraryRoot, resolve as resolve_root


def import_library(
    path: Path,
    *,
    device: str | None = None,
    dry_run: bool = False,
) -> None:
    """Resolve root, take the lock, and run the device→disk import."""
    path = path.resolve()
    try:
        root = resolve_root(start=path)
    except NoLibraryRoot as e:
        banner()
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    banner()

    if dry_run:
        # Read-only: no lock, no run folder.
        try:
            summary = run_import(root, device=device, dry_run=True)
        except ImportError_ as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1) from e
        typer.echo(
            f"{summary.device.friendly or summary.device.model} "
            f"(serial {summary.device.serial}): "
            f"{summary.downloaded} new, {summary.skipped} already imported."
        )
        return

    try:
        with acquire_lock(root, "import"):
            _run(root, device=device)
    except LockHeld as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    except ImportError_ as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e


def _run(root: Path, *, device: str | None) -> None:
    config = Config.load(settings_path(root))
    run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    runs_dir = config.runs_base(root) / run_id
    runs_dir.mkdir(parents=True, exist_ok=True)
    apply_log = runs_dir / "apply.log"

    try:
        with apply_log.open("a", encoding="utf-8") as log:
            summary = run_import(root, device=device, log=log)
    finally:
        typer.echo(f"Log: {apply_log}")

    typer.echo("")
    typer.echo(
        f"Device: {summary.device.friendly} (serial {summary.device.serial})"
    )
    typer.echo(f"Landed: {summary.landing}")
    typer.echo(
        f"Downloaded {summary.downloaded} file(s) "
        f"({format_size(summary.bytes_downloaded)}), "
        f"verified {summary.verified}, skipped {summary.skipped}, "
        f"in {summary.passes} pass(es)."
    )
    if summary.failed:
        typer.echo(f"{len(summary.failed)} file(s) FAILED:", err=True)
        for p in summary.failed:
            typer.echo(f"  {p}", err=True)
        raise typer.Exit(code=1)
