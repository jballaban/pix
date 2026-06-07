"""Per-file perceptual-video-fingerprint cache.

One JSON sidecar per video under `<library>/.pix/cache/`, suffix `.vfp`:

    media:  G:\\pix\\2023\\clip.mp4
    cache:  <library>/.pix/cache/G/pix/2023/clip.mp4.vfp

    {"size": ..., "mtime_ns": ..., "frames": [...], "width": ...,
     "height": ..., "duration": ..., "computed_at": "..."}

Valid when `(size, mtime_ns)` match the live file (same scheme as
`hash_cache`); otherwise stale → treated as missing.
Computing a fingerprint decodes several frames, so — like `pix hash` — the
cost is paid once and reused by every later `pix dedupe` run. A CONVERT
that rewrites the bytes changes mtime → cache invalidates → re-fingerprint
(which is correct: the new encode gets its own, encoder-independent,
fingerprint that still matches its siblings).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from pix import cache_base
from pix.video_fingerprint import VideoFingerprint


SUFFIX: str = ".vfp"


def cache_path_for(library_root: Path, file_path: Path) -> Path:
    """Return the cache file path that mirrors `file_path`."""
    return cache_base.cache_path_for(library_root, file_path, SUFFIX)


def _fingerprint_from_data(data: dict[str, object]) -> VideoFingerprint | None:
    """Build a VideoFingerprint from a parsed cache dict, or None on a bad row."""
    frames = data.get("frames")
    width = data.get("width")
    height = data.get("height")
    duration = data.get("duration")
    if not (
        isinstance(frames, list)
        and all(isinstance(f, int) for f in frames)  # pyright: ignore[reportUnknownVariableType]
        and isinstance(width, int)
        and isinstance(height, int)
        and isinstance(duration, (int, float))
    ):
        return None
    return VideoFingerprint(
        frames=tuple(frames),  # pyright: ignore[reportUnknownArgumentType]
        width=width,
        height=height,
        duration=float(duration),
    )


def read_cached_fingerprint(
    library_root: Path, file_path: Path
) -> VideoFingerprint | None:
    """Return the cached fingerprint, or None if missing/stale.

    Stats the live file to validate `(size, mtime_ns)`."""
    data = cache_base.read_json(cache_path_for(library_root, file_path))
    if data is None:
        return None
    try:
        st = file_path.stat()
    except OSError:
        return None
    if data.get("size") != st.st_size or data.get("mtime_ns") != st.st_mtime_ns:
        return None
    return _fingerprint_from_data(data)


def _validate_cached_fingerprint(
    library_root: Path,
    file_path: Path,
    expected_size: int,
    expected_mtime_ns: int,
) -> VideoFingerprint | None:
    """Like `read_cached_fingerprint` but uses caller-provided size+mtime_ns."""
    data = cache_base.read_json(cache_path_for(library_root, file_path))
    if data is None:
        return None
    if data.get("size") != expected_size:
        return None
    if data.get("mtime_ns") != expected_mtime_ns:
        return None
    return _fingerprint_from_data(data)


def write_cached_fingerprint(
    library_root: Path,
    file_path: Path,
    *,
    fingerprint: VideoFingerprint,
    size: int,
    mtime_ns: int,
) -> None:
    """Atomically write a cache entry for `file_path`."""
    payload: dict[str, object] = {
        "size": size,
        "mtime_ns": mtime_ns,
        "frames": list(fingerprint.frames),
        "width": fingerprint.width,
        "height": fingerprint.height,
        "duration": fingerprint.duration,
        "computed_at": datetime.now().isoformat(timespec="seconds"),
    }
    cache_base.write_json_atomic(
        cache_path_for(library_root, file_path), payload
    )


def read_all_cached_fingerprints(
    library_root: Path,
    paths_with_meta: list[tuple[Path, int, int]],
    on_batch: Callable[[int], None] | None = None,
    batch_size: int = cache_base.DEFAULT_BATCH_SIZE,
    max_workers: int = cache_base.DEFAULT_WORKERS,
) -> dict[Path, VideoFingerprint | None]:
    """Return `{path: cached_fingerprint_or_None}` for every input path."""
    return cache_base.read_all_parallel(
        library_root,
        paths_with_meta,
        _validate_cached_fingerprint,
        on_batch=on_batch,
        batch_size=batch_size,
        max_workers=max_workers,
    )
