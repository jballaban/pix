"""Single-writer locks on the share (spec/nas-app.md §9).

The old architecture's library lock was dropped deliberately — one long-lived app
owns the archive, so there is nothing to serialise. But some commands are
*desktop* commands writing into shared state, and two of them at once interleave
their writes.

That is not hypothetical: an observed `upload` run produced **94 torn JSON lines
and up to three records per file** because a second upload was started while the
first was still going. `iter_entries` skips malformed lines so nothing was lost,
but the ledger is the archive's record of what it holds, and it should not depend
on luck. `process` has the same shape — it decides what is missing, then spends
minutes making it, and a second run decides the same thing about the same files
and makes them again alongside.

**The lock lives on the share, so it is one lock for everybody.** Which is
exactly why the owner cannot be a bare process id. A PID is only meaningful on
the machine that issued it: reading `5000` off the share tells this desktop
nothing about whether the run on another one is alive, and small numbers are
reused constantly. A lock recording only a PID therefore gets *stolen* — the
second machine looks for 5000, does not find it, concludes the holder crashed,
and starts a concurrent run of the very thing the lock exists to serialise.

So the owner is **host and PID together**, and a stale lock is only ever taken
over on the host that wrote it — the one machine whose process table can answer
the question. A lock held by another machine is left alone and named, because
the honest answer there is *go and look*, not a guess dressed as a recovery.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any, cast

#: One file per activity, beside the state it protects.
UPLOAD_LOCK: str = ".upload.lock"
PROCESS_LOCK: str = ".process.lock"


class Locked(Exception):
    """Somebody else is already doing this."""


class UploadLocked(Locked):
    """Another upload is already running."""


class ShareLock:
    """Context manager taking a single-writer lock for one run.

    `what` is the activity in the words the message will use — it is read by
    somebody who has just been refused, and *another process is running* is
    worth more to them than a lock file's name.
    """

    def __init__(self, root: Path, name: str, what: str,
                 error: type[Locked] = Locked) -> None:
        self._path = root / name
        self._what = what
        self._error = error
        self._held = False

    @property
    def path(self) -> Path:
        return self._path

    def __enter__(self) -> "ShareLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            owner = self._read_owner()
            if owner is not None and not self._stale(owner):
                raise self._error(self._busy(owner)) from None
            # Either unreadable, or this machine's own crashed run. Take it
            # over rather than letting a crash block every future one.
            self._path.unlink(missing_ok=True)
            try:
                fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                # Somebody else won the race between the unlink and this.
                raise self._error(self._busy(self._read_owner())) from None
        os.write(fd, json.dumps({
            "host": socket.gethostname(), "pid": os.getpid(),
            "since": time.time(), "what": self._what,
        }).encode("utf-8"))
        os.close(fd)
        self._held = True
        return self

    def __exit__(self, *exc: object) -> None:
        if self._held:
            self._path.unlink(missing_ok=True)
            self._held = False

    def _stale(self, owner: dict[str, Any]) -> bool:
        """Whether the run that wrote this lock is definitely gone.

        Only answerable on the machine that wrote it. Elsewhere the honest
        answer is *no idea*, and a lock that might be live is a lock.
        """
        if str(owner.get("host") or "") != socket.gethostname():
            return False
        pid = owner.get("pid")
        return not isinstance(pid, int) or not _alive(pid)

    def _busy(self, owner: dict[str, Any] | None) -> str:
        if not owner:
            return (f"another {self._what} is running. Wait for it, or delete "
                    f"{self._path} if you are certain it is not.")
        host = str(owner.get("host") or "?")
        pid = owner.get("pid")
        where = ("this machine" if host == socket.gethostname()
                 else f"{host}")
        since = owner.get("since")
        ago = (f", started {(time.time() - float(since)) / 60:.0f} min ago"
               if isinstance(since, (int, float)) else "")
        return (f"another {self._what} is running on {where} (PID {pid}{ago}). "
                f"Wait for it, or delete {self._path} if you are certain it "
                f"is not.")

    def _read_owner(self) -> dict[str, Any] | None:
        try:
            raw: object = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return cast("dict[str, Any]", raw) if isinstance(raw, dict) else None


def UploadLock(root: Path) -> ShareLock:  # noqa: N802 - reads as a class
    """The upload lock, by the name its one caller already uses."""
    return ShareLock(root, UPLOAD_LOCK, "upload", UploadLocked)


def ProcessLock(root: Path) -> ShareLock:  # noqa: N802 - reads as a class
    """The lock `process` takes for a whole run.

    Held across the scan *and* the work, not just the writes: a second run
    would decide the same files are missing, spend the same minutes deriving
    them, and write them over the top. Nothing is corrupted by that — the
    derived tiers are disposable and a resize is a resize — but it is twice
    the work and twice the load on the NAS for nothing.
    """
    return ShareLock(root, PROCESS_LOCK, "process")


def _alive(pid: int) -> bool:
    """True if `pid` is a live process **on this machine**."""
    try:
        import psutil

        return psutil.pid_exists(pid)
    except ImportError:                      # pragma: no cover - psutil is a dep
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
