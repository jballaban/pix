"""Live one-line progress display for migrate's long phases.

Format: `NN% - LABEL path (Xs)` rewritten in place via `\\r` once per
second. A background thread re-renders on a 1s tick so the elapsed
counter advances during a single long action (a multi-GB MP4 convert
can take minutes).

The line is clipped to the terminal width so a long path can't wrap
onto a second row and break the `\\r` rewrite. When successive lines
have different lengths, the trailing chars from the previous line are
overwritten with spaces so no artifact is left behind.

Auto-disabled when stdout isn't a TTY — pytest captures and CI logs
get nothing rather than a flapping `\\r` mess.
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
from types import TracebackType
from typing import IO


class LiveProgress:
    """Context-managed live progress line.

    Usage:
        with LiveProgress(total=len(items)) as progress:
            for item in items:
                progress.begin(label, str(item.path))
                do_work(item)
                progress.advance()

    `begin` resets the per-action elapsed timer and updates the visible
    label/path. `advance` bumps the completed count (which drives
    `NN%`). On clean exit the display is forced to `100%`.
    """

    def __init__(
        self,
        total: int,
        stream: IO[str] | None = None,
    ) -> None:
        self._total = total
        self._stream = stream or sys.stdout
        self._enabled = self._stream.isatty() and total > 0
        self._idx = 0
        self._label: str = ""
        self._path: str = ""
        self._action_start: float = time.monotonic()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_line_len = 0

    def __enter__(self) -> "LiveProgress":
        if self._enabled:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._enabled:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        # Wrap to 100% on clean exit, even if the caller's iteration
        # count drifted from `total`.
        if exc is None and self._total > 0:
            with self._lock:
                self._idx = self._total
            self._render()
        self._stream.write("\n")
        self._stream.flush()

    def begin(self, label: str, path: str) -> None:
        """Mark the start of a new item; resets the per-action timer.

        `label` is the human-readable descriptor that follows `NN% - `,
        e.g. `L042 RENAME+TAG` (apply) or `planning` (plan-gen).
        """
        with self._lock:
            self._label = label
            self._path = path
            self._action_start = time.monotonic()
        self._render()

    def advance(self) -> None:
        """Mark the completion of the current item."""
        with self._lock:
            self._idx += 1
        self._render()

    def _loop(self) -> None:
        while not self._stop_event.wait(1.0):
            self._render()

    def _render(self) -> None:
        if not self._enabled:
            return
        with self._lock:
            if not self._label:
                return
            pct = (
                int(self._idx * 100 / self._total)
                if self._total > 0
                else 100
            )
            elapsed = int(time.monotonic() - self._action_start)
            # Most per-file actions finish in well under a second; only
            # surface the elapsed counter once it's worth showing. The
            # 1s thread tick keeps it updated for long-running actions.
            suffix = f" ({elapsed}s)" if elapsed >= 1 else ""
            line = f"{pct:02d}% - {self._label} {self._path}{suffix}"
            # Clip to terminal width minus one (avoid wrapping into a
            # second row — `\r` only resets the cursor on the current
            # row, so a wrap leaves the upper row stranded).
            cols = shutil.get_terminal_size((80, 24)).columns
            max_len = max(20, cols - 1)
            if len(line) > max_len:
                line = line[: max_len - 1] + "…"
            pad = max(0, self._last_line_len - len(line))
            self._last_line_len = len(line)
            self._stream.write("\r" + line + (" " * pad))
            self._stream.flush()
