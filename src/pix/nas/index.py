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
import re
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
SCHEMA_VERSION: int = 8

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

#: How the grid can be cut into sections, and the SQL that computes the key.
#: A **day** by default: a day is the unit people remember photographs in — *the
#: afternoon at the lake* — where an event is usually several of them and a
#: single folder is thousands. **A grouping is not a filter** — nothing may
#: fall out of the grid because of one — so a key that cannot be known reads as
#: NULL and its files gather in a section of their own.
#:
#: The date keys ask `precision` first. A file dated *August 2026, day unknown*
#: has an `effective_date` of `2026-08-01`, because there has to be something
#: to sort by; grouping on that filed it under the first of August beside
#: photographs actually taken that day. Inventing the day is worse than
#: admitting there is none.
GROUPINGS: dict[str, str | None] = {
    "none": None,
    "day": ("CASE WHEN files.precision >= 10 "
            "THEN substr(files.effective_date, 1, 10) END"),
    "month": ("CASE WHEN files.precision >= 7 "
              "THEN substr(files.effective_date, 1, 7) END"),
    "year": "CASE WHEN files.precision >= 4 THEN files.year END",
    "event": "COALESCE(files.event, '(none)')",
    "camera": "COALESCE(files.camera, '(unknown)')",
    "kind": "files.kind",
    # A stack is a section like any other: the file that speaks for the others
    # and the others themselves, together. Grouping by it is what *opens* every
    # stack in the view at once — which is the only way to review a shelf of
    # them, and the reason a suggestion needs no page of its own.
    #
    # Only what is actually in one. A photograph that stands alone is not a
    # stack of one, and giving each its own section would bury the sections
    # that mean something under a heading per thumbnail.
    "stack": ("CASE WHEN files.stacked_under IS NOT NULL"
              "       OR files.suggested_under IS NOT NULL"
              "       OR EXISTS (SELECT 1 FROM files m"
              "                  WHERE m.stacked_under ="
              "                        files.folder || '/' || files.name"
              "                     OR m.suggested_under ="
              "                        files.folder || '/' || files.name)"
              "     THEN COALESCE(files.stacked_under, files.suggested_under,"
              "                   files.folder || '/' || files.name) END"),
}

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
    deleted        INTEGER NOT NULL DEFAULT 0,   -- decision: soft-deleted
    precision      INTEGER NOT NULL DEFAULT 0,   -- how much of the date is known
    stacked_under  TEXT,          -- decision: `folder/name` this sits behind
    no_stack       INTEGER NOT NULL DEFAULT 0,   -- decision: never suggest this
    suggested_under TEXT,         -- guessed: the `folder/name` this looks like
    PRIMARY KEY (folder, name)
);
CREATE INDEX IF NOT EXISTS files_event ON files(event);
CREATE INDEX IF NOT EXISTS files_year  ON files(year);
CREATE INDEX IF NOT EXISTS files_eff   ON files(effective_date);
CREATE INDEX IF NOT EXISTS files_kind  ON files(kind);
CREATE INDEX IF NOT EXISTS files_band  ON files(band);
-- Every listing carries `deleted = 0`, so it is the one clause always present.
CREATE INDEX IF NOT EXISTS files_del   ON files(deleted);
-- Read twice for every listing: once to hide what is stacked, once to count
-- what is behind each file that is not.
CREATE INDEX IF NOT EXISTS files_stack ON files(stacked_under);
-- The same two reads again, for the stacks nobody has confirmed yet.
CREATE INDEX IF NOT EXISTS files_sugg  ON files(suggested_under);
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
    #: Guessed stacks found — reported because it is the size of a pile of work
    #: that appeared without anybody asking for it.
    suggested: int = 0
    skipped: list[str] = field(default_factory=lambda: [])


