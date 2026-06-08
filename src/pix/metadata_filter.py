"""Allowlist for what the metadata cache actually stores.

ExifTool returns *every* readable tag — MakerNotes, embedded thumbnails /
preview images (huge base64 blobs), ICC profiles, ratings, keywords, and so
on. pix consumes only a finite set (the `pix:*` fields, the DateAuto
candidates, file basics, and — for the planned face workflow — region tags).
Storing the rest bloated the old per-file `.meta` sidecars and now bloats the
`meta` column; filtering to the consumed set shrinks the cache and its
parse time substantially.

This module has **no pix imports on purpose** (literal keys, not the
`PIX_*` / date-key constants) so it can be used from `metadata_cache` and
`cache_db` without an import cycle. `tests/test_metadata_filter.py` asserts
this allowlist stays in sync with the constants the code actually reads — so
the duplication can't silently drift.

Live reads are unaffected: `read_metadata_batched` returns the *full* metadata
for the current run; only the persisted copy is trimmed. `pix meta` reads the
live file (no cache), so diagnostics still see every tag.
"""

from __future__ import annotations


# Exact group-prefixed (family-0) keys pix reads from cached metadata.
_CONSUMED_KEYS: frozenset[str] = frozenset(
    {
        "SourceFile",
        # pix:* namespace (plan.py / events.py / dates.py).
        "XMP:DateAuto",
        "XMP:DateAutoPrevious",
        "XMP:DateOverride",
        "XMP:EventAuto",
        "XMP:EventAutoPrevious",
        "XMP:EventOverride",
        "XMP:OriginalPath",
        "XMP:MergeEvent",
        "XMP:MergeDate",
        # DateAuto candidates (dates.py: photo + video key lists, mtime).
        "EXIF:DateTimeOriginal",
        "EXIF:CreateDate",
        "EXIF:DateTimeDigitized",
        "XMP:DateCreated",
        "XMP:CreateDate",
        "IPTC:DateCreated",
        "QuickTime:CreateDate",
        "QuickTime:MediaCreateDate",
        "File:FileModifyDate",
    }
)


def _is_consumed(key: str) -> bool:
    # Region tags (RegionInfo, RegionName, RegionType, RegionArea*, …) back the
    # planned face workflow; keep the whole family forward-compatibly.
    return key in _CONSUMED_KEYS or "Region" in key


def filter_consumed(raw: dict[str, object]) -> dict[str, object]:
    """Return `raw` with only the keys pix actually consumes from the cache."""
    return {k: v for k, v in raw.items() if _is_consumed(k)}
