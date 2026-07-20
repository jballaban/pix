"""Read-only media validation for `pix import` (spec/import.md → Import loop).

A **corruption tripwire, not a conformance gate**: `media_check` returns a
hard-error reason only when the landed bytes genuinely won't parse as the media
their extension claims — the signal that a transfer delivered garbage (or the
source file is itself broken). Warnings pass. Formats pix can't parse are
**exempt** (return `None`), so import keeps no format opinion on them.

It uses the **same decoders as `pix migrate`** — Pillow + pillow-heif for images
(importing `pix.convert` for its side effects: HEIF opener registered and
`LOAD_TRUNCATED_IMAGES` set), ffprobe for video. So a file that passes here is
one migrate can open, and vice versa: in particular mildly-truncated-but-
recoverable images pass (migrate accepts them via `LOAD_TRUNCATED_IMAGES`), while
unopenable ones fail.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PIL import Image

# Imported for its module-level side effects (pillow_heif.register_heif_opener()
# + ImageFile.LOAD_TRUNCATED_IMAGES), so our Pillow behavior matches migrate's.
# `_require_tool` etc. only run when called, so this import is cheap/pure.
from pix import convert  # noqa: F401  # pyright: ignore[reportUnusedImport]

_IMAGE_EXTS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif",
    ".tif", ".tiff", ".webp", ".bmp",
})
_VIDEO_EXTS: frozenset[str] = frozenset({
    ".mov", ".mp4", ".m4v", ".avi", ".mkv", ".wmv",
    ".webm", ".3gp", ".mts", ".mpg", ".mpeg",
})

_FFPROBE_TIMEOUT: float = 60.0


def media_check(path: Path) -> str | None:
    """Validate a landed media file.

    Returns `None` if the file parses cleanly *or* its format is one pix can't
    parse (exempt — verified on transfer integrity alone). Returns a short
    human-readable reason string on a **hard** parse failure.
    """
    ext = path.suffix.lower()
    if ext in _IMAGE_EXTS:
        return _check_image(path)
    if ext in _VIDEO_EXTS:
        return _check_video(path)
    return None  # unknown / non-media → exempt


def _check_image(path: Path) -> str | None:
    try:
        with Image.open(path) as img:
            img.load()  # force the full decode, not just header identification
    except Exception as e:  # noqa: BLE001 — any decode failure is a hard fail
        return f"image decode failed: {e}"
    return None


def _check_video(path: Path) -> str | None:
    ffprobe = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if ffprobe is None:
        # No validator available — a missing tool is an environment problem, not
        # a bad file. Treat as exempt rather than fail an otherwise-good import.
        return None
    cmd = [ffprobe, "-v", "error", "-show_format", "-show_streams", str(path)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            check=False, timeout=_FFPROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"ffprobe timed out after {_FFPROBE_TIMEOUT:.0f}s"
    if proc.returncode != 0:
        return f"ffprobe failed (exit={proc.returncode}): {proc.stderr.strip()[:200]}"
    if proc.stderr.strip():  # `-v error` → anything on stderr is a real error
        return f"ffprobe reported errors: {proc.stderr.strip()[:200]}"
    return None