@dataclass(frozen=True)
class Filters:
    """What the browser is currently looking at.

    Every field is independent and ANDed. `None` means *not filtering on this*,
    which is distinct from filtering on an empty value — `event=""` would be a
    filter nothing matches, where `event=None` is no filter at all.
    """

    event: str | None = None
    #: A **prefix** of the effective date, not a year: `2026`, `2026-08` or
    #: `2026-08-30`. One filter for three questions, because they are the same
    #: question asked at three widths, and a library is narrowed down in
    #: exactly that order. `UNDATED` is the fourth value it takes, and is a
    #: state rather than a date — *the ones nobody could place* is real work.
    date: str | None = None
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

    #: One stack, opened: the file named and everything stacked behind it.
    #: Without it a listing shows only what speaks for itself — the tops of
    #: stacks and everything unstacked — which is the whole point of stacking.
    within: str | None = None

    #: An explicit set of `(folder, name)`, and the one filter that is not a
    #: question about the files. *These particular ones* is what a link from
    #: the operation log needs: the files one edit touched have nothing in
    #: common the index could be asked about, and by the time you want to look
    #: at them they may have nothing in common at all.
    #:
    #: An empty tuple matches nothing, which is right — an operation that
    #: touched no files should show no files, not every file.
    chosen: tuple[tuple[str, str], ...] | None = None

    #: Which side of the deletion line to show. `None` — the default, and what
    #: everyone other than an administrator ever gets — is the living only.
    #: `only` is the bin; `with` shows both, which is the view where marking
    #: the deleted ones on the thumbnail earns its keep.
    #:
    #: It *is* a filter, unlike `viewer`: an administrator turns it on and off
    #: from the bar and it belongs in the URL like the rest. What is not
    #: negotiable is the default and who may change it — `web.filters` ignores
    #: the parameter entirely for a non-admin, so the ordinary grid cannot be
    #: talked into showing what somebody said should be gone.
    deleted: str | None = None

    #: Whether the app's own guesses are folded into the view, and how far.
    #: `None` — the default — is the library as people left it: a suggestion
    #: changes nothing until somebody accepts it. `with` folds each guessed
    #: group behind one of its photographs, so browsing *is* reviewing; `only`
    #: shows nothing else, which is the shelf of everything still to answer.
    #:
    #: Administrators only, like `deleted` and for the same reason: this hides
    #: photographs from a viewer on the strength of a guess, and only the
    #: person who can accept or refuse it should be able to turn it on.
    stacks: str | None = None

    #: **Not a filter** — a consequence of grouping by stack, which opens every
    #: stack in the view. It lives here because `_always` is the one place that
    #: decides what a listing holds, and the count, the grid and the *did this
    #: leave the view* check all have to agree about it. Set from the grouping
    #: by `web.filters`, never from a query parameter of its own.
    unfold: bool = False

    #: Every filterable column, in the order the top bar shows them, and what
    #: the page is handed so that it can rebuild its own address. `viewer` is
    #: deliberately absent — it is not a question the viewer is allowed to ask.
    #:
    #: `deleted` was missing from this while being offered as a chip, so the
    #: chip could never show what it was set to, and an edit made in the bin
    #: told the server it had been made in the ordinary grid — which is how it
    #: decides whether a file has left the view. Restoring a file left it on
    #: screen in a listing of the deleted.
    NAMES: ClassVar[tuple[str, ...]] = ("event", "tag", "date", "audience",
                                       "kind", "band", "deleted", "stacks",
                                       "within")


@dataclass(frozen=True)
class Suggestion:
    """One option for a dropdown, with how relevant it is to the current view."""

    value: str
    n: int
    #: `near` — an event whose own dates span what is selected; `all` — used by
    #: files matching every other active filter; `any` — used by files matching
    #: at least one; `other` — used elsewhere in the library.
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
        "run `pix2 index` to rebuild it")


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
            # Last, because it is a question about the library rather than
            # about any one file, and it cannot be asked until they are all in.
            stats.suggested = resuggest(conn)
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
    " has_sidecar, deleted, precision, stacked_under, no_stack) "
    "VALUES (:folder, :name, :size, :mtime_ns, :kind, :capture_date, :camera, "
    " :width, :height, :duration, :event, :date_override, "
    " :effective_date, :year, :band, :has_sidecar, :deleted, :precision,"
    " :stacked_under, :no_stack)"
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
    # Which guessed group this file was in, read before the row is replaced —
    # `_INSERT` does not carry `suggested_under`, so writing the row is already
    # this file leaving whatever it was part of.
    before = conn.execute(
        "SELECT suggested_under FROM files WHERE folder = ? AND name = ?",
        (folder, name)).fetchone()
    was = (str(before["suggested_under"])
           if before and before["suggested_under"] else None)
    with conn:
        conn.execute(_INSERT, row)
        _write_multi(conn, folder, name, decided.get(name))
        _regroup(conn, folder, name, was)
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
    captured = datestr.parse_exiftool(capture) if capture else None
    effective = datestr.effective(captured, override)
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
        "deleted": 1 if (decision and decision.deleted) else 0,
        "precision": datestr.precision(captured, override),
        "stacked_under": decision.stacked_under if decision else None,
        "no_stack": 1 if (decision and decision.no_stack) else 0,
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

