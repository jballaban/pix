"""Implementation of `pix info config` — show the library's resolved settings.

`pix.yaml` is hand-edited, and several operations are driven entirely by it:
[organize](../../spec/organize.md) re-applies `organize.template` when invoked
bare, and every [export](../../spec/export.md) distribution is config alone.
This command answers "what will pix actually do?" without running anything.

Three things it deliberately does beyond dumping the file:

1. **Shows effective values, not just written ones.** A setting that falls
   back to a default is printed *with* its default marked, so an omitted
   `extensions:` doesn't read as "ships everything".
2. **Validates.** Templates are parsed here (config can't — import cycle), so
   a broken one shows up as an error against its distribution rather than at
   the next `pix export`.
3. **Reports provisioning state.** Each distribution shows whether its target
   exists and how many members its manifest owns — the confirmation you want
   before trusting a delivery tier. That's a local manifest read and one
   `is_dir()`; it never walks a network target.

Read-only: touches nothing, takes no lock.
"""

from __future__ import annotations

from pathlib import Path

import typer

from pix import banner, export_manifest
from pix.config import (
    DEFAULT_EXPORT_EXTENSIONS,
    Config,
    Distribution,
    settings_path,
)
from pix.organize import OrganizeError, parse_template
from pix.root import NoLibraryRoot, resolve as resolve_root

_LABEL = 18  # width of the `key:` column (fits `organize.template:`)


def show_config(path: Path | None = None) -> None:
    """Print the resolved contents of the library's `pix.yaml`."""
    try:
        root = resolve_root(start=path) if path is not None else resolve_root()
    except NoLibraryRoot as e:
        banner()
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    banner()
    config_path = settings_path(root)
    typer.echo(f"Library:  {root}")
    typer.echo(
        f"Settings: {config_path}"
        + ("" if config_path.is_file() else "  (none yet — using defaults)")
    )

    try:
        config = Config.load(config_path)
    except ValueError as e:
        # A malformed file is exactly what this command exists to surface.
        typer.echo("")
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    typer.echo("")
    _echo_setting(
        "runs_dir",
        config.runs_dir or str(config.runs_base(root)),
        is_default=config.runs_dir is None,
    )
    _echo_setting(
        "organize.template",
        config.organize_template
        or "(not set — `pix organize <path> <template>` sets it)",
        is_default=False,
    )

    typer.echo("")
    if not config.exports:
        typer.echo("exports: none configured.")
        typer.echo(
            "  Add an `exports:` section to define a delivery distribution "
            "(see spec/export.md)."
        )
        return

    typer.echo(f"exports: {len(config.exports)} distribution(s)")
    for name in sorted(config.exports):
        _echo_distribution(root, config.exports[name])


def _echo_setting(key: str, value: str, *, is_default: bool) -> None:
    suffix = "  (default)" if is_default else ""
    typer.echo(f"{(key + ':').ljust(_LABEL)} {value}{suffix}")


def _echo_distribution(root: Path, dist: Distribution) -> None:
    typer.echo("")
    typer.echo(f"  {dist.name}")

    target = Path(dist.path)
    state = "exists" if target.is_dir() else "does not exist yet"
    _echo_field("path", f"{dist.path}  ({state})")
    _echo_field("filter", dist.filter.raw.strip() or "(everything)")
    _echo_field(
        "extensions",
        ",".join(sorted(dist.extensions))
        + ("  (default)" if dist.extensions == DEFAULT_EXPORT_EXTENSIONS else ""),
    )

    try:
        parse_template(dist.template)
        template_note = dist.template
    except OrganizeError as e:
        template_note = f"{dist.template}   << INVALID: {e}"
    _echo_field("template", template_note)

    manifest = export_manifest.load(root, dist.name)
    if manifest is None:
        provisioned = "never provisioned"
    elif manifest.target != dist.path:
        # The distribution was repointed since the last run; the manifest
        # describes a different folder, so export will re-adopt by hash.
        provisioned = (
            f"{len(manifest.members)} member(s), but recorded against "
            f"{manifest.target} — target changed, next run re-adopts"
        )
    else:
        provisioned = f"{len(manifest.members)} member(s) provisioned"
    _echo_field("provisioned", provisioned)


def _echo_field(key: str, value: str) -> None:
    typer.echo(f"    {(key + ':').ljust(_LABEL - 4)} {value}")
