"""Timeout wrapper for synchronous in-process operations.

Used for Pillow JPG encode, format-aware hash compute, and filesystem
rename — all pure-Python (or C-extension) calls that release the GIL
during the slow stretches and can be supervised from another thread.

A daemon thread runs the work; the caller polls with a deadline via
`Thread.join(timeout)`. On timeout the thread is left running (there's
no safe way to cancel a Python thread), and the caller raises
`OperationTimeout`. The leaked thread is harmless in practice because
every timeout in pix is "halt the run" — the process exits shortly
after the timeout fires.

Subprocess timeouts (ExifTool, ffmpeg) use their own native-timeout
mechanisms and don't go through this wrapper. See `exiftool_session`
and `convert.convert_to_mp4` respectively.
"""

from __future__ import annotations

import errno
import shutil
import threading
from pathlib import Path
from typing import Any, Callable, TypeVar


# Default timeout for any user-data filesystem rename/move. Spec puts
# this at 10s — catches AV scanners holding file locks and network-FS
# hangs without false-firing on a healthy disk.
RENAME_TIMEOUT: float = 10.0

# Ceiling for the cross-volume copy fallback in `safe_move`. A capture
# folder configured onto another drive turns the move into a copy of a
# possibly multi-GB original, so the 10s rename timeout is far too tight;
# this bounds a genuinely wedged copy without killing a legitimate one.
CROSS_VOLUME_MOVE_TIMEOUT: float = 3600.0  # 1 hour


class OperationTimeout(Exception):
    """Raised when an in-process operation exceeds its timeout.

    Deliberately not a subclass of any domain-specific exception —
    every timeout in pix halts the run so we can investigate. If a
    timeout policy needs to change to skip-and-continue for a specific
    op, the caller can catch this and convert.
    """


T = TypeVar("T")


def run_with_timeout(
    op_name: str,
    timeout: float,
    func: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Run `func(*args, **kwargs)` in a daemon thread; raise
    `OperationTimeout` if it doesn't finish within `timeout` seconds.

    Non-timeout exceptions from `func` are re-raised in the main thread,
    preserving traceback. `op_name` appears in the timeout message
    (e.g. `"Pillow JPG encode timed out after 60s"`).
    """
    result: list[T] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(func(*args, **kwargs))
        except BaseException as e:  # noqa: BLE001 — propagate everything
            error.append(e)

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise OperationTimeout(
            f"{op_name} timed out after {timeout:.0f}s"
        )
    if error:
        raise error[0]
    return result[0]


def safe_rename(
    src: Path, dst: Path, timeout: float = RENAME_TIMEOUT
) -> None:
    """`src.rename(dst)` with a hard timeout.

    Catches AV scanners holding file locks and network-FS hangs that
    would otherwise stall the apply loop forever. On timeout raises
    `OperationTimeout`; on any other rename failure (target exists, EACCES,
    cross-device, …) the underlying `OSError` propagates as-is so callers
    keep their existing error-handling behavior.
    """
    run_with_timeout(
        f"rename {src.name} → {dst.name}", timeout, src.rename, dst
    )


def safe_move(
    src: Path,
    dst: Path,
    timeout: float = RENAME_TIMEOUT,
    copy_timeout: float = CROSS_VOLUME_MOVE_TIMEOUT,
) -> None:
    """Move `src` to `dst`, transparently handling a cross-volume target.

    Tries a same-volume `os.rename` first (fast, atomic). If that fails
    with a cross-device error (`EXDEV` / Windows `ERROR_NOT_SAME_DEVICE`,
    winerror 17) — which is what happens when the run/capture folder is
    configured onto another drive — it falls back to `shutil.move`
    (copy + delete). The copy is crash-safe: the source is not removed
    until the copy completes. Any other `OSError` (target exists, EACCES,
    …) propagates unchanged; a wedged op still raises `OperationTimeout`.

    Used for run-folder captures, which may be relocated to another
    volume via `config.runs_dir`. Same-volume renames (markers, canonical
    finalize, organize moves) keep using `safe_rename`.
    """
    try:
        run_with_timeout(
            f"rename {src.name} → {dst.name}", timeout, src.rename, dst
        )
        return
    except OSError as e:
        cross_device = (
            e.errno == errno.EXDEV or getattr(e, "winerror", None) == 17
        )
        if not cross_device:
            raise
    # Cross-volume: copy + delete. `shutil.move` keeps the source until
    # the copy finishes, so a crash mid-copy leaves the source intact.
    run_with_timeout(
        f"cross-volume move {src.name} → {dst.name}",
        copy_timeout,
        shutil.move,
        str(src),
        str(dst),
    )
