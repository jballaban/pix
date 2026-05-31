"""Stash action — purist preservation of files we can't process in v1.

Per spec/migrate.md → stash policy: a `stash` extension action moves
files to `<library-root>/.pix/stash/` with a tiny YAML sidecar
recording the source path and timestamp. The on-disk filename is
opaque (`<run-id>_<line-id>.<ext>`), guaranteed unique by
construction — so no collision logic, no dedup, no hash compute at
stash time.

Dedup and other processing of stashed files are explicitly deferred:
when the user later decides what to do with their stashed RAW files,
proprietary 360 sources, etc., a future command (likely re-using
migrate's machinery) will handle that.

Sidecar format:

    origin: F:\\source\\trip-2023\\IMG_001.dng
    stashed_at: 2026-05-22T15:30:00

Recovery: the on-disk name carries the run-id and line-id; the
sidecar carries the source path and timestamp. Together they're
enough to roll back a stash (move the file back to `origin`).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

import yaml

from pix import __version__ as _PIX_VERSION


SIDECAR_SUFFIX: str = ".stashinfo"


@dataclass(frozen=True)
class StashSidecar:
    """Per-file provenance for a stashed file.

    `pix_version` records the `pix.__version__` at stash time. Migrate's
    cleanup phase restores any entry whose version differs from the
    running pix (a code change since the stash may now handle the file
    differently — e.g. a format that flipped from `stash` to `keep`).
    Empty `pix_version` = legacy sidecar (pre-versioning); treated as
    stale and eligible for restore. See `restore_stale_stash`.
    """

    origin: str
    stashed_at: str  # ISO 8601 datetime, second precision
    pix_version: str = ""  # `pix.__version__` at stash time

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            {
                "origin": self.origin,
                "stashed_at": self.stashed_at,
                "pix_version": self.pix_version,
            },
            default_flow_style=False,
            sort_keys=False,
        )

    @classmethod
    def from_yaml(cls, text: str) -> "StashSidecar":
        loaded: object = yaml.safe_load(text) or {}
        if not isinstance(loaded, dict):
            raise ValueError("stashinfo: top-level must be a mapping")
        data = cast("dict[str, object]", loaded)
        origin = data.get("origin")
        if not isinstance(origin, str):
            raise ValueError("stashinfo: 'origin' must be a string")
        stashed_at = data.get("stashed_at")
        if not isinstance(stashed_at, str):
            raise ValueError("stashinfo: 'stashed_at' must be a string")
        # Optional for backward compatibility with sidecars written before
        # stash-versioning; absent = "unknown older version" → stale.
        pix_version_raw = data.get("pix_version", "")
        pix_version = (
            pix_version_raw if isinstance(pix_version_raw, str) else ""
        )
        return cls(
            origin=origin, stashed_at=stashed_at, pix_version=pix_version
        )


def sidecar_path_for(stash_file: Path) -> Path:
    """Sidecar lives next to the stash file with `.stashinfo` appended."""
    return stash_file.parent / (stash_file.name + SIDECAR_SUFFIX)


def read_sidecar(stash_file: Path) -> StashSidecar | None:
    """Read the sidecar next to `stash_file`. None if missing/unreadable."""
    sidecar = sidecar_path_for(stash_file)
    if not sidecar.is_file():
        return None
    try:
        return StashSidecar.from_yaml(sidecar.read_text(encoding="utf-8"))
    except (ValueError, yaml.YAMLError):
        return None


def write_sidecar(stash_file: Path, sidecar: StashSidecar) -> None:
    """Persist `sidecar` next to `stash_file`."""
    sidecar_path_for(stash_file).write_text(
        sidecar.to_yaml(), encoding="utf-8"
    )


def stash_filename(run_id: str, line_id: str, source: Path) -> str:
    """Compute the opaque stash filename for a source file.

    Format: `<run-id>_<line-id><source-extension>`. The run-id is
    a timestamp (e.g., `2026-05-22_15-30-00`) and the line-id is the
    plan-line label (e.g., `L042`); the combination is globally
    unique, so no collision logic is ever needed.
    """
    return f"{run_id}_{line_id}{source.suffix.lower()}"


def stash_file(
    *,
    source: Path,
    target_path: Path,
    stashed_at: datetime | None = None,
    pix_version: str | None = None,
) -> None:
    """Move `source` to `target_path` and write the sidecar.

    `shutil.move` is used so cross-volume moves (source on a different
    drive from the library) fall back to copy+delete cleanly. The sidecar
    records the running `pix.__version__` (overridable for tests) so a
    later version bump can trigger an auto-restore (`restore_stale_stash`).
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    origin = str(source)
    shutil.move(str(source), str(target_path))
    ts = (stashed_at or datetime.now()).isoformat(timespec="seconds")
    version = pix_version if pix_version is not None else _PIX_VERSION
    write_sidecar(
        target_path,
        StashSidecar(origin=origin, stashed_at=ts, pix_version=version),
    )


