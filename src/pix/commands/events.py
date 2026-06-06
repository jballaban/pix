"""Implementation of `pix events` — list the library's unique event names.

Read-only and pipe-friendly (no version banner): prints each unique effective
event value, one per line, case-insensitively sorted. It backs the
context-menu Set-event autocomplete, and is handy on its own to see what
events exist.

Reads only the `.meta` cache sidecars (no media walk, no ExifTool), so it's
fast and reflects the last cached metadata — close enough for suggestions.
Files with no cached metadata (un-migrated, or cache not yet built) simply
don't contribute, and the `EVENT_NULL` force-null override resolves to no
event (so blanked files don't pollute the list).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import typer

from pix import cache_base
from pix.events import effective_event, events_cache_path
from pix.metadata import FileMetadata
from pix.metadata_cache import SUFFIX as META_SUFFIX
from pix.root import NoLibraryRoot, resolve as resolve_root

# Scanning a TB-scale library's .meta sidecars takes a few seconds, too slow to
# block an autocomplete prompt every time. Serve a recent cached list instantly
# instead; `set`/`clear` invalidate it (see events.invalidate_events_cache), and
# this TTL is the backstop for events introduced by other ops (e.g. migrate).
_CACHE_TTL_SECONDS = 6 * 3600


def list_events(path: Path | None = None) -> None:
    """Print the library's unique effective event names, one per line."""
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

    cache_root = cache_base.cache_root_for(root)
    if not cache_root.is_dir():
        return  # no metadata cache yet → no suggestions

    meta_files = list(cache_root.rglob(f"*{META_SUFFIX}"))

    def event_of(meta_file: Path) -> str | None:
        data = cache_base.read_json(meta_file)
        if data is None:
            return None
        md = data.get("metadata")
        if not isinstance(md, dict):
            return None
        return effective_event(
            FileMetadata(path=meta_file, raw=cast("dict[str, object]", md))
        )

    events: set[str] = set()
    with ThreadPoolExecutor(max_workers=cache_base.DEFAULT_WORKERS) as executor:
        for event in executor.map(event_of, meta_files):
            if event:
                events.add(event)

    text = "".join(f"{event}\n" for event in sorted(events, key=str.lower))
    typer.echo(text, nl=False)
    try:
        cache_file.write_text(text, encoding="utf-8")
    except OSError:
        pass
