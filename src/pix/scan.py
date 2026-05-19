"""Source-folder scanning utilities shared across migrate, organize, dedupe."""

from __future__ import annotations

from pathlib import Path


def walk_source_files(folder: Path) -> list[Path]:
    """Walk `folder` recursively for files, skipping `.pix/` state directories.

    pix's own state lives under `.pix/` — `runs/`, `staging/`, `checkouts/`,
    `faces/`, `config.yaml`, etc. None of that should ever be treated as
    source: if the user runs `pix migrate` from the library root itself, we
    must not iterate into the library's bookkeeping. Any `.pix/` at any
    depth is skipped — same convention as the ExifTool `-i .pix` flag used
    by the metadata cache (see `pix.metadata.build_cache`).
    """
    return [
        p.resolve()
        for p in folder.rglob("*")
        if p.is_file() and not _under_pix_dir(p)
    ]


def _under_pix_dir(path: Path) -> bool:
    return any(part == ".pix" for part in path.parts)
