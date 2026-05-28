"""Per-file content-hash cache.

See spec/hash.md for the schema. One tiny JSON sidecar per library
file under `<library>/.pix/cache/`, suffix `.hash`:

    media:  G:\\pix\\raw\\2023\\foo.jpg
    cache:  <library>/.pix/cache/G/pix/raw/2023/foo.jpg.hash

Cache file contents:

    {"size": ..., "mtime_ns": ..., "hash": "...", "computed_at": "..."}

A cache entry is **valid** when `(size, mtime_ns)` match the live
file. Otherwise it's stale — treated as missing.

Populated by `pix hash`; consumed by `pix dedupe` and `pix organize`.
Atomic writes (`.tmp` + fsync + replace) so a crash mid-write leaves
either the old entry or no entry — `pix hash` is expensive enough that
re-running it should hit cache rather than recompute.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from pix import cache_base


SUFFIX: str = ".hash"


def cache_path_for(library_root: Path, file_path: Path) -> Path:
    """Return the cache file path that mirrors `file_path`."""
    return cache_base.cache_path_for(library_root, file_path, SUFFIX)


def read_cached_hash(library_root: Path, file_path: Path) -> str | None:
    """Return the cached BLAKE3 hex digest, or None if missing/stale.

    Stats the live file to validate `(size, mtime_ns)`. Callers that
    already have those values from a `scandir` walk should use
    `_validate_cached_hash` via the parallel-read helpers below to
    save the stat.
    """
    data = cache_base.read_json(cache_path_for(library_root, file_path))
    if data is None:
        return None
    try:
        st = file_path.stat()
    except OSError:
        return None
    if data.get("size") != st.st_size:
        return None
    if data.get("mtime_ns") != st.st_mtime_ns:
        return None
    h = data.get("hash")
    if not isinstance(h, str):
        return None
    return h


def _validate_cached_hash(
    library_root: Path,
    file_path: Path,
    expected_size: int,
    expected_mtime_ns: int,
) -> str | None:
    """Like `read_cached_hash` but uses caller-provided size+mtime_ns
    instead of stat'ing the media file. Used by `read_all_cached_hashes`
    where the scandir walk already captured both values for free.
    """
    data = cache_base.read_json(cache_path_for(library_root, file_path))
    if data is None:
        return None
    if data.get("size") != expected_size:
        return None
    if data.get("mtime_ns") != expected_mtime_ns:
        return None
    h = data.get("hash")
    if not isinstance(h, str):
        return None
    return h


def write_cached_hash(
    library_root: Path,
    file_path: Path,
    *,
    hash_hex: str,
    size: int,
    mtime_ns: int,
) -> None:
    """Atomically write a cache entry for `file_path`."""
    payload: dict[str, object] = {
        "size": size,
        "mtime_ns": mtime_ns,
        "hash": hash_hex,
        "computed_at": datetime.now().isoformat(timespec="seconds"),
    }
    cache_base.write_json_atomic(
        cache_path_for(library_root, file_path), payload
    )


def read_all_cached_hashes(
    library_root: Path,
    paths_with_meta: list[tuple[Path, int, int]],
    on_batch: Callable[[int], None] | None = None,
    batch_size: int = cache_base.DEFAULT_BATCH_SIZE,
    max_workers: int = cache_base.DEFAULT_WORKERS,
) -> dict[Path, str | None]:
    """Return `{path: cached_hash_or_None}` for every input path.

    Single primitive for two consumers: hash uses
    `find_missing_hashes` (just needs the missing list); dedupe needs
    the actual hash values for grouping and shares this one parallel
    pass between the prereq check and the group-by-hash pass.
    """
    return cache_base.read_all_parallel(
        library_root,
        paths_with_meta,
        _validate_cached_hash,
        on_batch=on_batch,
        batch_size=batch_size,
        max_workers=max_workers,
    )


def find_missing_hashes(
    library_root: Path,
    paths_with_meta: list[tuple[Path, int, int]],
    on_batch: Callable[[int], None] | None = None,
    batch_size: int = cache_base.DEFAULT_BATCH_SIZE,
    max_workers: int = cache_base.DEFAULT_WORKERS,
) -> list[Path]:
    """Return library paths that lack a valid cached hash.

    Thin wrapper over `read_all_cached_hashes`: same parallel pass,
    discards the hash values, returns just the missing list. Used by
    `pix hash` discovery where the caller only needs to know which
    files still need hashing.
    """
    hashes = read_all_cached_hashes(
        library_root,
        paths_with_meta,
        on_batch=on_batch,
        batch_size=batch_size,
        max_workers=max_workers,
    )
    return [p for p, h in hashes.items() if h is None]
