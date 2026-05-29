"""Shared plumbing for per-file caches under `<library>/.pix/cache/`.

Three caches share this layer (one file per flavor on disk; see the
discussion in the per-cache modules for why we keep them separate):

- `<...>.cache` — ExifTool JSON, validated on size only
- `<...>.hash` — content-hash digest, validated on (size, mtime_ns)
- `<...>.video` — ffprobe codec/profile/pix_fmt, validated on (size, mtime_ns)

All three mirror the source path under `<library>/.pix/cache/` with a
suffix appended:

    media:  G:\\pix\\raw\\2023\\foo.jpg
    cache:  <library>/.pix/cache/G/pix/raw/2023/foo.jpg.<suffix>

This module owns the path-mirroring scheme, the JSON read/write
plumbing (atomic and plain variants), and the parallel-lookup
primitive. Each cache module declares its suffix + schema + validation
key and delegates the rest here.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar, cast


T = TypeVar("T")

# Default thread count for parallel cache reads. Matches the workers
# used elsewhere (metadata cache lookup, hash-cache scan); per-file
# checks are I/O-bound JSON reads of tiny files, so 32 is comfortable
# on any SSD/NVMe.
DEFAULT_WORKERS: int = 32
DEFAULT_BATCH_SIZE: int = 1000

# Known live cache suffixes. Anything else in `.pix/cache/` is either
# legacy (see `_LEGACY_SUFFIXES`) or stray.
LIVE_SUFFIXES: tuple[str, ...] = (".meta", ".hash", ".video")

# Suffixes from earlier pix versions that should be cleared on contact.
# Currently: `.cache` was renamed to `.meta` in v0.1.88; old sidecars
# are unreachable by the new code (the metadata cache reads `.meta`)
# but consume disk space until pruned.
_LEGACY_SUFFIXES: tuple[str, ...] = (".cache",)


def cache_root_for(library_root: Path) -> Path:
    """Return the on-disk cache directory: `<library>/.pix/cache/`."""
    return library_root / ".pix" / "cache"


def mirror_under(root: Path, file_path: Path, suffix: str = "") -> Path:
    """Mirror `file_path` under `root`, appending `suffix` to the filename.

    Drive letters (Windows) fold into the first folder name because
    NTFS dir names can't contain `:` (`G:\\pix\\foo.jpg` → `root/G/pix/foo.jpg`).
    Caller is expected to pass an absolute path — every pix caller goes
    through `pix.scan.walk_source_files` which returns absolute canonical
    paths. A defensive `resolve()` here would cost one stat per lookup.

    Shared by the cache tree (`<library>/.pix/cache/`, with a `.meta`/`.hash`/
    `.video` suffix) and the errors tree (`<library>/.pix/errors/`, no suffix —
    the moved file keeps its own name, and its *location* records the source
    path so a lost sidecar doesn't lose provenance).
    """
    parts = file_path.parts
    if not parts:
        return root / (file_path.name + suffix)
    drive = parts[0].rstrip("\\/").rstrip(":")
    rest = parts[1:]
    if rest:
        mirrored = Path(drive, *rest)
    else:
        mirrored = Path(drive)
    return root / mirrored.with_name(mirrored.name + suffix)


def cache_path_for(
    library_root: Path, file_path: Path, suffix: str
) -> Path:
    """Mirror `file_path` under `<library>/.pix/cache/` with `suffix`."""
    return mirror_under(cache_root_for(library_root), file_path, suffix)


def read_json(cache_path: Path) -> dict[str, object] | None:
    """Read + parse a JSON cache file. None on miss / unreadable / bad type.

    No `is_file()` precheck — `read_bytes()` is attempted directly and
    `FileNotFoundError` is treated as a miss. Saves one stat per cache
    hit on the hot path.
    """
    try:
        loaded: object = json.loads(cache_path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    return cast("dict[str, object]", loaded)


def write_json_atomic(
    cache_path: Path, payload: dict[str, object]
) -> None:
    """Atomic write: `<target>.tmp` + fsync + `os.replace`.

    A crash mid-write leaves either the old entry (if any) or no
    entry — never a truncated one. Used by caches that are expensive
    to rebuild (content hash, ffprobe video probe).
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.parent / (cache_path.name + ".tmp")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, cache_path)


