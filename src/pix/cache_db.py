"""Single-file SQLite cache store — one row per library file.

Replaces the old per-file sidecar tree (`<library>/.pix/cache/<mirror>.meta`
/ `.hash` / `.vfp`, up to three tiny files per media file). That scheme cost
every command a stat+read+parse of ~200k files just to answer "what changed?".
This is the same problem git solves with its **index**: one file, keyed by
path, holding the stat stamp + derived values, so the question is one read
instead of N.

Schema (`<library>/.pix/cache.db`, WAL):

    files(path PK, size, mtime_ns, meta, hash, vfp)
    _pix_cache(key, value)   -- schema_version, import_done

**Core invariant.** A row's `(size, mtime_ns)` is the file's identity. A
column (`meta`/`hash`/`vfp`) is valid iff it's non-NULL *and* the row stamp
matches the live file. Every in-place pix write that bumps mtime updates the
stamp and refreshes/carries-forward the populated columns (meta refreshed;
hash/vfp carried forward — they're content-invariant). Any read whose live
`(size, mtime_ns)` differs from the row stamp treats all columns as stale.
Unlike the old layout (meta validated on size only), all three share one
`(size, mtime_ns)` key.

The whole library's cache loads in one `SELECT` (`load_all`); writes are
plain statements; relocate/remove/prune are one statement / one query each.
A single process-wide connection per library is reused across calls (pix runs
one command per process under the library lock, so access is single-threaded).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, cast

import typer

from pix import cache_base
from pix.metadata_filter import filter_consumed
from pix.root import local_dir


SCHEMA_VERSION: int = 1

# Suffixes of the old sidecar layout, read once by the one-time import.
_OLD_META_SUFFIX: str = ".meta"
_OLD_HASH_SUFFIX: str = ".hash"
_OLD_VFP_SUFFIX: str = ".vfp"


@dataclass(frozen=True)
class CacheRow:
    """One file's cache row. `meta`/`vfp` are parsed JSON (or None); `hash`
    is the hex digest string (or None). `size`/`mtime_ns` are the stamp the
    columns were computed against."""

    size: int
    mtime_ns: int
    meta: dict[str, object] | None
    hash: str | None
    vfp: dict[str, object] | None


@dataclass(frozen=True)
class PruneStats:
    """Result of `prune`. `legacy_removed` is retained for log-message
    compatibility with the old sidecar sweep but is always 0 now (the
    one-time import reaps the legacy tree)."""

    orphans_removed: int
    legacy_removed: int


# Process-wide connection cache, keyed by the db path string. One command =
# one process under the library lock, so a single shared connection is safe.
_conns: dict[str, sqlite3.Connection] = {}


def db_path(library_root: Path) -> Path:
    """Return the on-disk DB path: `<library>/.pix/local/cache.db`.

    Self-healing across the pre-`local/` layout: prefer the new location, but
    fall back to the legacy `<library>/.pix/cache.db` while it still exists and
    hasn't been relocated yet (see `_relocate_legacy_db`). Never returns a path
    that would strand a populated legacy DB behind a fresh empty one.
    """
    new = local_dir(library_root) / "cache.db"
    if new.exists():
        return new
    legacy = library_root / ".pix" / "cache.db"
    if legacy.exists():
        return legacy
    return new


def _relocate_legacy_db(library_root: Path) -> None:
    """Move a pre-`local/` `.pix/cache.db` into `.pix/local/`, once.

    Checkpoints (TRUNCATE) and closes first so the `-wal`/`-shm` merge into the
    `.db` and are removed on clean close — then a single-file rename moves the
    whole cache with no risk of a split `.db`/`-wal` pair. Best-effort: if the
    rename fails (file held open), `db_path` keeps using the legacy path until
    the next run retries.
    """
    new = local_dir(library_root) / "cache.db"
    if new.exists():
        return
    legacy = library_root / ".pix" / "cache.db"
    if not legacy.exists():
        return
    new.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp = sqlite3.connect(str(legacy), isolation_level=None)
        try:
            tmp.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            tmp.close()
    except sqlite3.Error:
        pass
    try:
        legacy.rename(new)
    except OSError:
        return
    for suffix in ("-wal", "-shm"):  # defensive: should be gone post-checkpoint
        residual = legacy.with_name(legacy.name + suffix)
        if residual.exists():
            try:
                residual.rename(new.with_name(new.name + suffix))
            except OSError:
                pass


def _connect(library_root: Path) -> sqlite3.Connection:
    """Open (or reuse) the library's connection, init schema, run the
    one-time sidecar import if a legacy `.pix/cache/` tree is present."""
    _relocate_legacy_db(library_root)
    key = str(db_path(library_root))
    existing = _conns.get(key)
    if existing is not None:
        return existing
    path = db_path(library_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None → autocommit; WAL + NORMAL makes commits cheap
    # (no fsync per write; durable on app crash, may lose the last txn only
    # on a power/OS crash — fine for a rebuildable cache).
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _init_schema(conn)
    _conns[key] = conn
    _maybe_import(library_root, conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS files ("
        "path TEXT PRIMARY KEY, size INTEGER NOT NULL, "
        "mtime_ns INTEGER NOT NULL, meta TEXT, hash TEXT, vfp TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _pix_cache (key TEXT PRIMARY KEY, value TEXT)"
    )
    if _get_flag(conn, "schema_version") is None:
        _set_flag(conn, "schema_version", str(SCHEMA_VERSION))


def _get_flag(conn: sqlite3.Connection, key: str) -> str | None:
    cur = conn.execute("SELECT value FROM _pix_cache WHERE key=?", (key,))
    row = cur.fetchone()
    return cast("str", row[0]) if row is not None else None


def _set_flag(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO _pix_cache(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _loads(text: object) -> dict[str, object] | None:
    """Parse a JSON object column; None on NULL / parse error / wrong type."""
    if not isinstance(text, str):
        return None
    try:
        loaded: object = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return cast("dict[str, object]", loaded) if isinstance(loaded, dict) else None


def _dumps(value: dict[str, object] | None) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


# --- reads -----------------------------------------------------------------


def get(library_root: Path, path: Path) -> CacheRow | None:
    """Return the raw cache row for `path` (no validation), or None."""
    conn = _connect(library_root)
    cur = conn.execute(
        "SELECT size, mtime_ns, meta, hash, vfp FROM files WHERE path=?",
        (str(path),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    size, mtime_ns, meta_s, hash_h, vfp_s = row
    return CacheRow(
        size=cast("int", size),
        mtime_ns=cast("int", mtime_ns),
        meta=_loads(meta_s),
        hash=cast("str | None", hash_h),
        vfp=_loads(vfp_s),
    )


def load_all(library_root: Path) -> dict[Path, CacheRow]:
    """Load the entire cache into memory in one query. Callers validate each
    row's stamp against the live `(size, mtime_ns)` from their walk."""
    conn = _connect(library_root)
    out: dict[Path, CacheRow] = {}
    for row in conn.execute(
        "SELECT path, size, mtime_ns, meta, hash, vfp FROM files"
    ):
        path_s, size, mtime_ns, meta_s, hash_h, vfp_s = row
        out[Path(cast("str", path_s))] = CacheRow(
            size=cast("int", size),
            mtime_ns=cast("int", mtime_ns),
            meta=_loads(meta_s),
            hash=cast("str | None", hash_h),
            vfp=_loads(vfp_s),
        )
    return out


