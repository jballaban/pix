"""Implementation of `pix checkout` — tag editing via folder-shuffle.

Three actions on one command (see spec/tag-editing.md → CLI surface):

- `pix checkout <path> <template>` — start: materialize a scoped
  hard-link workspace + snapshot.
- `pix checkout --reset`           — discard the open checkout.
- `pix checkout --commit`          — apply tag edits (NOT yet built).
- bare `pix checkout`              — status.

Core logic lives in `pix.checkout`; this module is the CLI shell:
argument dispatch, root resolution, metadata loading, and console
output. The materialization itself runs under the library lock.
"""

from __future__ import annotations

import time
from pathlib import Path

import typer

from pix import banner
from pix.checkout import (
    CheckoutError,
    CheckoutExists,
    CheckoutScopeError,
    CheckoutUnmigratedError,
    checkout_dir,
    create_checkout,
    discard,
    is_open,
    read_snapshot,
    validate_checkout_template,
)
from pix.duration import format_duration_precise
from pix.library_lock import LockHeld, acquire as acquire_lock
from pix.metadata import (
    ExifToolFailed,
    ExifToolNotFound,
    FileMetadata,
    filter_cache_misses,
    read_metadata_batched,
)
from pix.metadata_cache import PerFileCache
from pix.organize import OrganizeError, Template, parse_template
from pix.progress import LiveProgress
from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.scan import walk_source_files
from pix.schema import SCHEMA_VERSION, SchemaTooNew, SchemaUpgradeRequired


def run_checkout(
    path: Path | None,
    template_str: str | None,
    *,
    commit: bool,
    reset: bool,
) -> None:
    """Dispatch the `pix checkout` action based on flags + positionals."""
    if commit and reset:
        typer.echo(
            "Error: --commit and --reset are mutually exclusive.", err=True
        )
        raise typer.Exit(code=1)
    if (commit or reset) and (path is not None or template_str is not None):
        typer.echo(
            "Error: --commit and --reset take no positional arguments.",
            err=True,
        )
        raise typer.Exit(code=1)

    if reset:
        _do_reset()
        return
    if commit:
        banner()
        typer.echo(
            "Error: `pix checkout --commit` is not yet implemented.", err=True
        )
        raise typer.Exit(code=1)

    if path is None and template_str is None:
        _do_status()
        return
    if path is None or template_str is None:
        banner()
        typer.echo(
            "Error: starting a checkout needs both <path> and <template>, "
            "e.g. `pix checkout . {year}/{event}`. For the whole library, "
            "pass the library root as <path>.",
            err=True,
        )
        raise typer.Exit(code=1)

    _do_start(path, template_str)


# --- status ------------------------------------------------------------------


def _do_status() -> None:
    """Print whether a checkout is open and its details."""
    try:
        root = resolve_root(start=None, check_schema=False)
    except NoLibraryRoot as e:
        banner()
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    banner()
    if not is_open(root):
        typer.echo("No checkout open.")
        typer.echo("Start one with `pix checkout <path> <template>`.")
        return

    cdir = checkout_dir(root)
    snap = read_snapshot(root)
    typer.echo(f"Checkout open at {cdir}")
    if snap is not None:
        typer.echo(f"  Template: {snap.template}")
        typer.echo(f"  Scope:    {snap.scope}")
        typer.echo(f"  Started:  {snap.created}")
        typer.echo(f"  Links:    {len(snap.links)}")
    else:
        typer.echo("  (snapshot.json missing or unreadable)")
    typer.echo("Run `pix checkout --commit` or `pix checkout --reset`.")


# --- reset -------------------------------------------------------------------


def _do_reset() -> None:
    """Discard the open checkout (the `--reset` action / unfreeze escape hatch)."""
    try:
        root = resolve_root(start=None, check_schema=False)
    except NoLibraryRoot as e:
        banner()
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    banner()
    if discard(root):
        typer.echo("Checkout discarded; the library is no longer frozen.")
    else:
        typer.echo("No checkout to reset.")


# --- start -------------------------------------------------------------------


