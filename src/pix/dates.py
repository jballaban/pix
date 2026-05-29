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
from datetime import datetime
from pathlib import Path

from pix import debug
from pix.metadata import FileMetadata

# Format used in pix:DateAuto, pix:DateOverride, etc. See spec/tags.md.
PIX_DATETIME_FORMAT: str = "%Y-%m-%d-%H:%M:%S"


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


# Filename patterns. Each matches `YYYY MM DD HH MM SS` in some shape.
_FILENAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    # YYYY-MM-DD_HHMMSS or YYYY-MM-DD-HHMMSS (pix canonical, similar)
    re.compile(r"(\d{4})-(\d{2})-(\d{2})[_\-](\d{2})(\d{2})(\d{2})"),
    # IMG_YYYYMMDD_HHMMSS, PXL_YYYYMMDD_HHMMSSsss, YYYYMMDDHHMMSS,
    # YYYYMMDDHHMMSSsss — separator between date and time is optional so
    # Synology-style burst exports (`20251013073558000.JPG`) match.
    re.compile(r"(?:^|[_\-])(\d{4})(\d{2})(\d{2})[_\-]?(\d{2})(\d{2})(\d{2})"),
)

# Folder-name patterns: date only (no time required).
_FOLDER_PATTERNS: tuple[re.Pattern[str], ...] = (
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

    Returns None when every candidate fails. Side effect: stores the
    selected source for `last_derivation_source()` to expose to callers
    that want to record provenance (debug logs, drift checks, etc.).
    """
    global _last_derivation_source
    _last_derivation_source = None

    debug.section("DateAuto derivation")
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
                debug.log(
                    f"  {key:<32} {value!r}  ->  {dt.isoformat()}  ✓ matched"
                )
                result = dt
                source_summary = f"{key} = {value!r}"
                break
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
        name_match = _match_first(_FILENAME_PATTERNS, original_name)
        if name_match is not None:
            debug.log(
                f"  Original-filename pattern matched on "
                f"{original_name!r}: {name_match.isoformat()}  ✓"
            )
            result = name_match
            source_summary = (
                f"filename pattern on pix:OriginalPath name {original_name!r}"
            )
        else:
            debug.log(
                f"  Original-filename pattern: no match "
                f"(pix:OriginalPath name = {original_name!r})"
            )

        # 3. Original parent-folder pattern
        if result is None:
            folder_match = _match_folder(original_parent_name)
            if folder_match is not None:
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
            else:
                debug.log(
                    f"  Original-folder pattern: no match "
                    f"(pix:OriginalPath parent = {original_parent_name!r})"
                )
    elif result is None:
        debug.log("  pix:OriginalPath: (absent — skipping original-path patterns)")

    # 4. Current filename pattern
    if result is None:
        name_match = _match_first(_FILENAME_PATTERNS, meta.path.name)
        if name_match is not None:
            debug.log(
                f"  Current-filename pattern matched: "
                f"{name_match.isoformat()}  ✓"
            )
            result = name_match
            source_summary = (
                f"filename pattern on current name {meta.path.name!r}"
            )
        else:
            debug.log(
                f"  Current-filename pattern: no match ({meta.path.name!r})"
            )

    # 5. Current parent-folder pattern
    if result is None:
        folder_match = _match_folder(meta.path.parent.name)
        if folder_match is not None:
            debug.log(
                f"  Current-folder pattern matched: "
                f"{folder_match.isoformat()}  ✓"
            )
            result = folder_match
            source_summary = (
                f"parent-folder pattern on current parent "
                f"{meta.path.parent.name!r}"
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
            if dt is not None:
                debug.log(
                    f"  File:FileModifyDate {mtime!r}  ->  "
                    f"{dt.isoformat()}  ✓ (last-resort fallback)"
                )
                result = dt
                source_summary = (
                    f"File:FileModifyDate = {mtime!r} (last-resort mtime fallback)"
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
    won. The winner is the first entry with a non-None `parsed`.
    """
    out: list[DateCandidate] = []

    keys = _VIDEO_DATE_KEYS if _is_video(meta.path) else _PHOTO_DATE_KEYS
    for key in keys:
        value = meta.get_str(key)
        if not value:
            out.append(DateCandidate(key, "(absent)", None, "absent"))
            continue
        dt = parse_exiftool_datetime(value)
        out.append(
            DateCandidate(key, value, dt, "matched" if dt else "unparseable")
        )

    original_raw = meta.get_str(_ORIGINAL_PATH_KEY)
    if original_raw:
        op = Path(original_raw)
        nm = _match_first(_FILENAME_PATTERNS, op.name)
        out.append(
            DateCandidate(
                "filename · OriginalPath",
                op.name,
                nm,
                "matched" if nm else "no pattern",
            )
        )
        fm = _match_folder(op.parent.name)
        out.append(
            DateCandidate(
                "folder · OriginalPath",
                op.parent.name,
                fm,
                "matched" if fm else "no pattern",
            )
        )
    else:
        for label in ("filename · OriginalPath", "folder · OriginalPath"):
            out.append(
                DateCandidate(label, "(OriginalPath absent)", None, "absent")
            )

    cnm = _match_first(_FILENAME_PATTERNS, meta.path.name)
    out.append(
        DateCandidate(
            "filename · current",
            meta.path.name,
            cnm,
            "matched" if cnm else "no pattern",
        )
    )
    cfm = _match_folder(meta.path.parent.name)
    out.append(
        DateCandidate(
            "folder · current",
            meta.path.parent.name,
            cfm,
            "matched" if cfm else "no pattern",
        )
    )

    mt = meta.get_str(_MTIME_KEY)
    if not mt:
        out.append(DateCandidate(_MTIME_KEY, "(absent)", None, "absent"))
    else:
        dt = parse_exiftool_datetime(mt)
        out.append(
            DateCandidate(_MTIME_KEY, mt, dt, "matched" if dt else "unparseable")
        )

    return out


def _match_first(
    patterns: tuple[re.Pattern[str], ...], text: str
) -> datetime | None:
    for pattern in patterns:
        m = pattern.search(text)
        if m is None:
            continue
        try:
            year, month, day, hour, minute, second = (
                int(g) for g in m.groups()
            )
            return datetime(year, month, day, hour, minute, second)
        except ValueError:
            continue
    return None


def _match_folder(name: str) -> datetime | None:
    for pattern in _FOLDER_PATTERNS:
        m = pattern.search(name)
        if m is None:
            continue
        try:
            year, month, day = (int(g) for g in m.groups()[:3])
            return datetime(year, month, day, 0, 0, 0)
        except ValueError:
            continue
    return None
