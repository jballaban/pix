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

`effective_date` and `year` sit alongside the raw `capture_date` and are the one
deliberate exception, because they are not interpretations — they are the defined
composition of a fact with a decision (`pix.datestr`), computed from two columns
of the same row. They exist because filtering and sorting a library by *the date
a photo actually has* is the common case, and re-deriving it per query is not.

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
from typing import Any, Callable, ClassVar, Iterator, Sequence, cast

from pix import datestr
from pix.nas import decisions
from pix.nas.const import MASTER_DIR, META_DIR
from pix.nas.decisions import Decision

#: Bumped whenever the shape changes. A mismatch drops and rebuilds rather than
#: migrating: the index is disposable by design, and a migration path is
#: machinery to maintain for something a `pix2 index` reproduces exactly.
SCHEMA_VERSION: int = 3

#: `audience` filter value meaning *nobody yet* — the "New" chip in the UI.
#: A sentinel rather than a separate reviewed flag: a file with no audience
#: has had no decision made about it, and modelling that twice invites the
#: two to disagree.
UNREVIEWED: str = "new"

#: Stand-ins for a missing value, so the landing page can link to those
#: groups like any other. A file with no date is a work item of its own —
#: 374 of them in the seeded year — not something to leave unreachable.
UNDATED: str = "(undated)"
NO_EVENT: str = "(none)"

#: Where *small/short*, *medium* and *large/long* fall. One filter whose
#: meaning follows the file: for a clip the question is length, for a photo
#: it is weight, and asking it as two controls would mean picking the right
#: one before you could ask.
#:
#: Tuned against the seeded year (5,685 photos, 724 clips): clip durations run
#: p25 3.2s / p50 9.7s / p75 17.9s, so 5s isolates the 32% that are throwaway
#: fragments and 60s the 5% that are real footage. Photos run p50 3.1MB, and
#: 500KB is well below anything a camera produces — it finds screenshots and
#: re-compressed messaging images rather than early-2000s originals, which
#: matters because those are still to be seeded.
SHORT_VIDEO_SECONDS: float = 5.0
LONG_VIDEO_SECONDS: float = 60.0
SMALL_IMAGE_BYTES: int = 500_000
LARGE_IMAGE_BYTES: int = 6_000_000

_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS files (
    folder         TEXT NOT NULL,
    name           TEXT NOT NULL,
    size           INTEGER,
    mtime_ns       INTEGER,
    kind           TEXT,          -- image | video | other
    capture_date   TEXT,          -- probed fact, ISO-ish, NULL if the file has none
    camera         TEXT,
    width          INTEGER,
    height         INTEGER,
    duration       REAL,
    event          TEXT,          -- decision, or inherited from embedded tags
    date_override  TEXT,          -- decision, may pin only some components
    effective_date TEXT,          -- capture_date as overridden; pix format
    year           TEXT,          -- first four of effective_date
    band           TEXT,          -- small | medium | large, by kind
    has_sidecar    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (folder, name)
);
CREATE INDEX IF NOT EXISTS files_event ON files(event);
CREATE INDEX IF NOT EXISTS files_year  ON files(year);
CREATE INDEX IF NOT EXISTS files_eff   ON files(effective_date);
CREATE INDEX IF NOT EXISTS files_kind  ON files(kind);
CREATE INDEX IF NOT EXISTS files_band  ON files(band);
CREATE TABLE IF NOT EXISTS file_tags (
    folder TEXT NOT NULL,
    name   TEXT NOT NULL,
    tag    TEXT NOT NULL,
    PRIMARY KEY (folder, name, tag)
);
CREATE INDEX IF NOT EXISTS file_tags_tag ON file_tags(tag);
CREATE TABLE IF NOT EXISTS file_audience (
    folder TEXT NOT NULL,
    name   TEXT NOT NULL,
    who    TEXT NOT NULL,
    PRIMARY KEY (folder, name, who)
);
CREATE INDEX IF NOT EXISTS file_audience_who ON file_audience(who);
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
    tags: int = 0
    skipped: list[str] = field(default_factory=lambda: [])


