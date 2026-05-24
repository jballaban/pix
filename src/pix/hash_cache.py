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
from datetime import datetime
from pathlib import Path
from typing import cast


def cache_path_for(library_root: Path, file_path: Path) -> Path:
    """Return the cache file path that mirrors `file_path`.

    Drive letters are folded into the first folder (NTFS dir names
    can't contain `:`). Same scheme as `pix.metadata_cache.PerFileCache`,
    suffix is `.hash` instead of `.cache`.
    """
    abs_path = file_path.resolve()
    parts = abs_path.parts
    cache_root = library_root / ".pix" / "cache"
    if not parts:
        return cache_root / (abs_path.name + ".hash")
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
    """
    cache_path = cache_path_for(library_root, file_path)
    if not cache_path.is_file():
        return None
    try:
        loaded: object = json.loads(cache_path.read_text(encoding="utf-8"))
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