#: `2026`, `2026-08`, `2026-08-30` — and nothing else.
_DATE_PREFIX = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


def date_prefix(value: str | None) -> str | None:
    """A date filter value, or None if it is not one.

    Anything unrecognisable is dropped rather than refused, the same way a
    grouping typo is: this comes out of a URL, which people type and edit by
    hand, and a half-written date should narrow nothing rather than 500.

    The width is what the clause interpolates into `substr`, so it has to be a
    number this module chose — never one a request did.
    """
    if value is None:
        return None
    value = value.strip()
    if value == UNDATED or _DATE_PREFIX.match(value):
        return value
    return None


def _clauses(filters: Filters) -> dict[str, tuple[str, dict[str, Any]]]:
    """Each active filter as a SQL fragment plus its parameters."""
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    if filters.within:
        out["within"] = (
            "(files.stacked_under = :f_within "
            " OR files.suggested_under = :f_within "
            " OR files.folder || '/' || files.name = :f_within)",
            {"f_within": filters.within})
    if filters.stacks == "only":
        # Both halves of a guessed group: the photograph that would speak for
        # it, and the ones that would sit behind it. Which of them a listing
        # actually shows is `_always`'s business — folded, only the first;
        # opened, all of them — and saying it once here keeps the two answers
        # from being two different ideas of what a suggestion is.
        out["stacks"] = (
            "(files.suggested_under IS NOT NULL OR EXISTS ("
            " SELECT 1 FROM files s"
            " WHERE s.suggested_under = files.folder || '/' || files.name))",
            {})
    if filters.chosen is not None:
        # One parameter rather than one per file: a single event edit can run
        # to seventeen hundred files, and a placeholder each would be an
        # expression the database refuses to compile.
        out["chosen"] = (
            "files.folder || char(10) || files.name IN "
            "(SELECT value FROM json_each(:f_chosen))",
            {"f_chosen": json.dumps(
                [f + chr(10) + n for f, n in filters.chosen])})
    if filters.event is not None:
        out["event"] = ("COALESCE(files.event, '(none)') = :f_event",
                        {"f_event": filters.event})
    if filters.date == UNDATED:
        # Undated is *no date at all*, not *not to that precision*. A file
        # known to be from August is not undated, and answering a day filter
        # with it would be the same invention grouping used to make.
        out["date"] = ("files.effective_date IS NULL", {})
    elif filters.date is not None:
        # Matched by prefix, at whatever width the value was given in, so one
        # clause answers year, month and day — and only where the date is known
        # to that width. `files.year` is left alone: it is the landing page's
        # index, and is no longer what this filters on.
        width = len(filters.date)
        out["date"] = (
            f"(files.precision >= {width} "
            f"AND substr(files.effective_date, 1, {width}) = :f_date)",
            {"f_date": filters.date})
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
    """Everything a listing must satisfy: the filters, the viewer scope, and
    which side of the deletion line the caller is on.

    Three states, and the **default is the safe one**: anything that is not
    explicitly asking for the deleted gets the living, so a caller that knows
    nothing about deletion cannot accidentally list it.

    The deletion clause is added **here**, in the one place every query
    already passes through, rather than at each call site. `files`, `count`,
    `events`, `suggest` and the year listing would each have had to remember
    it, and the one that forgot would put deleted files back on screen — which
    is the single way this feature can fail that a curator would not forgive.
    """
    clauses = _clauses(filters)
    parts = [sql for sql, _ in clauses.values()] + _always(filters)
    params = _bind(clauses)
    scope, scope_params = _scope(filters)
    if scope:
        parts.append(scope)
        params.update(scope_params)
    return (" AND ".join(parts), params)


