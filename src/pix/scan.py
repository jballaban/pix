"""Source-folder scanning utilities shared across migrate, organize, dedupe, hash."""

from __future__ import annotations

import os
from pathlib import Path


def walk_source_files(folder: Path) -> list[Path]:
    """Walk `folder` recursively for files, skipping `.pix/` state directories.

    pix's own state lives under `.pix/` — `runs/`, `staging/`, `checkouts/`,
    `faces/`, `cache/`, `errors/`, `config.yaml`, etc. None of that should
    ever be treated as source: if the user runs `pix migrate` from the
    library root itself, we must not iterate into the library's
    bookkeeping. Any `.pix/` at any depth is pruned from `dirnames`
    in-place so we never descend into it.

    Uses `os.walk` rather than `Path.rglob`: walk yields filenames
    separately from directory names from a single directory listing per
    folder, so we avoid an `is_file()` stat per entry. We also skip the
    `Path.resolve()` per entry — caller is expected to have resolved
    `folder` already, and `os.walk` on Windows yields the canonical
    NTFS case so output is consistent with what resolve() would have
    produced.
    """
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(str(folder)):
        dirnames[:] = [d for d in dirnames if d != ".pix"]
        for fn in filenames:
            out.append(Path(dirpath, fn))
    return out
