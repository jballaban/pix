"""Live one-line progress display for migrate's long phases.

Two modes:

- **Determinate** (`total=N`): `NNN% - LABEL path (Xphase / Yiter)`.
  Caller calls `begin(label, path)` to set the current item then
  `advance()` to bump the percent. The trailing parens always show
  phase-total elapsed; the per-iteration elapsed is appended after
  `/` only when it's worth surfacing (≥1s), so fast iterations
  collapse to just `(Xphase)`. Used by plan-gen and apply where the
  total iteration count is known up front.
- **Indeterminate** (`total=None`): `LABEL (Xs)`. No percent, just a
  ticking elapsed-time counter. Used for phases where the underlying
  work gives no progress feedback (the bulk ExifTool read, the
  source walk). Caller just calls `begin(label)` and lets the
  background thread tick the elapsed counter once per second.

Single rewriting line via `\\r` either way. Line is clipped to the
terminal width so a long path can't wrap and break the `\\r` rewrite.
When successive lines have different lengths, the trailing chars from
the previous line are overwritten with spaces so no artifact is left
behind.

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

from pix.duration import format_duration


def _truncate_path(path: str, max_chars: int) -> str:
    """Trim `path` from the left to fit in `max_chars`, ellipsis-prefixed.

    Snaps the cut to the nearest path separator (`\\` or `/`) so the
    result starts at a clean directory boundary — `…\\Subdir\\foo.jpg`
    rather than `…dir\\foo.jpg`. If no separator is found in the kept
    tail (a really long bare filename), falls back to a raw character
    cut so the budget is always respected.

    The point is to keep the progress line within the terminal width
    *without losing the trailing duration suffix*. Truncating the
    whole line from the right (the previous behavior) ate the
    `(Xphase / Yiter)` block, leaving the user with no temporal
    signal on the slowest cases.
    """
    if max_chars <= 0:
        return ""
    if len(path) <= max_chars:
        return path
    if max_chars == 1:
        return "…"
    tail = path[-(max_chars - 1):]
    for i, ch in enumerate(tail):
        if ch in "\\/":
            return "…" + tail[i:]
    return "…" + tail


class LiveProgress:
    """Context-managed live progress line.

    Determinate usage (known total):
        with LiveProgress(total=len(items)) as progress:
            for item in items:
                progress.begin(label, str(item.path))
                do_work(item)
                progress.advance()

    Indeterminate usage (just an elapsed-time ticker):
        with LiveProgress() as progress:
            progress.begin("Walking source folder...")
            walk_source()
            progress.begin("Reading metadata from N files...")
            bulk_read()

    `begin` resets the per-action elapsed timer and updates the visible
    label (and optionally path). `advance` bumps the completed count
    (which drives `NNN%`); irrelevant in indeterminate mode. On clean
    exit, determinate mode wraps to `100%`; indeterminate mode just
    drops a newline and leaves whatever the last rendered label was.
    """

    def __init__(
        self,
        total: int | None = None,
        stream: IO[str] | None = None,
    ) -> None:
        self._total = total
        self._stream = stream or sys.stdout
        # Determinate with total < 1 is meaningless — disable.
        if total is not None and total < 1:
            self._enabled = False
        else:
            self._enabled = self._stream.isatty()
        self._idx = 0
        self._label: str = ""
        self._path: str = ""
        now = time.monotonic()
        # `_phase_start` is set once at construction and never resets, so
        # the trailing `(Xphase)` ticker keeps climbing across every
        # iteration. `_action_start` resets per `begin()` call so the
        # per-iteration `(Yiter)` reflects just the current item.
        self._phase_start: float = now
        self._action_start: float = now
        # Number of `begin()` calls so far. Used to decide whether the
        # per-iter elapsed is distinct from the phase elapsed: with a
        # single begin() (e.g. the cache-load phase) iter ≈ phase, so
        # showing both as `(2s / 2s)` is just noise. From the second
        # begin() onward they diverge, and `(2m14s / 3s)` is useful.
        self._begin_count: int = 0
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
        # Determinate mode: wrap to 100% on clean exit, even if the
        # caller's iteration count drifted from `total`. Indeterminate
        # mode: leave the line at whatever the last label was.
        if (
            exc is None
            and self._total is not None
            and self._total > 0
        ):
            with self._lock:
                self._idx = self._total
            self._render()
        self._stream.write("\n")
        self._stream.flush()

    def begin(self, label: str, path: str = "") -> None:
        """Mark the start of a new item; resets the per-action timer.

        `label` is the human-readable descriptor that follows `NNN% - `,
        e.g. `L042 RENAME+TAG` (apply) or `planning` (plan-gen).
        `path` is optional — omit for phases where per-file paths
        flicker too fast to read (plan-gen iterates in sub-ms).
        """
        with self._lock:
            self._begin_count += 1
            self._label = label
            self._path = path
            self._action_start = time.monotonic()
        self._render()

    def advance(self, by: int = 1) -> None:
        """Mark the completion of `by` items (default 1)."""
        with self._lock:
            self._idx += by
        self._render()

    def set_label(self, label: str) -> None:
        """Update the label without resetting the elapsed-time counter.

        Useful when a long phase wants to show finer-grained status
        (e.g., a batch counter) without re-starting its `Xs` timer.
        """
        with self._lock:
            self._label = label
        self._render()

    def reset_timer(self) -> None:
        """Reset the elapsed-time counter to zero without touching the
        label or progress count.

        Used between sub-actions of a single phase — e.g. ExifTool
        batches — so each batch's `Xs` reflects only that batch's
        runtime, the same way a long-running CONVERT in apply shows
        its own per-action elapsed.
        """
        with self._lock:
            self._action_start = time.monotonic()
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
            now = time.monotonic()
            iter_elapsed = now - self._action_start
            phase_elapsed = now - self._phase_start
            if self._total is None:
                # Indeterminate — each begin() resets the timer, so the
                # tick here IS the per-action elapsed (which == phase
                # elapsed for single-begin uses, the common case).
                # Hidden under 1s to match the determinate per-iter rule.
                suffix = (
                    f" ({format_duration(iter_elapsed)})"
                    if iter_elapsed >= 1
                    else ""
                )
                head = self._label
            else:
                # Determinate — phase elapsed is always shown so the
                # user has a constant temporal anchor even when each
                # iteration is sub-second; per-iter is appended after
                # `/` only when it's worth surfacing AND distinct from
                # phase (>=2 begin() calls means iter is for the
                # current item, not the whole phase).
                pct = int(self._idx * 100 / self._total)
                show_iter = iter_elapsed >= 1 and self._begin_count > 1
                if show_iter:
                    suffix = (
                        f" ({format_duration(phase_elapsed)}"
                        f" / {format_duration(iter_elapsed)})"
                    )
                else:
                    suffix = f" ({format_duration(phase_elapsed)})"
                head = f"{pct:03d}% - {self._label}"

            # Clip to terminal width minus one (avoid wrapping into a
            # second row — `\r` only resets the cursor on the current
            # row, so a wrap leaves the upper row stranded). Long paths
            # are trimmed from the left with `_truncate_path` so the
            # duration suffix is preserved.
            cols = shutil.get_terminal_size((80, 24)).columns
            max_len = max(20, cols - 1)
            if self._path:
                # head + " " + path + suffix
                overhead = len(head) + 1 + len(suffix)
                path_budget = max_len - overhead
                if path_budget <= 0:
                    line = head + suffix
                else:
                    line = f"{head} {_truncate_path(self._path, path_budget)}{suffix}"
            else:
                line = head + suffix
            if len(line) > max_len:
                # Falls through only when head+suffix alone won't fit
                # (pathological terminal width or a wildly long label).
                line = line[: max_len - 1] + "…"
            pad = max(0, self._last_line_len - len(line))
            self._last_line_len = len(line)
            self._stream.write("\r" + line + (" " * pad))
            self._stream.flush()
