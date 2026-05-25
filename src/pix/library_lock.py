"""Library-wide lock for write-mode pix operations.

See spec/README.md → Concurrency. One sentinel file at
`<library>/.pix/lock` enforces single-active-op semantics. Contents:

    12345
    migrate
    2026-05-23T15:32:01

Acquired by migrate, organize, dedupe, hash. Released on clean exit
(including via exception, via context-manager `finally`). On hard
crash the file is left on disk; the next pix invocation detects the
dead PID (or recycled-to-non-pix PID) and stale-cleans it.
"""

from __future__ import annotations

import contextlib
import os
from datetime import datetime
from pathlib import Path
from typing import Iterator

import psutil
import typer


class LockHeld(Exception):
    """Raised when another live pix process holds the library lock."""

    def __init__(self, pid: int, op: str, started_at: str) -> None:
        super().__init__(
            f"another pix process is running: PID {pid}, op '{op}', "
            f"started {started_at}. Wait or kill it before retrying."
        )
        self.pid = pid
        self.op = op
        self.started_at = started_at


def _lock_path(library_root: Path) -> Path:
    return library_root / ".pix" / "lock"


def _is_pix_process(pid: int) -> bool:
    """True iff `pid` is alive AND its executable is a `pix` invocation.

    Process name comparison is case-insensitive and strips `.exe`, so
    Windows `pix.exe` and POSIX `pix` both match. Any psutil error
    (NoSuchProcess, AccessDenied, ZombieProcess) is treated as "not a
    live pix" — the lock will be stale-cleaned.
    """
    try:
        proc = psutil.Process(pid)
        name = proc.name() or ""
    except (psutil.Error, OSError):
        return False
    return name.lower().split(".")[0] == "pix"


def _read_lock(lock_file: Path) -> tuple[int, str, str] | None:
    """Parse the lock file. Returns None on any read/parse error.

    Treating a malformed lock as stale (vs. erroring out) is the safer
    direction — a corrupted lock file shouldn't prevent recovery.
    """
    try:
        text = lock_file.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.strip().splitlines()
    if len(lines) < 3:
        return None
    try:
        pid = int(lines[0].strip())
    except ValueError:
        return None
    return pid, lines[1].strip(), lines[2].strip()


@contextlib.contextmanager
def acquire(library_root: Path, op: str) -> Iterator[None]:
    """Acquire the library-wide lock; release on exit.

    Raises `LockHeld` if a live pix process already holds the lock.
    Stale-cleans (with a stderr notice) if the holder is dead.

    Release is best-effort on exit — if the unlink fails for any
    reason, the next invocation will detect the dead PID and stale-clean.
    """
    lock_file = _lock_path(library_root)
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    # Bounded retry to handle the (rare) race where two pix processes
    # both try to stale-clean and re-acquire simultaneously.
    fd: int | None = None
    for _attempt in range(3):
        try:
            fd = os.open(lock_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            break
        except FileExistsError:
            existing = _read_lock(lock_file)
            if existing is not None and _is_pix_process(existing[0]):
                raise LockHeld(*existing)
            stale_pid = existing[0] if existing else "?"
            typer.echo(
                f"cleaning stale lock from PID {stale_pid}", err=True
            )
            try:
                lock_file.unlink()
            except OSError:
                pass  # next attempt will see it and retry
    if fd is None:
        raise RuntimeError(
            "could not acquire library lock after repeated stale-clean "
            "attempts; check .pix/lock manually"
        )

    payload = (
        f"{os.getpid()}\n{op}\n"
        f"{datetime.now().isoformat(timespec='seconds')}\n"
    )
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)

    try:
        yield
    finally:
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass
