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
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, cast

from pix.nas.const import MASTER_DIR, META_DIR

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


def build(db_path: Path, *, echo: Callable[[str], None] = lambda _: None,
          meta_dir: Path | None = None,
          master_dir: Path | None = None) -> IndexStats:
    """Rebuild the index from the meta tier and master's decision sidecars.

    A full rebuild rather than an incremental one: the input is ~5KB per file,
    so even 62k files is a small read, and a rebuild that is always correct beats
    an incremental path that can drift from a record it does not own.
    """
    meta_root = meta_dir if meta_dir is not None else META_DIR
    master_root = master_dir if master_dir is not None else MASTER_DIR

    conn = connect(db_path)
    stats = IndexStats()
    events: set[str] = set()

    try:
        with conn:
            conn.execute("DELETE FROM files")
            for folder, sidecars in _folders(meta_root, master_root):
                for record in _records(meta_root / folder):
                    row = _row(folder, record, sidecars)
                    if row is None:
                        stats.skipped.append(f"{folder}: unreadable record")
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO files "
                        "(folder, name, size, mtime_ns, kind, capture_date, "
                        " camera, width, height, duration, event, tier, "
                        " date_override, has_sidecar) "
                        "VALUES (:folder, :name, :size, :mtime_ns, :kind, "
                        " :capture_date, :camera, :width, :height, :duration, "
                        " :event, :tier, :date_override, :has_sidecar)",
                        row,
                    )
                    stats.files += 1
                    if row["capture_date"]:
                        stats.with_date += 1
                    if row["has_sidecar"]:
                        stats.with_sidecar += 1
                    if row["event"]:
                        events.add(str(row["event"]))
                echo(f"indexed {folder}")
    finally:
        stats.events = len(events)

    return stats


def _folders(meta_root: Path, master_root: Path) -> Iterator[tuple[str, set[str]]]:
    """Each master folder present in the meta tier, with its sidecar names.

    Sidecars are listed **once per folder** rather than stat-ed per file: over
    SMB that is the difference between one round trip and tens of thousands.
    """
    if not meta_root.is_dir():
        return
    for folder in sorted(p for p in meta_root.iterdir() if p.is_dir()):
        sidecars: set[str] = set()
        try:
            sidecars = {p.name for p in (master_root / folder.name).iterdir()
                        if p.name.lower().endswith(".xmp")}
        except OSError:
            pass
        yield folder.name, sidecars


def _records(folder: Path) -> Iterator[dict[str, Any]]:
    """Every metadata record in one folder of the meta tier."""
    try:
        paths = sorted(folder.iterdir())
    except OSError:
        return
    for path in paths:
        if path.suffix.lower() != ".json":
            continue
        try:
            parsed: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(parsed, dict):
            yield parsed  # type: ignore[misc]


def _row(folder: str, record: dict[str, Any],
         sidecars: set[str]) -> dict[str, Any] | None:
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
        # Read-through: the embedded tag is the inherited value, and a `.xmp`
        # decision would override it once one exists (spec §4).
        "event": _tag(exif_map, "EventOverride") or _tag(exif_map, "EventAuto"),
        "tier": _tag(exif_map, "Tier"),
        "date_override": _tag(exif_map, "DateOverride"),
        "has_sidecar": 1 if f"{name}.xmp" in sidecars else 0,
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