def _always(filters: Filters) -> list[str]:
    """What is in the library at all, whatever is being asked about it.

    Two states take a file out of the ordinary view and neither is a filter:
    deleted, and stacked behind another. They are not optional, not banded, and
    not something a question can decline to apply — so they are not in
    `_clauses` with the filters, and every query has to carry them.

    Which is why they live here rather than in `_where`. `_where` is the grid's
    path; the dropdowns take their own, and had been answering with files the
    grid would never show. A tag carried only by a deleted file was offered as
    a filter, and clicking it gave an empty grid. A date whose only photograph
    was stacked behind another was counted twice over — once for the file you
    can see and once for the one you cannot.
    """
    out: list[str] = []
    if filters.deleted == "only":
        out.append("files.deleted = 1")
    elif filters.deleted != "with":
        out.append("files.deleted = 0")
    # A file stacked behind another does not appear on its own — that is what
    # stacking is. Opening one stack is the exception, and says which.
    #
    # Grouping by stack is the other exception, and a wider one: it opens every
    # stack in the view at once, each as its own section. That is a listing of
    # *photographs* rather than of what speaks for them, so nothing is hidden.
    if not filters.within and not filters.unfold:
        out.append("files.stacked_under IS NULL")
        # A guess hides nothing until it is turned on. With it on, a guessed
        # group behaves like a stack — one photograph on screen, the rest
        # behind it — because a suggestion you have to read as eight separate
        # files is not a suggestion, it is the pile you already had.
        if filters.stacks:
            out.append("files.suggested_under IS NULL")
    return out


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
          groups: Sequence[str] = (),
          limit: int = 500, offset: int = 0) -> list[sqlite3.Row]:
    """Files matching every active filter, in effective-date order.

    Undated files sort last rather than scattering through the grid: they are a
    work item of their own, not a date that happens to be small.

    `groups` names one or more keys — `("event", "day")` — and adds a `grp0`,
    `grp1` … column for each, sorting by them outermost-first. A page can then
    cut the grid into nested sections without a second query or a second idea
    of the order. Within the innermost section the order is unchanged:
    chronological, because that is how a day of photographs reads.
    """
    where, bound = _where(filters or Filters())
    keys = [GROUPINGS[g] for g in groups if GROUPINGS.get(g)]
    params: dict[str, Any] = {**bound, "limit": limit, "offset": offset}
    selected = "".join(f", {key} AS grp{i} " for i, key in enumerate(keys))
    ordered = "".join(f"grp{i} IS NULL, grp{i}, " for i in range(len(keys)))
    return list(conn.execute(
        "SELECT files.*, " + _TAGS_COL + ", " + _AUDIENCE_COL
        + ", " + _BEHIND_COL + ", " + _AHEAD_COL
        + (selected or ", NULL AS grp0 ")
        + "FROM files "
        + (f"WHERE {where} " if where else "")
        + "ORDER BY " + ordered
        + "effective_date IS NULL, effective_date, name "
        "LIMIT :limit OFFSET :offset", params
    ))


#: How many files defer to this one. Read per row so the grid can badge a stack
#: without a second query, and indexed so that it is a lookup rather than a
#: scan of the library for every thumbnail.
_BEHIND_COL: str = (
    "(SELECT COUNT(*) FROM files m "
    " WHERE m.stacked_under = files.folder || '/' || files.name) AS behind")

#: How many files the app *thinks* defer to this one. Beside `behind` rather
#: than merged into it, because the grid says which of the two it is: a number
#: nobody has confirmed is drawn differently from one somebody decided.
_AHEAD_COL: str = (
    "(SELECT COUNT(*) FROM files g "
    " WHERE g.suggested_under = files.folder || '/' || files.name) AS proposed")

_TAGS_COL: str = (
    "(SELECT group_concat(ft.tag, char(10)) FROM file_tags ft "
    " WHERE ft.folder = files.folder AND ft.name = files.name) AS tags")
_AUDIENCE_COL: str = (
    "(SELECT group_concat(fa.who, char(10)) FROM file_audience fa "
    " WHERE fa.folder = files.folder AND fa.name = files.name) AS audience")


