"""Perceptual-video-fingerprint cache — facade over `pix.cache_db`.

The fingerprint lives in the `vfp` column of each file's row as
`{"frames": [...], "width": ..., "height": ..., "duration": ...}`, validated
on `(size, mtime_ns)`. Computing one decodes several frames, so the cost is
paid once and reused by every later `pix dedupe`. A CONVERT that rewrites the
bytes changes the stamp → re-fingerprint (correct: the new encode gets its own
encoder-independent fingerprint that still matches its siblings).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, cast

from pix import cache_db
from pix.video_fingerprint import VideoFingerprint


_DEFAULT_BATCH_SIZE: int = 1000


def _fingerprint_from_data(data: dict[str, object]) -> VideoFingerprint | None:
    """Build a VideoFingerprint from a stored vfp dict, or None on a bad row."""
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
        frames=tuple(cast("list[int]", frames)),
        width=width,
        height=height,
        duration=float(duration),
    )


def _to_payload(fingerprint: VideoFingerprint) -> dict[str, object]:
    return {
        "frames": list(fingerprint.frames),
        "width": fingerprint.width,
        "height": fingerprint.height,
        "duration": fingerprint.duration,
    }


def read_cached_fingerprint(
    library_root: Path, file_path: Path
) -> VideoFingerprint | None:
    """Return the cached fingerprint, or None if missing/stale. Stats the file
    to validate `(size, mtime_ns)`."""
    row = cache_db.get(library_root, file_path)
    if row is None or row.vfp is None:
        return None
    try:
        st = file_path.stat()
    except OSError:
        return None
    if row.size != st.st_size or row.mtime_ns != st.st_mtime_ns:
        return None
    return _fingerprint_from_data(row.vfp)


def write_cached_fingerprint(
    library_root: Path,
    file_path: Path,
    *,
    fingerprint: VideoFingerprint,
    size: int,
    mtime_ns: int,
) -> None:
    """Write a fingerprint entry for `file_path` against the given stamp."""
    cache_db.put_vfp(
        library_root, file_path, _to_payload(fingerprint), size=size, mtime_ns=mtime_ns
    )


def read_all_cached_fingerprints(
    library_root: Path,
    paths_with_meta: list[tuple[Path, int, int]],
    on_batch: Callable[[int], None] | None = None,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> dict[Path, VideoFingerprint | None]:
    """Return `{path: cached_fingerprint_or_None}` for every input path
    (one `load_all` query + in-memory stamp validation)."""
    rows = cache_db.load_all(library_root)
    result: dict[Path, VideoFingerprint | None] = {}
    in_batch = 0
    for path, size, mtime_ns in paths_with_meta:
        row = rows.get(path)
        if (
            row is not None
            and row.vfp is not None
            and row.size == size
            and row.mtime_ns == mtime_ns
        ):
            result[path] = _fingerprint_from_data(row.vfp)
        else:
            result[path] = None
        in_batch += 1
        if in_batch >= batch_size and on_batch is not None:
            on_batch(in_batch)
            in_batch = 0
    if in_batch > 0 and on_batch is not None:
        on_batch(in_batch)
    return result
