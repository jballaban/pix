"""Reserved names for pix's transient on-disk markers.

During migrate, organize, and rotate, pix creates short-lived intermediate
files *in the media tree* (beside the real files): a case-only rename
intermediate, a CONVERT marker, an organize cycle-break park file, and a
rotate remux temp. Every one of them embeds `MARKER_INFIX` (`.__`), so the
single glob `PIX_MARKER_GLOB` (`*.__*`) matches all of them — that's the one
rule a file-sync client needs to exclude them (see
spec/implementation.md -> Sync client interaction).

**Invariant:** any new transient marker pix writes into the media tree MUST
embed `MARKER_INFIX`, so the single sync rule keeps covering it. `test_markers`
guards this for the markers defined here.

**Safe to exclude from sync.** None of these is ever the sole copy of unique
data: the committed file is always present in the media tree, or the original
is captured under `.pix/runs/` *first* (`apply._apply_convert` moves the source
into the run folder before finalizing the converted output). Excluding them
from sync loses at most reproducible work (a re-convert / re-tag / re-rename),
never data — provided `.pix/runs/` stays on synced/durable storage.

The one transient pix does *not* name is ExifTool's own atomic-write temp
(`EXIFTOOL_TMP_SUFFIX`): ExifTool creates it, pix only cleans it up. It does
NOT match `PIX_MARKER_GLOB` (single underscore, no `.__`), so a sync client
needs the separate `EXIFTOOL_TMP_GLOB` for it.
"""

from __future__ import annotations

# The shared substring every pix-authored marker contains, plus the sync-rule
# glob that matches it. Changing these means updating the documented sync rules.
MARKER_INFIX: str = ".__"
PIX_MARKER_GLOB: str = "*.__*"

# Specific markers — all embed MARKER_INFIX.
RENAME_SUFFIX: str = ".__pixrename__"           # <name>.__pixrename__
CONVERT_INFIX: str = ".__migrate__."            # <name>.__migrate__.<new-ext>
ORGANIZE_TMP_SUFFIX: str = ".__organize_tmp__"  # <line-id>.__organize_tmp__
ROTATE_INFIX: str = ".__rot__"                  # <stem>.__rot__<.ext>
IMPORT_TMP_SUFFIX: str = ".__import__"          # <name>.__import__ (partial download)

# ExifTool's own atomic-write temp — external (ExifTool names it), does NOT
# match PIX_MARKER_GLOB; a sync client needs this separate rule.
EXIFTOOL_TMP_SUFFIX: str = "_exiftool_tmp"
EXIFTOOL_TMP_GLOB: str = "*_exiftool_tmp"


def is_pix_marker(name: str) -> bool:
    """True if `name` is a pix-authored transient marker (matches PIX_MARKER_GLOB)."""
    return MARKER_INFIX in name
