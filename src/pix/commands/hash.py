"""Implementation of `pix hash <library-root>`.

See spec/hash.md. Populates the per-file content-hash cache at
`<library>/.pix/cache/.../<filename>.hash` for every file missing or
stale. Sequential v1; per-file failures are non-blocking (log and
continue); whole-run summary exits non-zero if any failed.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import IO

import typer

from pix import banner
from pix.content_hash import compute_content_hash
from pix.duration import (
    format_duration,
    format_duration_compact,
    format_duration_precise,
    format_size,
)
from pix.editor import prompt_proceed
from pix.hash_cache import read_cached_hash, write_cached_hash
from pix.library_lock import LockHeld, acquire as acquire_lock
from pix.progress import LiveProgress
from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.scan import walk_source_files
from pix.schema import SCHEMA_VERSION, SchemaTooNew, SchemaUpgradeRequired
from pix.telemetry import LineRecord, write_summary
from pix.timeout import OperationTimeout, run_with_timeout

# Hash compute timeout per spec/implementation.md → Subprocess hardening.
# Even a 10 GB MP4 hashes in seconds (BLAKE3 ~3 GB/s); 60s = pathological.
_HASH_TIMEOUT: float = 60.0


def hash_library(path: Path) -> None:
    """End-to-end hash: resolve root, discover stale files, prompt, hash."""
    user_path = str(path)
    path = path.resolve()
    try:
        root = resolve_root(start=path)
    except SchemaUpgradeRequired as e:
        banner()
        typer.echo(f"{e} Run `pix upgrade {user_path}`", err=True)
        raise typer.Exit(code=1) from e
    except (NoLibraryRoot, SchemaTooNew) as e:
        banner()
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    banner(schema_version=SCHEMA_VERSION)

    try:
        with acquire_lock(root, "hash"):
            _run_hash(root)
    except LockHeld as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e


def _run_hash(root: Path) -> None:
    """Inner workflow, called under the library lock."""
    run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    runs_dir = root / ".pix" / "runs" / run_id
    runs_dir.mkdir(parents=True)
    plan_log_path = runs_dir / "plan.log"
    apply_log_path = runs_dir / "apply.log"

    _plog(plan_log_path, f"Library root: {root}")

    # Walk the library.
    with LiveProgress() as walk_progress:
        t0 = time.monotonic()
        walk_progress.begin("Walking library...")
        library_files = walk_source_files(root)
        _plog(
            plan_log_path,
            f"Found {len(library_files)} file(s) in "
            f"{format_duration_precise(time.monotonic() - t0)}.",
        )

    if not library_files:
        typer.echo("Library is empty; nothing to hash.")
        return

    # Discovery: which files need (re)hashing?
    needs_hashing: list[Path] = []
    with LiveProgress(total=len(library_files)) as scan_progress:
        scan_progress.begin("hash-scan")
        for fp in library_files:
            scan_progress.begin("hash-scan", str(fp))
            if read_cached_hash(root, fp) is None:
                needs_hashing.append(fp)
            scan_progress.advance()

    _plog(
        plan_log_path,
        f"{len(needs_hashing)} file(s) need hashing "
        f"({len(library_files) - len(needs_hashing)} cache hit(s)).",
    )

    if not needs_hashing:
        typer.echo("0 files need hashing.")
        return

    typer.echo(f"{len(needs_hashing)} file(s) need hashing.")
    typer.echo("")
    if not prompt_proceed():
        typer.echo("Aborted.")
        return

    # Apply: hash + write cache entry, one file at a time.
    completed = 0
    failures: list[tuple[Path, str]] = []
    records: list[LineRecord] = []
    t_apply = time.monotonic()
    with (
        apply_log_path.open("a", encoding="utf-8") as log,
        LiveProgress(total=len(needs_hashing)) as progress,
    ):
        for i, fp in enumerate(needs_hashing, start=1):
            line_id = f"L{i:03d}"
            rel = _rel_or_abs(fp, root)
            progress.begin(f"{line_id} HASH", str(fp))
            size_bytes: int | None = None
            try:
                size_bytes = fp.stat().st_size
            except OSError:
                pass
            t_start = time.monotonic()
            _log(log, line_id, "Started", rel, size_bytes=size_bytes)
            try:
                st = fp.stat()
                hash_hex = run_with_timeout(
                    f"hash {fp.name}",
                    _HASH_TIMEOUT,
                    compute_content_hash,
                    fp,
                )
                write_cached_hash(
                    root,
                    fp,
                    hash_hex=hash_hex,
                    size=st.st_size,
                    mtime_ns=st.st_mtime_ns,
                )
            except KeyboardInterrupt:
                _log(
                    log,
                    line_id,
                    "Interrupted",
                    rel,
                    dur_seconds=time.monotonic() - t_start,
                )
                raise
            except OperationTimeout as e:
                # Halt the run so the user can investigate why this file
                # wedged. Re-raised as typer.Exit by the outer handler.
                dur = time.monotonic() - t_start
                _log(
                    log,
                    line_id,
                    "Failed",
                    rel,
                    detail=str(e),
                    dur_seconds=dur,
                )
                records.append(
                    LineRecord(
                        line_id=line_id,
                        action="HASH",
                        duration_seconds=dur,
                        rel_path=rel,
                        size_bytes=size_bytes,
                        failed=True,
                    )
                )
                write_summary(log, records)
                typer.echo(f"\nError: {e}", err=True)
                raise typer.Exit(code=1) from e
            except Exception as e:
                dur = time.monotonic() - t_start
                _log(
                    log,
                    line_id,
                    "Failed",
                    rel,
                    detail=str(e),
                    dur_seconds=dur,
                )
                failures.append((fp, str(e)))
                records.append(
                    LineRecord(
                        line_id=line_id,
                        action="HASH",
                        duration_seconds=dur,
                        rel_path=rel,
                        size_bytes=size_bytes,
                        failed=True,
                    )
                )
                progress.advance()
                continue
            dur = time.monotonic() - t_start
            _log(log, line_id, "Completed", rel, dur_seconds=dur)
            records.append(
                LineRecord(
                    line_id=line_id,
                    action="HASH",
                    duration_seconds=dur,
                    rel_path=rel,
                    size_bytes=size_bytes,
                )
            )
            progress.advance()
            completed += 1

        write_summary(log, records)

    duration = format_duration(time.monotonic() - t_apply)
    typer.echo("")
    if failures:
        typer.echo(
            f"Hashed {completed} file(s); {len(failures)} failed — "
            f"see {apply_log_path}.",
            err=True,
        )
        for fp, err in failures:
            typer.echo(f"  {fp}", err=True)
            typer.echo(f"    {err}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Hashed {completed} file(s) in {duration}.")
    typer.echo(f"Log: {apply_log_path}")


def _rel_or_abs(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _log(
    log: IO[str],
    line_id: str,
    state: str,
    rel_path: str,
    detail: str | None = None,
    *,
    dur_seconds: float | None = None,
    size_bytes: int | None = None,
) -> None:
    """Append one transition line to apply.log and flush."""
    ts = datetime.now().isoformat(timespec="milliseconds")
    extras: list[str] = []
    if dur_seconds is not None:
        extras.append(f"dur={format_duration_compact(dur_seconds)}")
    if size_bytes is not None:
        extras.append(f"size={format_size(size_bytes)}")
    extras_str = f"  [{' '.join(extras)}]" if extras else ""
    detail_str = f": {detail}" if detail else ""
    log.write(
        f"{ts} {line_id} {state:<9} {'HASH':<18}  "
        f"{rel_path}{extras_str}{detail_str}\n"
    )
    log.flush()


def _plog(plan_log_path: Path, msg: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    with plan_log_path.open("a", encoding="utf-8") as f:
        f.write(f"{ts} {msg}\n")