@dataclass(frozen=True)
class Filters:
    """What the browser is currently looking at.

    Every field is independent and ANDed. `None` means *not filtering on this*,
    which is distinct from filtering on an empty value — `event=""` would be a
    filter nothing matches, where `event=None` is no filter at all.
    """

    event: str | None = None
    year: str | None = None
    tag: str | None = None
    audience: str | None = None
    kind: str | None = None
    band: str | None = None

    #: Restricts every query to what this person may see. **Not a filter** —
    #: it is never read from a URL and cannot be cleared from one. A filter is
    #: a question the viewer asks; this is the answer to a question they are
    #: not allowed to ask.
    #: A **set** of names rather than one, because access is granted to a role
    #: as readily as to a person: someone in `parents` and `family` can see
    #: anything shared with either, and a share is just a name either way.
    viewer: frozenset[str] | None = None

    #: Every filterable column, in the order the top bar shows them. `viewer`
    #: is deliberately absent.
    NAMES: ClassVar[tuple[str, ...]] = ("event", "year", "tag", "audience",
                                       "kind", "band")

    def active(self) -> tuple[str, ...]:
        """Which filters are set, by name."""
        return tuple(n for n in self.NAMES if getattr(self, n) is not None)


@dataclass(frozen=True)
class Suggestion:
    """One option for a dropdown, with how relevant it is to the current view."""

    value: str
    n: int
    #: `all` — used by files matching every other active filter; `any` — used by
    #: files matching at least one; `other` — used elsewhere in the library.
    scope: str


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the index database, at the current schema.

    A schema mismatch drops everything and starts over. That is safe precisely
    because this file is a cache: the decisions live in master, and the only
    cost of throwing it away is the `pix2 index` that follows.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if _stored_version(conn) not in (None, SCHEMA_VERSION):
        conn.executescript(
            "DROP TABLE IF EXISTS files;"
            "DROP TABLE IF EXISTS file_tags;"
            "DROP TABLE IF EXISTS file_audience;")
    conn.executescript(_SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def _stored_version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema'").fetchone()
    except sqlite3.Error:
        return None
    try:
        return int(row["value"]) if row else None
    except (TypeError, ValueError):
        return None


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
    _require_current(conn)
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
    _require_current(conn)
    return conn


class StaleIndex(RuntimeError):
    """The index on disk predates the code reading it."""


def _require_current(conn: sqlite3.Connection) -> None:
    """Refuse an index this code cannot read, loudly.

    A schema bump drops the old tables the next time anything opens the file
    for writing, which leaves a **valid, empty** index behind. Readers then
    answer every question with zero rows, and an archive that reports itself
    as empty is indistinguishable from one that is gone. Saying so is the
    difference between a five-second fix and an afternoon.
    """
    found = _stored_version(conn)
    if found == SCHEMA_VERSION:
        return
    raise StaleIndex(
        f"index is schema v{found}, this build reads v{SCHEMA_VERSION} — "
        "run pix 0.1.251 to rebuild it")


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
    events_seen: set[str] = set()
    tags_seen: set[str] = set()

    try:
        with conn:
            conn.execute("DELETE FROM files")
            conn.execute("DELETE FROM file_tags")
            conn.execute("DELETE FROM file_audience")
            for folder, decided in _folders(meta_root, master_root):
                for record in _records(meta_root / folder):
                    row = _row(folder, record, decided)
                    if row is None:
                        stats.skipped.append(f"{folder}: unreadable record")
                        continue
                    conn.execute(_INSERT, row)
                    name = str(row["name"])
                    decision = decided.get(name)
                    if decision is not None:
                        _write_multi(conn, folder, name, decision)
                        tags_seen.update(decision.tags)
                    stats.files += 1
                    if row["capture_date"]:
                        stats.with_date += 1
                    if row["has_sidecar"]:
                        stats.with_sidecar += 1
                    if row["event"]:
                        events_seen.add(str(row["event"]))
                echo(f"indexed {folder}")
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('built_at', ?)",
                         (str(int(time.time())),))
    finally:
        stats.events = len(events_seen)
        stats.tags = len(tags_seen)

    return stats


