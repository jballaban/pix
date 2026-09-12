"""The date strings pix reads and writes, and the grammar of a partial one.

An override is `YYYY-MM-DD-HH:MM:SS` where **any component may be `*`**, and the
effective date is the probed capture date with each non-`*` component replaced.
That is what lets a curator say *this scan is from 1987* without inventing a
month, a day and a time nobody knows — and inventing them is not a harmless
convenience, because a fabricated `1987-01-01 00:00:00` is indistinguishable
from a real one a year later.

| capture date | override | effective |
|---|---|---|
| `2023-08-15-14:32:05` | (none) | `2023-08-15-14:32:05` |
| `2023-08-15-14:32:05` | `*-03-*-*:*:*` | `2023-03-15-14:32:05` |
| `2023-08-15-14:32:05` | `2020-*-01-*:*:*` | `2020-08-01-14:32:05` |
| (none) | `1987-*-*-*:*:*` | `1987-01-01-00:00:00` — year anchors it |

An override with no capture date behind it falls back to the minimum for each
unpinned component, and needs a **year** to anchor: without one there is nothing
to be relative to. Only what was actually set is ever stored; the defaults are
applied here, at read time.

Its own module, with **no pix dependencies at all**, because both architectures
need it: `dates`/`plan` for the library pipeline, `nas.decisions` and
`nas.index` for the sidecars. `dates` drags in the metadata cache, which the
NAS app has no business importing in order to parse a string.
"""

from __future__ import annotations

import re
from datetime import datetime

#: Format used by `pix:DateAuto`, `pix:DateOverride` and friends (spec/tags.md).
PIX_DATETIME_FORMAT: str = "%Y-%m-%d-%H:%M:%S"

#: Six components, each either a literal or `*`. Groups are both named and
#: positional, so callers can take whichever shape suits them.
OVERRIDE_RE: re.Pattern[str] = re.compile(
    r"^(?P<Y>\*|\d{4})-(?P<M>\*|\d{2})-(?P<D>\*|\d{2})-"
    r"(?P<h>\*|\d{2}):(?P<m>\*|\d{2}):(?P<s>\*|\d{2})$"
)

_SLOTS: tuple[str, ...] = ("Y", "M", "D", "h", "m", "s")

#: ExifTool's own shape, `2026:01:04 14:51:34`, with either separator and an
#: optional trailing timezone that pix discards — every date here is naive
#: local time (spec/tags.md).
EXIFTOOL_DATETIME_RE: re.Pattern[str] = re.compile(
    r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[\sT](\d{2}):(\d{2}):(\d{2})"
)


def parse_exiftool(value: str) -> datetime | None:
    """Parse an ExifTool datetime, or None if it is absent or junk.

    `0000:00:00 00:00:00` is a real reading from 45 files in the seeded year
    and is not a date; it parses as junk here rather than as year zero.
    """
    match = EXIFTOOL_DATETIME_RE.match(value.strip()) if value else None
    if match is None:
        return None
    try:
        return datetime(*(int(g) for g in match.groups()))  # type: ignore[arg-type]
    except ValueError:
        return None


def parse_pix(value: str) -> datetime | None:
    """Parse a fully-pinned pix datetime string."""
    try:
        return datetime.strptime(value, PIX_DATETIME_FORMAT)
    except ValueError:
        return None


def format_pix(moment: datetime) -> str:
    """Render a datetime in the pix string format."""
    return moment.strftime(PIX_DATETIME_FORMAT)


def valid(value: str) -> bool:
    """True if `value` is a well-formed override pattern."""
    return OVERRIDE_RE.match(value) is not None


def pins_anything(value: str | None) -> bool:
    """True if `value` actually pins at least one component.

    An all-`*` override is equivalent to no override and should never be
    stored. This is defensive: if such a string is on disk, treat it as pinning
    nothing rather than as a decision.
    """
    return bool(value) and any(c.isdigit() for c in value or "")


