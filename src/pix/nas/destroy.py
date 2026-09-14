"""Removing a file from every tier, for good (spec/nas-app.md §12).

This is the one thing in the app that destroys an original. Everything else
writes a 2KB sidecar and can be taken back; this cannot. [§12] accepts exactly
one destructive act — deliberate deletion — and names the undo for it: Btrfs
snapshots and Hyper Backup, not anything pix builds. So this module's whole job
is to be honest about that and to leave nothing behind.

**Only ever reached from a soft delete.** The order is *decide, then destroy*:
a file has to be marked `deleted` in its sidecar, and seen in that state on the
admin page, before this can be called on it. That is not a UI nicety — it is
what makes the destructive step a second, separate decision by a second kind of
person, rather than a mis-click away from the grid everyone curates in.

**Derived tiers first, master last.** If this is interrupted half way, what
survives is the original and its sidecar — the two things that cannot be
recomputed — and the leftovers are regenerable files that `pix2 process` would
rebuild anyway. Done in the other order, a crash would leave the archive
holding thumbnails of a photograph it no longer has.

Nothing here raises for a file that is already absent. Destroying is meant to
be safe to retry after a partial failure, and a tier that never held a
derivative of this file is the ordinary case, not an error: most images have no
render, and a file that was never processed has no meta.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pix.nas import decisions
from pix.nas.const import LARGE_DIR, PREVIEW_DIR, THUMB_DIR
from pix.nas.derive import derived_path, meta_path, render_path


@dataclass(frozen=True)
class Removed:
    """What actually went, so the log can say rather than assume."""

    master: bool = False
    sidecar: bool = False
    derived: int = 0
    indexed: bool = False

    def nothing(self) -> bool:
        return not (self.master or self.sidecar or self.derived
                    or self.indexed)


def targets(media: Path) -> tuple[Path, ...]:
    """Every path this file occupies, derived tiers first.

    Listed by asking the modules that create them where they put things,
    rather than rebuilding the naming here: a second copy of that rule would
    drift, and the failure mode is orphaned files nobody ever looks for again.
    """
    return (derived_path(media, THUMB_DIR),
            derived_path(media, LARGE_DIR),
            derived_path(media, PREVIEW_DIR),
            render_path(media),
            meta_path(media),
            decisions.sidecar_path(media),
            media)


def destroy(media: Path, *, conn: sqlite3.Connection | None = None,
            folder: str = "", name: str = "") -> Removed:
    """Remove `media` and everything derived from it. There is no undo.

    The index row goes **first**. It is the only one of these that is not a
    file, and leaving it behind would be worse than leaving a thumbnail: the
    grid would offer a photograph that no longer exists, and clicking it would
    be a 404 the curator has no way to explain.
    """
    indexed = False
    if conn is not None and folder and name:
        try:
            for table in ("file_tags", "file_audience", "files"):
                conn.execute(
                    f"DELETE FROM {table} WHERE folder = ? AND name = ?",
                    (folder, name))
            conn.commit()
            indexed = True
        except sqlite3.Error:
            indexed = False

    master = sidecar = False
    derived = 0
    sidecar_path = decisions.sidecar_path(media)
    for path in targets(media):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            # Keep going. A render locked by a player should not strand the
            # original, and what is left is named in the result either way.
            continue
        if path == media:
            master = True
        elif path == sidecar_path:
            sidecar = True
        else:
            derived += 1
    return Removed(master=master, sidecar=sidecar, derived=derived,
                   indexed=indexed)
