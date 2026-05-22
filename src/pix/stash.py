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

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

import yaml


SIDECAR_SUFFIX: str = ".stashinfo"


@dataclass(frozen=True)
class StashSidecar:
    """Per-file provenance for a stashed file."""

    origin: str
    stashed_at: str  # ISO 8601 datetime, second precision

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            {"origin": self.origin, "stashed_at": self.stashed_at},
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
        return cls(origin=origin, stashed_at=stashed_at)


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
) -> None:
    """Move `source` to `target_path` and write the sidecar.

    `shutil.move` is used so cross-volume moves (source on a different
    drive from the library) fall back to copy+delete cleanly.
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    origin = str(source)
    shutil.move(str(source), str(target_path))
    ts = (stashed_at or datetime.now()).isoformat(timespec="seconds")
    write_sidecar(
        target_path, StashSidecar(origin=origin, stashed_at=ts)
    )
