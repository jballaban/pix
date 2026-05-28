"""Shared plumbing for per-file caches under `<library>/.pix/cache/`.

Three caches share this layer (one file per flavor on disk; see the
discussion in the per-cache modules for why we keep them separate):

- `<...>.cache` — ExifTool JSON, validated on size only
- `<...>.hash` — content-hash digest, validated on (size, mtime_ns)
- `<...>.video` — ffprobe codec/profile/pix_fmt, validated on (size, mtime_ns)

All three mirror the source path under `<library>/.pix/cache/` with a
suffix appended:

    media:  G:\\pix\\raw\\2023\\foo.jpg
    cache:  <library>/.pix/cache/G/pix/raw/2023/foo.jpg.<suffix>

This module owns the path-mirroring scheme, the JSON read/write
plumbing (atomic and plain variants), and the parallel-lookup
primitive. Each cache module declares its suffix + schema + validation
key and delegates the rest here.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, TypeVar, cast


T = TypeVar("T")

# Default thread count for parallel cache reads. Matches the workers
# used elsewhere (metadata cache lookup, hash-cache scan); per-file
# checks are I/O-bound JSON reads of tiny files, so 32 is comfortable
# on any SSD/NVMe.
DEFAULT_WORKERS: int = 32
DEFAULT_BATCH_SIZE: int = 1000


def cache_root_for(library_root: Path) -> Path:
    """Return the on-disk cache directory: `<library>/.pix/cache/`."""
    return library_root / ".pix" / "cache"


def cache_path_for(
    library_root: Path, file_path: Path, suffix: str
) -> Path:
    """Mirror `file_path` under `<library>/.pix/cache/` with `suffix`.

    Drive letters (Windows) fold into the first folder name because
    NTFS dir names can't contain `:`. Caller is expected to pass an
    absolute path — every pix caller goes through `pix.scan.walk_source_files`
    which returns absolute canonical paths. A defensive `resolve()` here
    would cost one stat per cache lookup.
    """
    parts = file_path.parts
    cache_root = cache_root_for(library_root)
    if not parts:
        return cache_root / (file_path.name + suffix)
    drive = parts[0].rstrip("\\/").rstrip(":")
    rest = parts[1:]
    if rest:
        mirrored = Path(drive, *rest)
    else:
        mirrored = Path(drive)
    return cache_root / mirrored.with_name(mirrored.name + suffix)


def read_json(cache_path: Path) -> dict[str, object] | None:
    """Read + parse a JSON cache file. None on miss / unreadable / bad type.

    No `is_file()` precheck — `read_bytes()` is attempted directly and
    `FileNotFoundError` is treated as a miss. Saves one stat per cache
    hit on the hot path.
    """
    try:
        loaded: object = json.loads(cache_path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    return cast("dict[str, object]", loaded)


def write_json_atomic(
    cache_path: Path, payload: dict[str, object]
) -> None:
    """Atomic write: `<target>.tmp` + fsync + `os.replace`.

    A crash mid-write leaves either the old entry (if any) or no
    entry — never a truncated one. Used by caches that are expensive
    to rebuild (content hash, ffprobe video probe).
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.parent / (cache_path.name + ".tmp")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, cache_path)


def write_json_plain(
    cache_path: Path, payload: dict[str, object]
) -> None:
    """Non-atomic write — fast path for high-volume caches.

    No `.tmp`, no fsync. A crash mid-write leaves a truncated entry
    that `read_json` will reject (JSON parse fails) on the next pass,
    so the worst case is "re-derive that one entry." Used by the
    metadata cache, which writes thousands of entries per migrate run
    and is cheap to rebuild from ExifTool.
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def remove(cache_path: Path) -> None:
    """Best-effort delete; never raises."""
    try:
        cache_path.unlink(missing_ok=True)
    except OSError:
        pass


def rename(old: Path, new: Path) -> None:
    """Best-effort rename alongside a media-file move; never raises."""
    if not old.is_file():
        return
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        old.replace(new)
    except OSError:
        pass


def read_all_parallel(
    library_root: Path,
    paths_with_meta: list[tuple[Path, int, int]],
    validator: Callable[[Path, Path, int, int], "T | None"],
    on_batch: Callable[[int], None] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_workers: int = DEFAULT_WORKERS,
) -> dict[Path, "T | None"]:
    """Look up cache entries in parallel for a batch of files.

    `validator(library_root, path, size, mtime_ns)` is the per-cache
    validation routine — it reads the sidecar, checks (size, mtime_ns)
    against the caller-supplied values, and returns the typed cached
    value (or None on miss/stale).

    Results arrive in submission order via `ThreadPoolExecutor.map`;
    `on_batch(n)` fires from the consumer thread every `batch_size`
    files, so no locking is needed in the callback.
    """
    if not paths_with_meta:
        return {}

    result: dict[Path, T | None] = {}
    in_batch = 0

    def check_one(
        item: tuple[Path, int, int],
    ) -> tuple[Path, T | None]:
        path, size, mtime_ns = item
        return path, validator(library_root, path, size, mtime_ns)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for path, cached in executor.map(check_one, paths_with_meta):
            result[path] = cached
            in_batch += 1
            if in_batch >= batch_size:
                if on_batch is not None:
                    on_batch(in_batch)
                in_batch = 0

    if in_batch > 0 and on_batch is not None:
        on_batch(in_batch)
    return result
