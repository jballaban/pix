"""Per-file ffprobe video-profile cache.

Caches the (codec, profile, pix_fmt) triple returned by
`pix.convert.probe_video_profile` for every keep-policy mp4/m4v
candidate. Per spec/migrate.md → Windows playability check, plan-gen
probes these files to decide keep vs CONVERT-for-re-encode; the cache
turns "probe ~10k mp4s × ~100ms each" into a sub-second cache-read
pass on subsequent migrate runs.

Cache file layout (suffix `.video`):

    media:  G:\\pix\\raw\\2023\\foo.mp4
    cache:  <library>/.pix/cache/G/pix/raw/2023/foo.mp4.video

    {"size": ..., "mtime_ns": ..., "codec": "h264",
     "profile": "Main", "pix_fmt": "yuv420p"}

Validation is by `(size, mtime_ns)` — same scheme as `pix.hash_cache`,
because the profile is a derived fact about the file's bytes that we
don't update synchronously when content changes. CONVERT rewrites a
file's bytes → mtime changes → cache invalidates → next migrate
re-probes (and finds the file is now HEVC — the canonical codec — since
CONVERT re-encodes to libx265).

The cache stores the profile **triple**, not the codec verdict. That way
if the canonical-codec rule ever changes, the cache stays correct — only
the verdict gets recomputed each run from the cached triple.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from pix import cache_base
from pix.convert import VideoProfile


SUFFIX: str = ".video"


def cache_path_for(library_root: Path, file_path: Path) -> Path:
    """Return the cache file path that mirrors `file_path`."""
    return cache_base.cache_path_for(library_root, file_path, SUFFIX)


def _profile_from_data(data: dict[str, object]) -> VideoProfile | None:
    """Extract a VideoProfile from a parsed cache dict, or None on a bad row."""
    codec = data.get("codec")
    profile = data.get("profile")
    pix_fmt = data.get("pix_fmt")
    if not (
        isinstance(codec, str)
        and isinstance(profile, str)
        and isinstance(pix_fmt, str)
    ):
        return None
    return VideoProfile(codec=codec, profile=profile, pix_fmt=pix_fmt)


def read_cached_profile(
    library_root: Path, file_path: Path
) -> VideoProfile | None:
    """Return the cached VideoProfile, or None if missing/stale.

    Stats the live file to validate `(size, mtime_ns)`. Callers that
    already have those values (from a scandir walk) should use the
    parallel-read helper to skip the stat.
    """
    data = cache_base.read_json(cache_path_for(library_root, file_path))
    if data is None:
        return None
    try:
        st = file_path.stat()
    except OSError:
        return None
    if data.get("size") != st.st_size:
        return None
    if data.get("mtime_ns") != st.st_mtime_ns:
        return None
    return _profile_from_data(data)


def _validate_cached_profile(
    library_root: Path,
    file_path: Path,
    expected_size: int,
    expected_mtime_ns: int,
) -> VideoProfile | None:
    """Like `read_cached_profile` but uses caller-provided size+mtime_ns."""
    data = cache_base.read_json(cache_path_for(library_root, file_path))
    if data is None:
        return None
    if data.get("size") != expected_size:
        return None
    if data.get("mtime_ns") != expected_mtime_ns:
        return None
    return _profile_from_data(data)


def write_cached_profile(
    library_root: Path,
    file_path: Path,
    *,
    profile: VideoProfile,
    size: int,
    mtime_ns: int,
) -> None:
    """Atomically write a cache entry for `file_path`."""
    payload: dict[str, object] = {
        "size": size,
        "mtime_ns": mtime_ns,
        "codec": profile.codec,
        "profile": profile.profile,
        "pix_fmt": profile.pix_fmt,
    }
    cache_base.write_json_atomic(
        cache_path_for(library_root, file_path), payload
    )


def read_all_cached_profiles(
    library_root: Path,
    paths_with_meta: list[tuple[Path, int, int]],
    on_batch: Callable[[int], None] | None = None,
    batch_size: int = cache_base.DEFAULT_BATCH_SIZE,
    max_workers: int = cache_base.DEFAULT_WORKERS,
) -> dict[Path, VideoProfile | None]:
    """Return `{path: cached_profile_or_None}` for every input path."""
    return cache_base.read_all_parallel(
        library_root,
        paths_with_meta,
        _validate_cached_profile,
        on_batch=on_batch,
        batch_size=batch_size,
        max_workers=max_workers,
    )