# --- Version-gated auto-restore ---------------------------------------------


@dataclass(frozen=True)
class RestoredStashEntry:
    """One stash blob moved back to its origin path."""

    origin: Path
    sidecar_pix_version: str  # "" for legacy sidecars


@dataclass(frozen=True)
class SkippedStashEntry:
    """One stash blob we couldn't restore. Reason is human-readable."""

    entry_path: Path
    reason: str


def _iter_stash_blobs(stash_dir: Path) -> list[Path]:
    """Stashed media files (flat) — excludes `.stashinfo` sidecars + `.tmp`."""
    return sorted(
        p
        for p in stash_dir.iterdir()
        if p.is_file()
        and not p.name.endswith(SIDECAR_SUFFIX)
        and not p.name.endswith(".tmp")
    )


def _is_under(child: Path, parent: Path) -> bool:
    """True if `child` is `parent` or nested beneath it (case-insensitive,
    no filesystem access — `child` need not exist)."""
    c = os.path.normcase(os.path.abspath(str(child)))
    p = os.path.normcase(os.path.abspath(str(parent)))
    return c == p or c.startswith(p + os.sep)


def restore_stale_stash(
    library_root: Path, folder: Path
) -> tuple[list[RestoredStashEntry], list[SkippedStashEntry], int]:
    """Restore stash blobs back to their origin when a version bump means
    migrate may now handle them differently.

    Mirrors `pix.errors.restore_stale_errors`, with two stash-specific
    rules:

    - **Folder-scoped.** Only entries whose sidecar `origin` falls under
      `folder` (the folder being migrated) are restored — they land where
      this run's plan-gen will see them, and a targeted migrate doesn't
      scatter unrelated stash entries across the disk. Out-of-scope
      entries are left untouched (they restore when their folder is
      migrated). This differs from errors, which restores library-wide
      (failures are rare; the stash can hold hundreds of files).
    - **Version-gated.** An entry whose sidecar records the running
      `pix.__version__` is left in place (the same code would just
      re-stash it). A different/empty version is stale → restore. Restore
      recreates the original camera filename and location from the
      sidecar `origin`, so provenance and the keep/delete/re-stash
      decision all run correctly on the re-migrate.

    Returns `(restored, skipped, kept_current_version)`:
    - `restored`: blobs moved back to their origin.
    - `skipped`: blobs we couldn't restore (no/unreadable sidecar, origin
      slot occupied, or move failed).
    - `kept_current_version`: in-scope entries left in place because they
      were stashed by the running version (the normal resting state).
    """
    stash_dir = library_root / ".pix" / "stash"
    if not stash_dir.is_dir():
        return [], [], 0

    restored: list[RestoredStashEntry] = []
    skipped: list[SkippedStashEntry] = []
    kept = 0

    for blob in _iter_stash_blobs(stash_dir):
        sidecar = read_sidecar(blob)
        if sidecar is None:
            skipped.append(
                SkippedStashEntry(
                    entry_path=blob,
                    reason="missing or unreadable .stashinfo — origin unknown",
                )
            )
            continue

        origin = Path(sidecar.origin)
        if not _is_under(origin, folder):
            continue  # out of scope for this migrate — leave in stash

        if sidecar.pix_version == _PIX_VERSION:
            kept += 1  # stashed by the running version — leave it
            continue

        if origin.exists():
            skipped.append(
                SkippedStashEntry(
                    entry_path=blob,
                    reason=f"target {origin} already exists — not overwriting",
                )
            )
            continue

        try:
            origin.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(blob), str(origin))
        except OSError as e:
            skipped.append(
                SkippedStashEntry(entry_path=blob, reason=f"move failed: {e}")
            )
            continue

        try:
            sidecar_path_for(blob).unlink(missing_ok=True)
        except OSError:
            pass

        restored.append(
            RestoredStashEntry(
                origin=origin, sidecar_pix_version=sidecar.pix_version
            )
        )

    return restored, skipped, kept
