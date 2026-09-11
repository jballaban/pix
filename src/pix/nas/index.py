"""The app's index — an aggregation over the derived tiers (spec/nas-app.md §4).

**Disposable, and never authoritative.** The record is master: original bytes,
`.xmp` decisions beside them, and the ledgers. This is a queryable projection of
that, because you cannot scan 62k files for every UI filter.

Two rules from §4 shape it:

- **Facts, not interpretations.** Rows carry what ExifTool read — the raw
  capture date, dimensions, camera — never the effective value after pix's
  heuristics. Facts cannot go stale, because master files are immutable;
  interpretations go stale the moment a heuristic improves.
- **Read through to embedded tags.** A file's decisions come from its `.xmp` if
  one exists, and otherwise from the `pix:*` tags already inside the legacy file.
  That is what let seeding skip writing ~62k sidecars: inherited events simply
  appear, and a sidecar is created only when a value is first changed.

Built from the **meta tier** rather than the media, which is the whole reason
`process` writes it: rebuilding reads small JSON instead of opening every file.

Two ways in, and the difference matters. `build` replaces everything, and is what
`pix2 index` and the tail of `pix2 process` run: the file *set* changes only when
ingest runs, so recomputing it wholesale is both correct and rare. `refresh`
rewrites a single row, and is what a curation write runs — because reading 62k
records to record one decision is not a UI anyone uses twice.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, cast

from pix.nas import decisions
from pix.nas.const import MASTER_DIR, META_DIR
from pix.nas.decisions import Decision

SCHEMA_VERSION: int = 1

_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS files (
    folder        TEXT NOT NULL,
    name          TEXT NOT NULL,
    size          INTEGER,
    mtime_ns      INTEGER,
    kind          TEXT,          -- image | video | other
    capture_date  TEXT,          -- probed fact, ISO-ish, NULL if the file has none
    camera        TEXT,
    width         INTEGER,
    height        INTEGER,
    duration      REAL,
    event         TEXT,          -- decision, or inherited from embedded tags
    tier          TEXT,          -- decision: none | photo | top; NULL = unreviewed
    date_override TEXT,
    has_sidecar   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (folder, name)
);
CREATE INDEX IF NOT EXISTS files_event   ON files(event);
CREATE INDEX IF NOT EXISTS files_tier    ON files(tier);
CREATE INDEX IF NOT EXISTS files_capture ON files(capture_date);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_VIDEO_EXTS: frozenset[str] = frozenset({
    ".mov", ".mp4", ".m4v", ".avi", ".mkv", ".wmv",
    ".webm", ".3gp", ".mts", ".mpg", ".mpeg", ".insv", ".insp",
})
_IMAGE_EXTS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif",
    ".tif", ".tiff", ".webp", ".bmp",
})


@dataclass
class IndexStats:
    """What one build did."""

    files: int = 0
    with_date: int = 0
    with_sidecar: int = 0
    events: int = 0
    skipped: list[str] = field(default_factory=lambda: [])


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the index database."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def open_ro(db_path: Path) -> sqlite3.Connection:
    """Open the index **read-only**, without creating or migrating anything.

    Every browsing request takes this connection. `connect` writes schema and a
    version row, which fails outright on a read-only mount and — worse, where
    the mount is writable — would let a page it does not own quietly mutate the
    projection. Read-only is the honest shape for a reader, and it makes the
    mistake impossible rather than merely unlikely.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def open_rw(db_path: Path) -> sqlite3.Connection:
    """Open an **existing** index for writing single rows.

    The narrow exception to "the app is a reader". `sidecar first, index
    follows` (spec §4) means the app updates the row for a file whose decision
    it just wrote — which is a projection catching up with the record, not the
    app becoming a second source of truth. It still never *builds*: `mode=rw`
    rather than `rwc` fails on a missing file instead of creating an empty index
    that would read as "the archive is gone".
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def build(db_path: Path, *, echo: Callable[[str], None] = lambda _: None,
          meta_dir: Path | None = None,
          master_dir: Path | None = None) -> IndexStats:
    """Rebuild the index from the meta tier and master's decision sidecars.

    Wholesale, because this is the path that discovers **which files exist** —
    and that only changes when ingest runs, so it is the rare operation. The
    input is ~5KB per file, so even 62k is a small read. Changing a decision
    takes `refresh` instead.
    """
    meta_root = meta_dir if meta_dir is not None else META_DIR
    master_root = master_dir if master_dir is not None else MASTER_DIR

    conn = connect(db_path)
    stats = IndexStats()
    events: set[str] = set()

    try:
        with conn:
            conn.execute("DELETE FROM files")
            for folder, decided in _folders(meta_root, master_root):
                for record in _records(meta_root / folder):
                    row = _row(folder, record, decided)
                    if row is None:
                        stats.skipped.append(f"{folder}: unreadable record")
                        continue
                    conn.execute(_INSERT, row)
                    stats.files += 1
                    if row["capture_date"]:
                        stats.with_date += 1
                    if row["has_sidecar"]:
                        stats.with_sidecar += 1
                    if row["event"]:
                        events.add(str(row["event"]))
                echo(f"indexed {folder}")
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('built_at', ?)",
                         (str(int(time.time())),))
    finally:
        stats.events = len(events)

    return stats


_INSERT: str = (
    "INSERT OR REPLACE INTO files "
    "(folder, name, size, mtime_ns, kind, capture_date, camera, width, height, "
    " duration, event, tier, date_override, has_sidecar) "
    "VALUES (:folder, :name, :size, :mtime_ns, :kind, :capture_date, :camera, "
    " :width, :height, :duration, :event, :tier, :date_override, :has_sidecar)"
)


def refresh(conn: sqlite3.Connection, folder: str, name: str, *,
            meta_dir: Path | None = None,
            master_dir: Path | None = None) -> bool:
    """Re-read one file's facts and decision, and rewrite just its row.

    This is what makes curation usable. A full rebuild reads every record in the
    meta tier — minutes over SMB for 62k files — which is fine as an occasional
    maintenance step and absurd as the cost of tiering one photo. So a decision
    write updates the one row it changed.

    **Row-level only, deliberately.** It re-derives from the same two inputs a
    rebuild uses, so it cannot invent a value a rebuild would not produce, and
    it never removes or adds rows — the set of files is `process`'s business.
    Drift stays in one direction: the index can be behind, never wrong.

    Returns False when there are no probed facts for the file, which means it
    has not been processed and has no row to catch up. The sidecar is still the
    record; `pix2 index` picks it up once `process` has run.
    """
    meta_root = meta_dir if meta_dir is not None else META_DIR
    master_root = master_dir if master_dir is not None else MASTER_DIR

    record = _record(meta_root / folder / f"{name}.json")
    if record is None:
        return False
    media = master_root / folder / name
    decided = ({name: decisions.read(media)}
               if decisions.sidecar_path(media).is_file() else {})
    row = _row(folder, record, decided)
    if row is None:
        return False
    with conn:
        conn.execute(_INSERT, row)
    return True


def built_at(conn: sqlite3.Connection) -> float | None:
    """When the index was last built, as a unix timestamp.

    Staleness has no other signal: nothing watches the share, so an index that
    predates the last `process` run simply does not know about the files it
    made. Surfacing the age is how that gets noticed rather than experienced as
    photos mysteriously missing.
    """
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'built_at'").fetchone()
    except sqlite3.Error:
        return None
    try:
        return float(row["value"]) if row else None
    except (TypeError, ValueError):
        return None


def _folders(
    meta_root: Path, master_root: Path
) -> Iterator[tuple[str, dict[str, Decision | None]]]:
    """Each master folder in the meta tier, with the decisions made in it.

    Sidecars are **listed once per folder** rather than stat-ed per file: over
    SMB that is one round trip against tens of thousands. Only the sidecars that
    actually exist are then opened, which in the steady state is a small
    fraction — a file has no sidecar until a human decides something about it.
    """
    if not meta_root.is_dir():
        return
    for folder in sorted(p for p in meta_root.iterdir() if p.is_dir()):
        master_folder = master_root / folder.name
        decided: dict[str, Decision | None] = {}
        try:
            sidecars = [p.name for p in master_folder.iterdir()
                        if p.name.lower().endswith(".xmp")]
        except OSError:
            sidecars = []
        for sidecar in sidecars:
            media = master_folder / sidecar[: -len(".xmp")]
            # Keyed by presence, valued by content: a sidecar that will not
            # parse still counts as one. Master is the record, so a damaged file
            # there has to stay visible rather than reading as "never decided".
            decided[media.name] = decisions.read(media)
        yield folder.name, decided


def _records(folder: Path) -> Iterator[dict[str, Any]]:
    """Every metadata record in one folder of the meta tier."""
    try:
        paths = sorted(folder.iterdir())
    except OSError:
        return
    for path in paths:
        if path.suffix.lower() != ".json":
            continue
        record = _record(path)
        if record is not None:
            yield record


def _record(path: Path) -> dict[str, Any] | None:
    """One metadata record, or None if it is missing or unreadable."""
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else None


def _row(folder: str, record: dict[str, Any],
         decided: dict[str, Decision | None]) -> dict[str, Any] | None:
    """Flatten one metadata record into an index row."""
    name = record.get("file")
    if not isinstance(name, str) or not name:
        return None
    raw_exif: object = record.get("exif")
    exif_map: dict[str, Any] = (
        cast("dict[str, Any]", raw_exif) if isinstance(raw_exif, dict) else {})

    suffix = Path(name).suffix.lower()
    kind = ("video" if suffix in _VIDEO_EXTS
            else "image" if suffix in _IMAGE_EXTS else "other")

    width, height = _dimensions(exif_map)
    # Read-through, field by field: a `.xmp` decision wins, and where it is
    # silent the tag already embedded in the legacy file is the inherited value
    # (spec §4). Per-field rather than whole-record, so tiering a photo does not
    # hide the event it inherited.
    decision = decided.get(name)
    return {
        "folder": folder,
        "name": name,
        "size": record.get("size"),
        "mtime_ns": record.get("mtime_ns"),
        "kind": kind,
        "capture_date": _capture_date(exif_map),
        "camera": _tag(exif_map, "Model"),
        "width": width,
        "height": height,
        "duration": _duration(exif_map),
        "event": ((decision.event if decision else None)
                  or _tag(exif_map, "EventOverride")
                  or _tag(exif_map, "EventAuto")),
        "tier": (decision.tier if decision else None) or _tag(exif_map, "Tier"),
        "date_override": ((decision.date_override if decision else None)
                          or _tag(exif_map, "DateOverride")),
        "has_sidecar": 1 if name in decided else 0,
    }


def _tag(exif: dict[str, Any], key: str) -> str | None:
    """A tag by its bare name, ignoring ExifTool's group prefixes.

    ExifTool returns `XMP:EventAuto`, `EXIF:Model` and so on depending on where
    it found the value, so matching on the suffix is what makes this robust to
    the same fact living in different groups across file types.
    """
    for full, value in exif.items():
        if full.split(":")[-1] == key and value not in (None, ""):
            return str(value)
    return None


def _capture_date(exif: dict[str, Any]) -> str | None:
    """The raw capture reading, preferring the most specific source."""
    for key in ("DateTimeOriginal", "CreateDate", "MediaCreateDate",
                "ContentCreateDate"):
        value = _tag(exif, key)
        if value and not value.startswith("0000"):
            return value
    return None


def _dimensions(exif: dict[str, Any]) -> tuple[int | None, int | None]:
    width, height = _tag(exif, "ImageWidth"), _tag(exif, "ImageHeight")
    try:
        return (int(width) if width else None, int(height) if height else None)
    except ValueError:
        return (None, None)


def _duration(exif: dict[str, Any]) -> float | None:
    raw = _tag(exif, "Duration") or _tag(exif, "MediaDuration")
    if not raw:
        return None
    try:
        return float(str(raw).split()[0])
    except ValueError:
        return None


# --- queries -----------------------------------------------------------------

def events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every event with its file count and date range, largest first.

    Ordered by size because that is where the work is: the seeded year has one
    1,766-file "event" that is really a whole phone dump needing splitting, and
    it should be the first thing you see.
    """
    return list(conn.execute(
        "SELECT COALESCE(event, '(none)') AS event, COUNT(*) AS n, "
        "       MIN(capture_date) AS first_seen, MAX(capture_date) AS last_seen, "
        "       SUM(CASE WHEN tier IS NULL THEN 1 ELSE 0 END) AS unreviewed "
        "FROM files GROUP BY event ORDER BY n DESC"
    ))


def files(conn: sqlite3.Connection, *, event: str | None = None,
          tier: str | None = None, limit: int = 500,
          offset: int = 0) -> list[sqlite3.Row]:
    """Files, optionally filtered, in capture order."""
    where: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if event is not None:
        where.append("COALESCE(event, '(none)') = :event")
        params["event"] = event
    if tier is not None:
        where.append("tier IS :tier" if tier == "" else "tier = :tier")
        params["tier"] = tier or None
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return list(conn.execute(
        f"SELECT * FROM files {clause} "
        "ORDER BY capture_date IS NULL, capture_date, name "
        "LIMIT :limit OFFSET :offset", params
    ))


def summary(conn: sqlite3.Connection) -> sqlite3.Row:
    """Headline counts for the landing page."""
    return conn.execute(
        "SELECT COUNT(*) AS files, "
        "       COUNT(DISTINCT event) AS events, "
        "       SUM(CASE WHEN tier IS NULL THEN 1 ELSE 0 END) AS unreviewed, "
        "       SUM(CASE WHEN capture_date IS NULL THEN 1 ELSE 0 END) AS undated "
        "FROM files"
    ).fetchone()
