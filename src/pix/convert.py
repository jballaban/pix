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

import functools
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

# pix's canonical archival video codec. migrate normalizes every kept
# video to HEVC/H.265 (see spec/migrate.md → Canonical video codec): it
# re-muxes when the source is already HEVC and re-encodes otherwise, so
# the library converges to one efficient codec. HEVC playback needs the
# (free) Windows HEVC Video Extension — an opt-in pix assumes is present.
CANONICAL_VIDEO_CODEC: str = "hevc"

# Re-encode quality. CRF 22 on x265 is visually transparent for archival
# content (VMAF ~97-99 vs the source on representative library footage)
# while roughly halving the bloated x264-CRF18 files. 8-bit 4:2:0 (Main
# profile), `hvc1` tag so Windows/Apple players recognize the stream.
_X265_CRF: str = "22"

# GPU (NVENC) re-encode quality. cq 30 on Blackwell hevc_nvenc at preset
# p7 + multipass lands within ~1 VMAF of x265 CRF22 *at the same file size*
# on representative library footage — visually indistinguishable without
# 4x pixel-peeping (measured: matched-size NVENC mean VMAF 93.3 vs x265
# 94.5, worst frame 86.5; differences imperceptible in motion). NVENC is
# less bitrate-efficient than x265, so the win isn't per-clip quality —
# it's throughput: the GPU encodes alongside the CPU x265 workers on an
# otherwise-idle chip. See spec/migrate.md → Convert concurrency.
_NVENC_CQ: str = "30"

# Routing threshold (apply.py): clips at or above this height re-encode on
# the GPU (NVENC), where x265 is brutally slow and NVENC's efficiency gap
# shrinks; smaller clips prefer the CPU (x265), where it's both fast and
# more space-efficient. 2160 = 4K.
NVENC_MIN_HEIGHT: int = 2160

# Valid `encoder` values for `convert_to_mp4`'s re-encode branch.
ENCODER_X265: str = "x265"
ENCODER_NVENC: str = "nvenc"

# Subprocess timeouts per spec/implementation.md → Subprocess hardening.
_FFPROBE_TIMEOUT: float = 30.0       # codec probe; small read, generous margin
_FFMPEG_REMUX_TIMEOUT: float = 300.0  # 5 min for `-c copy` (cheap; long for safety)
_FFMPEG_REENCODE_TIMEOUT: float = 7200.0  # 2 hours — libx265 medium is slow on long/4K clips
_PILLOW_TIMEOUT: float = 60.0         # Pillow JPG decode + encode

# Parallel-probe pool size — ffprobe is dominated by process startup,
# so concurrency helps. Matches the `metadata.CACHE_LOOKUP_WORKERS`
# size used for the hash-cache scan.
_PROBE_WORKERS: int = 32


@dataclass(frozen=True)
class VideoProfile:
    """Codec / profile / pixel format triple from `ffprobe`.

    All fields are lowercased except `profile`, which preserves
    ffprobe's casing (e.g. `Main`, `High 4:2:2`). Only `codec` drives the
    convert decision now (HEVC = canonical); `profile`/`pix_fmt` are kept
    for diagnostics and error messages.
    """

    codec: str
    profile: str
    pix_fmt: str
    # Frame dimensions. Default 0 (unknown) — the plan-time probe and the
    # `.video` cache only carry the codec triple, so cached/plan profiles
    # report 0x0. Populated only by `probe_video_profile_with_geometry`,
    # which apply uses to route 4K clips to the GPU encoder.
    width: int = 0
    height: int = 0


def is_canonical_video_codec(profile: VideoProfile) -> bool:
    """True iff the stream is already in pix's canonical codec (HEVC).

    A canonical-codec source only needs re-muxing into MP4 (`-c copy`);
    anything else is re-encoded to HEVC. See spec/migrate.md → Canonical
    video codec.
    """
    return profile.codec == CANONICAL_VIDEO_CODEC


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