_INSERT: str = (
    "INSERT OR REPLACE INTO files "
    "(folder, name, size, mtime_ns, kind, capture_date, camera, width, height, "
    " duration, event, date_override, effective_date, year, band, "
    " has_sidecar) "
    "VALUES (:folder, :name, :size, :mtime_ns, :kind, :capture_date, :camera, "
    " :width, :height, :duration, :event, :date_override, "
    " :effective_date, :year, :band, :has_sidecar)"
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
    decided: dict[str, Decision | None] = (
        {name: decisions.read(media)}
        if decisions.sidecar_path(media).is_file() else {})
    row = _row(folder, record, decided)
    if row is None:
        return False
    with conn:
        conn.execute(_INSERT, row)
        _write_multi(conn, folder, name, decided.get(name))
    return True


def _write_multi(conn: sqlite3.Connection, folder: str, name: str,
                 decision: Decision | None) -> None:
    """Replace one file's tag and audience rows.

    Delete-then-insert, or un-sharing a file would silently do nothing — and
    a share that cannot be taken back is not access control.
    """
    for table, column, values in (
        ("file_tags", "tag", decision.tags if decision else ()),
        ("file_audience", "who", decision.audience if decision else ()),
    ):
        conn.execute(f"DELETE FROM {table} WHERE folder = ? AND name = ?",
                     (folder, name))
        if values:
            conn.executemany(
                f"INSERT OR REPLACE INTO {table} (folder, name, {column}) "
                "VALUES (?,?,?)",
                [(folder, name, v) for v in values])


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
    capture = _capture_date(exif_map)
    override = ((decision.date_override if decision else None)
                or _tag(exif_map, "DateOverride"))
    effective = datestr.effective(
        datestr.parse_exiftool(capture) if capture else None, override)
    return {
        "folder": folder,
        "name": name,
        "size": record.get("size"),
        "mtime_ns": record.get("mtime_ns"),
        "kind": kind,
        "capture_date": capture,
        "camera": _tag(exif_map, "Model"),
        "width": width,
        "height": height,
        "duration": _duration(exif_map),
        "event": ((decision.event if decision else None)
                  or _tag(exif_map, "EventOverride")
                  or _tag(exif_map, "EventAuto")),
        "date_override": override,
        "effective_date": datestr.format_pix(effective) if effective else None,
        "year": f"{effective.year:04d}" if effective else None,
        "band": _band(kind, record.get("size"), _duration(exif_map)),
        "has_sidecar": 1 if name in decided else 0,
    }


def _band(kind: str, size: object, duration: float | None) -> str | None:
    """Which size band a file falls in — by length for video, weight for stills.

    One axis rather than two because the question being asked is the same one:
    *is this a throwaway?* A 3-second clip and a 50KB image are the same kind
    of suspect, and making the curator pick the right control first would be
    asking them to know the answer before the question.
    """
    if kind == "video":
        if duration is None:
            return None
        return ("small" if duration < SHORT_VIDEO_SECONDS
                else "large" if duration > LONG_VIDEO_SECONDS else "medium")
    if not isinstance(size, int):
        return None
    return ("small" if size < SMALL_IMAGE_BYTES
            else "large" if size > LARGE_IMAGE_BYTES else "medium")


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
    """Seconds, from either shape ExifTool emits.

    It writes `12.53 s` for some sources and `0:00:38` for others — 91 of the
    seeded year's 724 clips take the second form, and reading only the first
    made them render as the word "video" instead of a length.
    """
    raw = _tag(exif, "Duration") or _tag(exif, "MediaDuration")
    if not raw:
        return None
    text = str(raw).strip()
    if ":" in text:
        try:
            parts = [float(p) for p in text.split(":")]
        except ValueError:
            return None
        seconds = 0.0
        for part in parts:                      # H:MM:SS, or MM:SS
            seconds = seconds * 60 + part
        return seconds
    try:
        return float(text.split()[0])
    except (ValueError, IndexError):
        return None


# --- filtering ---------------------------------------------------------------

def _clauses(filters: Filters) -> dict[str, tuple[str, dict[str, Any]]]:
    """Each active filter as a SQL fragment plus its parameters."""
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    if filters.event is not None:
        out["event"] = ("COALESCE(files.event, '(none)') = :f_event",
                        {"f_event": filters.event})
    if filters.year is not None:
        out["year"] = ("COALESCE(files.year, :undated) = :f_year",
                       {"f_year": filters.year, "undated": UNDATED})
    if filters.tag is not None:
        out["tag"] = (
            "EXISTS (SELECT 1 FROM file_tags ft WHERE ft.folder = files.folder "
            "AND ft.name = files.name AND ft.tag = :f_tag)",
            {"f_tag": filters.tag})
    if filters.audience is not None:
        shared = ("EXISTS (SELECT 1 FROM file_audience fa "
                  "WHERE fa.folder = files.folder AND fa.name = files.name")
        out["audience"] = (
            (f"NOT {shared})" if filters.audience == UNREVIEWED
             else f"{shared} AND fa.who = :f_audience)"),
            {} if filters.audience == UNREVIEWED
            else {"f_audience": filters.audience})
    if filters.kind is not None:
        out["kind"] = ("files.kind = :f_kind", {"f_kind": filters.kind})
    if filters.band is not None:
        out["band"] = ("files.band = :f_band", {"f_band": filters.band})
    return out


def _scope(filters: Filters) -> tuple[str, dict[str, Any]]:
    """The clause restricting a non-admin to what has been shared with them.

    Applied to **every** query rather than folded into `_clauses`, so it
    cannot be dropped by a caller that forgets it or overridden by a query
    parameter. An admin has no scope at all: seeing everything is what the
    role means.

    An **empty** set is not the same as no scope — it is somebody who has been
    granted nothing, and must see nothing. Treating the two alike is the classic
    way an access check turns into an access grant.
    """
    if filters.viewer is None:
        return "", {}
    if not filters.viewer:
        return "0", {}
    names = {f"scope{i}": who for i, who in enumerate(sorted(filters.viewer))}
    holes = ",".join(f":{k}" for k in names)
    return ("EXISTS (SELECT 1 FROM file_audience fv "
            "WHERE fv.folder = files.folder AND fv.name = files.name "
            f"AND fv.who IN ({holes}))", dict(names))


def _where(filters: Filters) -> tuple[str, dict[str, Any]]:
    """Everything a listing must satisfy: the filters, and the viewer scope."""
    clauses = _clauses(filters)
    parts = [sql for sql, _ in clauses.values()]
    params = _bind(clauses)
    scope, scope_params = _scope(filters)
    if scope:
        parts.append(scope)
        params.update(scope_params)
    return (" AND ".join(parts), params)


def _bind(clauses: dict[str, tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for _, bound in clauses.values():
        params.update(bound)
    return params


def _combine(clauses: dict[str, tuple[str, dict[str, Any]]],
             joiner: str) -> str:
    """Fold fragments into one expression, with `1` standing for "no filters"."""
    if not clauses:
        return "1"
    return "(" + f" {joiner} ".join(sql for sql, _ in clauses.values()) + ")"


# --- queries -----------------------------------------------------------------

def events(conn: sqlite3.Connection,
           filters: Filters | None = None) -> list[sqlite3.Row]:
    """Every (year, event) pair, newest year first and biggest event within it.

    **Scoped like everything else.** An event name is information: a list of
    every trip and birthday in the house, shown to somebody who can open none
    of the photographs, leaks exactly what the audience model exists to keep.
    Somebody who has been shared nothing sees an empty page, not a table of
    counts they cannot click through.

    Grouped by year because a library is remembered that way and worked
    through that way — a flat list of every event across twenty-five years is
    a list nobody can find their place in. Within a year, by size, because
    that is where the work is: the seeded year has one 1,766-file event that
    is really a whole phone dump needing splitting.

    An event spanning New Year appears under **both** years, each with that
    year's count. That is not a duplicate to be collapsed: the row is a link
    to `year + event`, and the two halves are genuinely different slices of
    work.
    """
    where, bound = _where(filters or Filters())
    return list(conn.execute(
        "SELECT COALESCE(year, :undated) AS year, "
        "       COALESCE(event, :none) AS event, COUNT(*) AS n, "
        "       MIN(effective_date) AS first_seen, "
        "       MAX(effective_date) AS last_seen, "
        "       SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM file_audience fa "
        "         WHERE fa.folder = files.folder AND fa.name = files.name) "
        "       THEN 1 ELSE 0 END) AS unreviewed "
        "FROM files " + (f"WHERE {where} " if where else "")
        + "GROUP BY year, event "
        # On the raw column, not the alias: `NULL = '(undated)'` is NULL,
        # and SQLite sorts NULLs first — which put the undated group at the
        # top of the page instead of the bottom.
        "ORDER BY files.year IS NULL, files.year DESC, n DESC",
        {**bound, "undated": UNDATED, "none": NO_EVENT}
    ))


def files(conn: sqlite3.Connection, filters: Filters | None = None, *,
          limit: int = 500, offset: int = 0) -> list[sqlite3.Row]:
    """Files matching every active filter, in effective-date order.

    Undated files sort last rather than scattering through the grid: they are a
    work item of their own, not a date that happens to be small.
    """
    where, bound = _where(filters or Filters())
    params: dict[str, Any] = {**bound, "limit": limit, "offset": offset}
    return list(conn.execute(
        "SELECT files.*, " + _TAGS_COL + ", " + _AUDIENCE_COL + " FROM files "
        + (f"WHERE {where} " if where else "")
        + "ORDER BY effective_date IS NULL, effective_date, name "
        "LIMIT :limit OFFSET :offset", params
    ))


_TAGS_COL: str = (
    "(SELECT group_concat(ft.tag, char(10)) FROM file_tags ft "
    " WHERE ft.folder = files.folder AND ft.name = files.name) AS tags")
_AUDIENCE_COL: str = (
    "(SELECT group_concat(fa.who, char(10)) FROM file_audience fa "
    " WHERE fa.folder = files.folder AND fa.name = files.name) AS audience")


def one(conn: sqlite3.Connection, folder: str,
        name: str) -> sqlite3.Row | None:
    """A single row with its tags, or None if the file is not indexed."""
    return conn.execute(
        "SELECT files.*, " + _TAGS_COL + ", " + _AUDIENCE_COL
        + " FROM files WHERE folder = ? AND name = ?",
        (folder, name)).fetchone()


def record_for(folder: str, name: str, *,
               meta_dir: Path | None = None) -> dict[str, Any] | None:
    """The probed facts `process` wrote for one file.

    Read straight from the meta tier rather than cached in the index: it is
    ~176 keys per file, wanted only when someone opens one photograph, and
    duplicating it into 62k rows to serve that would be the wrong trade.
    """
    root = meta_dir if meta_dir is not None else META_DIR
    return _record(root / folder / f"{name}.json")


def tag(exif: dict[str, Any], key: str) -> str | None:
    """Public name for the group-insensitive tag lookup (see `_tag`)."""
    return _tag(exif, key)


def count(conn: sqlite3.Connection, filters: Filters | None = None) -> int:
    """How many files match, regardless of the page being shown."""
    where, bound = _where(filters or Filters())
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM files " + (f"WHERE {where}" if where else ""),
        bound).fetchone()
    return int(row["n"])


def matching(conn: sqlite3.Connection, filters: Filters,
             targets: Sequence[tuple[str, str]]) -> set[tuple[str, str]]:
    """Which of `targets` still match `filters`.

    Asked after a write, so the grid can drop the files that no longer belong in
    the view — add a date while filtered to undated, and those files should
    leave. The client cannot work this out for itself: a partial override merges
    with the capture date here, so only the index knows the resulting year. Re-
    using the same clauses the listing uses is what keeps the two from drifting.
    """
    if not targets:
        return set()
    where, bound = _where(filters)
    # Joined on a newline because neither component can contain one, so the pair
    # round-trips exactly — no separator a filename could forge.
    sep = chr(10)
    keys = {f"k{i}": folder + sep + name
            for i, (folder, name) in enumerate(targets)}
    holes = ",".join(f":{k}" for k in keys)
    rows = conn.execute(
        "SELECT folder, name FROM files WHERE "
        + (f"({where}) AND " if where else "")
        + f"files.folder || char(10) || files.name IN ({holes})",
        {**bound, **keys})
    return {(str(r["folder"]), str(r["name"])) for r in rows}


def suggest(conn: sqlite3.Connection, column: str,
            filters: Filters | None = None, *,
            limit: int = 400) -> list[Suggestion]:
    """Existing values for `column`, most relevant to the current view first.

    Three bands, in order: values used by files matching **every** other active
    filter, then by files matching **any** of them, then everything else.
    Looking at `year:2026 + tag:tv` and reaching for an event name, the events
    already used in that exact slice come first, the ones used in either slice
    next, and the rest of the library last. With no other filters set, every
    value lands in the first band and the list is simply most-used-first.

    The filter on `column` itself is excluded from the scope, or the first band
    would only ever contain the value already being filtered on — which is the
    one option nobody is reaching for.

    The **viewer** restriction is not part of that banding and is never
    dropped: a dropdown listing every event in the house to somebody who can
    open none of them tells them exactly what they were not shown.
    """
    view = filters or Filters()
    clauses = _clauses(view)
    clauses.pop(column, None)
    seen, seen_params = _scope(view)
    params: dict[str, Any] = {**_bind(clauses), **seen_params, "limit": limit}
    tally = (f"COUNT(*) AS n, "
             f"SUM(CASE WHEN {_combine(clauses, 'AND')} THEN 1 ELSE 0 END) AS n_all, "
             f"SUM(CASE WHEN {_combine(clauses, 'OR')} THEN 1 ELSE 0 END) AS n_any ")
    order = ("ORDER BY n_all > 0 DESC, n_any > 0 DESC, n DESC, value "
             "LIMIT :limit")

    def visible(*extra: str) -> str:
        """The WHERE that every suggestion is drawn from."""
        parts = [p for p in (seen, *extra) if p]
        return f"WHERE {' AND '.join(parts)} " if parts else ""

    if column in ("tag", "audience"):
        table, col = (("file_tags", "tag") if column == "tag"
                      else ("file_audience", "who"))
        sql = (f"SELECT m.{col} AS value, " + tally
               + f"FROM {table} m "
                 "JOIN files ON files.folder = m.folder AND files.name = m.name "
               + visible() + "GROUP BY value " + order)
    elif column == "year":
        # Undated files are offered as a year, because *show me the ones with
        # no date* is a real piece of work rather than an absence to hide.
        params["undated"] = UNDATED
        sql = ("SELECT COALESCE(files.year, :undated) AS value, " + tally
               + "FROM files " + visible() + "GROUP BY value " + order)
    elif column in ("event", "kind", "band"):
        sql = (f"SELECT files.{column} AS value, " + tally
               + "FROM files "
               + visible(f"files.{column} IS NOT NULL")
               + "GROUP BY value " + order)
    else:
        raise ValueError(f"cannot suggest values for {column!r}")

    return [Suggestion(value=str(r["value"]), n=int(r["n"]),
                       scope=("all" if r["n_all"] else
                              "any" if r["n_any"] else "other"))
            for r in conn.execute(sql, params)]


def summary(conn: sqlite3.Connection,
            filters: Filters | None = None) -> sqlite3.Row:
    """Headline counts for the landing page, scoped to the viewer."""
    where, bound = _where(filters or Filters())
    return conn.execute(
        "SELECT COUNT(*) AS files, "
        "       COUNT(DISTINCT event) AS events, "
        "       SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM file_audience fa "
        "         WHERE fa.folder = files.folder AND fa.name = files.name) "
        "       THEN 1 ELSE 0 END) AS unreviewed, "
        "       SUM(CASE WHEN effective_date IS NULL THEN 1 ELSE 0 END) AS undated "
        "FROM files " + (f"WHERE {where}" if where else ""), bound
    ).fetchone()
