"""Always-on per-file plan-generation audit log.

Every file the source walk considers gets a detailed log of how its
action was decided: extension policy lookup, date-candidate trace,
effective-date computation, first-migrate detection, collision
resolution, and the final decision (including no-action cases).

Output streams to `<run-dir>/debug.log` as plan-gen progresses, so
memory cost is O(1) regardless of library size. The file is opened
in append mode for the duration of plan-gen and closed at the end.

API: enter `writing_to(run_dir)` once around plan-gen. Inside it,
`for_file(path)` writes a section header for the current file;
subsequent `log()` / `section()` calls go to that section. Outside
`writing_to`, all calls are silent no-ops, so tests and library
callers can leave them in place without setup.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator, IO


_stream: IO[str] | None = None
_current_path: Path | None = None


@contextmanager
def writing_to(run_dir: Path) -> Generator[None, None, None]:
    """Open `<run_dir>/debug.log` and stream debug output into it.

    Nested calls preserve the outer stream (which is what most callers
    want — only the outermost open writes the file).
    """
    global _stream
    if _stream is not None:
        # Already open from an outer context. Don't reopen.
        yield
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    _stream = (run_dir / "debug.log").open("a", encoding="utf-8")
    try:
        yield
    finally:
        _stream.close()
        _stream = None


@contextmanager
def for_file(path: Path) -> Generator[None, None, None]:
    """Bind subsequent `log()` / `section()` calls to `path`'s section."""
    global _current_path
    prior = _current_path
    _current_path = path.resolve()
    if _stream is not None:
        _stream.write(f"\n=== {_current_path} ===\n")
    try:
        yield
    finally:
        _current_path = prior


def log(message: str) -> None:
    """Write `message` to the current file's section (no-op when inactive)."""
    if _stream is None or _current_path is None:
        return
    _stream.write(f"{message}\n")


def section(title: str) -> None:
    """Start a new sub-section in the current file's section."""
    if _stream is None or _current_path is None:
        return
    _stream.write(f"\n--- {title} ---\n")
