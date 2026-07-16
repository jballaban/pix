"""Implementation of `pix tag rotate <degrees> <paths>` — lossless video rotation.

Adds `<degrees>` of **clockwise** display rotation to each video by rewriting
only the container's rotation matrix (`ffmpeg -c copy` — no re-encode, no
quality loss), then re-applying the `pix:*` XMP that the remux drops and
re-stamping the content-invariant cache (`.hash`). Folders expand to
the videos inside them; non-video paths are skipped.

Orientation only — no date/event change, so `pix organize` isn't needed.
Reversible: rotate by the complement (e.g. 90 then 270) to undo. The pixel
stream is copied verbatim, so repeated rotations never degrade quality.

`-display_rotation V` stores rotation V, which a player applies as V degrees
*counter-clockwise*; so for a clockwise request `d` on a file currently at
`cur`, the new stored value is `normalize(cur - d)`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import typer

from pix import banner, exiftool_config_path
from pix.checkout import CheckoutOpen, ensure_no_open_checkout
from pix.config import Config, settings_path
from pix.editor import prompt_proceed
from pix.events import PIX_ORIGINAL_PATH
from pix.hash_cache import read_cached_hash, write_cached_hash
from pix.library_lock import LockHeld, acquire as acquire_lock
from pix.markers import ROTATE_INFIX
from pix.metadata import (
    ExifToolFailed,
    ExifToolNotFound,
    read_metadata_batched,
)
from pix import cache_db
from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.scan import walk_source_files

_VALID_DEGREES = (90, 180, 270)
# Container formats we can losslessly re-tag rotation on.
_ROTATABLE_EXT = {"mp4", "m4v", "mov"}
_REMUX_TIMEOUT = 1800.0
_PROBE_TIMEOUT = 60.0
_EXIFTOOL_TIMEOUT = 300.0


def _fail(msg: str) -> None:
    typer.echo(f"Error: {msg}", err=True)
    raise typer.Exit(code=1)


def _is_rotatable(path: Path) -> bool:
    return path.suffix.lower().lstrip(".") in _ROTATABLE_EXT


def _expand_videos(raw: list[Path]) -> list[Path]:
    """Files pass through if rotatable; folders expand to their rotatable
    videos (skipping `.pix/`). Order preserved, first occurrence wins."""
    out: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        if p not in seen and _is_rotatable(p):
            seen.add(p)
            out.append(p)

    for p in raw:
        if p.is_dir():
            for fp, _s, _m in walk_source_files(p):
                _add(fp)
        else:
            _add(p)
    return out


def _stored_rotation(src: Path, ffprobe: str) -> int:
    """Current stored display rotation in degrees (0 if none)."""
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream_side_data=rotation:stream_tags=rotate",
             "-of", "default=nw=1:nk=1", str(src)],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
        ).stdout or ""
    except (subprocess.SubprocessError, OSError):
        return 0
    for line in out.splitlines():
        try:
            d = int(round(float(line.strip())))
        except ValueError:
            continue
        if d % 360 != 0:
            return d
    return 0


def _normalize(deg: int) -> int:
    """Map degrees into (-180, 180]."""
    return ((deg + 180) % 360) - 180


def _cached_original(root: Path, src: Path) -> str | None:
    row = cache_db.get(root, src)
    if row is None or row.meta is None:
        return None
    val = row.meta.get(PIX_ORIGINAL_PATH)
    return val if isinstance(val, str) else None


def rotate_videos(
    degrees: int, paths: list[Path], no_prompt: bool = False
) -> None:
    """Rotate the given videos `degrees` clockwise, losslessly, in place."""
    banner()

    if degrees not in _VALID_DEGREES:
        _fail(f"degrees must be one of {_VALID_DEGREES}, got {degrees}.")
    if not paths:
        _fail("no files given.")
    raw = [p.resolve() for p in paths]
    bad = [p for p in raw if not p.is_file() and not p.is_dir()]
    if bad:
        _fail(f"not a file or folder: {bad[0]}")

    try:
        root = resolve_root(start=raw[0])
    except NoLibraryRoot as e:
        _fail(str(e))
        return
    outside = [p for p in raw if p != root and root not in p.parents]
    if outside:
        _fail(f"{outside[0]} is not inside the library at {root}.")

    try:
        ensure_no_open_checkout(root)
    except CheckoutOpen as e:
        _fail(str(e))
        return

    Config.load(settings_path(root))  # validate settings
    videos = _expand_videos(raw)
    if not videos:
        _fail("no rotatable video files found in the given paths.")

    typer.echo(f"Rotate {len(videos)} video(s) by {degrees}° clockwise (lossless).")
    if not no_prompt and not prompt_proceed():
        typer.echo("Aborted; no changes made.")
        return

    try:
        with acquire_lock(root, "rotate"):
            _apply(root, videos, degrees)
    except LockHeld as e:
        _fail(str(e))


def _apply(root: Path, videos: list[Path], degrees: int) -> None:
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    exiftool = shutil.which("exiftool") or "exiftool"
    cfg = str(exiftool_config_path())

    fixed = 0
    failed: list[Path] = []
    for src in videos:
        new_rot = _normalize(_stored_rotation(src, ffprobe) - degrees)
        tmp = src.with_name(f"{src.stem}{ROTATE_INFIX}{src.suffix}")
        try:
            pre_hash = read_cached_hash(root, src)
            orig_op = _cached_original(root, src)

            r = subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error",
                 "-display_rotation", str(new_rot), "-i", str(src),
                 "-c", "copy", "-map_metadata", "0",
                 "-movflags", "+faststart", str(tmp)],
                capture_output=True, text=True, timeout=_REMUX_TIMEOUT,
            )
            if r.returncode != 0 or not tmp.is_file():
                raise RuntimeError((r.stderr or "ffmpeg failed").strip()[:160])

            subprocess.run(
                [exiftool, "-config", cfg, "-tagsFromFile", str(src),
                 "-xmp:all", "-overwrite_original", str(tmp)],
                capture_output=True, timeout=_EXIFTOOL_TIMEOUT,
            )

            if _stored_rotation(tmp, ffprobe) != new_rot:
                raise RuntimeError("rotation not applied")
            if orig_op is not None:
                meta = read_metadata_batched([tmp]).get(tmp)
                if (meta.get_str(PIX_ORIGINAL_PATH) if meta else None) != orig_op:
                    raise RuntimeError("pix:OriginalPath not preserved")

            os.replace(str(tmp), str(src))
            st = src.stat()
            if pre_hash is not None:
                write_cached_hash(
                    root, src, hash_hex=pre_hash,
                    size=st.st_size, mtime_ns=st.st_mtime_ns,
                )
            fixed += 1
        except (ExifToolNotFound, ExifToolFailed) as e:
            _fail(f"exiftool error: {e}")
        except Exception as e:  # noqa: BLE001 - per-file, keep going
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            failed.append(src)
            typer.echo(f"  skipped {src.name}: {e}", err=True)

    typer.echo("")
    typer.echo(f"Rotated {fixed} video(s).")
    if failed:
        typer.echo(f"{len(failed)} could not be rotated (left unchanged).", err=True)