def _do_start(path: Path, template_str: str) -> None:
    """Materialize a new scoped checkout workspace."""
    user_path = str(path)
    scope = path.resolve()
    try:
        root = resolve_root(start=scope)
    except SchemaUpgradeRequired as e:
        banner()
        typer.echo(f"{e} Run `pix upgrade {user_path}`", err=True)
        raise typer.Exit(code=1) from e
    except (NoLibraryRoot, SchemaTooNew) as e:
        banner()
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    banner(schema_version=SCHEMA_VERSION)

    if is_open(root):
        typer.echo(f"Error: {CheckoutExists(checkout_dir(root))}", err=True)
        raise typer.Exit(code=1)

    # Scope validation: must be a real directory, and not inside .pix/
    # (tool scaffolding — nothing to check out). `resolve_root` already
    # guarantees scope is at or under the library root.
    pix_dir = (root / ".pix").resolve()
    if scope == pix_dir or pix_dir in scope.parents:
        typer.echo(
            f"Error: {scope} is inside .pix/ (tool state); pick a media "
            f"folder under the library root.",
            err=True,
        )
        raise typer.Exit(code=1)
    if not scope.is_dir():
        typer.echo(f"Error: {scope} is not a directory.", err=True)
        raise typer.Exit(code=1)

    try:
        template = parse_template(template_str)
        validate_checkout_template(template)
    except OrganizeError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    except CheckoutError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    try:
        with acquire_lock(root, "checkout"):
            _materialize(root, scope, template, template_str)
    except LockHeld as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e


def _materialize(
    root: Path, scope: Path, template: Template, template_str: str
) -> None:
    """Load scoped metadata, then build the workspace under the lock."""
    t0 = time.monotonic()
    scanned = walk_source_files(scope)
    if not scanned:
        typer.echo(f"No files under {scope}; nothing to check out.")
        return

    cache = _load_metadata(root, scanned)
    typer.echo(
        f"Read {len(cache)} file(s) in "
        f"{format_duration_precise(time.monotonic() - t0)}."
    )

    try:
        with LiveProgress() as progress:
            progress.begin("Linking checkout")
            count = create_checkout(
                library_root=root,
                scope=scope,
                template=template,
                cache=cache,
            )
    except CheckoutUnmigratedError as e:
        typer.echo(f"Error: {e}", err=True)
        for p in e.paths:
            typer.echo(f"  {p}", err=True)
        raise typer.Exit(code=1) from e
    except (CheckoutScopeError, CheckoutExists, CheckoutError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    cdir = checkout_dir(root)
    typer.echo("")
    typer.echo(f"Checked out {count} file(s) to {cdir}")
    typer.echo(f"Template: {template_str}")
    typer.echo(
        "Shuffle the links in your file explorer, then "
        "`pix checkout --commit` (or `--reset` to discard)."
    )


def _load_metadata(
    root: Path, scanned: list[tuple[Path, int, int]]
) -> dict[Path, FileMetadata]:
    """Cache lookup + ExifTool fill for the scoped file set.

    Same shape as organize/dedupe, minus the hash and video passes
    (checkout needs neither). Files with no metadata at all get an
    empty record so the prereq check can flag them as un-migrated.
    """
    meta_cache = PerFileCache.for_library(root)

    with LiveProgress(total=len(scanned)) as check_progress:
        check_progress.begin("Loading cache")

        def _on_check_batch(n: int) -> None:
            check_progress.advance(by=n)

        hits, misses = filter_cache_misses(
            scanned, meta_cache, on_batch=_on_check_batch
        )

    fresh: dict[Path, FileMetadata] = {}
    if misses:
        try:
            with LiveProgress(total=len(misses)) as read_progress:
                read_progress.begin("Filling missing cache")

                def _on_batch(n: int) -> None:
                    read_progress.advance(by=n)

                fresh = read_metadata_batched(
                    misses, cache=meta_cache, on_batch=_on_batch
                )
        except ExifToolNotFound as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1) from e
        except ExifToolFailed as e:
            typer.echo(f"Error: exiftool failed.\n{e}", err=True)
            raise typer.Exit(code=1) from e

    cache = {**hits, **fresh}
    for path, _size, _mtime in scanned:
        if path not in cache:
            cache[path] = FileMetadata(
                path=path, raw={"SourceFile": str(path)}
            )
    return cache
