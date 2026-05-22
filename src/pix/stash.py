"""Stash action — set aside files we can't process in v1 for later.

Per spec/migrate.md → stash policy: a `stash` extension action moves
files to `<library-root>/.pix/stash/` with a `.stashinfo` sidecar.
Whole-file BLAKE3 hash (via `pix.content_hash.compute_content_hash`)
dedups across all stash entries; same content from multiple sources
is stored once with a multi-origin sidecar.

Sidecar format (YAML):

    hash: <64 hex chars>
    origins:
      - <source path 1>
      - <source path 2>
    original_filename: <optional>  # present only when stash filename
                                   # was modified due to collision

A subsequent stash of the same content from a different source path
appends to `origins`. A subsequent stash of *different* content with
the same source filename gets a `_NNN` suffix on its stash filename
(same algorithm as canonical-filename collisions), and that file's
sidecar records `original_filename`.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import yaml

from pix.content_hash import compute_content_hash


SIDECAR_SUFFIX: str = ".stashinfo"


@dataclass
class StashSidecar:
    """Per-file metadata for a stashed file."""

    hash: str
    origins: list[str] = field(default_factory=lambda: [])
    original_filename: str | None = None

    def to_yaml(self) -> str:
        data: dict[str, object] = {
            "hash": self.hash,
            "origins": list(self.origins),
        }
        if self.original_filename is not None:
            data["original_filename"] = self.original_filename
        return yaml.safe_dump(
            data, default_flow_style=False, sort_keys=False
        )

    @classmethod
    def from_yaml(cls, text: str) -> "StashSidecar":
        loaded: object = yaml.safe_load(text) or {}
        if not isinstance(loaded, dict):
            raise ValueError("stashinfo: top-level must be a mapping")
        data = cast("dict[str, object]", loaded)
        h = data.get("hash")
        if not isinstance(h, str):
            raise ValueError("stashinfo: 'hash' must be a string")
        raw_origins = data.get("origins", [])
        if not isinstance(raw_origins, list):
            raise ValueError("stashinfo: 'origins' must be a list")
        origins = [str(o) for o in cast("list[object]", raw_origins)]
        orig_name = data.get("original_filename")
        if orig_name is not None and not isinstance(orig_name, str):
            raise ValueError(
                "stashinfo: 'original_filename' must be a string"
            )
        return cls(hash=h, origins=origins, original_filename=orig_name)


def sidecar_path_for(stash_file: Path) -> Path:
    """Sidecar lives next to the stash file with `.stashinfo` appended."""
    return stash_file.parent / (stash_file.name + SIDECAR_SUFFIX)


def read_sidecar(stash_file: Path) -> StashSidecar | None:
    """Read the sidecar next to `stash_file`. None if missing/unreadable."""
    sidecar = sidecar_path_for(stash_file)
    if not sidecar.is_file():
        return None
    try:
        return StashSidecar.from_yaml(
            sidecar.read_text(encoding="utf-8")
        )
    except (ValueError, yaml.YAMLError):
        return None


def write_sidecar(stash_file: Path, sidecar: StashSidecar) -> None:
    """Persist `sidecar` next to `stash_file`."""
    sidecar_path_for(stash_file).write_text(
        sidecar.to_yaml(), encoding="utf-8"
    )


def load_stash_index(stash_dir: Path) -> dict[str, Path]:
    """Build a `{hash: stash_file_path}` map from sidecars in `stash_dir`.

    Malformed or missing-stash-file sidecars are skipped silently —
    they're self-healing (the bytes still exist or don't; the next
    stash op will re-detect).
    """
    if not stash_dir.is_dir():
        return {}

    index: dict[str, Path] = {}
    for sidecar in stash_dir.glob(f"*{SIDECAR_SUFFIX}"):
        # Stash filename = sidecar name with .stashinfo stripped.
        stash_name = sidecar.name[: -len(SIDECAR_SUFFIX)]
        stash_file = stash_dir / stash_name
        if not stash_file.is_file():
            continue
        info = read_sidecar(stash_file)
        if info is None:
            continue
        index[info.hash] = stash_file
    return index


def _pick_stash_filename(
    stash_dir: Path, source_name: str
) -> tuple[str, bool]:
    """Choose a filename inside `stash_dir` for a new entry.

    Returns `(filename, was_renamed)`. `was_renamed` is True iff the
    chosen filename differs from `source_name` due to a collision with
    a different-content existing stash entry.

    Collision algorithm matches the canonical-filename rule from
    library.md: `name_001.ext`, `name_002.ext`, …
    """
    if not _name_taken(stash_dir, source_name):
        return source_name, False

    stem, dot, ext = source_name.rpartition(".")
    if not dot:
        stem, ext = source_name, ""
        sep = ""
    else:
        sep = "."

    i = 1
    while True:
        candidate = f"{stem}_{i:03d}{sep}{ext}"
        if not _name_taken(stash_dir, candidate):
            return candidate, True
        i += 1


def _name_taken(stash_dir: Path, name: str) -> bool:
    """True if either the stash file or its sidecar exists in `stash_dir`."""
    if (stash_dir / name).exists():
        return True
    if (stash_dir / (name + SIDECAR_SUFFIX)).exists():
        return True
    return False


def stash_file(
    *,
    source: Path,
    stash_dir: Path,
    index: dict[str, Path],
    dup_capture_path: Path,
) -> tuple[bool, Path]:
    """Stash `source`. Returns `(was_dup, final_path)`.

    If `source`'s content matches an entry in `index`, treat as a
    duplicate: move source to `dup_capture_path` (the run-folder
    capture, mirroring DELETE's conservation) and append the new
    origin to the existing sidecar. The keeper stays put.

    Otherwise pick a stash filename (source name, suffix on
    different-content collision), move source into `stash_dir`,
    write a fresh sidecar. Update `index` in place so callers can
    keep stashing in the same loop.

    `shutil.move` is used (not `Path.rename`) so cross-volume moves
    fall back to copy+delete cleanly. The hash is computed before
    the move, while source is still readable.
    """
    source_hash = compute_content_hash(source)
    original_name = source.name

    # --- Dup branch ----------------------------------------------------
    if source_hash in index:
        existing = index[source_hash]
        sidecar = read_sidecar(existing)
        if sidecar is None:
            # Sidecar missing/corrupt — rebuild minimally.
            sidecar = StashSidecar(hash=source_hash, origins=[])
        sidecar.origins.append(str(source))
        write_sidecar(existing, sidecar)

        dup_capture_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(dup_capture_path))
        return True, existing

    # --- New entry branch ----------------------------------------------
    stash_dir.mkdir(parents=True, exist_ok=True)
    final_name, renamed = _pick_stash_filename(stash_dir, original_name)
    final_path = stash_dir / final_name

    shutil.move(str(source), str(final_path))

    sidecar = StashSidecar(
        hash=source_hash,
        origins=[str(source)],
        original_filename=original_name if renamed else None,
    )
    write_sidecar(final_path, sidecar)
    index[source_hash] = final_path
    return False, final_path
