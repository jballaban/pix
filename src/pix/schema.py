"""Library-state schema versioning.

Persisted state lives in `<root>/.pix/`. As pix evolves, the shape of
that state may change (new mandatory config field, new subfolder
layout, etc.). Rather than building a migration framework, we track a
single integer `schema_version` in `<root>/.pix/state.yaml`:

- Library version equal to `SCHEMA_VERSION`: nothing to do.
- Library version less than `SCHEMA_VERSION`: archive everything in
  `.pix/` (except `archive/`) into `.pix/archive/v{old}/`, then
  recreate fresh defaults. No prompt — the archive is the safety net.
- Library version greater than `SCHEMA_VERSION`: refuse. A newer pix
  touched this library; we don't know what we'd break.
- `state.yaml` missing: write a fresh one at `SCHEMA_VERSION`, no
  archive. This is the bootstrap path for libraries created before the
  versioning system existed.

Bump `SCHEMA_VERSION` only when something **material** in `.pix/`
changes shape — a new mandatory config field, a renamed subfolder, a
removed file format. Most pix releases don't touch persisted state and
should not bump it.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from pix.config import DEFAULT_CONFIG_YAML


# Bump only when a material change to .pix/ layout or config schema
# ships. Past bumps are recorded here so we have a written history of
# what each version meant.
#
# v1 — Initial schema. .pix/{config.yaml, state.yaml, runs/, staging/}.
# v2 — Added `stash` extension action and the `.pix/stash/` subfolder
#      (created lazily on first stash). Default config gains
#      dng/insp/insv → stash. Existing libraries archive-and-reset to
#      pick up the new defaults; users restore prior customizations
#      from .pix/archive/v1/.
SCHEMA_VERSION: int = 2


class SchemaTooNew(Exception):
    """Library was touched by a newer pix version."""


@dataclass(frozen=True)
class SchemaCheckResult:
    """Outcome of `ensure_current`.

    `archived_from` is set when the library was reset and the prior
    contents moved to `<root>/.pix/archive/v{archived_from}/`. The
    caller may surface a notice to the user.
    """

    archived_from: int | None = None


def ensure_current(root: Path) -> SchemaCheckResult:
    """Bring `<root>/.pix/` to the current schema version.

    Raises `SchemaTooNew` when the on-disk version is greater than
    `SCHEMA_VERSION`.
    """
    state_path = root / ".pix" / "state.yaml"

    if not state_path.exists():
        _write_state(state_path, SCHEMA_VERSION)
        return SchemaCheckResult()

    library_version = _read_version(state_path)

    if library_version == SCHEMA_VERSION:
        return SchemaCheckResult()

    if library_version > SCHEMA_VERSION:
        raise SchemaTooNew(
            f"Library at {root} has schema_version={library_version}, "
            f"but this pix only understands up to v{SCHEMA_VERSION}. "
            f"A newer pix touched this library; upgrade pix and re-run."
        )

    _archive_and_reset(root, from_version=library_version)
    return SchemaCheckResult(archived_from=library_version)


def _read_version(state_path: Path) -> int:
    """Parse `schema_version` from `state.yaml`."""
    loaded: object = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(
            f"{state_path}: top-level must be a mapping, got "
            f"{type(loaded).__name__}"
        )
    version: object = cast("dict[str, object]", loaded).get("schema_version")
    if not isinstance(version, int):
        raise ValueError(
            f"{state_path}: schema_version must be an integer, got "
            f"{type(version).__name__}"
        )
    return version


def _write_state(state_path: Path, version: int) -> None:
    """Write a fresh `state.yaml` at the given schema version."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        f"schema_version: {version}\n", encoding="utf-8"
    )


def _archive_and_reset(root: Path, from_version: int) -> None:
    """Move everything in `.pix/` (except `archive/`) into the version
    archive, then recreate default `config.yaml` and `state.yaml`.
    """
    pix_dir = root / ".pix"
    archive_dir = pix_dir / "archive" / f"v{from_version}"
    if archive_dir.exists():
        # Shouldn't happen — each version only gets archived once. If
        # we somehow re-hit the same from_version, refuse rather than
        # silently merge.
        raise RuntimeError(
            f"Archive folder {archive_dir} already exists; refusing "
            f"to overwrite. Move or delete it and re-run."
        )
    archive_dir.mkdir(parents=True)

    for item in pix_dir.iterdir():
        if item.name == "archive":
            continue
        shutil.move(str(item), str(archive_dir / item.name))

    (pix_dir / "config.yaml").write_text(
        DEFAULT_CONFIG_YAML, encoding="utf-8"
    )
    _write_state(pix_dir / "state.yaml", SCHEMA_VERSION)