def write_json_plain(
    cache_path: Path, payload: dict[str, object]
) -> None:
    """Non-atomic write — fast path for high-volume caches.

    No `.tmp`, no fsync. A crash mid-write leaves a truncated entry
    that `read_json` will reject (JSON parse fails) on the next pass,
    so the worst case is "re-derive that one entry." Used by the
    metadata cache, which writes thousands of entries per migrate run
    and is cheap to rebuild from ExifTool.
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def remove(cache_path: Path) -> None:
    """Best-effort delete; never raises."""
    try:
        cache_path.unlink(missing_ok=True)
    except OSError:
        pass


def rename(old: Path, new: Path) -> None:
    """Best-effort rename alongside a media-file move; never raises."""
    if not old.is_file():
        return
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        old.replace(new)
    except OSError:
        pass


def relocate_all(
    library_root: Path, old_media: Path, new_media: Path
) -> None:
    """Move *every* cache sidecar (.meta/.hash/.video) with a media move.

    A media MOVE/RENAME doesn't touch the file's bytes, size, or mtime,
    so a valid hash/video entry stays valid at the new path — but only
    if its sidecar follows. Relocating just `.meta` (the historical
    behavior) left `.hash`/`.video` orphaned at the old mirror, where
    the next walk's `prune_orphans` deleted them; the file then needed a
    fresh `pix hash` / ffprobe pass for no reason. Best-effort per
    suffix.
    """
    for suffix in LIVE_SUFFIXES:
        rename(
            cache_path_for(library_root, old_media, suffix),
            cache_path_for(library_root, new_media, suffix),
        )


def remove_all(library_root: Path, media: Path) -> None:
    """Delete every cache sidecar for a media file that's gone (DELETE/
    STASH/CONVERT-source). Best-effort per suffix."""
    for suffix in LIVE_SUFFIXES:
        remove(cache_path_for(library_root, media, suffix))


def unmirror_under(
    root: Path, mirrored: Path, suffixes: tuple[str, ...]
) -> Path | None:
    """Reverse `mirror_under`: recover the absolute source path that
    `mirrored` (a file under `root`) mirrors. Returns None if `mirrored`
    isn't under `root`, is too shallow to carry a drive folder, or its
    name doesn't end in one of `suffixes`.

    Reversal is straightforward because the mirror only mutates the
    first path component (drive letter folds to a bare letter folder)
    and appends a suffix to the filename. Pass `("",)` for the errors
    tree, whose files carry no suffix — `endswith("")` matches and the
    stem is the whole name.
    """
    try:
        rel = mirrored.relative_to(root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 2:
        return None
    name = parts[-1]
    for suffix in suffixes:
        if name.endswith(suffix):
            stem = name[: len(name) - len(suffix)] if suffix else name
            drive = parts[0]
            interior = parts[1:-1]
            # Restore the colon + root-slash to form an absolute
            # Windows path. `Path("G:")` alone is "current dir on G:";
            # `Path("G:\\")` is the drive root.
            return Path(f"{drive}:\\", *interior, stem)
    return None


def _unmirror_path(
    cache_path: Path, library_root: Path
) -> Path | None:
    """Reverse `cache_path_for` for a cache sidecar (recognized suffix)."""
    return unmirror_under(
        cache_root_for(library_root), cache_path, LIVE_SUFFIXES
    )


@dataclass(frozen=True)
class PruneStats:
    """Result of `prune_orphans`. All fields are counts."""

    orphans_removed: int
    legacy_removed: int


def prune_orphans(
    library_root: Path,
    expected_paths: set[Path],
    allowed_prefix: Path | None = None,
) -> PruneStats:
    """Remove cache sidecars whose source media file isn't in
    `expected_paths`, plus any legacy-suffix sidecars from older pix
    versions.

    `allowed_prefix`: when set, only prune sidecars whose mirrored
    media path lives under that prefix. Used by migrate, which walks a
    subfolder and shouldn't touch cache entries for files outside the
    source folder. Library-wide commands (organize, dedupe, hash) pass
    `None` and prune anything not in `expected_paths`.

    Legacy-suffix sidecars are always pruned regardless of
    `allowed_prefix` — their suffix is no longer recognized by any
    live code path, so they're pure dead bytes.
    """
    cache_root = cache_root_for(library_root)
    if not cache_root.is_dir():
        return PruneStats(0, 0)

    orphans_removed = 0
    legacy_removed = 0
    for cache_file in cache_root.rglob("*"):
        if not cache_file.is_file():
            continue
        name = cache_file.name
        if any(name.endswith(s) for s in _LEGACY_SUFFIXES):
            try:
                cache_file.unlink()
                legacy_removed += 1
            except OSError:
                pass
            continue
        if not any(name.endswith(s) for s in LIVE_SUFFIXES):
            continue  # stray file we don't manage; leave alone
        media_path = _unmirror_path(cache_file, library_root)
        if media_path is None:
            continue
        if allowed_prefix is not None:
            try:
                media_path.relative_to(allowed_prefix)
            except ValueError:
                continue  # outside the walked prefix; not ours to judge
        if media_path not in expected_paths:
            try:
                cache_file.unlink()
                orphans_removed += 1
            except OSError:
                pass
    return PruneStats(
        orphans_removed=orphans_removed,
        legacy_removed=legacy_removed,
    )


def read_all_parallel(
    library_root: Path,
    paths_with_meta: list[tuple[Path, int, int]],
    validator: Callable[[Path, Path, int, int], "T | None"],
    on_batch: Callable[[int], None] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_workers: int = DEFAULT_WORKERS,
) -> dict[Path, "T | None"]:
    """Look up cache entries in parallel for a batch of files.

    `validator(library_root, path, size, mtime_ns)` is the per-cache
    validation routine — it reads the sidecar, checks (size, mtime_ns)
    against the caller-supplied values, and returns the typed cached
    value (or None on miss/stale).

    Results arrive in submission order via `ThreadPoolExecutor.map`;
    `on_batch(n)` fires from the consumer thread every `batch_size`
    files, so no locking is needed in the callback.
    """
    if not paths_with_meta:
        return {}

    result: dict[Path, T | None] = {}
    in_batch = 0

    def check_one(
        item: tuple[Path, int, int],
    ) -> tuple[Path, T | None]:
        path, size, mtime_ns = item
        return path, validator(library_root, path, size, mtime_ns)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for path, cached in executor.map(check_one, paths_with_meta):
            result[path] = cached
            in_batch += 1
            if in_batch >= batch_size:
                if on_batch is not None:
                    on_batch(in_batch)
                in_batch = 0

    if in_batch > 0 and on_batch is not None:
        on_batch(in_batch)
    return result
