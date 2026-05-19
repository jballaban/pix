"""Optional plan-generation debug logging for `pix migrate --debug`.

When enabled (via `pix migrate --debug`), every file the source walk
considers — whether it ends up with a plan line or not — gets a detailed
log of how its action was decided: extension policy lookup, date-candidate
trace, effective-date computation, canonical-filename derivation, first-
migrate detection, collision resolution, and final decision.

Logs are dumped to `<run-dir>/debug/<rel-path>.log` after plan generation,
before the editor opens, so the user can consult them while reviewing the
plan.

API: call `enabled()` (context manager) at the start of plan-gen,
`for_file(path)` (also a context manager) to scope subsequent `log()` /
`section()` calls to a specific file, then `dump_to(...)` at the end to
flush buffers to disk.

When `enabled()` hasn't been entered, all `log()` / `section()` calls are
silent no-ops, so it's cheap to leave them in production code paths.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator


_buffer: dict[Path, list[str]] | None = None
_current_path: Path | None = None


def is_enabled() -> bool:
    return _buffer is not None


@contextmanager
def enabled() -> Generator[None, None, None]:
    """Activate debug capture. Nested calls preserve the outer buffer."""
    global _buffer
    prior = _buffer
    _buffer = {}
    try:
        yield
    finally:
        _buffer = prior


@contextmanager
def for_file(path: Path) -> Generator[None, None, None]:
    """Bind subsequent `log()` / `section()` calls to `path`'s buffer."""
    global _current_path
    prior = _current_path
    _current_path = path.resolve()
    try:
        yield
    finally:
        _current_path = prior


def log(message: str) -> None:
    """Append `message` to the current file's debug buffer (no-op when disabled)."""
    if _buffer is None or _current_path is None:
        return
    _buffer.setdefault(_current_path, []).append(message)


def section(title: str) -> None:
    """Start a new section in the current file's debug buffer."""
    if _buffer is None or _current_path is None:
        return
    bucket = _buffer.setdefault(_current_path, [])
    if bucket:
        bucket.append("")
    bucket.append(f"--- {title} ---")


def dump_to(
    run_dir: Path, source: Path, line_id_by_path: dict[Path, str]
) -> None:
    """Write per-file logs to `<run_dir>/debug/<rel-path>.log`.

    `line_id_by_path` maps source paths to their assigned `L###` line ID
    (if any). Files without a plan line still get a log; their header
    notes `Plan line: (none — no action)`.
    """
    if _buffer is None:
        return
    debug_dir = run_dir / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    source_resolved = source.resolve()
    for path, messages in sorted(_buffer.items()):
        try:
            rel = path.relative_to(source_resolved)
        except ValueError:
            rel = Path(path.name)

        out_path = debug_dir / f"{rel}.log"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        line_id = line_id_by_path.get(path)
        header = [
            f"File: {path}",
            (
                f"Plan line: {line_id}"
                if line_id
                else "Plan line: (none — no action)"
            ),
        ]
        out_path.write_text(
            "\n".join(header + [""] + messages) + "\n",
            encoding="utf-8",
        )
