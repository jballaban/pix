"""Implementation of `pix upgrade <path>`.

Walks per-version upgrade entries (see `pix.schema.UPGRADES`) to apply
additive / removal config changes to the user's existing config. The
prior `.pix/` contents are archived to `.pix/archive/v<old>/` first so
nothing is lost. Conflicts (user has a key with a value that differs
from the new default) are surfaced as git-style markers in the new
config; pix refuses to operate on a marker-laden config until the user
picks a side.

Terminology: a schema *upgrade* is an internal `.pix/` state
transition. Don't conflate with `pix migrate`, which is the file-level
normalization command.
"""

from __future__ import annotations

from pathlib import Path

import typer

from pix import banner
from pix.checkout import CheckoutOpen, ensure_no_open_checkout
from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.schema import SchemaTooNew, upgrade


def upgrade_library(path: Path) -> None:
    """Run an explicit schema upgrade against the library at `path`."""
    banner()
    path = path.resolve()

    # `check_schema=False`: this command is the schema fix; refusing to
    # resolve when the schema is old would be circular.
    try:
        root = resolve_root(start=path, check_schema=False)
    except NoLibraryRoot as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    # An open checkout archives into oblivion if upgrade sweeps .pix/;
    # refuse until it's committed or reset (same freeze as every op).
    try:
        ensure_no_open_checkout(root)
    except CheckoutOpen as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    try:
        result = upgrade(root)
    except ValueError as e:
        # Library already current, or no state.yaml to upgrade.
        typer.echo(f"{e}", err=True)
        raise typer.Exit(code=1) from e
    except SchemaTooNew as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    typer.echo(
        f"Upgraded {root} from schema v{result.from_version} "
        f"to v{result.to_version}."
    )
    typer.echo(f"Prior .pix/ contents archived to {result.archive_path}.")
    typer.echo("")

    if result.archived_user_data:
        total = sum(d.file_count for d in result.archived_user_data)
        typer.echo(
            f"⚠  IMPORTANT: {total} irreplaceable file(s) were moved into "
            f"the archive and pix will NOT see them there:",
            err=True,
        )
        for d in result.archived_user_data:
            kind = (
                "originals you stashed"
                if d.name == "stash"
                else "files that failed conversion"
            )
            typer.echo(
                f"  .pix/{d.name}/  →  {d.path}  "
                f"({d.file_count} file(s) — {kind})",
                err=True,
            )
        typer.echo(
            "These are the only copies. While they sit in the archive, "
            "stashed files won't be listed and errored files won't be "
            "auto-retried by migrate. Move anything you want to keep back "
            "out of the archive (into your library or a safe location) "
            "before you delete it.",
            err=True,
        )
        typer.echo("", err=True)

    if result.added:
        typer.echo(f"Added {len(result.added)} entr"
                   f"{'y' if len(result.added) == 1 else 'ies'}:")
        for entry in result.added:
            typer.echo(f"  + {entry}")
        typer.echo("")

    if result.removed:
        typer.echo(f"Removed {len(result.removed)} entr"
                   f"{'y' if len(result.removed) == 1 else 'ies'}:")
        for entry in result.removed:
            typer.echo(f"  - {entry}")
        typer.echo("")

    if result.conflicts:
        typer.echo(
            f"{len(result.conflicts)} conflict"
            f"{'' if len(result.conflicts) == 1 else 's'} — "
            f"your value differs from the new default. config.yaml has "
            f"been written with git-style markers around each conflict.",
            err=True,
        )
        for c in result.conflicts:
            typer.echo(
                f"  {c.section}.{c.key}: current={c.current_value!r}, "
                f"v{c.version} default={c.new_default!r}",
                err=True,
            )
        typer.echo("", err=True)
        typer.echo(
            "Edit .pix/config.yaml to remove `<<<<<<<`, `=======`, "
            "and `>>>>>>>` markers, keeping only the line you want for "
            "each conflict. Pix refuses to run any normal command until "
            "the markers are gone.",
            err=True,
        )
        raise typer.Exit(code=1)

    if not result.added and not result.removed:
        typer.echo(
            "No config changes needed (your existing config already "
            "matches the new defaults)."
        )
