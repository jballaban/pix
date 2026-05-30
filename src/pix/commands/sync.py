"""Implementation of `pix sync` — the do-everything-reasonable pipeline.

Runs the four main commands back-to-back, non-interactively:

    migrate → hash → dedupe → organize

`hash` precedes `dedupe`/`organize` because both consume the content-hash
cache; `dedupe` precedes `organize` so duplicates are removed before the
survivors are shaped (rather than shuffled around and then deleted).

Each sub-command is invoked with `no_prompt=True`, so its `Apply?` /
`Proceed?` confirmation is skipped and the generated plan is applied
directly — but the plan is still written to its run folder. On any
failure a sub-command raises `typer.Exit`, which propagates out of
`sync_library` and **stops the chain** so an error isn't buried under
later steps. Re-run `pix sync` after fixing whatever halted it.

`dedupe` is included — `sync` is the "do everything reasonable" action.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import typer

from pix.commands.dedupe import dedupe_library
from pix.commands.hash import hash_library
from pix.commands.migrate import migrate_folder
from pix.commands.organize import organize_library


def sync_library(path: Path, template_str: str | None = None) -> None:
    """Run migrate → hash → dedupe → organize over `path`, auto-applying.

    `path` scopes `migrate` to that folder and resolves the library root
    for the (library-wide) hash/dedupe/organize steps. `template_str` is
    passed to organize; when None, organize re-applies the stored
    template (and errors — halting sync — if none is stored).
    """
    steps: tuple[tuple[str, Callable[[], None]], ...] = (
        ("migrate", lambda: migrate_folder(folder=path, no_prompt=True)),
        ("hash", lambda: hash_library(path=path, no_prompt=True)),
        ("dedupe", lambda: dedupe_library(path=path, no_prompt=True)),
        (
            "organize",
            lambda: organize_library(
                path=path, template_str=template_str, no_prompt=True
            ),
        ),
    )
    total = len(steps)
    for i, (name, run) in enumerate(steps, start=1):
        typer.echo("")
        typer.echo(f"===== pix sync [{i}/{total}] {name} =====")
        run()  # raises typer.Exit on failure → chain stops here

    typer.echo("")
    typer.echo("sync complete: migrate → hash → dedupe → organize.")
