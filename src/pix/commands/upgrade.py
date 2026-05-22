"""Implementation of `pix upgrade <path>`.

Archives the library's prior `.pix/` contents into
`.pix/archive/v<old>/` and creates fresh defaults at the current
`SCHEMA_VERSION`. Run explicitly by the user when a normal command
raises `SchemaUpgradeRequired` (commands stop themselves rather than
silently archive customizations).
"""

from __future__ import annotations

from pathlib import Path

import typer

from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.schema import SchemaTooNew, upgrade


def upgrade_library(path: Path) -> None:
    """Run an explicit schema upgrade against the library at `path`."""
    path = path.resolve()

    # `check_schema=False`: this command is the schema fix; refusing
    # to resolve when the schema is old would be circular.
    try:
        root = resolve_root(start=path, check_schema=False)
    except NoLibraryRoot as e:
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
    typer.echo(
        f"Prior .pix/ contents are at {result.archive_path}. "
        f"Re-add any customizations from there (e.g., extension policy "
        f"changes in config.yaml, persisted organize.template)."
    )
