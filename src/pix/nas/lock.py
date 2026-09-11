"""A single-writer lock for upload (spec/nas-app.md §9).

The old architecture's library lock was dropped deliberately — one long-lived app
owns the archive, so there is nothing to serialise. But `upload` is a *desktop*
command appending to a shared ledger, and two of them running at once interleave
their writes.

That is not hypothetical: an observed run produced **94 torn JSON lines and up to
three records per file** because a second upload was started while the first was
still going. `iter_entries` skips malformed lines so nothing was lost, but the
ledger is the archive's record of what it holds, and it should not depend on luck.

The lock is a file holding the owning PID. A dead PID means a crashed run, and the
lock is taken over — a stale lock must never be able to block work forever.
"""

from __future__ import annotations

import os
from pathlib import Path

LOCK_NAME: str = ".upload.lock"


class UploadLocked(Exception):
    """Another upload is already running."""


class UploadLock:
    """Context manager taking the single-writer lock for a run."""

    def __init__(self, root: Path) -> None:
        self._path = root / LOCK_NAME
        self._held = False

    def __enter__(self) -> "UploadLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            owner = self._read_owner()
            if owner is not None and _alive(owner):
                raise UploadLocked(
                    f"another upload is running (PID {owner}). Wait for it, or "
                    f"delete {self._path} if you are certain it is not."
                ) from None
            # Stale: the owning process is gone. Take it over rather than
            # letting a crash block every future run.
            self._path.unlink(missing_ok=True)
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        self._held = True
        return self

    def __exit__(self, *exc: object) -> None:
        if self._held:
            self._path.unlink(missing_ok=True)
            self._held = False

    def _read_owner(self) -> int | None:
        try:
            return int(self._path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None


def _alive(pid: int) -> bool:
    """True if `pid` is a live process."""
    try:
        import psutil

        return psutil.pid_exists(pid)
    except ImportError:                      # pragma: no cover - psutil is a dep
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
