"""Source-folder scanning utilities shared across migrate, organize, dedupe, hash."""

from __future__ import annotations

import os
from pathlib import Path


def walk_source_files(folder: Path) -> list[tuple[Path, int, int]]:
    """Walk `folder` recursively for files, skipping `.pix/` state directories.

    Returns `(absolute_path, file_size_bytes, mtime_ns)` triples. Both
    size and mtime come free from the directory entry on Windows (NTFS
    dirents cache them), so downstream cache validators can avoid a
    follow-up `stat()` per file — notably the per-file metadata cache
    check (size-validated) and the per-file hash cache check (validated
    on size+mtime_ns).

    pix's own state lives under `.pix/` — `runs/`, `staging/`, `checkouts/`,
    `faces/`, `cache/`, `errors/`, `config.yaml`, etc. None of that should
    ever be treated as source: if the user runs `pix migrate` from the
    library root itself, we must not iterate into the library's
    bookkeeping. Any `.pix/` at any depth is pruned in-place so we never
    descend into it.

    Uses `os.scandir` (iteratively) rather than `os.walk` so we can keep
    the `DirEntry` and read its cached `st_size` / `st_mtime_ns` without
    extra syscalls. The caller is expected to have resolved `folder`
    already; on Windows `scandir` yields canonical NTFS case so output
    is consistent with what `resolve()` would have produced.
    """
    out: list[tuple[Path, int, int]] = []
    stack: list[str] = [str(folder)]
    while stack:
        dirpath = stack.pop()
        try:
            with os.scandir(dirpath) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name != ".pix":
                            stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        st = entry.stat()
                        out.append(
                            (Path(entry.path), st.st_size, st.st_mtime_ns)
                        )
        except OSError:
            continue
    return out