#: How close in time two photographs have to be to read as one moment. Measured
#: against the real library: at two seconds it finds 1,551 groups covering 4,342
#: files, two thirds of them simple pairs. At five it finds 6,011 files, which is
#: no longer bursts — it is how somebody shoots an afternoon.
BURST_SECONDS: int = 2

#: The legacy collision suffix. Two files whose generated names differ only by
#: it had the same event and the same second, which is the same claim the burst
#: window makes and survives a missing camera or a partial date.
_SUFFIX = re.compile(r"_\d{3}$")


def suggestions(rows: Sequence[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    """Files that look like one moment, grouped — a proposal, never a decision.

    Two signals, and the second is nearly a subset of the first: photographs
    taken within `BURST_SECONDS` on the same camera, and files whose names
    differ only by the collision suffix. Against the real library the second
    adds about thirty groups to the first's fifteen hundred, and it is kept
    because what it catches is the case the first cannot see — a file with no
    camera recorded, or a date known only to the day.

    Computed rather than stored. It is derived from facts the index already
    holds, and [§4] keeps nothing in master that can be recomputed — a
    suggestion is not even a decision, only an offer to make one.

    Singletons are not returned: there is nothing to review about a photograph
    that resembles none of its neighbours.
    """
    groups: dict[str, list[sqlite3.Row]] = {}

    by_camera: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if row["no_stack"] or row["stacked_under"]:
            continue
        if row["precision"] == datestr.FULL and row["effective_date"]:
            by_camera.setdefault(str(row["camera"] or ""), []).append(row)

    for camera, items in by_camera.items():
        items.sort(key=lambda r: str(r["effective_date"]))
        key = ""
        previous: sqlite3.Row | None = None
        for row in items:
            if previous is None or not _same_moment(previous, row):
                key = f"burst:{camera}:{row['effective_date']}"
            groups.setdefault(key, []).append(row)
            previous = row

    for row in rows:
        if row["no_stack"] or row["stacked_under"]:
            continue
        # Only where the date is really known. A generated name carries the
        # date, so files dated to a month all get the same name and collide
        # with each other — 68 photographs of a skiing trip read as one burst
        # because none of them knows which day it happened on. The collision is
        # an artefact of the naming, and a fabricated timestamp is not evidence
        # of anything.
        if row["precision"] != datestr.FULL:
            continue
        # The file without a suffix belongs to the family too — it is the one
        # the others collided with.
        base = _SUFFIX.sub("", str(row["name"]).rsplit(".", 1)[0])
        groups.setdefault(f"name:{row['folder']}:{base}", []).append(row)

    return _merged(groups)


def _same_moment(a: sqlite3.Row, b: sqlite3.Row) -> bool:
    """Whether `b` was taken within the burst window of `a`."""
    first, second = str(a["effective_date"]), str(b["effective_date"])
    if first[:10] != second[:10]:
        return False
    return abs(_clock(second) - _clock(first)) <= BURST_SECONDS


def _clock(when: str) -> int:
    try:
        return (int(when[11:13]) * 3600 + int(when[14:16]) * 60
                + int(when[17:19]))
    except ValueError:
        return 0


def _merged(groups: dict[str, list[sqlite3.Row]]) -> list[list[sqlite3.Row]]:
    """One group per file, and only groups worth reviewing.

    The two signals overlap almost entirely, so a file can arrive in both. It
    belongs to one proposal or the reviewing is nonsense — you would accept it
    into a stack and still be asked about it.
    """
    seen: set[tuple[str, str]] = set()
    out: list[list[sqlite3.Row]] = []
    for group in groups.values():
        kept = [r for r in group
                if (str(r["folder"]), str(r["name"])) not in seen]
        if len(kept) < 2:
            continue
        seen.update((str(r["folder"]), str(r["name"])) for r in kept)
        out.append(kept)
    return out


def resuggest(conn: sqlite3.Connection) -> int:
    """Recompute every suggestion in the library. Returns how many it found.

    Run as part of a build, because that is when the set of files changes and
    a guess about which of them were taken together is a fact about the set.
    Cheap next to the read that precedes it — the rows are already local by
    then, and grouping 62k of them is a sort and two passes.
    """
    conn.execute("UPDATE files SET suggested_under = NULL")
    # Never the deleted: they are not in the library any viewer sees, and a
    # suggestion is an offer to curate what is there.
    rows = conn.execute("SELECT * FROM files WHERE deleted = 0").fetchall()
    return _store(conn, suggestions(rows))


def _store(conn: sqlite3.Connection,
           groups: Sequence[Sequence[sqlite3.Row]]) -> int:
    """Write one guessed group per `groups`, each behind one of its own."""
    for group in groups:
        lead = _lead(group)
        key = f'{lead["folder"]}/{lead["name"]}'
        conn.executemany(
            "UPDATE files SET suggested_under = ? WHERE folder = ? AND name = ?",
            [(key, r["folder"], r["name"]) for r in group if r is not lead])
    return len(groups)


def _lead(group: Sequence[sqlite3.Row]) -> sqlite3.Row:
    """Which photograph a guessed group speaks through.

    The earliest, which in a burst is the one the shutter was pressed for —
    the rest are what the camera did afterwards. Nothing rides on it being
    right: it is what the grid shows until somebody says otherwise, and saying
    otherwise is one click on the one you meant.

    By name where the clock cannot separate them, so the same library always
    proposes the same photograph. A leader that moved between two builds would
    make the grid rearrange itself for no reason anybody could see.
    """
    return min(group, key=lambda r: (str(r["effective_date"] or ""),
                                     str(r["name"])))


def _regroup(conn: sqlite3.Connection, folder: str, name: str,
             was: str | None) -> None:
    """Recompute the guesses around one file, after its row is rewritten.

    Called on every decision, because a decision is usually an answer to a
    guess — accepted, and the members are a stack now; refused, and they must
    never be offered again; and taken back, in which case the guess has to
    come back with it. A revert that left the shelf empty would be an undo
    that only undid half of what it said.

    **Around, not within.** Recomputing only the group this file was in could
    dissolve one but never find one, so anything that made a guess true again
    left no way to say so short of rebuilding the whole index. The
    neighbourhood is what the guessing actually reads: the same camera on the
    same day, the files whose names collided with this one, and whatever was
    grouped with it before — each of them an indexed range rather than a scan,
    because this runs once per file of a three-hundred file edit.

    Closed over one more step, so that nothing outside is left pointing at a
    photograph in here that has stopped speaking for anybody.
    """
    seed = _around(conn, folder, name, was)
    if not seed:
        return
    keys = [f'{r["folder"]}/{r["name"]}' for r in seed]
    rows = {k: r for k, r in zip(keys, seed)}
    for row in _rows_under(conn, keys):
        rows.setdefault(f'{row["folder"]}/{row["name"]}', row)
    conn.executemany(
        "UPDATE files SET suggested_under = NULL WHERE folder = ? AND name = ?",
        [(r["folder"], r["name"]) for r in rows.values()])
    _store(conn, suggestions([r for r in rows.values() if not r["deleted"]]))


def _around(conn: sqlite3.Connection, folder: str, name: str,
            was: str | None) -> list[sqlite3.Row]:
    """The rows a guess about this file could possibly involve."""
    row = one(conn, folder, name)
    if row is None:
        return []
    day = str(row["effective_date"] or "")[:10]
    base = _SUFFIX.sub("", name.rsplit(".", 1)[0])
    lead_folder, _, lead_name = (was or "").partition("/")
    return list(conn.execute(
        "SELECT * FROM files WHERE "
        # The same moment: the burst pass never reaches across a day.
        " (:day <> '' AND effective_date >= :day AND effective_date <= :day_hi)"
        # The same name, suffix and extension aside — a range over the primary
        # key rather than a LIKE, so it is a seek and not a scan.
        " OR (folder = :folder AND name >= :base AND name <= :base_hi)"
        # Whatever this was grouped with, which may be neither by now.
        " OR suggested_under = :was"
        " OR (folder = :lead_folder AND name = :lead_name)",
        {"day": day, "day_hi": day + chr(0xFFFF),
         "folder": folder, "base": base, "base_hi": base + chr(0xFFFF),
         "was": was or "", "lead_folder": lead_folder,
         "lead_name": lead_name}))


def _rows_under(conn: sqlite3.Connection,
                keys: Sequence[str]) -> list[sqlite3.Row]:
    """Everything currently guessed to sit behind any of `keys`."""
    holes = ",".join(f":k{i}" for i in range(len(keys)))
    return list(conn.execute(
        f"SELECT * FROM files WHERE suggested_under IN ({holes})",
        {f"k{i}": k for i, k in enumerate(keys)}))


def proposed(conn: sqlite3.Connection, key: str) -> list[tuple[str, str]]:
    """The files the app guesses belong behind `key`, as `(folder, name)`.

    Asked before a decision is written to a file that speaks for a guessed
    group, so that what it hides is answered along with it.
    """
    return [(str(r["folder"]), str(r["name"])) for r in conn.execute(
        "SELECT folder, name FROM files WHERE suggested_under = ?", (key,))]


def members(conn: sqlite3.Connection, key: str) -> list[tuple[str, str]]:
    """The files stacked behind `key`, as `(folder, name)`.

    Asked when a file that speaks for others is itself put behind something:
    they have to come with it, or they are stranded one level down where no
    listing will reach them.
    """
    return [(str(r["folder"]), str(r["name"])) for r in conn.execute(
        "SELECT folder, name FROM files WHERE stacked_under = ?", (key,))]


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


#: How far either side of a selection an event still counts as *around* it.
#: Strict overlap misses the common case by a hair — the last afternoon of a
#: trip, photographed after midnight, or a camera an hour out — and an event
#: proposed a day too late is no proposal at all.
NEAR_DAYS: int = 1


def widen(span: tuple[str, str], days: int = NEAR_DAYS) -> tuple[str, str]:
    """A date range, loosened by `days` at each end.

    Given anything unparseable, it is returned as-is: the comparison it feeds
    is a string one, and a range nobody can widen is still a range that works.
    """
    lo, hi = span
    low, high = datestr.parse_pix(lo), datestr.parse_pix(hi)
    if low is None or high is None:
        return span
    from datetime import timedelta
    return (datestr.format_pix(low - timedelta(days=days)),
            datestr.format_pix(high + timedelta(days=days)))


def _date_level(current: str | None) -> tuple[int, str | None]:
    """How wide the next date values should be, and what to list them within.

    Years with nothing set; a year's months once a year is chosen; that
    month's days inside a month. A day already chosen lists its **siblings** —
    the other days of the same month — because having picked the 30th, what
    you want next is the 29th, not a list of one.
    """
    if current is None or current == UNDATED:
        return 4, None
    if len(current) == 4:
        return 7, current
    if len(current) == 7:
        return 10, current
    return 10, current[:7]


def suggest(conn: sqlite3.Connection, column: str,
            filters: Filters | None = None, *,
            near: tuple[str, str] | None = None,
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

    `near` is the date range of what the curator has selected, and it adds a
    band **above** all three: events whose own span overlaps it. This is the
    time-neighbour proposal from §8 — *these forty photographs fall between two
    files tagged France Trip* — asked at the moment of assignment rather than
    at import. It is the strongest signal there is for naming an event, and it
    beats the filter bands precisely because it knows something they do not:
    photographs taken on the same days are usually the same occasion.

    Within that band the **tightest** event comes first. A fortnight in France
    that covers your day is a claim about your day; a folder called `alina`
    holding eight months of somebody's phone covers it too and says nothing.
    Both are offered — the long one might be right — but the specific one is
    the proposal.

    Only events have a span, so `near` is ignored for every other column.
    """
    view = filters or Filters()
    clauses = _clauses(view)
    clauses.pop(column, None)
    seen, seen_params = _scope(view)
    params: dict[str, Any] = {**_bind(clauses), **seen_params, "limit": limit}
    # An event's span is MIN..MAX over its own group, so *does it overlap the
    # selection* is one expression evaluated per group rather than a second
    # query. NULL dates fall out of MIN/MAX and the comparison reads false,
    # which is right: an undated event is near nothing.
    near_sql = "0 AS n_near, 0 AS span "
    if near and column == "event":
        lo, hi = widen(near)
        params["near_lo"], params["near_hi"] = lo, hi
        near_sql = ("(MIN(files.effective_date) <= :near_hi "
                    " AND MAX(files.effective_date) >= :near_lo) AS n_near, "
                    " julianday(substr(MAX(files.effective_date),1,10)) "
                    " - julianday(substr(MIN(files.effective_date),1,10)) "
                    " AS span ")
    tally = (f"COUNT(*) AS n, " + near_sql + ", "
             f"SUM(CASE WHEN {_combine(clauses, 'AND')} THEN 1 ELSE 0 END) AS n_all, "
             f"SUM(CASE WHEN {_combine(clauses, 'OR')} THEN 1 ELSE 0 END) AS n_any ")
    # Tightest first within the band. A fortnight in France that covers your
    # day is a claim about your day; a folder called `alina` holding eight
    # months of a phone covers it too and says nothing. Against the real
    # library, overlap alone proposed seven events for a date in France and
    # only one of them was an occasion — the rest were device dumps long enough
    # to overlap everything.
    order = ("ORDER BY n_near DESC, CASE WHEN n_near THEN span END ASC, "
             "n_all > 0 DESC, n_any > 0 DESC, n DESC, value LIMIT :limit")

    def visible(*extra: str) -> str:
        """The WHERE that every suggestion is drawn from.

        `_always` included, or a dropdown offers values that no listing will
        ever show: the deleted and the stacked are not part of the library a
        filter can reach.
        """
        parts = [p for p in (seen, *extra, *_always(view)) if p]
        return f"WHERE {' AND '.join(parts)} " if parts else ""

    if column in ("tag", "audience"):
        table, col = (("file_tags", "tag") if column == "tag"
                      else ("file_audience", "who"))
        sql = (f"SELECT m.{col} AS value, " + tally
               + f"FROM {table} m "
                 "JOIN files ON files.folder = m.folder AND files.name = m.name "
               + visible() + "GROUP BY value " + order)
    elif column == "date":
        # A drill-down rather than a flat list. With nothing set it offers
        # years; inside a year, that year's months; inside a month, its days.
        # Anything else means offering 3,000 days at once to somebody who
        # knows only that it was a summer.
        #
        # The parent is **kept** rather than popped, which is the opposite of
        # every other column. Popping exists so the first band is not just the
        # value already filtered on; here the value already filtered on is what
        # says which months are even worth listing.
        params["undated"] = UNDATED
        level, parent = _date_level(view.date)
        if parent:
            params["f_parent"] = parent
            clauses["date"] = (
                f"substr(files.effective_date, 1, {len(parent)}) = :f_parent",
                {"f_parent": parent})
        else:
            clauses.pop("date", None)
        # Rebuilt, because the clauses just changed under it.
        params.update(_bind(clauses))
        tally = (f"COUNT(*) AS n, 0 AS n_near, 0 AS span, "
                 f"SUM(CASE WHEN {_combine(clauses, 'AND')} THEN 1 ELSE 0 END) AS n_all, "
                 f"SUM(CASE WHEN {_combine(clauses, 'OR')} THEN 1 ELSE 0 END) AS n_any ")
        # Only files whose date is known to this width have a value at it. A
        # file dated to its month has no day to offer, and offering it as
        # `(undated)` would be the same lie in a different place — it is not
        # undated, it is reachable one level up.
        narrow = [f"files.precision >= {level}"] if parent else []
        if parent:
            narrow.append(
                f"substr(files.effective_date, 1, {len(parent)}) = :f_parent")
        where = visible(*narrow)
        value_sql = (f"substr(files.effective_date, 1, {level})" if parent else
                     f"COALESCE(CASE WHEN files.precision >= {level} THEN "
                     f"substr(files.effective_date, 1, {level}) END, :undated)")
        sql = (f"SELECT {value_sql} AS value, " + tally
               + "FROM files " + where + "GROUP BY value "
               # Newest first, the way the library is remembered. Undated sorts
               # to the end on its own: a bracket is below every digit.
               + "ORDER BY n_all > 0 DESC, n_any > 0 DESC, value DESC "
                 "LIMIT :limit")
    elif column in ("event", "kind", "band"):
        sql = (f"SELECT files.{column} AS value, " + tally
               + "FROM files "
               + visible(f"files.{column} IS NOT NULL")
               + "GROUP BY value " + order)
    else:
        raise ValueError(f"cannot suggest values for {column!r}")

    return [Suggestion(value=str(r["value"]), n=int(r["n"]),
                       scope=("near" if r["n_near"] else
                              "all" if r["n_all"] else
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
