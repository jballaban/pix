"""The `rating` tag — a 0–5 star curation value in the standard `XMP:Rating`.

Unlike `date`/`event`, rating has **no `_auto`/override split** (spec/tags.md →
Rating): it's pure human judgment, so pix reads and writes the industry-standard
`XMP:Rating` field directly — the one standard field pix writes rather than only
reads. Migrate never touches it; [checkout](tag-editing.md) assigns it and
[dedupe](dedupe.md) consolidates it (max across a duplicate group).

Absent (or negative — XMP's `-1` "rejected" sentinel, which pix has no concept
of) reads as unrated (`None`); an explicit `0`–`5` renders to its own folder in
`{rating}` templates.
"""

from __future__ import annotations

from pix.metadata import FileMetadata

# Standard XMP field — NOT the pix:* namespace. See spec/tags.md → Metadata
# mapping (the sole standard-field exception pix writes).
XMP_RATING: str = "XMP:Rating"


def effective_rating(meta: FileMetadata) -> str | None:
    """Return the effective `rating` as a string ``"0"``–``"5"``, or None.

    None when `XMP:Rating` is absent, non-numeric, or negative. Values above 5
    clamp to ``"5"``. ExifTool emits Rating as a JSON number, so the raw value
    may be int / float / numeric string; all are coerced. See spec/tags.md →
    Effective value computation.
    """
    raw = meta.raw.get(XMP_RATING)
    if isinstance(raw, bool):  # bool is an int subclass — never a rating
        return None
    if isinstance(raw, (int, float)):
        n = int(round(raw))
    elif isinstance(raw, str):
        try:
            n = int(round(float(raw)))
        except ValueError:
            return None
    else:
        return None
    if n < 0:
        return None
    return str(min(n, 5))