def iter_meta(library_root: Path) -> Iterator[tuple[Path, dict[str, object]]]:
    """Yield `(path, metadata)` for every row with a non-NULL meta column.
    Backs `pix info events` (one query, no per-file reads)."""
    conn = _connect(library_root)
    for row in conn.execute(
        "SELECT path, meta FROM files WHERE meta IS NOT NULL"
    ):
        path_s, meta_s = row
        md = _loads(meta_s)
        if md is not None:
            yield Path(cast("str", path_s)), md


# --- writes ----------------------------------------------------------------


def _put_column(
    library_root: Path,
    path: Path,
    column: str,
    value: str | None,
    *,
    size: int,
    mtime_ns: int,
) -> None:
    """Set one column for `path` against the `(size, mtime_ns)` stamp.

    If the row is absent or its stamp differs (the file changed), replace the
    row at the new stamp with only this column populated (the others are stale
    by definition). Otherwise just update the one column. `column` is an
    internal literal (`meta`/`hash`/`vfp`), never user input.
    """
    conn = _connect(library_root)
    p = str(path)
    cur = conn.execute("SELECT size, mtime_ns FROM files WHERE path=?", (p,))
    row = cur.fetchone()
    if row is None or row[0] != size or row[1] != mtime_ns:
        cols: dict[str, str | None] = {"meta": None, "hash": None, "vfp": None}
        cols[column] = value
        conn.execute(
            "INSERT INTO files(path, size, mtime_ns, meta, hash, vfp) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "size=excluded.size, mtime_ns=excluded.mtime_ns, "
            "meta=excluded.meta, hash=excluded.hash, vfp=excluded.vfp",
            (p, size, mtime_ns, cols["meta"], cols["hash"], cols["vfp"]),
        )
    else:
        conn.execute(f"UPDATE files SET {column}=? WHERE path=?", (value, p))


