"""Long-running ExifTool subprocess (`-stay_open True`).

Spawns one ExifTool process at the start of apply, sends commands via
stdin, and reads framed responses (each response terminated by a `{ready}`
sentinel). Avoids the ~200ms-per-spawn overhead at TB scale.

A daemon **reader thread** drains stdout (and a second drains stderr) onto
queues so the main thread can poll with a short timeout. Two payoffs:

- **Per-call timeout**: `execute(..., timeout=N)` kills the subprocess and
  raises `ExifToolTimeout` if `{ready}` doesn't arrive within `N` seconds.
  Without the reader thread we'd block in a C-level `readline()` that
  doesn't honor wall-clock budgets.
- **CTRL+C works**: the main thread sits in `queue.get(timeout=...)`,
  which is a Python-level call that the SIGINT default handler can
  interrupt. The previous direct-readline version pinned the main thread
  inside a C blocking read; SIGINT just queued until the read returned,
  which is "never" when the subprocess wedged.

Defaults per [spec/implementation.md → Subprocess hardening](../../spec/implementation.md#subprocess-hardening):
30 second per-`execute` timeout. Halt-on-timeout (the wrapper just raises;
apply decides whether to halt the whole run).
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import IO, cast

from pix import exiftool_config_path
from pix.metadata import require_exiftool


_READY_SENTINEL: str = "{ready}\n"

# Per-`execute` timeout. 30s is the wedged-line per spec/implementation.md;
# any single metadata op should finish in well under a second.
_DEFAULT_TIMEOUT: float = 30.0

# How often the main thread wakes up to check for timeout / interrupt. The
# trade is responsiveness vs. CPU wakeup cost; 250ms keeps Ctrl-C latency
# imperceptible without busy-waiting.
_POLL_TICK: float = 0.25


class ExifToolTimeout(RuntimeError):
    """Raised when an ExifTool `-execute` call exceeds its timeout.

    The subprocess has already been killed by the time this fires; the
    session is no longer usable. Callers should treat it like any other
    apply failure (halt + log).
    """


class ExifToolSession:
    """Wraps one long-running ExifTool subprocess.

    Lifecycle: create (spawns subprocess + reader threads) → multiple
    execute() calls → close() (cleanly shuts the subprocess down).
    Use as a context manager to guarantee shutdown.
    """

    def __init__(self, exe: str | None = None) -> None:
        self._exe: str = exe or require_exiftool()
        self._proc: subprocess.Popen[str] = subprocess.Popen(
            [
                self._exe,
                "-config",
                str(exiftool_config_path()),
                "-stay_open",
                "True",
                "-@",
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        # mypy/pyright can't infer that Popen with PIPE has non-None streams
        if (
            self._proc.stdin is None
            or self._proc.stdout is None
            or self._proc.stderr is None
        ):
            raise RuntimeError("Failed to attach pipes to exiftool subprocess")
        self._stdin: IO[str] = self._proc.stdin
        self._stdout: IO[str] = self._proc.stdout
        self._stderr: IO[str] = self._proc.stderr

        # stdout drained to a queue so the main thread can poll with a short
        # timeout. stderr drained to a separate queue (only inspected on
        # failure, but draining prevents the OS pipe buffer from filling up
        # and blocking the subprocess).
        self._stdout_q: queue.Queue[str | None] = queue.Queue()
        self._stderr_q: queue.Queue[str | None] = queue.Queue()
        self._stdout_thread = threading.Thread(
            target=self._drain, args=(self._stdout, self._stdout_q), daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._drain, args=(self._stderr, self._stderr_q), daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()
        self._killed: bool = False

    @staticmethod
    def _drain(pipe: IO[str], q: queue.Queue[str | None]) -> None:
        """Reader thread loop. Pushes lines until EOF, then a None sentinel."""
        try:
            for line in iter(pipe.readline, ""):
                q.put(line)
        finally:
            q.put(None)

    def execute(self, *args: str, timeout: float = _DEFAULT_TIMEOUT) -> str:
        """Send one batch of arguments to ExifTool; return its stdout output.

        Raises `ExifToolTimeout` if `{ready}` doesn't arrive within
        `timeout` seconds (default 30s). Raises `KeyboardInterrupt` if
        the user interrupts via SIGINT during the wait.
        """
        for arg in args:
            self._stdin.write(arg + "\n")
        self._stdin.write("-execute\n")
        self._stdin.flush()

        deadline = time.monotonic() + timeout
        out: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stderr_tail = self._drain_stderr()
                self._kill()
                raise ExifToolTimeout(
                    f"exiftool -execute timed out after {timeout:.0f}s"
                    + (f"; stderr: {stderr_tail}" if stderr_tail else "")
                )
            try:
                line = self._stdout_q.get(timeout=min(_POLL_TICK, remaining))
            except queue.Empty:
                # Tick. queue.get is Python-interruptible, so SIGINT
                # will raise KeyboardInterrupt during the wait, not here.
                continue
            if line is None:
                # Subprocess exited unexpectedly. Drain whatever stderr we
                # can to surface the cause.
                stderr_tail = self._drain_stderr()
                raise RuntimeError(
                    "exiftool subprocess exited unexpectedly during execute"
                    + (f"; stderr: {stderr_tail}" if stderr_tail else "")
                )
            if line == _READY_SENTINEL:
                return "".join(out)
            out.append(line)

    def write_tags(self, file: Path, tags: dict[str, str]) -> None:
        """Write `tags` to `file` in place via `-overwrite_original`."""
        if not tags:
            return
        args: list[str] = []
        for key, value in tags.items():
            # ExifTool tag-set syntax: `-<key>=<value>`
            args.append(f"-{key}={value}")
        args.append("-overwrite_original")
        args.append(str(file))
        self.execute(*args)

    def copy_metadata_and_write_tags(
        self,
        source: Path,
        dest: Path,
        tags: dict[str, str],
    ) -> None:
        """Copy all metadata from `source` to `dest` and write `tags` in one call.

        Used by the CONVERT step (pixel/container conversion happens via
        Pillow/ffmpeg without metadata; this call then layers the source's
        EXIF/XMP/IPTC into the converted file alongside the pix:* writes).
        """
        args: list[str] = [
            "-tagsFromFile",
            str(source),
            "-all:all",  # copy every readable tag from source
        ]
        for key, value in tags.items():
            args.append(f"-{key}={value}")
        args.append("-overwrite_original")
        args.append(str(dest))
        self.execute(*args)

    def read_metadata(self, file: Path) -> dict[str, object] | None:
        """Read `file`'s metadata via the live session; return the raw dict.

        Same flags as the bulk-read path (`-j -G:0 -fast2`) so the result
        is shape-compatible with the rest of the metadata pipeline.
        Returns None on parse failure or missing SourceFile — caller
        should treat that as "couldn't refresh, will rebuild next run".

        Used by apply after CONVERT so the new file's metadata lands in
        the persistent cache immediately, instead of forcing the next
        migrate to re-read it.
        """
        stdout = self.execute("-j", "-G:0", "-fast2", str(file))
        stripped = stdout.strip()
        if not stripped:
            return None
        try:
            data: object = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, list) or not data:
            return None
        first = data[0]
        if not isinstance(first, dict):
            return None
        entry = cast("dict[str, object]", first)
        if not isinstance(entry.get("SourceFile"), str):
            return None
        return entry

    def export_xmp_sidecar(
        self, file: Path, sidecar_path: Path
    ) -> None:
        """Write `file`'s metadata to a fresh XMP sidecar at `sidecar_path`."""
        # `-o <path>` creates a new file from the metadata; combined with
        # all-tag selection this captures everything ExifTool can read.
        # If the sidecar already exists, ExifTool refuses to overwrite; we
        # remove first to keep the op idempotent.
        if sidecar_path.exists():
            sidecar_path.unlink()
        self.execute("-o", str(sidecar_path), str(file))

    def close(self) -> None:
        """Shut down the ExifTool subprocess.

        Normal path: send `-stay_open\nFalse\n`, wait for clean exit. If
        we're called during exception unwinding (KeyboardInterrupt,
        ExifToolTimeout, …) stdin is in an unknown state — a half-written
        command may have left the subprocess processing garbage, and a
        further write can fail with OSError(EINVAL) on Windows. In that
        case skip the polite handshake and just kill.
        """
        if self._proc.poll() is not None:
            return
        if sys.exc_info()[0] is not None:
            # Exception is in flight. Don't try to be polite.
            self._kill()
            return
        try:
            self._stdin.write("-stay_open\nFalse\n")
            self._stdin.flush()
            self._stdin.close()
        except (BrokenPipeError, ValueError, OSError):
            # Subprocess already gone, or pipe in an inconsistent state.
            # Fall through to wait/kill.
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._kill()

    def _kill(self) -> None:
        """Force-kill the subprocess. Idempotent."""
        if self._killed:
            return
        self._killed = True
        if self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                # Best effort — the goal is to make sure the subprocess
                # isn't still running; if kill failed it's probably
                # already gone.
                pass
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    def _drain_stderr(self, max_lines: int = 20) -> str:
        """Pull whatever's currently buffered on stderr (for error messages)."""
        out: list[str] = []
        while len(out) < max_lines:
            try:
                line = self._stderr_q.get_nowait()
            except queue.Empty:
                break
            if line is None:
                break
            out.append(line)
        return "".join(out).strip()

    def __enter__(self) -> "ExifToolSession":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # On normal exit, close() does the polite shutdown. On exception
        # (including KeyboardInterrupt and ExifToolTimeout), skip the polite
        # path and just kill — stdin may be in an unknown state.
        if exc is None:
            self.close()
        else:
            self._kill()