def convert_to_mp4(
    src: Path,
    dst: Path,
    *,
    encoder: str = ENCODER_X265,
    profile: VideoProfile | None = None,
) -> None:
    """Convert `src` to MP4 at `dst` via ffmpeg.

    Re-muxes (`-c copy`) when the source is already in the canonical
    codec (HEVC) — just rewrapping into MP4. Otherwise re-encodes to
    HEVC, the canonical archival format (see spec/migrate.md → Canonical
    video codec). Container-level metadata copied via `-map_metadata 0`;
    pix:* fields are written separately by the apply layer.

    `encoder` selects the re-encode codec (ignored for the re-mux path,
    which is codec-copy either way):
    - `ENCODER_X265` (default): libx265 CRF 22, `-preset medium` — the
      space-efficient CPU path.
    - `ENCODER_NVENC`: hevc_nvenc on the GPU (preset p7, multipass, cq 30,
      full CUDA decode pipeline) — faster throughput, slightly larger at
      matched quality. apply routes 4K and CPU-overflow clips here.

    `profile` lets the caller pass an already-probed `VideoProfile` (apply
    probes once for routing); when None we probe here. Both encoders emit
    8-bit 4:2:0 + `hvc1` so players recognize the stream.
    """
    ffmpeg = _require_tool("ffmpeg")
    if profile is None:
        profile = probe_video_profile(src)

    if is_canonical_video_codec(profile):
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
    elif encoder == ENCODER_NVENC:
        # Full GPU pipeline: CUDA-decode the source and keep frames on the
        # GPU for NVENC (no host round-trip). On a source NVENC's CUDA path
        # can't handle (e.g. an exotic pixel format) this raises and the
        # caller falls back to x265 — so we don't force a pix_fmt here.
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-hwaccel",
            "cuda",
            "-hwaccel_output_format",
            "cuda",
            "-i",
            str(src),
            "-c:v",
            "hevc_nvenc",
            "-preset",
            "p7",
            "-tune",
            "hq",
            "-rc",
            "vbr",
            "-cq",
            _NVENC_CQ,
            "-b_ref_mode",
            "middle",
            "-temporal-aq",
            "1",
            "-spatial-aq",
            "1",
            "-multipass",
            "fullres",
            "-tag:v",
            "hvc1",
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
            "-preset",
            "medium",
            "-crf",
            _X265_CRF,
            "-pix_fmt",
            "yuv420p",
            "-tag:v",
            "hvc1",
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


def probe_video_profile_with_geometry(src: Path) -> VideoProfile:
    """Like `probe_video_profile`, but also fills `width`/`height`.

    apply uses the dimensions to route 4K clips to the GPU encoder. Kept
    separate from `probe_video_profile` (and the `.video` cache) because
    those only need the codec triple, so this richer probe runs only at
    apply time for the lines actually being converted.

    Parses **`key=value`** output (`-of default=nw=1`), not the positional
    `nk=1` form `probe_video_profile` uses: ffprobe emits stream fields in
    the file's natural order (width/height come *before* pix_fmt), not the
    order requested, so positional parsing would misalign them. A
    missing/garbage dimension falls back to 0 (treated as "not 4K" →
    CPU/x265 route).
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
                "stream=codec_name,profile,pix_fmt,width,height",
                "-of",
                "default=nw=1",
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
    fields: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()

    def _to_int(s: str) -> int:
        try:
            return int(s)
        except ValueError:
            return 0

    return VideoProfile(
        codec=fields.get("codec_name", "").lower().split(",", 1)[0],
        profile=fields.get("profile", ""),
        pix_fmt=fields.get("pix_fmt", "").lower(),
        width=_to_int(fields.get("width", "")),
        height=_to_int(fields.get("height", "")),
    )


@functools.lru_cache(maxsize=1)
def nvenc_available() -> bool:
    """True iff this machine can encode HEVC on an NVIDIA GPU via NVENC.

    Two-stage check, cached for the process: (1) `hevc_nvenc` is listed by
    the ffmpeg build, and (2) a tiny throwaway encode actually succeeds —
    the encoder can be *listed* yet fail at runtime (no NVIDIA GPU, no
    driver, or a driver/runtime mismatch). The functional test is the only
    reliable signal, so we pay its ~1-2s cost once. Any failure → False,
    and apply runs every conversion on the CPU (x265) exactly as before —
    keeping pix portable to non-NVIDIA machines.
    """
    try:
        ffmpeg = _require_tool("ffmpeg")
    except ToolNotFound:
        return False
    try:
        listing = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=_FFPROBE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if listing.returncode != 0 or "hevc_nvenc" not in (listing.stdout or ""):
        return False
    # Functional probe: encode a 1-frame synthetic clip to null via NVENC.
    try:
        test = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.04",
                "-c:v", "hevc_nvenc", "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=_FFPROBE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return test.returncode == 0


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
