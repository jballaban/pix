"""Perceptual video fingerprint for re-encode-robust dedupe.

A video's *content hash* (`content_hash.hash_mp4`) is an exact digest of the
encoded `mdat` bytes, so two encodes of the same source — x265 vs NVENC, or
the same encoder across versions — hash differently and `pix dedupe`'s
exact pass misses them. This module computes an **encoder-independent**
fingerprint instead: a small set of perceptual hashes (dHash) of frames
sampled at fixed *fractional timestamps*.

Two properties make it robust where the byte hash isn't:

- **Sampled by time, not keyframe.** x265 and NVENC place keyframes
  differently; sampling at `t = fraction * duration` (input-seek) picks the
  same picture regardless of GOP structure.
- **Perceptual, on the decoded picture.** Lossy re-encodes decode to
  *slightly* different pixels; reducing each frame to a 9x8 grayscale and
  hashing adjacent-pixel gradients (dHash) discards the high-frequency
  compression artifacts that differ between encoders.

Validated on representative library footage: same-source/different-encoder
(and double-encoded) pairs score within ~24 Hamming bits across the
`len(FRAC)`-frame set, while distinct videos sit far higher; `pix dedupe`
groups within a small band (default <= 30). See spec/dedupe.md.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


# Fractional sample points down the clip. Deliberately avoids 0% / 100%
# (fade-in/out and black tails collide across *different* videos); the
# interior spread discriminates well. Changing this set invalidates every
# cached fingerprint — bump the cache suffix if it ever changes.
FRAC: tuple[float, ...] = (0.08, 0.24, 0.40, 0.56, 0.72, 0.88)

_FRAME_BITS: int = 64
FP_BITS: int = len(FRAC) * _FRAME_BITS  # total fingerprint width (384)

_FFPROBE_TIMEOUT: float = 30.0
_FFMPEG_FRAME_TIMEOUT: float = 60.0


class FingerprintFailed(Exception):
    """Raised when a video can't be probed/decoded enough to fingerprint."""


@dataclass(frozen=True)
class VideoFingerprint:
    """Perceptual fingerprint plus the geometry/duration dedupe pre-filters on.

    `frames` holds `len(FRAC)` 64-bit dHashes. A failed frame grab is stored
    as -1; `fingerprint_distance` treats any -1 as "incomparable" so a
    partially-decoded clip never matches anything.
    """

    frames: tuple[int, ...]
    width: int
    height: int
    duration: float


def _require(name: str) -> str:
    exe = shutil.which(name) or shutil.which(f"{name}.exe")
    if exe is None:
        raise FingerprintFailed(f"{name} not found on PATH")
    return exe


def _probe_geometry(path: str) -> tuple[int, int, float]:
    """Return (width, height, duration_seconds) via one ffprobe call.

    key=value output (not positional): ffprobe emits stream fields in the
    file's natural order, so we map by name. Raises `FingerprintFailed` if
    width/height/duration can't be read."""
    ffprobe = _require("ffprobe")
    try:
        proc = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-show_entries",
                "format=duration", "-of", "default=nw=1",
                path,
            ],
            capture_output=True, text=True, encoding="utf-8",
            check=False, timeout=_FFPROBE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise FingerprintFailed(f"ffprobe failed on {path}: {e}") from e
    fields: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    try:
        return int(fields["width"]), int(fields["height"]), float(fields["duration"])
    except (KeyError, ValueError) as e:
        raise FingerprintFailed(
            f"ffprobe gave no usable geometry/duration for {path}"
        ) from e


def _dhash_at(ffmpeg: str, path: str, t: float) -> int:
    """64-bit dHash of the frame at time `t`.

    Input-seek (`-ss` before `-i`) — fast and, in modern ffmpeg, accurate to
    the requested timestamp — then scale to 9x8 grayscale and emit raw bytes.
    dHash = for each of the 8 rows, 8 bits comparing each pixel to its right
    neighbor. Returns -1 if the frame couldn't be decoded (treated as
    incomparable by `fingerprint_distance`)."""
    try:
        raw = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1",
                "-vf", "scale=9:8,format=gray", "-f", "rawvideo", "-",
            ],
            capture_output=True, check=False, timeout=_FFMPEG_FRAME_TIMEOUT,
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return -1
    if len(raw) < 72:
        return -1
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if raw[base + col] > raw[base + col + 1] else 0)
    return bits


def compute_fingerprint(path: str) -> VideoFingerprint:
    """Probe geometry + sample `len(FRAC)` perceptual frame hashes.

    Raises `FingerprintFailed` only when geometry/duration is unreadable
    (the file isn't a usable video); individual frame-grab failures are
    tolerated as -1 entries so a partially-readable clip still yields a
    fingerprint that simply won't match anything."""
    ffmpeg = _require("ffmpeg")
    w, h, dur = _probe_geometry(path)
    frames = tuple(_dhash_at(ffmpeg, path, f * dur) for f in FRAC)
    return VideoFingerprint(frames=frames, width=w, height=h, duration=dur)


def fingerprint_distance(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    """Summed Hamming distance across the frame hashes (0..FP_BITS).

    Returns `FP_BITS + 1` ("never matches") if either side has a failed
    frame (-1) or the frame counts differ — a conservative non-match so a
    bad fingerprint can't trigger a deletion."""
    if len(a) != len(b):
        return FP_BITS + 1
    total = 0
    for x, y in zip(a, b):
        if x < 0 or y < 0:
            return FP_BITS + 1
        total += bin(x ^ y).count("1")
    return total
