"""Per-file persistent metadata cache.

One tiny JSON sidecar per media file under `<library>/.pix/cache/`,
suffix `.cache`:

    media:  G:\\pix\\raw\\2023\\Hawaii\\foo.jpg
    cache:  <library>/.pix/cache/G/pix/raw/2023/Hawaii/foo.jpg.cache

Cache file format:

    {"v": 1, "size": 4521234, "metadata": { ... ExifTool JSON ... }}

Validation is by `size` only — under pix's single-writer trust model,
size is enough insurance against the rare in-place edit. (Hash and
video caches also check mtime_ns; the metadata cache deliberately
doesn't, because pix's own TAG writes change mtime but we refresh
the cache synchronously, so an mtime check would cause spurious
invalidations after every tag write.)

Mutations are best-effort: if a rename or delete on the cache file
fails, the next run just rebuilds that one entry from ExifTool.
Non-atomic writes (`pix.cache_base.write_json_plain` — no `.tmp`/fsync)
since these run thousands of times per migrate batch and rebuilding
one stray truncated entry from ExifTool is cheap.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pix import cache_base


CACHE_FILE_VERSION: int = 1
SUFFIX: str = ".cache"


@dataclass(frozen=True)
class PerFileCache:
    """Persistent metadata cache rooted at `<library>/.pix/`."""

    library_root: Path

    @classmethod
    def for_library(cls, library_root: Path) -> "PerFileCache":
        return cls(library_root=library_root)

    def cache_path_for(self, media_path: Path) -> Path:
        """Return the cache file path that mirrors `media_path`."""
        return cache_base.cache_path_for(
            self.library_root, media_path, SUFFIX
        )

    def get(
        self,
        media_path: Path,
        expected_size: int | None = None,
    ) -> dict[str, object] | None:
        """Return cached metadata if present and current; else None.

        If `expected_size` is given, validates the cache entry's
        recorded size against it. Pass `None` to skip validation —
        e.g. update-after-write, where the caller knows the cache is
        fresh and re-stat'ing the file would be redundant.
        """
        data = cache_base.read_json(self.cache_path_for(media_path))
        if data is None:
            return None
        if data.get("v") != CACHE_FILE_VERSION:
            return None
        if expected_size is not None and data.get("size") != expected_size:
            return None
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            return None
        return cast("dict[str, object]", metadata)

    def add(self, media_path: Path, metadata: dict[str, object]) -> None:
        """Write a cache entry for `media_path`.

        Records the current file size for later validation. If the file
        disappears between metadata read and cache write, silently
        skip — next run rebuilds.
        """
        try:
            size = media_path.stat().st_size
        except OSError:
            return
        cache_base.write_json_plain(
            self.cache_path_for(media_path),
            {
                "v": CACHE_FILE_VERSION,
                "size": size,
                "metadata": metadata,
            },
        )

    def remove(self, media_path: Path) -> None:
        """Delete the cache entry for `media_path` if present."""
        cache_base.remove(self.cache_path_for(media_path))

    def rename(
        self, old_media_path: Path, new_media_path: Path
    ) -> None:
        """Move the cache file alongside a media file rename/move."""
        cache_base.rename(
            self.cache_path_for(old_media_path),
            self.cache_path_for(new_media_path),
        )

    def update_metadata(
        self, media_path: Path, updates: dict[str, object]
    ) -> None:
        """Merge `updates` into the cached metadata dict for `media_path`.

        No-op if there's no cache entry to update. Re-stamps `size` to
        the file's current size after the update.
        """
        current = self.get(media_path)
        if current is None:
            return
        merged = {**current, **updates}
        self.add(media_path, merged)
