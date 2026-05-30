"""Format conversion for `pix migrate` CONVERT actions.

Two conversions are supported, mirroring the extension-policy actions:
- `convert_to_jpg` — Pillow + pillow-heif handles the pixel decode/encode.
- `convert_to_mp4` — ffmpeg subprocess handles container/codec.

Metadata carry-over is **not** handled here. Per the cross-cutting
invariant in spec/README.md, CONVERT must preserve all source metadata;
that's done by the apply layer with a single ExifTool `-tagsFromFile`
call after the pixel/container conversion completes, so this module
focuses on the pixel/audio/video bytes only.
"""

from __future__ import annotations

import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageFile
import pillow_heif  # pyright: ignore[reportMissingTypeStubs]

from pix.timeout import OperationTimeout, run_with_timeout


# Register HEIC/HEIF support with Pillow once at import time so
# `Image.open('foo.heic')` works.
pillow_heif.register_heif_opener()  # pyright: ignore[reportUnknownMemberType]

# Tolerate truncated images so partial-but-recoverable sources (camera
# crashed mid-write, network copy interrupted, drive failure mid-import,
# …) produce the recoverable portion as a valid JPEG instead of refusing
# the convert and quarantining to .pix/errors/. Healthy files decode
# strictly anyway — this only changes behavior on inputs that would
# otherwise raise `"image file is truncated"`. Visually: the top of the
# image is intact; missing pixels at the bottom render black or garbage,
# which is far more useful than no JPG at all.
ImageFile.LOAD_TRUNCATED_IMAGES = True


class ConvertFailed(Exception):
    """Raised when a conversion step fails (data-quality issue)."""


class ToolNotFound(Exception):
    """Raised when an external binary (ffmpeg, ffprobe) isn't on PATH."""


JPG_QUALITY: int = 95

# Windows-playable H.264 profile names per spec/migrate.md → Windows
# playability check. ffprobe reports `profile` as a human-readable
# string; these are the values we accept for `-c copy`.
_WINDOWS_PLAYABLE_H264_PROFILES: frozenset[str] = frozenset(
    {"Constrained Baseline", "Baseline", "Main", "High"}
)

# 8-bit 4:2:0 pixel formats Windows' stock H.264 decoder handles. Both
# entries are 4:2:0 8-bit; `yuvj420p` is just the full-range (JPEG
# range, 0-255) flavor of `yuv420p` (limited range, 16-235) — the
# chroma subsampling and bit depth are identical, so it plays fine.
# Older consumer cameras/phones tag their H.264 as yuvj420p; treating
# it as non-playable needlessly re-encodes huge volumes of footage.
# The genuinely-unplayable formats (yuv422p/yuvj422p, yuv444p, 10-bit)
# are deliberately absent.
_WINDOWS_PLAYABLE_H264_PIX_FMTS: frozenset[str] = frozenset(
    {"yuv420p", "yuvj420p"}
)

# Subprocess timeouts per spec/implementation.md → Subprocess hardening.
_FFPROBE_TIMEOUT: float = 30.0       # codec probe; small read, generous margin
_FFMPEG_REMUX_TIMEOUT: float = 300.0  # 5 min for `-c copy` (cheap; long for safety)
_FFMPEG_REENCODE_TIMEOUT: float = 3600.0  # 1 hour for libx264 re-encode
_PILLOW_TIMEOUT: float = 60.0         # Pillow JPG decode + encode

# Parallel-probe pool size — ffprobe is dominated by process startup,
# so concurrency helps. Matches the `metadata.CACHE_LOOKUP_WORKERS`
# size used for the hash-cache scan.
_PROBE_WORKERS: int = 32


@dataclass(frozen=True)
class VideoProfile:
    """Codec / profile / pixel format triple from `ffprobe`.

    All fields are lowercased except `profile`, which preserves
    ffprobe's casing (e.g. `Main`, `High 4:2:2`) because the H.264
    playability check compares to canonical profile names.
    """

    codec: str
    profile: str
    pix_fmt: str


