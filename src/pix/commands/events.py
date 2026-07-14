"""Implementation of `pix info events` — list the library's unique event names.

Read-only and pipe-friendly (no version banner): prints one line per unique
effective event, case-insensitively sorted, as `name<TAB>range` — where
`range` is the span of effective dates across that event's files (empty when
none of them are dated). It backs the context-menu Set-event autocomplete
(the range is shown as a hint to tell similar events apart), and is handy on
its own to see what events exist and roughly when.

Reads only the cached metadata (no media walk, no ExifTool) — one query over
`cache.db`, so it's fast and reflects the last cached metadata, close enough for
suggestions. Files with no cached metadata (un-migrated, or cache not yet built)
simply don't contribute, and the `EVENT_NULL` force-null override resolves to no
event (so blanked files don't pollute the list).
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import typer

from pix import cache_db
from pix.events import effective_event, events_cache_path
from pix.metadata import FileMetadata
from pix.plan import effective_date
from pix.root import NoLibraryRoot, resolve as resolve_root

# A query over cache.db is quick, but still too slow to block an autocomplete
# prompt every keystroke. Serve a recent cached list instantly instead;
# `set`/`clear` invalidate it (see events.invalidate_events_cache), and this TTL
# is the backstop for events introduced by other ops (e.g. migrate).
_CACHE_TTL_SECONDS = 6 * 3600


def format_range(lo: datetime | None, hi: datetime | None) -> str:
    """Compact span of two dates, collapsing the shared leading parts:

    - no dates             → ""
    - same day             → "2023-01-01"
    - within one month     → "2023-01-01..13"
    - within one year      → "2023-01-01...02-13"
    - spanning years       → "2023-01-01...2024-02-13"
    """
    if lo is None or hi is None:
        return ""
    start = lo.strftime("%Y-%m-%d")
    if lo.date() == hi.date():
        return start
    if lo.year == hi.year and lo.month == hi.month:
        return f"{start}..{hi.strftime('%d')}"
    if lo.year == hi.year:
        return f"{start}...{hi.strftime('%m-%d')}"
    return f"{start}...{hi.strftime('%Y-%m-%d')}"


def list_events(path: Path | None = None) -> None:
    """Print each unique event as `name<TAB>date-range`, one per line."""
    try:
        root = resolve_root(start=path) if path is not None else resolve_root()
    except NoLibraryRoot as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    cache_file = events_cache_path(root)
    try:
        if time.time() - cache_file.stat().st_mtime < _CACHE_TTL_SECONDS:
            typer.echo(cache_file.read_text(encoding="utf-8"), nl=False)
            return
    except OSError:
        pass  # missing / stale / unreadable → recompute below

    # event -> (min_date, max_date); a date of None just doesn't widen the span.
    spans: dict[str, tuple[datetime | None, datetime | None]] = {}
    for path, md in cache_db.iter_meta(root):
        meta = FileMetadata(path=path, raw=md)
        event = effective_event(meta)
        if not event:
            continue
        dt = effective_date(meta)
        lo, hi = spans.get(event, (None, None))
        if dt is not None:
            lo = dt if lo is None or dt < lo else lo
            hi = dt if hi is None or dt > hi else hi
        spans[event] = (lo, hi)

    text = "".join(
        f"{event}\t{format_range(*spans[event])}\n"
        for event in sorted(spans, key=str.lower)
    )
    typer.echo(text, nl=False)
    try:
        cache_file.write_text(text, encoding="utf-8")
    except OSError:
        pass
