"""Content-hash cache — facade over the SQLite store (`pix.cache_db`).

The BLAKE3 digest lives in the `hash` column of each file's row, validated
on `(size, mtime_ns)`. Populated by `pix hash`; consumed by `pix dedupe` and
`pix organize`.

`read_all_cached_hashes` loads the whole cache in one `SELECT` and validates
each entry against the caller's scandir `(size, mtime_ns)` — replacing the old
per-file parallel sidecar read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from pix import cache_db


_DEFAULT_BATCH_SIZE: int = 1000


def read_cached_hash(library_root: Path, file_path: Path) -> str | None:
    """Return the cached digest, or None if missing/stale. Stats the file to
    validate `(size, mtime_ns)`."""
    row = cache_db.get(library_root, file_path)
    if row is None or row.hash is None:
        return None
    try:
        st = file_path.stat()
    except OSError:
        return None
    if row.size != st.st_size or row.mtime_ns != st.st_mtime_ns:
        return None
    return row.hash


def write_cached_hash(
    library_root: Path,
    file_path: Path,
    *,
    hash_hex: str,
    size: int,
    mtime_ns: int,
) -> None:
    """Write a hash entry for `file_path` against the given stamp."""
    cache_db.put_hash(
        library_root, file_path, hash_hex, size=size, mtime_ns=mtime_ns
    )


def read_all_cached_hashes(
    library_root: Path,
    paths_with_meta: list[tuple[Path, int, int]],
    on_batch: Callable[[int], None] | None = None,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> dict[Path, str | None]:
    """Return `{path: cached_hash_or_None}` for every input path.

    One `load_all` query, then validate each scanned `(size, mtime_ns)`
    against the row stamp in memory."""
    rows = cache_db.load_all(library_root)
    result: dict[Path, str | None] = {}
    in_batch = 0
    for path, size, mtime_ns in paths_with_meta:
        row = rows.get(path)
        if (
            row is not None
            and row.hash is not None
            and row.size == size
            and row.mtime_ns == mtime_ns
        ):
            result[path] = row.hash
        else:
            result[path] = None
        in_batch += 1
        if in_batch >= batch_size and on_batch is not None:
            on_batch(in_batch)
            in_batch = 0
    if in_batch > 0 and on_batch is not None:
        on_batch(in_batch)
    return result


def find_missing_hashes(
    library_root: Path,
    paths_with_meta: list[tuple[Path, int, int]],
    on_batch: Callable[[int], None] | None = None,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> list[Path]:
    """Return library paths that lack a valid cached hash."""
    hashes = read_all_cached_hashes(
        library_root, paths_with_meta, on_batch=on_batch, batch_size=batch_size
    )
    return [p for p, h in hashes.items() if h is None]
