"""Per-file persistent metadata cache.

Each media file gets one tiny JSON sidecar under
`<library>/.pix/cache/`, with the cache file's path mirroring the
media file's absolute path:

    media:  G:\\pix\\raw\\2023\\Hawaii\\foo.jpg
    cache:  <library>/.pix/cache/G/pix/raw/2023/Hawaii/foo.jpg.cache

Drive letters are folded into folder names (no `:` in NTFS dir names);
filename gets `.cache` appended.

Cache file format:

    {"v": 1, "size": 4521234, "metadata": { ... ExifTool JSON ... }}

Lookup is a single stat + small JSON read per file. Validation is by
`size` only — under pix's single-writer trust model, `size` is enough
insurance against the rare in-place edit. No mtime, no hash.

Mutations are best-effort: if a rename or delete on the cache file
fails, the next run just rebuilds that single file's entry from
ExifTool. Failures don't propagate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

CACHE_FILE_VERSION: int = 1


@dataclass(frozen=True)
class PerFileCache:
    """Persistent metadata cache rooted at `<library>/.pix/cache/`."""

    cache_root: Path

    @classmethod
    def for_library(cls, library_root: Path) -> "PerFileCache":
        return cls(cache_root=library_root / ".pix" / "cache")

    def cache_path_for(self, media_path: Path) -> Path:
        """Return the cache file path that mirrors `media_path`.

        Absolute media path → cache path under `cache_root` with the
        drive letter as a top-level folder and `.cache` appended to
        the filename.

        Caller is expected to pass an absolute path — every pix caller
        goes through `pix.scan.walk_source_files` which returns
        already-absolute canonical paths. A defensive `resolve()` here
        would cost one stat per lookup and is the dominant cost of the
        cache-check phase at scale.
        """
        parts = media_path.parts
        if not parts:
            # Shouldn't happen for a real file; defensive.
            return self.cache_root / (media_path.name + ".cache")
        # On Windows, parts[0] is like "G:\\" — strip trailing slashes
        # and colon to get just the drive letter.
        drive = parts[0].rstrip("\\/").rstrip(":")
        rest = parts[1:]
        if rest:
            mirrored = Path(drive, *rest)
        else:
            mirrored = Path(drive)
        return self.cache_root / mirrored.with_name(mirrored.name + ".cache")

    def get(self, media_path: Path) -> dict[str, object] | None:
        """Return cached metadata if present and current; else None.

        Validates `size` against the media file's current size. Mismatch
        or any read/parse error returns None — next read will overwrite
        the cache entry naturally.
        """
        cache_path = self.cache_path_for(media_path)
        if not cache_path.is_file():
            return None
        try:
            loaded: object = json.loads(cache_path.read_bytes())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(loaded, dict):
            return None
        data = cast("dict[str, object]", loaded)
        if data.get("v") != CACHE_FILE_VERSION:
            return None
        try:
            current_size = media_path.stat().st_size
        except OSError:
            return None
        if data.get("size") != current_size:
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
        cache_path = self.cache_path_for(media_path)
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "v": CACHE_FILE_VERSION,
                        "size": size,
                        "metadata": metadata,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass  # best-effort

    def remove(self, media_path: Path) -> None:
        """Delete the cache entry for `media_path` if present."""
        cache_path = self.cache_path_for(media_path)
        try:
            cache_path.unlink(missing_ok=True)
        except OSError:
            pass  # best-effort

    def rename(self, old_media_path: Path, new_media_path: Path) -> None:
        """Move the cache file alongside a media file rename/move."""
        old_cache = self.cache_path_for(old_media_path)
        if not old_cache.is_file():
            return
        new_cache = self.cache_path_for(new_media_path)
        try:
            new_cache.parent.mkdir(parents=True, exist_ok=True)
            old_cache.replace(new_cache)
        except OSError:
            pass  # best-effort

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