def is_windows_playable(profile: VideoProfile) -> bool:
    """Return True iff the stream plays in stock Windows H.264/HEVC decoders.

    See spec/migrate.md → Windows playability check. H.264 must be
    Baseline/Main/High (4:2:0) at 8-bit. HEVC is accepted unconditionally
    — the user opts in via the Windows HEVC Video Extension.
    """
    if profile.codec == "h264":
        if profile.profile not in _WINDOWS_PLAYABLE_H264_PROFILES:
            return False
        if profile.pix_fmt not in _WINDOWS_PLAYABLE_H264_PIX_FMTS:
            return False
        return True
    if profile.codec == "hevc":
        return True
    return False


def convert_to_jpg(src: Path, dst: Path) -> None:
    """Decode `src` and re-encode as JPEG at `dst`. No metadata copied.

    Conversion fails (raises `ConvertFailed`) if Pillow can't open the
    source, can't decode it, or can't write the destination. Raises
    `OperationTimeout` if Pillow takes longer than `_PILLOW_TIMEOUT`
    seconds (typically pathological — see spec/implementation.md).
    """
    def _do() -> None:
        with Image.open(src) as img:
            # JPEG doesn't support alpha or paletted/RGBA modes — coerce to RGB.
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(dst, format="JPEG", quality=JPG_QUALITY)

    try:
        run_with_timeout("Pillow JPG encode", _PILLOW_TIMEOUT, _do)
    except OperationTimeout:
        raise  # halt the run; caller treats as fatal
    except Exception as e:
        raise ConvertFailed(
            f"Pillow failed to convert {src} to JPEG: {e}"
        ) from e


def convert_to_mp4(src: Path, dst: Path) -> None:
    """Convert `src` to MP4 at `dst` via ffmpeg.

    Re-muxes (`-c copy`) when the source meets the Windows-playable
    criteria (see spec/migrate.md → Windows playability check).
    Otherwise re-encodes with libx264 Main + yuv420p + AAC, the
    universally playable lowest-common-denominator. Container-level
    metadata copied via `-map_metadata 0`; pix:* fields are written
    separately by the apply layer.
    """
    ffmpeg = _require_tool("ffmpeg")
    profile = probe_video_profile(src)

    if is_windows_playable(profile):
        cmd: list[str] = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",  # `dst` is in staging; overwriting is fine.
            "-i",
            str(src),
            "-c",
            "copy",
            "-map_metadata",
            "0",
            "-movflags",
            "+faststart",
            str(dst),
        ]
        timeout = _FFMPEG_REMUX_TIMEOUT
    else:
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-c:v",
            "libx264",
            "-profile:v",
            "main",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-map_metadata",
            "0",
            "-movflags",
            "+faststart",
            str(dst),
        ]
        timeout = _FFMPEG_REENCODE_TIMEOUT

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise OperationTimeout(
            f"ffmpeg timed out after {timeout:.0f}s converting {src} to MP4 "
            f"(codec={profile.codec!r}, profile={profile.profile!r}, "
            f"pix_fmt={profile.pix_fmt!r})"
        ) from e
    if proc.returncode != 0:
        raise ConvertFailed(
            f"ffmpeg failed converting {src} to MP4 "
            f"(codec={profile.codec!r}, profile={profile.profile!r}, "
            f"pix_fmt={profile.pix_fmt!r}, exit={proc.returncode}):\n"
            f"{proc.stderr}"
        )


# Video containers a damaged file can be salvaged into via `-c copy`.
# Used by migrate's repair-on-tag-failure path; output keeps the source
# extension so codecs stay compatible with their original container.
_REMUXABLE_VIDEO_EXTS: frozenset[str] = frozenset(
    {"mp4", "mov", "m4v", "mkv", "avi", "wmv", "webm", "3gp"}
)


def is_remuxable_video(path: Path) -> bool:
    """True if `path`'s extension is a video container we can remux-repair."""
    return path.suffix.lower().lstrip(".") in _REMUXABLE_VIDEO_EXTS


