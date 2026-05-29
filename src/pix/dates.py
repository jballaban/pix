"""Date parsing and `DateAuto` derivation.

Implements the ordered candidate list from spec/tags.md → "DateAuto
derivation". First match wins; returns None if every candidate fails.

The internal datetime representation is naïve (no timezone) — pix treats
all dates as local time per spec/tags.md. ExifTool date strings sometimes
include timezone offsets which we strip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pix import debug
from pix.metadata import FileMetadata

# Format used in pix:DateAuto, pix:DateOverride, etc. See spec/tags.md.
PIX_DATETIME_FORMAT: str = "%Y-%m-%d-%H:%M:%S"

# A media file can't have been created in the future — a date past "now"
# is garbage (notably HandBrake remuxes and some device firmwares stamp a
# bogus future QuickTime CreateDate). We reject any candidate beyond a
# small grace past the current moment, then fall through to the next
# source. The grace absorbs timezone skew on genuinely fresh imports:
# QuickTime stores UTC and pix treats timestamps as naïve local, so a
# just-shot clip can read several hours ahead of local "now". 48h covers
# any real-world offset (max ~26h span) while still catching 2036-style junk.
_FUTURE_GRACE: timedelta = timedelta(hours=48)


_EXIFTOOL_DATETIME_RE = re.compile(
    r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[\sT](\d{2}):(\d{2}):(\d{2})"
)


def parse_exiftool_datetime(value: str) -> datetime | None:
    """Parse an ExifTool date string (`2023:08:15 14:32:05[+05:00]`).

    Tolerates `:` or `-` as date separators and ` ` or `T` between date and
    time. Ignores subseconds and timezone offsets. Returns None if the
    string doesn't match.
    """
    m = _EXIFTOOL_DATETIME_RE.match(value)
    if m is None:
        return None
    try:
        year, month, day, hour, minute, second = (int(g) for g in m.groups())
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def format_pix_datetime(dt: datetime) -> str:
    """Render a datetime as a pix-spec datetime string."""
    return dt.strftime(PIX_DATETIME_FORMAT)


# Candidate keys per spec/tags.md → "DateAuto derivation".
# Group-prefixed exiftool keys (family 0).
_PHOTO_DATE_KEYS: tuple[str, ...] = (
    "EXIF:DateTimeOriginal",
    "EXIF:CreateDate",
    "EXIF:DateTimeDigitized",
    "XMP:DateCreated",
    "XMP:CreateDate",
    "IPTC:DateCreated",
)
_VIDEO_DATE_KEYS: tuple[str, ...] = (
    "QuickTime:CreateDate",
    "QuickTime:MediaCreateDate",
    "XMP:CreateDate",
)
_MTIME_KEY: str = "File:FileModifyDate"
_ORIGINAL_PATH_KEY: str = "XMP:OriginalPath"


# Filename patterns carrying a full timestamp. Each matches
# `YYYY MM DD HH MM SS` in some shape.
_FILENAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    # YYYY-MM-DD_HHMMSS or YYYY-MM-DD-HHMMSS (pix canonical, similar)
    re.compile(r"(\d{4})-(\d{2})-(\d{2})[_\-](\d{2})(\d{2})(\d{2})"),
    # IMG_YYYYMMDD_HHMMSS, PXL_YYYYMMDD_HHMMSSsss, YYYYMMDDHHMMSS,
    # YYYYMMDDHHMMSSsss — separator between date and time is optional so
    # Synology-style burst exports (`20251013073558000.JPG`) match.
    re.compile(r"(?:^|[_\-])(\d{4})(\d{2})(\d{2})[_\-]?(\d{2})(\d{2})(\d{2})"),
)

# Date-only patterns (no time). Used for folder names and as a filename
# fallback (against the stem, so the `.ext` boundary doesn't block a bare
# `YYYYMMDD`). Both the dashed `YYYY-MM-DD` and bare `YYYYMMDD` forms are
# accepted; `_build_date` normalizes the historical `YYYY-MM-00` (unknown
# day) convention to the 1st.
_DATE_ONLY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|[_\- ])(\d{4})-(\d{2})-(\d{2})(?:$|[_\- ])"),
    re.compile(r"(?:^|[_\- ])(\d{4})(\d{2})(\d{2})(?:$|[_\- ])"),
)


_VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {"mp4", "mov", "m4v", "3gp", "mkv", "wmv", "webm", "avi"}
)


def _is_video(path: Path) -> bool:
    return path.suffix.lower().lstrip(".") in _VIDEO_EXTENSIONS


_last_derivation_source: str | None = None


def last_derivation_source() -> str | None:
    """Return the source string identifying where the most recent
    `derive_date_auto` call got its value (e.g.
    `"EXIF:DateTimeOriginal = '2023:08:15 14:32:05'"`). None if the
    last call returned None or hasn't run yet.
    """
    return _last_derivation_source


def derive_date_auto(meta: FileMetadata) -> datetime | None:
    """Derive `DateAuto` per the spec's ordered candidate list.

    Order (first match wins):
    1. Metadata-recorded datetime (EXIF/QuickTime/XMP/IPTC depending on
       photo vs video).
    2. Filename pattern on the file's **original** path (per
       `pix:OriginalPath`, if set). Once a file has been migrated its
       current name is just our canonical output — circular to derive
       from. The original name is the surviving filesystem-side signal.
    3. Parent-folder pattern on the file's original parent.
    4. Filename pattern on the file's current name (matches first-migrate
       files where `pix:OriginalPath` isn't set yet, and any oddball
       cases where the user hand-renamed a migrated file).
    5. Parent-folder pattern on the current parent.
    6. File modify time (`File:FileModifyDate`) — least trustworthy.

    Any candidate that resolves to a date implausibly far in the future
    (see `_FUTURE_GRACE`) is rejected as garbage and the search falls
    through to the next source — a file can't have been created after now.

    Returns None when every candidate fails. Side effect: stores the
    selected source for `last_derivation_source()` to expose to callers
    that want to record provenance (debug logs, drift checks, etc.).
    """
    global _last_derivation_source
    _last_derivation_source = None

    debug.section("DateAuto derivation")
    now = datetime.now()
    is_video = _is_video(meta.path)
    candidates = _VIDEO_DATE_KEYS if is_video else _PHOTO_DATE_KEYS
    debug.log(f"  Candidate set: {'video' if is_video else 'photo'}")

    result: datetime | None = None
    source_summary: str | None = None

    # 1. Metadata candidates
    for key in candidates:
        value = meta.get_str(key)
        if value:
            dt = parse_exiftool_datetime(value)
            if dt is not None:
                if _is_future(dt, now):
                    debug.log(
                        f"  {key:<32} {value!r}  ->  {dt.isoformat()}  "
                        f"✗ future, ignored"
                    )
                else:
                    debug.log(
                        f"  {key:<32} {value!r}  ->  {dt.isoformat()}  ✓ matched"
                    )
                    result = dt
                    source_summary = f"{key} = {value!r}"
                    break
            else:
                debug.log(f"  {key:<32} {value!r}  ->  unparseable")
        else:
            debug.log(f"  {key:<32} (absent)")

    if result is None:
        debug.log("  (metadata candidates exhausted)")

    # 2. + 3. Filename + folder pattern on pix:OriginalPath (if set)
    original_raw = meta.get_str(_ORIGINAL_PATH_KEY)
    if result is None and original_raw:
        original_path = Path(original_raw)
        original_name = original_path.name
        original_parent_name = original_path.parent.name

        # 2. Original filename pattern
        name_match = _match_filename(original_name)
        if name_match is not None and not _is_future(name_match, now):
            debug.log(
                f"  Original-filename pattern matched on "
                f"{original_name!r}: {name_match.isoformat()}  ✓"
            )
            result = name_match
            source_summary = (
                f"filename pattern on pix:OriginalPath name {original_name!r}"
            )
        elif name_match is not None:
            debug.log(
                f"  Original-filename pattern on {original_name!r}: "
                f"{name_match.isoformat()}  ✗ future, ignored"
            )
        else:
            debug.log(
                f"  Original-filename pattern: no match "
                f"(pix:OriginalPath name = {original_name!r})"
            )

        # 3. Original parent-folder pattern
        if result is None:
            folder_match = _match_folder(original_parent_name)
            if folder_match is not None and not _is_future(folder_match, now):
                debug.log(
                    f"  Original-folder pattern matched on "
                    f"{original_parent_name!r}: "
                    f"{folder_match.isoformat()}  ✓"
                )
                result = folder_match
                source_summary = (
                    f"parent-folder pattern on pix:OriginalPath parent "
                    f"{original_parent_name!r}"
                )
            elif folder_match is not None:
                debug.log(
                    f"  Original-folder pattern on {original_parent_name!r}: "
                    f"{folder_match.isoformat()}  ✗ future, ignored"
                )
            else:
                debug.log(
                    f"  Original-folder pattern: no match "
                    f"(pix:OriginalPath parent = {original_parent_name!r})"
                )
    elif result is None:
        debug.log("  pix:OriginalPath: (absent — skipping original-path patterns)")

    # 4. Current filename pattern
    if result is None:
        name_match = _match_filename(meta.path.name)
        if name_match is not None and not _is_future(name_match, now):
            debug.log(
                f"  Current-filename pattern matched: "
                f"{name_match.isoformat()}  ✓"
            )
            result = name_match
            source_summary = (
                f"filename pattern on current name {meta.path.name!r}"
            )
        elif name_match is not None:
            debug.log(
                f"  Current-filename pattern: {name_match.isoformat()}  "
                f"✗ future, ignored ({meta.path.name!r})"
            )
        else:
            debug.log(
                f"  Current-filename pattern: no match ({meta.path.name!r})"
            )

    # 5. Current parent-folder pattern
    if result is None:
        folder_match = _match_folder(meta.path.parent.name)
        if folder_match is not None and not _is_future(folder_match, now):
            debug.log(
                f"  Current-folder pattern matched: "
                f"{folder_match.isoformat()}  ✓"
            )
            result = folder_match
            source_summary = (
                f"parent-folder pattern on current parent "
                f"{meta.path.parent.name!r}"
            )
        elif folder_match is not None:
            debug.log(
                f"  Current-folder pattern: {folder_match.isoformat()}  "
                f"✗ future, ignored ({meta.path.parent.name!r})"
            )
        else:
            debug.log(
                f"  Current-folder pattern: no match "
                f"({meta.path.parent.name!r})"
            )

    # 6. mtime fallback
    if result is None:
        mtime = meta.get_str(_MTIME_KEY)
        if mtime:
            dt = parse_exiftool_datetime(mtime)
            if dt is not None and not _is_future(dt, now):
                debug.log(
                    f"  File:FileModifyDate {mtime!r}  ->  "
                    f"{dt.isoformat()}  ✓ (last-resort fallback)"
                )
                result = dt
                source_summary = (
                    f"File:FileModifyDate = {mtime!r} (last-resort mtime fallback)"
                )
            elif dt is not None:
                debug.log(
                    f"  File:FileModifyDate {mtime!r}  ->  {dt.isoformat()}  "
                    f"✗ future, ignored"
                )
            else:
                debug.log(f"  File:FileModifyDate {mtime!r}  ->  unparseable")
        else:
            debug.log("  File:FileModifyDate (absent)")

    # Summary block so the chosen source is impossible to miss when
    # scanning a debug log.
    debug.log("")
    if result is not None:
        debug.log(f"  >>> DateAuto = {result.isoformat()}")
        debug.log(f"  >>> Source:   {source_summary}")
    else:
        debug.log("  >>> DateAuto = (none — no date source matched)")

    _last_derivation_source = source_summary
    return result


@dataclass(frozen=True)
class DateCandidate:
    """One evaluated date source, for the `pix meta` inspector.

    `parsed` is the datetime the source yields (None if it produced
    nothing). `note` is a short human status: matched / unparseable /
    absent / no pattern.
    """

    label: str
    detail: str
    parsed: datetime | None
    note: str


def date_candidates(meta: FileMetadata) -> list[DateCandidate]:
    """Evaluate *every* date source in priority order (first parsed wins).

    Unlike `derive_date_auto`, which short-circuits on the first match,
    this evaluates them all so the inspector can show, e.g., that the
    folder *would* have yielded a different date than the filename that
    won. The winner is the first entry with a non-None `parsed`. A future
    date is shown with its raw value but `parsed=None` and a "future
    (ignored)" note, so it never wins — matching `derive_date_auto`.
    """
    out: list[DateCandidate] = []
    now = datetime.now()

    def _entry(
        label: str, detail: str, dt: datetime | None, missing_note: str
    ) -> DateCandidate:
        if dt is None:
            return DateCandidate(label, detail, None, missing_note)
        if _is_future(dt, now):
            return DateCandidate(label, detail, None, "future (ignored)")
        return DateCandidate(label, detail, dt, "matched")

    keys = _VIDEO_DATE_KEYS if _is_video(meta.path) else _PHOTO_DATE_KEYS
    for key in keys:
        value = meta.get_str(key)
        if not value:
            out.append(DateCandidate(key, "(absent)", None, "absent"))
            continue
        out.append(
            _entry(key, value, parse_exiftool_datetime(value), "unparseable")
        )

    original_raw = meta.get_str(_ORIGINAL_PATH_KEY)
    if original_raw:
        op = Path(original_raw)
        out.append(
            _entry(
                "filename · OriginalPath",
                op.name,
                _match_filename(op.name),
                "no pattern",
            )
        )
        out.append(
            _entry(
                "folder · OriginalPath",
                op.parent.name,
                _match_folder(op.parent.name),
                "no pattern",
            )
        )
    else:
        for label in ("filename · OriginalPath", "folder · OriginalPath"):
            out.append(
                DateCandidate(label, "(OriginalPath absent)", None, "absent")
            )

    out.append(
        _entry(
            "filename · current",
            meta.path.name,
            _match_filename(meta.path.name),
            "no pattern",
        )
    )
    out.append(
        _entry(
            "folder · current",
            meta.path.parent.name,
            _match_folder(meta.path.parent.name),
            "no pattern",
        )
    )

    mt = meta.get_str(_MTIME_KEY)
    if not mt:
        out.append(DateCandidate(_MTIME_KEY, "(absent)", None, "absent"))
    else:
        out.append(
            _entry(_MTIME_KEY, mt, parse_exiftool_datetime(mt), "unparseable")
        )

    return out


def _is_future(dt: datetime, now: datetime) -> bool:
    """True if `dt` is implausibly far in the future (see `_FUTURE_GRACE`)."""
    return dt > now + _FUTURE_GRACE


def _build_date(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> datetime | None:
    """Construct a datetime, normalizing the `YYYY-MM-00` convention.

    A day of `00` (used historically to mean "month known, day unknown")
    is mapped to the 1st so it becomes a real date. Returns None if the
    components still don't form a valid date (e.g. month 00, day 32).
    """
    if day == 0:
        day = 1
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def _match_first(
    patterns: tuple[re.Pattern[str], ...], text: str
) -> datetime | None:
    for pattern in patterns:
        m = pattern.search(text)
        if m is None:
            continue
        year, month, day, hour, minute, second = (int(g) for g in m.groups())
        dt = _build_date(year, month, day, hour, minute, second)
        if dt is not None:
            return dt
    return None


def _match_date_only(text: str) -> datetime | None:
    """Match a date-only pattern (`YYYY-MM-DD` or `YYYYMMDD`) at midnight."""
    for pattern in _DATE_ONLY_PATTERNS:
        m = pattern.search(text)
        if m is None:
            continue
        year, month, day = (int(g) for g in m.groups()[:3])
        dt = _build_date(year, month, day)
        if dt is not None:
            return dt
    return None


def _match_filename(name: str) -> datetime | None:
    """Date from a filename: a full timestamp first, else a date-only
    fallback on the stem (so `2015-08-30-1.m4v` and `20150830.m4v` resolve
    to that day at midnight, the `.ext` boundary notwithstanding)."""
    dt = _match_first(_FILENAME_PATTERNS, name)
    if dt is not None:
        return dt
    return _match_date_only(Path(name).stem)


def _match_folder(name: str) -> datetime | None:
    return _match_date_only(name)
