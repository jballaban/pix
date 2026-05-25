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
from pathlib import Path

from PIL import Image
import pillow_heif  # pyright: ignore[reportMissingTypeStubs]

from pix.timeout import OperationTimeout, run_with_timeout


# Register HEIC/HEIF support with Pillow once at import time so
# `Image.open('foo.heic')` works.
pillow_heif.register_heif_opener()  # pyright: ignore[reportUnknownMemberType]


class ConvertFailed(Exception):
    """Raised when a conversion step fails (data-quality issue)."""


class ToolNotFound(Exception):
    """Raised when an external binary (ffmpeg, ffprobe) isn't on PATH."""


JPG_QUALITY: int = 95
H264_HEVC_CODECS: frozenset[str] = frozenset({"h264", "hevc"})

# Subprocess timeouts per spec/implementation.md → Subprocess hardening.
_FFPROBE_TIMEOUT: float = 30.0       # codec probe; small read, generous margin
_FFMPEG_REMUX_TIMEOUT: float = 300.0  # 5 min for `-c copy` (cheap; long for safety)
_FFMPEG_REENCODE_TIMEOUT: float = 3600.0  # 1 hour for libx265 re-encode
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
    """Convert `src` to MP4 at `dst` via ffmpeg.

    Re-muxes (`-c copy`) when the source video stream is H.264 or H.265
    (cheap, near-instant). Otherwise re-encodes with libx265 + AAC per
    spec/migrate.md. Container-level metadata copied via `-map_metadata 0`;
    pix:* fields are written separately by the apply layer.
    """
    ffmpeg = _require_tool("ffmpeg")
    codec = _probe_video_codec(src)

    if codec in H264_HEVC_CODECS:
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
            "libx265",
            "-crf",
            "23",
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
            f"(codec={codec!r})"
        ) from e
    if proc.returncode != 0:
        raise ConvertFailed(
            f"ffmpeg failed converting {src} to MP4 "
            f"(codec={codec!r}, exit={proc.returncode}):\n{proc.stderr}"
        )


def _probe_video_codec(src: Path) -> str:
    """Return the first video stream's codec name (e.g. 'h264', 'hevc')."""
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
                "stream=codec_name",
                "-of",
                "csv=p=0",
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
    return proc.stdout.strip().lower()


def _require_tool(name: str) -> str:
    exe = shutil.which(name) or shutil.which(f"{name}.exe")
    if exe is None:
        raise ToolNotFound(
            f"{name} not found on PATH. Install ffmpeg "
            f"(https://ffmpeg.org/) and place it on PATH."
        )
    return exe
