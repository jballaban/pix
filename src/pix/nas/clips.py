"""Clips — a stretch or a frame of a source video (spec/clips.md).

A clip is **virtual**: no master bytes of its own, and the source is never
touched. What makes it is a human decision — where it starts and ends — so it
lives in master as a sidecar beside the source, and it is a library item like
any other, with its own event, people, tags and audience.

```
master/.../IMG_C.MOV              the source, untouched
master/.../IMG_C.MOV~k3f.xmp      a clip: its range and its decisions
```

**The name is the relation.** `IMG_C.MOV~k3f` can only belong to `IMG_C.MOV`
in the same folder, the way a render path is worked out from a master path, so
nothing records which source a clip has.

**The id is random, never a position.** Numbering clips renumbers them the
moment one is split, and every clip after it silently takes its neighbour's
tags. Clips are ordered by where they start, so the id needs no meaning.

This module is the model — names, ranges, dates, merging — and touches no
files. The writes go through `decisions`, like every other decision.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from pix import datestr
from pix.nas.decisions import Decision

#: Between a source's name and a clip's id. Not a character a camera puts in a
#: name, and legal on every filesystem the archive is reachable from.
SEP: str = "~"

_ALPHABET: str = "abcdefghijkmnpqrstuvwxyz23456789"
ID_LEN: int = 4
_ID_RE: re.Pattern[str] = re.compile(rf"[{_ALPHABET}]{{{ID_LEN}}}")

#: What a source may be. 360 footage has nothing to play (spec/nas-app.md §5),
#: so there is nothing to cut it on.
_NO_SPLICE_EXTS: frozenset[str] = frozenset({".insv", ".insp"})


class ClipError(ValueError):
    """A clip that cannot be what was asked."""


def name_of(source: str, clip_id: str) -> str:
    return f"{source}{SEP}{clip_id}"


def source_of(name: str) -> str | None:
    """The source a clip's name belongs to, or None if it is not a clip name.

    Decided by the shape of the tail, not by the separator alone: a file from
    an old camera can have a `~` in its own name (`IMG~1.JPG`), and reading
    that as a clip would hide a real photograph behind a source that is not
    there.
    """
    source, sep, tail = name.rpartition(SEP)
    if not sep or not source or not _ID_RE.fullmatch(tail):
        return None
    return source


def new_id(taken: Iterable[str]) -> str:
    """An id no sibling has. Random, so it means nothing and never has to be
    renumbered; checked, because four characters can collide."""
    used = set(taken)
    while True:
        candidate = "".join(secrets.choice(_ALPHABET) for _ in range(ID_LEN))
        if candidate not in used:
            return candidate


def id_of(name: str) -> str:
    return name.rpartition(SEP)[2]


def can_splice(name: str, kind: str | None) -> str | None:
    """Why `name` cannot be a source, or None if it can.

    A clip is not a source: clips are always cut on the timeline of the video
    they came from, so there is never a clip of a clip.
    """
    if kind != "video":
        return "only a video can be cut into clips"
    if source_of(name) is not None:
        return "a clip is cut on its source's timeline, not on its own"
    if Path(name).suffix.lower() in _NO_SPLICE_EXTS:
        return "360 footage has nothing to play, so nothing to cut"
    return None


def check(clip_in: float, clip_out: float, *,
          siblings: Iterable[tuple[float, float]],
          duration: float | None) -> None:
    """Refuse a range that runs backwards, off the end, or over a sibling.

    **Ranges never overlap** (spec/clips.md §2): two clips over the same
    footage are two copies of it. Touching is not overlapping — a split makes
    two clips that meet exactly. Stills are points and never conflict with
    anything, so a still is checked only for being inside the video.
    """
    if not 0 <= clip_in <= clip_out:
        raise ClipError("a clip runs forwards from zero")
    # A second's grace, because the duration on record is ExifTool's, and it
    # writes anything over half a minute as `0:05:44` — whole seconds. The
    # page measures the real end (344.3s), and a clip that ran to it was
    # refused for running off a video that is in fact that long. A cut asked
    # to run past the real end simply stops there.
    if duration is not None and clip_out > duration + 1.0:
        raise ClipError(f"the video is only {duration:g}s long")
    if clip_in == clip_out:
        return
    for other_in, other_out in siblings:
        if other_in == other_out:
            continue
        if clip_in < other_out and other_in < clip_out:
            raise ClipError(
                f"{clip_in:g}s–{clip_out:g}s would overlap the clip at "
                f"{other_in:g}s–{other_out:g}s")


def inherited(source: Decision | None, event: str | None) -> Decision:
    """What a new clip starts with: a copy of its source's content decisions.

    **Copied, not inherited live** (spec/clips.md §2). Live inheritance fails
    on the set-valued fields — taking Mum off one clip when she comes from the
    source needs a stored *not Mum*, which nothing else in the model has.

    `event` is the source's *effective* event, which for most of the library
    is in its embedded tags rather than its sidecar — so the caller passes
    what the index shows.

    Not copied: deleted, stacked-under and no-stack, which are about the
    source's place in the grid rather than what is in it — and **not
    `hidden`**, for the same reason. A clip is usually made in order to hide
    its source; a clip born hidden would vanish the moment it was made.
    """
    from pix.nas.decisions import ARCHIVED

    was = source or Decision()
    return Decision(event=event or was.event, tags=was.tags, people=was.people,
                    audience=tuple(a for a in was.audience if a != ARCHIVED))


def merged(first: Decision, first_at: float,
           second: Decision, second_at: float) -> Decision:
    """Two adjacent clips' decisions as one, for removing the split between
    them (spec/clips.md §2).

    The duplicate ladder from spec/nas-app.md §15: the sets **union** — a set
    cannot conflict with itself — and a single value is **latest wins**, then
    the earlier clip's. *Latest* is when each sidecar was last written, which
    is when its last decision was made.

    The range is the caller's; this returns the content only.
    """
    latest, other = ((second, first) if second_at > first_at
                     else (first, second))
    return Decision(
        event=latest.event or other.event,
        date_override=latest.date_override or other.date_override,
        tags=(*first.tags, *second.tags),
        people=(*first.people, *second.people),
        audience=(*first.audience, *second.audience))


def date(source_capture: str | None, source_override: str | None,
         clip_in: float, clip_override: str | None
         ) -> tuple[str | None, datetime | None, int]:
    """A clip's capture date, effective date and precision.

    **Live, and the one thing that is** (spec/clips.md §2): the source's
    effective date plus where the clip starts, so correcting the source's
    date moves every clip with it. A clip's own override then applies on top,
    exactly as one does on any file.

    Only a source whose date is known to the second has one to add to. A
    source known only to the day has no time of day to offset, and inventing
    one would publish a precision nobody has.

    The capture date returned is the source's, shifted — what the clip's
    first frame would have said if it had been a file of its own.
    """
    captured = datestr.parse_exiftool(source_capture) if source_capture else None
    base = datestr.effective(captured, source_override)
    known = datestr.precision(captured, source_override)
    shifted = captured + timedelta(seconds=clip_in) if captured else None
    capture = shifted.strftime("%Y:%m:%d %H:%M:%S") if shifted else None
    if base is not None and known == datestr.FULL:
        start = base + timedelta(seconds=clip_in)
        return (capture, datestr.effective(start, clip_override),
                datestr.precision(start, clip_override))
    if clip_override and datestr.pins_anything(clip_override):
        return (capture, datestr.effective(None, clip_override),
                datestr.precision(None, clip_override))
    return capture, base, known