def slots(value: str | None) -> list[str]:
    """The six components of `value`, or all-`*` if it is absent or malformed."""
    if value:
        match = OVERRIDE_RE.match(value)
        if match is not None:
            return list(match.groups())
    return ["*"] * 6


def compose(*, year: str | int | None = None, month: str | int | None = None,
            day: str | int | None = None, hour: str | int | None = None,
            minute: str | int | None = None,
            second: str | int | None = None) -> str | None:
    """Build an override from whichever components are known.

    `None` leaves a component unpinned. Returns None when nothing is pinned at
    all, because that is the same as having no override — and storing an
    all-`*` string would create a sidecar recording no decision.
    """
    given = (year, month, day, hour, minute, second)
    widths = (4, 2, 2, 2, 2, 2)
    parts = ["*" if v is None or v == "" else str(v).zfill(w)
             for v, w in zip(given, widths)]
    if all(p == "*" for p in parts):
        return None
    built = f"{parts[0]}-{parts[1]}-{parts[2]}-{parts[3]}:{parts[4]}:{parts[5]}"
    return built if valid(built) else None


#: How much of a date is known, as the length of the prefix that can be
#: trusted: nothing, the year, the month, the day, the whole timestamp. They
#: are substring lengths because that is what both the index and its queries do
#: with them.
NOTHING, YEAR, MONTH, DAY, FULL = 0, 4, 7, 10, 19


def precision(auto: datetime | None, value: str | None) -> int:
    """How much of the effective date is actually known.

    An override with holes in it has to keep them. `alone` fills them with
    minimums so that there is a date to sort by at all — *this is from 1987*
    becomes `1987-01-01 00:00:00` — and that is the right thing for ordering
    and the wrong thing for everything else, because the made-up first of
    January is indistinguishable from a real one. This says how far along that
    string the truth stops.

    **A capture date makes everything known.** An override on top of one
    replaces the components it pins and leaves the rest reading off the camera,
    so the day is still the real day even when the year has been corrected.
    """
    if auto is not None:
        return FULL
    if not pins_anything(value):
        return NOTHING
    parts = slots(value)
    if parts[0] == "*":
        # No year to anchor it, so `alone` gives nothing and neither does this.
        return NOTHING
    known = 1
    for part in parts[1:]:
        if part == "*":
            break
        known += 1
    return {1: YEAR, 2: MONTH, 3: DAY}.get(known, FULL)


def apply(auto: datetime, value: str) -> datetime | None:
    """Patch `auto` with the non-`*` components of `value`."""
    match = OVERRIDE_RE.match(value)
    if match is None:
        return None
    parts = match.groupdict()
    fallback = (auto.year, auto.month, auto.day,
                auto.hour, auto.minute, auto.second)
    return _build(parts, fallback)


def alone(value: str | None) -> datetime | None:
    """The date an override implies with no capture date behind it.

    Unpinned components take their minimum — month and day `01`, time zeroes —
    but a **year is required** as the anchor, so `*-03-*-*:*:*` on an undated
    file stays undated rather than guessing a year.
    """
    if not value:
        return None
    match = OVERRIDE_RE.match(value)
    if match is None or match.group("Y") == "*":
        return None
    return _build(match.groupdict(), (0, 1, 1, 0, 0, 0))


def effective(auto: datetime | None, value: str | None) -> datetime | None:
    """The date a file actually has: its capture date, as overridden."""
    if not value or not pins_anything(value):
        return auto
    return apply(auto, value) if auto is not None else alone(value)


def _build(parts: dict[str, str],
           fallback: tuple[int, int, int, int, int, int]) -> datetime | None:
    values = [int(parts[slot]) if parts[slot] != "*" else default
              for slot, default in zip(_SLOTS, fallback)]
    try:
        return datetime(*values)  # type: ignore[arg-type]
    except ValueError:
        return None
