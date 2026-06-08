"""Persistent metadata cache — facade over the SQLite store (`pix.cache_db`).

Keeps the `PerFileCache` API the rest of pix already calls; the storage is now
one row per file in `<library>/.pix/cache.db` (see `pix.cache_db`). The `meta`
column holds the (filtered) ExifTool JSON, validated together with the file's
content caches on `(size, mtime_ns)`.

Mutations are best-effort: a failed write just means the next run rebuilds that
one entry from ExifTool.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pix import cache_db
from pix.metadata_filter import filter_consumed


@dataclass(frozen=True)
class PerFileCache:
    """Metadata cache rooted at `<library>/.pix/cache.db`."""

    library_root: Path

    @classmethod
    def for_library(cls, library_root: Path) -> "PerFileCache":
        return cls(library_root=library_root)

    def get(
        self,
        media_path: Path,
        expected_size: int | None = None,
        expected_mtime_ns: int | None = None,
    ) -> dict[str, object] | None:
        """Return cached metadata if present and current; else None.

        With `expected_size` / `expected_mtime_ns` (from the scandir walk),
        validates the row's stamp — both are checked when given (unified key).
        Pass neither to skip validation (e.g. update-after-write, where the
        caller knows the cache is fresh).
        """
        row = cache_db.get(self.library_root, media_path)
        if row is None or row.meta is None:
            return None
        if expected_size is not None and row.size != expected_size:
            return None
        if expected_mtime_ns is not None and row.mtime_ns != expected_mtime_ns:
            return None
        return row.meta

    def add(self, media_path: Path, metadata: dict[str, object]) -> None:
        """Write a metadata row for `media_path`, stamping the current
        `(size, mtime_ns)`. Stores only the consumed tags (see
        `pix.metadata_filter`). Skips silently if the file vanished."""
        try:
            st = media_path.stat()
        except OSError:
            return
        cache_db.put_meta(
            self.library_root,
            media_path,
            filter_consumed(metadata),
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
        )

    def remove(self, media_path: Path) -> None:
        """Delete the cache row for `media_path` if present."""
        cache_db.remove(self.library_root, media_path)

    def rename(self, old_media_path: Path, new_media_path: Path) -> None:
        """Move the cache row alongside a media file rename/move."""
        cache_db.relocate(self.library_root, old_media_path, new_media_path)

    def update_metadata(
        self, media_path: Path, updates: dict[str, object]
    ) -> None:
        """Reflect an in-place metadata-only write: merge `updates`, re-stamp,
        and carry the content hash + fingerprint forward (unchanged by a
        metadata write). No-op if there's no row yet."""
        try:
            st = media_path.stat()
        except OSError:
            return
        cache_db.note_inplace_metadata_change(
            self.library_root,
            media_path,
            meta_updates=updates,
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
        )
