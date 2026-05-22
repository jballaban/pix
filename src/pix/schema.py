"""Library-state schema versioning.

Persisted state lives in `<root>/.pix/`. As pix evolves, the shape of
that state may change (new mandatory config field, new subfolder
layout, etc.). Rather than building a migration framework, we track a
single integer `schema_version` in `<root>/.pix/state.yaml`:

- Library version equal to `SCHEMA_VERSION`: nothing to do.
- Library version less than `SCHEMA_VERSION`: refuse with
  `SchemaUpgradeRequired`. The user runs `pix upgrade <path>`
  explicitly when they're ready; that command archives everything in
  `.pix/` (except `archive/`) into `.pix/archive/v{old}/`, then
  recreates fresh defaults. Auto-archiving on every command was
  rejected because the user may have config customizations that
  would silently land in the archive on what they thought was an
  unrelated command.
- Library version greater than `SCHEMA_VERSION`: refuse with
  `SchemaTooNew`. A newer pix touched this library; we don't know
  what we'd break.
- `state.yaml` missing: write a fresh one at `SCHEMA_VERSION`, no
  archive. This is the bootstrap path for libraries created before
  the versioning system existed; it doesn't destroy any customization
  so it stays silent.

Bump `SCHEMA_VERSION` only when something **material** in `.pix/`
changes shape — a new mandatory config field, a renamed subfolder, a
removed file format. Most pix releases don't touch persisted state
and should not bump it.
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
#      (created lazily on first stash, holds opaque-named files
#      `<run-id>_<line-id>.<ext>` with `.stashinfo` sidecars).
#      Default config gains dng/insp/insv → stash. `pix upgrade`
#      archives prior contents to .pix/archive/v1/ and creates fresh
#      defaults; users restore customizations from there as needed.
SCHEMA_VERSION: int = 2


class SchemaTooNew(Exception):
    """Library was touched by a newer pix version."""


class SchemaUpgradeRequired(Exception):
    """Library schema is older than this pix; user must run `pix upgrade`."""

    def __init__(self, library_version: int, root: Path) -> None:
        super().__init__(
            f"Library at {root} has schema_version={library_version}, "
            f"but this pix expects v{SCHEMA_VERSION}. Run "
            f"`pix upgrade {root}` to migrate (your current .pix/ "
            f"contents will be archived to "
            f".pix/archive/v{library_version}/ first, then fresh "
            f"defaults are created)."
        )
        self.library_version = library_version
        self.root = root


@dataclass(frozen=True)
class UpgradeResult:
    """What `upgrade` did. Returned to the `pix upgrade` command for
    user-facing reporting."""

    from_version: int
    to_version: int
    archive_path: Path


def ensure_current(root: Path) -> None:
    """Verify the library schema is current; bootstrap if state is missing.

    Raises `SchemaUpgradeRequired` when the on-disk version is older.
    Raises `SchemaTooNew` when it's newer. Bootstrapping (writing a
    fresh `state.yaml` at `SCHEMA_VERSION` when none exists) happens
    silently — it touches no customizations.
    """
    state_path = root / ".pix" / "state.yaml"

    if not state_path.exists():
        _write_state(state_path, SCHEMA_VERSION)
        return

    library_version = _read_version(state_path)

    if library_version == SCHEMA_VERSION:
        return

    if library_version > SCHEMA_VERSION:
        raise SchemaTooNew(
            f"Library at {root} has schema_version={library_version}, "
            f"but this pix only understands up to v{SCHEMA_VERSION}. "
            f"A newer pix touched this library; upgrade pix and re-run."
        )

    raise SchemaUpgradeRequired(library_version, root)


def upgrade(root: Path) -> UpgradeResult:
    """Archive prior `.pix/` contents and create fresh defaults.

    Used by the `pix upgrade` command. Refuses if the library is
    already current (returns no result; raises `ValueError`). Refuses
    if the library is newer than this pix (raises `SchemaTooNew`).
    """
    state_path = root / ".pix" / "state.yaml"
    if state_path.exists():
        library_version = _read_version(state_path)
    else:
        # Pre-versioning library. Treat as v0 — bumping it through
        # archive+reset is overkill (the bootstrap path already
        # handles missing state.yaml without destruction), so refuse
        # the upgrade and tell the caller to just run any normal
        # command which will silently bootstrap.
        raise ValueError(
            f"Library at {root} has no state.yaml — there's nothing "
            f"to upgrade. Run any pix command to bootstrap the "
            f"version stamp silently."
        )

    if library_version == SCHEMA_VERSION:
        raise ValueError(
            f"Library at {root} is already at schema_version="
            f"{SCHEMA_VERSION}. Nothing to upgrade."
        )
    if library_version > SCHEMA_VERSION:
        raise SchemaTooNew(
            f"Library at {root} has schema_version={library_version}, "
            f"but this pix only understands up to v{SCHEMA_VERSION}. "
            f"Upgrade pix, not the library."
        )

    archive_path = _archive_and_reset(root, from_version=library_version)
    return UpgradeResult(
        from_version=library_version,
        to_version=SCHEMA_VERSION,
        archive_path=archive_path,
    )


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


def _archive_and_reset(root: Path, from_version: int) -> Path:
    """Move everything in `.pix/` (except `archive/`) into the version
    archive, then recreate default `config.yaml` and `state.yaml`.
    Returns the archive path."""
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
    return archive_dir
