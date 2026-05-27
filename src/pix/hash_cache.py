"""Per-file content-hash cache.

See spec/hash.md for the schema. One tiny JSON sidecar per library
file, under `<library>/.pix/cache/`, with the cache path mirroring
the media file's absolute path:

    media:  G:\\pix\\raw\\2023\\foo.jpg
    cache:  <library>/.pix/cache/G/pix/raw/2023/foo.jpg.hash

Cache file contents:

    {"size": ..., "mtime_ns": ..., "hash": "...", "computed_at": "..."}

A cache entry is **valid** when `(size, mtime_ns)` match the live
file. Otherwise it's stale — treated as missing.

Populated by `pix hash`; consumed by `pix dedupe` and `pix organize`.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, cast


def cache_path_for(library_root: Path, file_path: Path) -> Path:
    """Return the cache file path that mirrors `file_path`.

    Drive letters are folded into the first folder (NTFS dir names
    can't contain `:`). Same scheme as `pix.metadata_cache.PerFileCache`,
    suffix is `.hash` instead of `.cache`.

    Caller is expected to pass an absolute path — every pix caller goes
    through `pix.scan.walk_source_files` which returns absolute canonical
    paths. A defensive `resolve()` here would cost one stat per lookup.
    """
    parts = file_path.parts
    cache_root = library_root / ".pix" / "cache"
    if not parts:
        return cache_root / (file_path.name + ".hash")
    drive = parts[0].rstrip("\\/").rstrip(":")
    rest = parts[1:]
    if rest:
        mirrored = Path(drive, *rest)
    else:
        mirrored = Path(drive)
    return cache_root / mirrored.with_name(mirrored.name + ".hash")


def read_cached_hash(library_root: Path, file_path: Path) -> str | None:
    """Return the cached BLAKE3 hex digest, or None if missing/stale.

    Validates the entry against the file's current `(size, mtime_ns)`
    — any drift makes the entry stale, same as missing.

    No is_file precheck: `read_bytes` is attempted directly and
    `FileNotFoundError` is treated as a miss. Saves one stat per cache
    hit on the hot path.
    """
    cache_path = cache_path_for(library_root, file_path)
    try:
        loaded: object = json.loads(cache_path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    data = cast("dict[str, object]", loaded)
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
    instead of stat'ing the media file.

    Used by `find_missing_hashes` where the scandir walk already
    captured both values for free from the dirent.
    """
    cache_path = cache_path_for(library_root, file_path)
    try:
        loaded: object = json.loads(cache_path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    data = cast("dict[str, object]", loaded)
    if data.get("size") != expected_size:
        return None
    if data.get("mtime_ns") != expected_mtime_ns:
        return None
    h = data.get("hash")
    if not isinstance(h, str):
        return None
    return h


# Match `metadata.CACHE_LOOKUP_WORKERS` — same I/O-bound JSON-read
# workload, same SSD parallelism profile.
_HASH_LOOKUP_WORKERS: int = 32
_HASH_LOOKUP_BATCH: int = 1000


def read_all_cached_hashes(
    library_root: Path,
    paths_with_meta: list[tuple[Path, int, int]],
    on_batch: Callable[[int], None] | None = None,
    batch_size: int = _HASH_LOOKUP_BATCH,
    max_workers: int = _HASH_LOOKUP_WORKERS,
) -> dict[Path, str | None]:
    """Return `{path: cached_hash_or_None}` for every input path.

    Validates each entry's `(size, mtime_ns)` against the values
    supplied by the caller (sourced from
    `pix.scan.walk_source_files`'s scandir dirents). Mismatches and
    missing-entirely both yield `None`.

    Lookups run in a thread pool — each per-file check is one
    `read_bytes` of a small JSON file + a cheap validation, and lookups
    are independent. Concurrent execution on SSD/NVMe pushes the total
    phase time ~10× lower than sequential.

    `on_batch(batch_size)` fires every `batch_size` files from the
    consumer thread (results arrive in submission order via
    `ThreadPoolExecutor.map`).

    Single primitive for two consumers: hash uses
    `find_missing_hashes` (just needs the missing list); dedupe needs
    the actual hash values for grouping and shares this one parallel
    pass between the prereq check and the group-by-hash pass.
    """
    if not paths_with_meta:
        return {}

    result: dict[Path, str | None] = {}
    in_batch = 0

    def check_one(item: tuple[Path, int, int]) -> tuple[Path, str | None]:
        path, size, mtime_ns = item
        return path, _validate_cached_hash(
            library_root,
            path,
            expected_size=size,
            expected_mtime_ns=mtime_ns,
        )

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


def find_missing_hashes(
    library_root: Path,
    paths_with_meta: list[tuple[Path, int, int]],
    on_batch: Callable[[int], None] | None = None,
    batch_size: int = _HASH_LOOKUP_BATCH,
    max_workers: int = _HASH_LOOKUP_WORKERS,
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


def write_cached_hash(
    library_root: Path,
    file_path: Path,
    *,
    hash_hex: str,
    size: int,
    mtime_ns: int,
) -> None:
    """Atomically write a cache entry for `file_path`.

    Writes to `<target>.tmp`, fsyncs, then renames over the target so a
    crash mid-write leaves either the old entry (if any) or no entry —
    never a truncated one. Same crash protection as the metadata cache.
    """
    cache_path = cache_path_for(library_root, file_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.parent / (cache_path.name + ".tmp")

    payload = {
        "size": size,
        "mtime_ns": mtime_ns,
        "hash": hash_hex,
        "computed_at": datetime.now().isoformat(timespec="seconds"),
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, cache_path)