def put_meta(
    library_root: Path,
    path: Path,
    metadata: dict[str, object],
    *,
    size: int,
    mtime_ns: int,
) -> None:
    _put_column(
        library_root, path, "meta", _dumps(metadata), size=size, mtime_ns=mtime_ns
    )


def put_hash(
    library_root: Path,
    path: Path,
    hash_hex: str,
    *,
    size: int,
    mtime_ns: int,
) -> None:
    _put_column(
        library_root, path, "hash", hash_hex, size=size, mtime_ns=mtime_ns
    )


def put_vfp(
    library_root: Path,
    path: Path,
    vfp: dict[str, object],
    *,
    size: int,
    mtime_ns: int,
) -> None:
    _put_column(
        library_root, path, "vfp", _dumps(vfp), size=size, mtime_ns=mtime_ns
    )


def note_inplace_metadata_change(
    library_root: Path,
    path: Path,
    *,
    meta_updates: dict[str, object],
    size: int,
    mtime_ns: int,
) -> None:
    """Reflect a metadata-only in-place write (tag/override/rotation re-tag).

    Such a write bumps `(size, mtime_ns)` but leaves the *content* — and so
    the content hash and perceptual fingerprint — unchanged. In one
    transaction: re-stamp the row, merge `meta_updates` into the existing
    meta, and **carry the existing hash/vfp forward** to the new stamp so a
    no-op re-run doesn't re-hash or (the expensive one) re-fingerprint.

    No-op if there's no row to update (nothing was cached for this file yet).
    If the row has no `meta` yet (e.g. `pix hash` ran before this write), the
    updates are NOT merged into a fabricated partial dict — meta stays NULL so
    a later read does a full ExifTool fill rather than trusting a partial entry.
    This single helper replaces the per-command "capture hash/vfp, re-stamp
    after the write" dance that set/dedupe used to hand-roll.
    """
    conn = _connect(library_root)
    p = str(path)
    cur = conn.execute(
        "SELECT meta, hash, vfp FROM files WHERE path=?", (p,)
    )
    row = cur.fetchone()
    if row is None:
        return
    meta = _loads(row[0])
    # Only merge into an existing meta dict; never fabricate a partial one.
    # Filter the updates so the meta column stays trimmed regardless of caller.
    merged = {**meta, **filter_consumed(meta_updates)} if meta is not None else None
    conn.execute(
        "UPDATE files SET size=?, mtime_ns=?, meta=? WHERE path=?",
        (size, mtime_ns, _dumps(merged), p),
    )


def relocate(library_root: Path, old: Path, new: Path) -> None:
    """Move a row to a new path (a media move leaves bytes/stamp valid).

    Best-effort: drop any stale row already at `new` (its file is gone — it's
    being replaced), then repoint `old`'s row. No-op if `old` has no row.
    """
    conn = _connect(library_root)
    try:
        conn.execute("DELETE FROM files WHERE path=?", (str(new),))
        conn.execute(
            "UPDATE files SET path=? WHERE path=?", (str(new), str(old))
        )
    except sqlite3.Error:
        pass


def remove(library_root: Path, path: Path) -> None:
    """Delete a file's row (file gone: DELETE/STASH/CONVERT-source)."""
    conn = _connect(library_root)
    try:
        conn.execute("DELETE FROM files WHERE path=?", (str(path),))
    except sqlite3.Error:
        pass