def remux_repair(src: Path, dst: Path) -> None:
    """Remux `src` into a freshly written container at `dst` via `ffmpeg
    -c copy` — the salvage path for a structurally damaged video.

    A truncated `mdat` / unknown trailer can make ExifTool refuse to write
    tags. Copying the streams into a clean container recovers the playable
    portion and drops the broken trailer, yielding a file that accepts
    XMP. Lossless (no re-encode); container metadata carried via
    `-map_metadata 0`. `dst` should share `src`'s extension so the
    original codecs stay valid in the rewritten container.

    Raises `ConvertFailed` if ffmpeg errors or produces no output (the
    file is too damaged to salvage), `OperationTimeout` on timeout.
    """
    ffmpeg = _require_tool("ffmpeg")
    cmd: list[str] = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-c",
        "copy",
        "-map_metadata",
        "0",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=_FFMPEG_REMUX_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise OperationTimeout(
            f"ffmpeg timed out after {_FFMPEG_REMUX_TIMEOUT:.0f}s "
            f"remux-repairing {src}"
        ) from e
    if (
        proc.returncode != 0
        or not dst.exists()
        or dst.stat().st_size == 0
    ):
        raise ConvertFailed(
            f"ffmpeg remux-repair failed for {src} "
            f"(exit={proc.returncode}):\n{proc.stderr}"
        )


def probe_video_profile(src: Path) -> VideoProfile:
    """Return the first video stream's codec, profile, and pixel format.

    Single ffprobe call with multi-field output. The earlier
    single-field `default=nw=1:nk=1` form trimmed cleanly; the
    multi-field form returns one value per line in field order, so we
    split on newlines and lowercase codec / pix_fmt while preserving
    profile casing for the playability check (which compares to
    canonical names like `Main`, `High 4:2:2`).
    """
    ffprobe = _require_tool("ffprobe")
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,profile,pix_fmt",
                "-of",
                "default=nw=1:nk=1",
                str(src),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=_FFPROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise OperationTimeout(
            f"ffprobe timed out after {_FFPROBE_TIMEOUT:.0f}s on {src}"
        ) from e
    if proc.returncode != 0:
        raise ConvertFailed(
            f"ffprobe failed on {src} (exit {proc.returncode}):\n{proc.stderr}"
        )
    parts = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    # ffprobe emits empty/missing values as "unknown" or just blanks; we
    # want a populated triple regardless so the playability check has
    # something to compare. Pad missing tail values with "" so a stream
    # that lacks profile or pix_fmt info still produces a VideoProfile
    # (it'll just fail the playability check, routing to re-encode).
    while len(parts) < 3:
        parts.append("")
    codec, profile, pix_fmt = parts[0], parts[1], parts[2]
    return VideoProfile(
        codec=codec.lower().split(",", 1)[0],
        profile=profile,
        pix_fmt=pix_fmt.lower(),
    )


def probe_videos_parallel(
    paths: list[Path],
    on_batch: Callable[[int], None] | None = None,
    batch_size: int = 1000,
    max_workers: int = _PROBE_WORKERS,
) -> dict[Path, VideoProfile | None]:
    """Probe a batch of video files in a thread pool.

    Per-file failure (ffprobe error, corrupt file, missing video stream)
    yields `None` for that path rather than raising — the caller treats
    `None` as "can't determine playability, schedule a re-encode to be
    safe."

    Mirrors the `read_all_cached_hashes` pattern: 32-worker thread pool,
    consumer-thread `on_batch` callback for progress, results collected
    in submission order via `executor.map`.
    """
    if not paths:
        return {}

    result: dict[Path, VideoProfile | None] = {}
    in_batch = 0

    def probe_one(p: Path) -> tuple[Path, VideoProfile | None]:
        try:
            return p, probe_video_profile(p)
        except (ConvertFailed, OperationTimeout, ToolNotFound):
            return p, None

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for path, profile in executor.map(probe_one, paths):
            result[path] = profile
            in_batch += 1
            if in_batch >= batch_size:
                if on_batch is not None:
                    on_batch(in_batch)
                in_batch = 0

    if in_batch > 0 and on_batch is not None:
        on_batch(in_batch)
    return result


def _require_tool(name: str) -> str:
    exe = shutil.which(name) or shutil.which(f"{name}.exe")
    if exe is None:
        raise ToolNotFound(
            f"{name} not found on PATH. Install ffmpeg "
            f"(https://ffmpeg.org/) and place it on PATH."
        )
    return exe
