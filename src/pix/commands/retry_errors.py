"""Implementation of `pix retry-errors <library>`.

Restores files quarantined under `<library>/.pix/errors/` back to the
`original_path` recorded in their `.errorinfo` sidecar. After running,
the user re-runs `pix migrate <source>` to process them — the convert
layer's updated truncation tolerance (and any other fixes since the
original failure) gives them a fresh chance.

See spec/migrate.md → Failure handling for the errors/ folder semantics.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

import typer
import yaml

from pix import banner
from pix.library_lock import LockHeld, acquire as acquire_lock
from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.schema import SCHEMA_VERSION, SchemaTooNew, SchemaUpgradeRequired


def retry_errors(path: Path) -> None:
    """Restore every file in <library>/.pix/errors/ to its original path."""
    user_path = str(path)
    path = path.resolve()
    try:
        root = resolve_root(start=path)
    except SchemaUpgradeRequired as e:
        banner()
        typer.echo(f"{e} Run `pix upgrade {user_path}`", err=True)
        raise typer.Exit(code=1) from e
    except (NoLibraryRoot, SchemaTooNew) as e:
        banner()
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    banner(schema_version=SCHEMA_VERSION)

    try:
        with acquire_lock(root, "retry-errors"):
            _run_retry(root)
    except LockHeld as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e


def _run_retry(root: Path) -> None:
    errors_dir = root / ".pix" / "errors"
    if not errors_dir.is_dir():
        typer.echo(f"No errors directory at {errors_dir}; nothing to retry.")
        return

    errorinfos = sorted(errors_dir.glob("*.errorinfo"))
    if not errorinfos:
        typer.echo(f"No quarantined files in {errors_dir}; nothing to retry.")
        return

    restored = 0
    skipped: list[tuple[Path, str]] = []
    source_dirs: set[Path] = set()
    for errorinfo in errorinfos:
        # The quarantined data file sits next to its sidecar with the
        # `.errorinfo` suffix stripped.
        data_file = errorinfo.with_suffix("")
        if not data_file.is_file():
            skipped.append(
                (errorinfo, "quarantined file missing alongside sidecar")
            )
            continue

        try:
            info_raw = yaml.safe_load(errorinfo.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            skipped.append((errorinfo, f"errorinfo unreadable: {e}"))
            continue
        if not isinstance(info_raw, dict):
            skipped.append((errorinfo, "errorinfo isn't a mapping"))
            continue
        info = cast("dict[str, object]", info_raw)

        original_path_raw = info.get("original_path")
        if not isinstance(original_path_raw, str) or not original_path_raw:
            skipped.append(
                (errorinfo, "errorinfo missing `original_path`")
            )
            continue
        original_path = Path(original_path_raw)

        if original_path.exists():
            skipped.append(
                (
                    data_file,
                    f"target {original_path} already exists — not overwriting",
                )
            )
            continue

        try:
            original_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(data_file), str(original_path))
        except OSError as e:
            skipped.append((data_file, f"move failed: {e}"))
            continue

        try:
            errorinfo.unlink()
        except OSError:
            # Sidecar cleanup is best-effort — the file restore succeeded
            # and that's what matters. A leftover .errorinfo with no
            # adjacent data file is harmless; the next retry-errors run
            # will skip it cleanly.
            pass

        source_dirs.add(original_path.parent)
        restored += 1

    typer.echo("")
    typer.echo(f"Restored {restored} file(s) from {errors_dir}.")
    if skipped:
        typer.echo(f"Skipped {len(skipped)}:", err=True)
        for path_, reason in skipped:
            typer.echo(f"  {path_}: {reason}", err=True)

    if restored and source_dirs:
        # One-line hint per distinct source dir so the user knows which
        # `pix migrate` invocation to run next. Most quarantines come
        # from one folder, so this is usually a single line.
        typer.echo("")
        typer.echo("Re-run `pix migrate` to process the restored files:")
        for d in sorted(source_dirs):
            typer.echo(f"  pix migrate {d}")
