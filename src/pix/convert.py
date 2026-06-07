"""Format conversion for `pix migrate` CONVERT actions.

Two conversions are supported, mirroring the extension-policy actions:
- `convert_to_jpg` — Pillow + pillow-heif handles the pixel decode/encode.
- `convert_to_mp4` — ffmpeg subprocess losslessly remuxes the container
  (`-c copy`); pix never re-encodes video.

Metadata carry-over is **not** handled here. Per the cross-cutting
invariant in spec/README.md, CONVERT must preserve all source metadata;
that's done by the apply layer with a single ExifTool `-tagsFromFile`
call after the pixel/container conversion completes, so this module
focuses on the pixel/audio/video bytes only.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

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

# AAC bitrate for the audio-only fallback in `convert_to_mp4` (see there).
_AAC_BITRATE: str = "192k"

# Subprocess timeouts per spec/implementation.md → Subprocess hardening.
_FFMPEG_REMUX_TIMEOUT: float = 300.0  # 5 min for `-c copy` (cheap; long for safety)
_PILLOW_TIMEOUT: float = 60.0         # Pillow JPG decode + encode


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
    """Losslessly remux `src` into MP4 at `dst` via `ffmpeg -c copy`.

    pix never re-encodes video (see spec/migrate.md → Video handling): a
    CONVERT for a video only normalizes the *container* to MP4, rewrapping
    the existing audio/video bitstreams verbatim. This preserves codec,
    quality, the rotation matrix, and side data — and is fast. Already-`.mp4`
    sources don't reach here (they're `keep`); this handles `.mov`/`.avi`/
    `.mts`/`.mpg`/`.mpeg`/`.vob`. Container-level metadata is carried via
    `-map_metadata 0`; pix:* fields are written separately by the apply layer.

    Two-stage, video always copied:
    1. `-c copy` — copy both streams. The ideal: fully lossless.
    2. On failure, `-c:v copy -c:a aac` — some containers' audio codecs (e.g.
       PCM in an old AVI) can't be muxed into MP4 with `-c copy`. The **video
       bitstream is still copied verbatim** (the part we care about); only the
       audio is re-encoded to AAC — which is exactly what the prior transcode
       paths did for audio anyway, so it's no regression. A source whose
       *video* codec MP4 can't carry still fails here → the caller quarantines.
    """
    ffmpeg = _require_tool("ffmpeg")
    base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    tail = ["-map_metadata", "0", "-movflags", "+faststart", str(dst)]
    copy_all = [*base, "-c", "copy", *tail]
    copy_video_aac_audio = [
        *base, "-c:v", "copy", "-c:a", "aac", "-b:a", _AAC_BITRATE, *tail
    ]

    def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
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
                f"remuxing {src} to MP4"
            ) from e

    proc = _run(copy_all)
    if proc.returncode == 0:
        return
    # Fall back to copying video while re-encoding incompatible audio.
    fallback = _run(copy_video_aac_audio)
    if fallback.returncode != 0:
        raise ConvertFailed(
            f"ffmpeg failed remuxing {src} to MP4 "
            f"(exit={fallback.returncode}); video stream is not MP4-muxable.\n"
            f"-c copy stderr:\n{proc.stderr}\n"
            f"-c:v copy -c:a aac stderr:\n{fallback.stderr}"
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


# Keep-policy image extensions that reach a TAG write (and so can hit a
# tag-write failure). A re-encode via `convert_to_jpg` salvages them —
# Pillow decodes by content (so a PNG mislabeled `.jpg`, or a JPEG with a
# proprietary trailer ExifTool won't rewrite, still decodes) into a clean
# JPEG that accepts tags. (.png/.bmp/.heic are `convert_to_jpg` policy, so
# they go through CONVERT and never reach a keep TAG.)
_REENCODABLE_IMAGE_EXTS: frozenset[str] = frozenset({"jpg", "jpeg"})


def is_reencodable_image(path: Path) -> bool:
    """True if `path` is a keep-policy image we can re-encode to repair."""
    return path.suffix.lower().lstrip(".") in _REENCODABLE_IMAGE_EXTS


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


def _require_tool(name: str) -> str:
    exe = shutil.which(name) or shutil.which(f"{name}.exe")
    if exe is None:
        raise ToolNotFound(
            f"{name} not found on PATH. Install ffmpeg "
            f"(https://ffmpeg.org/) and place it on PATH."
        )
    return exe
