"""Implementation of `pix import` (see spec/import.md).

Pulls new photos/videos off a connected phone (USB / WPD) and lands them,
verified, under `.pix/local/import/<device>/`. Device→disk only: nothing
consumes the landed files yet (the migrate ingest seam is deferred — see
spec/import.md → Ingestion seam). Runs under the library write lock.
"""

from __future__ import annotations

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
    name: str | None = None,
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
            summary = run_import(root, device=device, name=name, dry_run=True)
        except ImportError_ as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1) from e
        seeded = (
            f", {summary.seed_skipped} skipped via manifest"
            if summary.seed_skipped else ""
        )
        typer.echo(
            f"{summary.device.friendly or summary.device.model} "
            f"(serial {summary.device.serial}): "
            f"{summary.downloaded} new, {summary.skipped} already imported{seeded}."
        )
        _warn_manifests_deprecated(summary)
        return

    try:
        with acquire_lock(root, "import"):
            _run(root, device=device, name=name)
    except LockHeld as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    except ImportError_ as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e


def _warn_manifests_deprecated(summary: object) -> None:
    """When the seed-manifest folder is present but empty, every device's list
    has been used up — nudge to delete the now-dead lookup code."""
    if getattr(summary, "manifests_deprecated", False):
        typer.echo(
            "Note: .pix/import-manifests/ is empty — all import-seed manifests "
            "have been consumed. The manifest-skip lookup is now dead code and "
            "can be removed.",
            err=True,
        )


def _run(root: Path, *, device: str | None, name: str | None) -> None:
    config = Config.load(settings_path(root))
    # run_import creates the run folder only after a device is selected+named, so
    # a selection error leaves no empty run folder behind.
    summary = run_import(
        root, device=device, name=name, runs_base=config.runs_base(root)
    )
    if summary.apply_log is not None:
        typer.echo(f"Log: {summary.apply_log}")

    typer.echo("")
    typer.echo(
        f"Device: {summary.device.friendly} (serial {summary.device.serial})"
    )
    typer.echo(f"Landed: {summary.landing}")
    recovered = (
        f", recovered {summary.recovered}" if summary.recovered else ""
    )
    seeded = (
        f", {summary.seed_skipped} skipped via manifest"
        if summary.seed_skipped else ""
    )
    typer.echo(
        f"Downloaded {summary.downloaded} file(s) "
        f"({format_size(summary.bytes_downloaded)}), "
        f"verified {summary.verified}, skipped {summary.skipped}{recovered}{seeded}, "
        f"in {summary.passes} pass(es)."
    )
    _warn_manifests_deprecated(summary)
    if summary.needs_session:
        typer.echo("")
        typer.echo(
            f"{len(summary.needs_session)} file(s) failed to validate and need a "
            "device reconnect to retry — unplug, replug, and re-run:"
        )
        for p in summary.needs_session:
            typer.echo(f"  {p}")
    if summary.failed_media:
        typer.echo("")
        typer.echo(
            f"{len(summary.failed_media)} file(s) could not be validated even after "
            "a reconnect — likely damaged on the device; resolve them there "
            "(re-export / delete), then re-run:",
            err=True,
        )
        for p in summary.failed_media:
            typer.echo(f"  {p}", err=True)
    if summary.device_lost:
        typer.echo("")
        typer.echo(
            "Device disconnected mid-run. Progress above is saved — replug and "
            "re-run to resume (verified files are skipped, partial downloads "
            "re-pulled).",
            err=True,
        )
        raise typer.Exit(code=1)
    if summary.failed:
        typer.echo(f"{len(summary.failed)} file(s) FAILED:", err=True)
        for p in summary.failed:
            typer.echo(f"  {p}", err=True)
        raise typer.Exit(code=1)
    if summary.failed_media:
        # Terminal validation failures need user action → nonzero exit (the
        # detail was already printed above).
        raise typer.Exit(code=1)
