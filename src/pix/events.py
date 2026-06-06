"""`EventAuto` derivation from the file's parent folder name.

Implements the rule from spec/tags.md → "EventAuto derivation": take the
immediate parent folder of `pix:OriginalPath` (or, when OriginalPath
isn't set yet — first migrate — the file's current parent folder), strip
a leading run of digits and separators, and return what's left if it
contains at least one alphabetic character.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePath

from pix import debug
from pix.metadata import FileMetadata

# Constants used by callers (defined here so plan.py doesn't have to
# stutter `PIX_EVENT_*` definitions; events.py owns the event-related
# field names alongside the derivation logic).
PIX_EVENT_AUTO: str = "XMP:EventAuto"
PIX_EVENT_AUTO_PREVIOUS: str = "XMP:EventAutoPrevious"
PIX_EVENT_OVERRIDE: str = "XMP:EventOverride"
PIX_ORIGINAL_PATH: str = "XMP:OriginalPath"
# Dedupe-consolidated event, consulted before the folder-name heuristic.
# See spec/tags.md → Merge fields and spec/dedupe.md → Tag merge.
PIX_MERGE_EVENT: str = "XMP:MergeEvent"

# Force-null sentinel for `pix:EventOverride`: an explicit "no event" that
# beats `pix:EventAuto` (an absent/empty override otherwise reverts to the
# auto). Mirrors the `*` null convention used for date overrides. `pix clear
# event` writes this when a file would otherwise show an auto-derived event.
EVENT_NULL: str = "*"


def events_cache_path(library_root: Path) -> Path:
    """Path to the cached unique-event list (backs `pix events`/autocomplete)."""
    return library_root / ".pix" / "events.cache"


def invalidate_events_cache(library_root: Path) -> None:
    """Drop the cached event list so the next `pix events` recomputes it.

    Called after a tag write that can change the set of events (set/clear), so
    a value you just assigned shows up in the next autocomplete. Best-effort.
    """
    try:
        events_cache_path(library_root).unlink()
    except OSError:
        pass


# Leading digits + common separators (-, _, ., space) that look like a
# date prefix on a folder name. We strip as much as matches; whatever
# is left is the candidate event name.
_DATE_PREFIX_RE = re.compile(r"^[\d\-_. ]+")


def effective_event(meta: FileMetadata) -> str | None:
    """Return the effective `event` value for `meta`, or None.

    `pix:EventOverride` wins if set; otherwise `pix:EventAuto`. The
    `EVENT_NULL` (`*`) override is an explicit "no event" that beats the
    auto. See spec/tags.md → Effective value computation.
    """
    override = meta.get_str(PIX_EVENT_OVERRIDE)
    if override == EVENT_NULL:
        return None
    if override:
        return override
    return meta.get_str(PIX_EVENT_AUTO)


def derive_event_auto(meta: FileMetadata) -> str | None:
    """Return the derived `pix:EventAuto` value for `meta`, or None.

    `pix:MergeEvent` (dedupe-consolidated) wins outright when present.
    Otherwise the source is the parent folder of `pix:OriginalPath` (if
    set) or the file's current parent. The folder name is processed as:

    1. Strip a leading run of `[\\d\\-_. ]+` (digits + `-`, `_`, `.`, space).
    2. Trim trailing whitespace.
    3. If the result is empty or has no alphabetic char, return None.
    4. Otherwise return the cleaned string (case + internal separators
       preserved).
    """
    debug.section("EventAuto derivation")

    # pix:MergeEvent — dedupe-consolidated value, consulted before the
    # folder-name heuristic (spec/tags.md → Merge fields).
    merge = meta.get_str(PIX_MERGE_EVENT)
    if merge:
        debug.log(f"  pix:MergeEvent present: {merge!r}")
        debug.log(f"  Result: {merge!r}")
        return merge

    original = meta.get_str(PIX_ORIGINAL_PATH)
    source = PurePath(original) if original else PurePath(str(meta.path))
    parent_name = source.parent.name

    debug.log(
        f"  Source path: {source} ("
        f"{'from OriginalPath' if original else 'current path'})"
    )
    debug.log(f"  Parent folder: {parent_name!r}")

    if not parent_name:
        debug.log("  Result: (none — no parent folder)")
        return None

    stripped = _DATE_PREFIX_RE.sub("", parent_name).strip()
    if not stripped:
        debug.log("  Result: (none — stripped to empty)")
        return None
    if not any(c.isalpha() for c in stripped):
        debug.log(
            f"  Result: (none — {stripped!r} has no alphabetic chars)"
        )
        return None

    debug.log(f"  Result: {stripped!r}")
    return stripped
