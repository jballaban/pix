"""Implementation of `pix init`.

Creates a library root at <path> (or CWD) with a `.pix/` directory and a
`pix.yaml` settings file. Refuses to nest a library inside an existing one.
The format policy is a property of the pix build (see config.EXTENSION_POLICY),
not written per library; the library is version-less (no schema/upgrade).
"""

from __future__ import annotations

from pathlib import Path

import typer

from pix import banner, sync_check
from pix.config import CONFIG_FILENAME
from pix.root import local_dir

SYNC_REMINDER = """\
Reminder for file-sync clients (Synology Drive, OneDrive, Dropbox, ...):
exclude .pix/local/ from syncing. It holds regenerable caches (cache.db), the
machine-local lock, and transient workspaces (staging, checkout) — syncing it
risks cache corruption and wasted churn. The durable data (.pix/runs holds
full run captures, .pix/errors and .pix/stash hold only-copy files) may be
synced or backed up, but note .pix/runs roughly doubles storage per migrate run.

Also add two filename exclude rules for pix's transient markers, which live
briefly in the media tree during runs (safe to exclude — never the sole copy
of data):
    *.__*            (pix's own markers: rename/convert/organize/rotate)
    *_exiftool_tmp   (ExifTool's atomic-write temp)
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
    # Create local/ up front so it exists to be deselected in a sync client
    # before the first run (you can't exclude a folder that isn't there yet).
    local_dir(target).mkdir()
    (pix_dir / CONFIG_FILENAME).write_text(_SETTINGS_HEADER, encoding="utf-8")

    typer.echo(f"Initialized pix library root at {target}.")
    typer.echo()
    typer.echo(SYNC_REMINDER)

    # If this new root already sits inside a sync task, validate it now
    # (informational — init never blocks).
    sync_check.require_ready(target, block=False)