def prune(
    library_root: Path,
    expected_paths: set[Path],
    allowed_prefix: Path | None = None,
) -> PruneStats:
    """Delete rows whose file isn't in `expected_paths`.

    `allowed_prefix`: when set, only rows whose path lives under it are
    eligible (migrate walks a subfolder and must not prune elsewhere).
    """
    conn = _connect(library_root)
    expected = {str(p) for p in expected_paths}
    to_delete: list[str] = []
    for (path_s,) in conn.execute("SELECT path FROM files"):
        p = cast("str", path_s)
        if allowed_prefix is not None:
            try:
                Path(p).relative_to(allowed_prefix)
            except ValueError:
                continue
        if p not in expected:
            to_delete.append(p)
    if to_delete:
        conn.executemany(
            "DELETE FROM files WHERE path=?", [(p,) for p in to_delete]
        )
    return PruneStats(orphans_removed=len(to_delete), legacy_removed=0)


# --- one-time import of the legacy sidecar tree ----------------------------


def _read_old_meta(library_root: Path, path: Path, size: int) -> dict[str, object] | None:
    """Read a legacy `.meta` sidecar, validating on size (its old rule)."""
    side = cache_base.cache_path_for(library_root, path, _OLD_META_SUFFIX)
    data = cache_base.read_json(side)
    if data is None or data.get("size") != size:
        return None
    md = data.get("metadata")
    if not isinstance(md, dict):
        return None
    # Trim to consumed tags on the way in, matching fresh-read storage.
    return filter_consumed(cast("dict[str, object]", md))


def _read_old_hash(
    library_root: Path, path: Path, size: int, mtime_ns: int
) -> str | None:
    side = cache_base.cache_path_for(library_root, path, _OLD_HASH_SUFFIX)
    data = cache_base.read_json(side)
    if data is None or data.get("size") != size or data.get("mtime_ns") != mtime_ns:
        return None
    h = data.get("hash")
    return h if isinstance(h, str) else None


def _read_old_vfp(
    library_root: Path, path: Path, size: int, mtime_ns: int
) -> dict[str, object] | None:
    side = cache_base.cache_path_for(library_root, path, _OLD_VFP_SUFFIX)
    data = cache_base.read_json(side)
    if data is None or data.get("size") != size or data.get("mtime_ns") != mtime_ns:
        return None
    # Keep only the fingerprint payload (drop legacy size/mtime/computed_at).
    keep = {k: data[k] for k in ("frames", "width", "height", "duration") if k in data}
    return keep or None


def _maybe_import(library_root: Path, conn: sqlite3.Connection) -> None:
    """Fold a legacy `.pix/cache/` sidecar tree into the DB once, then reap it.

    For each live media file, carry over each sidecar whose old validation
    still holds, stamped with the live `(size, mtime_ns)`. Idempotent
    (INSERT OR REPLACE), so an interrupted import resumes; the tree is only
    deleted after the flag is set.
    """
    if _get_flag(conn, "import_done") == "1":
        return
    tree = cache_base.cache_root_for(library_root)
    if not tree.is_dir():
        _set_flag(conn, "import_done", "1")
        return

    # Local import avoids a module cycle (scan has no cache deps).
    from pix.scan import walk_source_files

    typer.echo("Migrating cache to cache.db (one-time)...")
    scanned = walk_source_files(library_root)
    imported = 0
    conn.execute("BEGIN")
    try:
        for path, size, mtime_ns in scanned:
            meta = _read_old_meta(library_root, path, size)
            hash_hex = _read_old_hash(library_root, path, size, mtime_ns)
            vfp = _read_old_vfp(library_root, path, size, mtime_ns)
            if meta is None and hash_hex is None and vfp is None:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO files(path, size, mtime_ns, meta, hash, vfp) "
                "VALUES(?,?,?,?,?,?)",
                (str(path), size, mtime_ns, _dumps(meta), hash_hex, _dumps(vfp)),
            )
            imported += 1
        conn.execute("COMMIT")
    except sqlite3.Error:
        conn.execute("ROLLBACK")
        raise
    _set_flag(conn, "import_done", "1")
    shutil.rmtree(tree, ignore_errors=True)
    typer.echo(
        f"Cache migrated: {imported} entr(ies) imported; "
        f"removed legacy .pix/cache/ tree."
    )


def close_all() -> None:
    """Close every cached connection (tests / shutdown)."""
    for conn in _conns.values():
        try:
            conn.close()
        except sqlite3.Error:
            pass
    _conns.clear()
