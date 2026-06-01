"""Apply loop for `pix migrate`.

Per spec/migrate.md → Workflow step 7: process plan lines sequentially.
Each destructive operation captures the data it replaces into the run
folder before destroying anything.

`plan.txt` is immutable once written — apply reads it but never mutates
it. Progress streams to a sibling `apply.log` opened in append mode: one
line per state transition (`Started` / `Completed` / `Failed`). A crash
truncates the log to whatever was flushed; the tail of missing lines is
the work that didn't finish.

Plan lines are topologically sorted before execution so that a rename
that vacates a slot another rename wants always runs first. plan.txt
itself stays in path order (the user's editing view); only the execution
order is reshuffled. Apply.log's L-id sequence reflects the execution
order, which may not be numerically ascending.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import IO

from pix.convert import (
    ConvertFailed,
    convert_to_jpg,
    convert_to_mp4,
    is_reencodable_image,
    is_remuxable_video,
    remux_repair,
)
from pix.errors import move_to_errors
from pix.timeout import OperationTimeout, safe_move, safe_rename
from pix.exiftool_session import (
    ExifToolSession,
    ExifToolTimeout,
    TagWriteFailed,
)
from pix.duration import format_duration_compact, format_size
from pix.metadata_cache import PerFileCache
from pix.plan import NAME_PRESERVING_KEEP, Action, Plan, PlanLine
from pix.progress import LiveProgress
from pix.stash import stash_file
from pix.telemetry import LineRecord, write_summary


class ApplyError(Exception):
    """Raised when an action fails mid-apply. Apply halts on this."""


# Actions whose plan line claims a `target_path` and whose `abs_path`
# becomes free at end-of-line. Topo sort orders these so a vacate
# always runs before any claim of the vacated slot.
_RENAME_ACTIONS: frozenset[Action] = frozenset(
    {Action.RENAME, Action.RENAME_TAG, Action.CONVERT_RENAME_TAG}
)


# Number of CONVERT encodes to run concurrently. The apply loop is
# otherwise serial (single ExifTool session, append-only crash log,
# topo-ordered renames); only the CPU-bound encode is parallelized.
# Rationale: a single libx265 encode extracts limited frame/WPP
# parallelism and tops out around a third of a many-core CPU, so
# encoding one file at a time leaves the rest of the box idle. Three
# concurrent encodes fill a 16-core machine to ~90% while leaving
# headroom for the main thread's ExifTool/rename finalize work and the
# OS. See spec/migrate.md → Apply.
_CONVERT_WORKERS: int = 3

# How many encodes to keep queued ahead of the consumer, beyond the
# worker count, so the pool never starves while the main thread finalizes
# a line. Bounds staging-dir occupancy to ~(_CONVERT_WORKERS + this) files.
_CONVERT_LOOKAHEAD: int = 3


def _encode_staging(ln: PlanLine) -> None:
    """Produce the converted file at `ln.staging_path` — the CPU-bound half
    of a CONVERT line (step 1 of `_apply_convert`).

    Pure, off-library work: reads the source, writes the staging file,
    touches no shared state (no ExifTool, no rename slots, no apply.log),
    so it's safe to run in a worker thread. Each line's staging path is
    keyed by `line_id`, so concurrent encodes never collide. Raises the
    same `ConvertFailed` / `OperationTimeout` / `ApplyError` the inline
    encode would; the apply loop sees them when it awaits the result.
    """
    assert ln.staging_path is not None and ln.target_path is not None
    if ln.staging_path.exists():
        ln.staging_path.unlink()
    target_ext = ln.target_path.suffix.lstrip(".").lower()
    if target_ext == "jpg":
        convert_to_jpg(ln.abs_path, ln.staging_path)
    elif target_ext == "mp4":
        convert_to_mp4(ln.abs_path, ln.staging_path)
    else:
        raise ApplyError(
            f"{ln.line_id}: unsupported CONVERT target extension {target_ext!r}"
        )


class _StagingPrefetcher:
    """Runs CONVERT encodes in a worker pool, ahead of the serial apply loop.

    The apply loop stays sequential, but instead of encoding each CONVERT
    line inline it pulls an already-encoded staging file from this pool, so
    several encodes run concurrently while the main thread does the cheap
    ExifTool + rename finalize for the previous line. Only the encode is
    parallel; everything with shared state (the ExifTool session, apply.log,
    rename slots) stays on the main thread, so crash-safety and rename
    ordering are unchanged.

    Encodes are submitted in apply order and bounded to a sliding window of
    `workers + _CONVERT_LOOKAHEAD` outstanding lines, so staging-dir disk
    use stays bounded even if a line stalls. `take(ln)` blocks until that
    line's staging file is ready and re-raises any encode error, leaving the
    loop's existing per-line failure handling intact.
    """

    def __init__(self, lines: list[PlanLine], workers: int) -> None:
        self._lines = lines
        self._index = {ln.line_id: i for i, ln in enumerate(lines)}
        self._executor = ThreadPoolExecutor(max_workers=workers)
        self._futures: dict[str, Future[None]] = {}
        self._submitted = 0
        self._window = workers + _CONVERT_LOOKAHEAD
        self._lock = threading.Lock()
        with self._lock:
            self._submit_through(self._window)

    def _submit_through(self, upto: int) -> None:
        """Submit encodes up to (but not including) index `upto`. Caller
        holds `self._lock`. Monotonic — never resubmits an earlier line."""
        while self._submitted < min(upto, len(self._lines)):
            ln = self._lines[self._submitted]
            self._futures[ln.line_id] = self._executor.submit(
                _encode_staging, ln
            )
            self._submitted += 1

    def take(self, ln: PlanLine) -> bool:
        """Await `ln`'s prefetched encode.

        Returns True if a staging file is now ready at `ln.staging_path`;
        False if this line was never prefetched (e.g. a repair re-run, whose
        source differs from what was prefetched), so the caller should encode
        inline. Re-raises encode errors (ConvertFailed / OperationTimeout /
        ApplyError) so the apply loop handles them exactly as for an inline
        encode. Advances the submission window as a side effect.
        """
        fut = self._futures.pop(ln.line_id, None)
        if fut is None:
            return False
        try:
            fut.result()
            return True
        finally:
            with self._lock:
                self._submit_through(
                    self._index[ln.line_id] + 1 + self._window
                )

    def shutdown(self) -> None:
        """Drop queued encodes and stop accepting work. In-flight encodes
        finish on their own (a worker thread can't safely cancel a running
        ffmpeg/Pillow call); on a normal completion the loop has already
        consumed every line, so nothing is in flight."""
        self._executor.shutdown(wait=False, cancel_futures=True)


def _quarantine_line(
    ln: PlanLine,
    run_dir: Path,
    log: IO[str],
    error: str,
    dur: float,
    size_bytes: int | None,
    convert_failures: list[tuple[PlanLine, str]],
    records: list[LineRecord],
    library_root: Path | None = None,
) -> None:
    """Log the failure, move the file into `.pix/errors/`, and record it.

    Shared by the CONVERT-failed and (unrepairable) TAG-write-failed paths.
    Raises `ApplyError` only if the move itself fails (an environment
    problem) — consistent with the rename-failure halt policy.

    `library_root` must be passed when the run folder may be relocated off
    the library volume (`config.runs_dir`) — otherwise the errors tree
    would be derived relative to the run folder's drive, not the library's.
    Falls back to deriving from `run_dir` (runs/<id> → runs → .pix → root)
    for the default in-library layout.
    """
    _log(log, ln, "Failed", detail=error, dur_seconds=dur)
    if library_root is None:
        library_root = run_dir.parent.parent.parent
    run_id = run_dir.name
    try:
        dest = move_to_errors(
            source=ln.abs_path,
            library_root=library_root,
            run_id=run_id,
            line_id=ln.line_id,
            error=error,
        )
    except Exception as move_err:
        _log(
            log, ln, "Failed",
            detail=f"move to .pix/errors/ failed: {move_err}",
        )
        raise ApplyError(
            f"{ln.line_id} ({ln.rel_path}): action failed and move to "
            f".pix/errors/ also failed: {move_err}"
        ) from move_err
    # The errors tree mirrors the source path, so the sublocation carries
    # the provenance, not just the bare filename.
    try:
        rel_dest = dest.relative_to(library_root / ".pix")
    except ValueError:
        rel_dest = Path(dest.name)
    _log(log, ln, "Quarantined", detail=str(rel_dest).replace("\\", "/"))
    convert_failures.append((ln, error))
    records.append(
        LineRecord(
            line_id=ln.line_id,
            action=ln.action.value,
            duration_seconds=dur,
            rel_path=ln.rel_path,
            size_bytes=size_bytes,
            failed=True,
        )
    )


def _swap_in_repaired(
    ln: PlanLine, run_dir: Path, repaired: Path, suffix: str
) -> None:
    """Conserve the original to `runs/<id>/data/<line>_<name>.<suffix>`,
    then move the salvaged file into the original's place. The capture
    happens first, so the swap is rollback-safe."""
    src = ln.abs_path
    data_dir = run_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    captured = data_dir / f"{ln.line_id}_{src.name}.{suffix}"
    safe_move(src, captured)  # capture → runs (may be on another volume)
    safe_rename(repaired, src)  # staging → source, same volume


def _repair_video_container(
    ln: PlanLine, run_dir: Path, staging_dir: Path
) -> bool:
    """Salvage a damaged video by remuxing it into a clean container.

    Returns True if a clean, taggable file now sits at `ln.abs_path` (the
    caller re-runs the line); False if the file isn't a remuxable video or
    ffmpeg couldn't salvage it (the caller quarantines). Lossless. The
    damaged original is conserved to `runs/<id>/data/<line>_<name>.damaged`
    before the clean file is swapped into place.
    """
    src = ln.abs_path
    if not is_remuxable_video(src):
        return False
    staging_dir.mkdir(parents=True, exist_ok=True)
    repaired = staging_dir / f"{ln.line_id}_repair{src.suffix}"
    if repaired.exists():
        repaired.unlink()
    try:
        remux_repair(src, repaired)
    except (ConvertFailed, OperationTimeout):
        # Too damaged to salvage (or ffmpeg wedged): leave the original in
        # place for the caller to quarantine.
        if repaired.exists():
            try:
                repaired.unlink()
            except OSError:
                pass
        return False
    _swap_in_repaired(ln, run_dir, repaired, "damaged")
    return True


def _repair_image(
    ln: PlanLine,
    run_dir: Path,
    staging_dir: Path,
    exiftool: ExifToolSession,
) -> bool:
    """Salvage an image ExifTool can't tag by re-encoding it to a clean
    JPEG (Pillow decodes by content, so a PNG mislabeled `.jpg` or a JPEG
    with a proprietary trailer / missing EOI still decodes), then copying
    the source's metadata across so EXIF survives the re-encode.

    Returns True if a clean, taggable file now sits at `ln.abs_path` (the
    caller re-runs the line to write pix:* tags); False if it isn't a
    re-encodable image or Pillow couldn't decode it. **Lossy** — the image
    is re-compressed and any proprietary trailer (e.g. Samsung motion
    photo) is dropped; the original is conserved to
    `runs/<id>/data/<line>_<name>.original` before the swap.
    """
    src = ln.abs_path
    if not is_reencodable_image(src):
        return False
    staging_dir.mkdir(parents=True, exist_ok=True)
    repaired = staging_dir / f"{ln.line_id}_repair.jpg"
    if repaired.exists():
        repaired.unlink()
    try:
        convert_to_jpg(src, repaired)
    except (ConvertFailed, OperationTimeout):
        if repaired.exists():
            try:
                repaired.unlink()
            except OSError:
                pass
        return False
    # Carry the source's EXIF/XMP/IPTC onto the clean JPEG (a bare
    # re-encode drops it). Reading the original works even though writing
    # it didn't. Best-effort: the file is taggable regardless, and the
    # re-run writes pix:* tags next.
    try:
        exiftool.copy_metadata_and_write_tags(
            source=src, dest=repaired, tags={}
        )
    except (ExifToolTimeout, RuntimeError):
        pass
    _swap_in_repaired(ln, run_dir, repaired, "original")
    return True


def apply_plan(
    plan: Plan,
    plan_path: Path,
    run_dir: Path,
    kept_line_ids: set[str],
    staging_dir: Path | None = None,
    meta_cache: PerFileCache | None = None,
    library_root: Path | None = None,
) -> tuple[int, list[tuple[PlanLine, str]]]:
    """Apply the plan, streaming progress to `<run_dir>/apply.log`.

    Returns `(completed, convert_failures)` where `convert_failures` is a
    list of `(plan_line, error_message)` for CONVERT lines that failed on
    the conversion step itself. Per spec/migrate.md → Failure handling,
    CONVERT failures skip-and-log (broken source files are a per-file data
    issue); TAG/RENAME/DELETE failures halt the run and raise `ApplyError`.

    When `meta_cache` is provided, CONVERT writes a fresh cache entry for
    the new file via the live ExifTool session — without this, the next
    migrate would have to re-read the converted output via ExifTool.
    Best-effort: a failed cache write doesn't fail the apply.
    """
    del plan_path  # plan.txt is immutable from apply's perspective

    runnable = [
        ln for ln in plan.lines if ln.line_id in kept_line_ids
    ]
    runnable = _order_for_apply(runnable)

    needs_exiftool = any(
        ln.action
        in (Action.TAG, Action.RENAME_TAG, Action.CONVERT_RENAME_TAG)
        for ln in runnable
    )
    convert_lines = [
        ln for ln in runnable if ln.action == Action.CONVERT_RENAME_TAG
    ]
    needs_staging = bool(convert_lines)
    # Any action other than a pure RENAME or STASH captures something
    # into `<run-dir>/data/`. STASH writes to `.pix/stash/` directly;
    # no run-folder capture.
    needs_data = any(
        ln.action not in (Action.RENAME, Action.STASH)
        for ln in runnable
    )
    if needs_data:
        (run_dir / "data").mkdir(parents=True, exist_ok=True)

    log_path = run_dir / "apply.log"
    exiftool: ExifToolSession | None = None
    prefetcher: _StagingPrefetcher | None = None
    completed = 0
    convert_failures: list[tuple[PlanLine, str]] = []
    records: list[LineRecord] = []
    with (
        log_path.open("a", encoding="utf-8") as log,
        LiveProgress(total=len(runnable)) as progress,
    ):
        try:
            if needs_exiftool:
                exiftool = ExifToolSession()
            if needs_staging:
                if staging_dir is None:
                    raise ApplyError(
                        "CONVERT actions require a staging directory but "
                        "none was provided"
                    )
                staging_dir.mkdir(parents=True, exist_ok=True)
                # Start encoding the first window of CONVERT lines now, so
                # several encodes are already in flight by the time the loop
                # reaches them. Only built when there are CONVERTs (which is
                # exactly when `needs_staging` is true).
                if convert_lines:
                    prefetcher = _StagingPrefetcher(
                        convert_lines, _CONVERT_WORKERS
                    )
            for ln in runnable:
                progress.begin(
                    f"{ln.line_id} {ln.action.value}", str(ln.abs_path)
                )
                # File size is only interesting for CONVERT (correlates
                # with encode/transcode time); for TAG/RENAME/DELETE it
                # doesn't predict duration so we skip the stat() to save
                # a syscall per line.
                size_bytes: int | None = None
                if ln.action == Action.CONVERT_RENAME_TAG:
                    try:
                        size_bytes = ln.abs_path.stat().st_size
                    except OSError:
                        pass
                t_start = time.monotonic()
                _log(log, ln, "Started", size_bytes=size_bytes)
                try:
                    _apply_one(
                        ln, run_dir, exiftool, staging_dir, meta_cache,
                        prefetcher,
                    )
                except KeyboardInterrupt:
                    _log(
                        log,
                        ln,
                        "Interrupted",
                        dur_seconds=time.monotonic() - t_start,
                    )
                    raise
                except ConvertFailed as e:
                    # Broken source that won't encode: quarantine and keep
                    # going rather than halting the whole run.
                    _quarantine_line(
                        ln, run_dir, log, str(e),
                        time.monotonic() - t_start, size_bytes,
                        convert_failures, records, library_root,
                    )
                    progress.advance()
                    continue
                except TagWriteFailed as e:
                    # A file ExifTool can't write tags to. Try to salvage it
                    # into a clean, taggable file, then re-run the line; only
                    # quarantine if the repair didn't help. Videos remux
                    # losslessly; images re-encode to a clean JPEG (lossy).
                    # For TAG/RENAME+TAG the tag write runs before the
                    # rename, so the file is still at `abs_path`.
                    # Salvage carve-out for Insta360 360 media: a remux
                    # (`ffmpeg -c copy`) or JPEG re-encode would strip the
                    # proprietary Insta360 trailer (gyro + dual-fisheye lens
                    # calibration), destroying reframe-ability — exactly the
                    # data we kept these files for. Never salvage them; a
                    # failed tag write quarantines the file untouched so the
                    # user can inspect it. (Runt/truncated .insv are the
                    # likely trigger.) See spec/migrate.md → salvage carve-out.
                    is_360 = (
                        ln.abs_path.suffix.lower().lstrip(".")
                        in NAME_PRESERVING_KEEP
                    )
                    repaired = False
                    if staging_dir is not None and not is_360:
                        if is_remuxable_video(ln.abs_path):
                            repaired = _repair_video_container(
                                ln, run_dir, staging_dir
                            )
                        elif (
                            is_reencodable_image(ln.abs_path)
                            and exiftool is not None
                        ):
                            repaired = _repair_image(
                                ln, run_dir, staging_dir, exiftool
                            )
                    if repaired:
                        _log(
                            log, ln, "Repaired",
                            detail="salvaged to a clean file, re-applying",
                            dur_seconds=time.monotonic() - t_start,
                        )
                        try:
                            # Repair re-run: the prefetched encode (if any)
                            # was made from the pre-repair source, so pass
                            # no prefetcher — `_apply_convert` re-encodes the
                            # repaired file inline.
                            _apply_one(
                                ln, run_dir, exiftool, staging_dir, meta_cache,
                                None,
                            )
                        except (ConvertFailed, TagWriteFailed) as e2:
                            _quarantine_line(
                                ln, run_dir, log, str(e2),
                                time.monotonic() - t_start, size_bytes,
                                convert_failures, records, library_root,
                            )
                            progress.advance()
                            continue
                        except Exception as e2:
                            dur = time.monotonic() - t_start
                            _log(
                                log, ln, "Failed",
                                detail=str(e2), dur_seconds=dur,
                            )
                            raise ApplyError(
                                f"{ln.line_id} ({ln.rel_path}): {e2}"
                            ) from e2
                        # Repaired and applied — fall through to the normal
                        # Completed/record path below.
                    else:
                        _quarantine_line(
                            ln, run_dir, log, str(e),
                            time.monotonic() - t_start, size_bytes,
                            convert_failures, records, library_root,
                        )
                        progress.advance()
                        continue
                except Exception as e:
                    dur = time.monotonic() - t_start
                    _log(log, ln, "Failed", detail=str(e), dur_seconds=dur)
                    raise ApplyError(
                        f"{ln.line_id} ({ln.rel_path}): {e}"
                    ) from e
                dur = time.monotonic() - t_start
                _log(log, ln, "Completed", dur_seconds=dur)
                records.append(
                    LineRecord(
                        line_id=ln.line_id,
                        action=ln.action.value,
                        duration_seconds=dur,
                        rel_path=ln.rel_path,
                        size_bytes=size_bytes,
                    )
                )
                progress.advance()
                completed += 1
        finally:
            if prefetcher is not None:
                prefetcher.shutdown()
            if exiftool is not None:
                exiftool.close()
            write_summary(log, records)

    return completed, convert_failures


def _order_for_apply(runnable: list[PlanLine]) -> list[PlanLine]:
    """Topologically order plan lines so vacates happen before claims.

    Dependency: a rename-style line L with `target_path` T depends on any
    other rename-style line M whose `abs_path` is T. M vacates the slot
    that L wants to claim, so M must run first. Within independent
    lines, original order is preserved (Kahn's with FIFO queue).

    Cycles (A → B's slot, B → A's slot) require intermediate-name
    handling that we don't have in v1; detected and raised as
    `ApplyError`. The library that triggers one is rare enough to deal
    with by editing the plan (skip one side of the swap, re-run).
    """
    n = len(runnable)
    if n == 0:
        return []

    # Map abs_path -> index of the rename-style line sourcing from it.
    # Only rename-style lines vacate their source.
    source_to_idx: dict[Path, int] = {}
    for i, ln in enumerate(runnable):
        if ln.action in _RENAME_ACTIONS:
            source_to_idx[ln.abs_path] = i

    indegree = [0] * n
    # blocks[j] = indices blocked by j (j must run before each of them).
    blocks: list[list[int]] = [[] for _ in range(n)]

    for i, ln in enumerate(runnable):
        if ln.action in _RENAME_ACTIONS and ln.target_path is not None:
            j = source_to_idx.get(ln.target_path)
            if j is not None and j != i:
                indegree[i] += 1
                blocks[j].append(i)

    queue: deque[int] = deque(
        i for i, deg in enumerate(indegree) if deg == 0
    )
    order: list[int] = []
    while queue:
        i = queue.popleft()
        order.append(i)
        for k in blocks[i]:
            indegree[k] -= 1
            if indegree[k] == 0:
                queue.append(k)

    if len(order) < n:
        cycle_indices = [i for i in range(n) if indegree[i] > 0]
        cycle_ids = [runnable[i].line_id for i in cycle_indices[:5]]
        more = (
            ""
            if len(cycle_indices) <= 5
            else f" (+{len(cycle_indices) - 5} more)"
        )
        raise ApplyError(
            f"rename cycle detected across {len(cycle_indices)} plan "
            f"line(s): {', '.join(cycle_ids)}{more}. Edit plan.txt to "
            f"skip one side of the swap and re-run."
        )

    return [runnable[i] for i in order]


def _log(
    log: IO[str],
    ln: PlanLine,
    state: str,
    detail: str | None = None,
    *,
    dur_seconds: float | None = None,
    size_bytes: int | None = None,
) -> None:
    """Append one transition line to apply.log and flush.

    Millisecond timestamp precision so sub-second actions are
    distinguishable. Optional `dur=…` / `size=…` extras land in a
    `[…]` bracket between the path and any detail/error message.
    """
    ts = datetime.now().isoformat(timespec="milliseconds")
    extras: list[str] = []
    if dur_seconds is not None:
        extras.append(f"dur={format_duration_compact(dur_seconds)}")
    if size_bytes is not None:
        extras.append(f"size={format_size(size_bytes)}")
    extras_str = f"  [{' '.join(extras)}]" if extras else ""
    detail_str = f": {detail}" if detail else ""
    log.write(
        f"{ts} {ln.line_id} {state:<9} {ln.action.value:<18}  "
        f"{ln.rel_path}{extras_str}{detail_str}\n"
    )
    log.flush()


def _apply_one(
    ln: PlanLine,
    run_dir: Path,
    exiftool: ExifToolSession | None,
    staging_dir: Path | None,
    meta_cache: PerFileCache | None,
    prefetcher: "_StagingPrefetcher | None",
) -> None:
    """Dispatch one plan line to its action handler."""
    if ln.action == Action.DELETE:
        _apply_delete(ln, run_dir)
    elif ln.action == Action.STASH:
        _apply_stash(ln)
    elif ln.action == Action.RENAME:
        _apply_rename(ln)
    elif ln.action == Action.TAG:
        assert exiftool is not None, "TAG requires an ExifTool session"
        _apply_tag(ln, run_dir, exiftool)
    elif ln.action == Action.RENAME_TAG:
        # Order matters: TAG first (writes metadata into the file at its
        # current name), then RENAME (renames the same file). This way the
        # sidecar capture in TAG sees the pre-write state, and the RENAME
        # leaves the file with both its new name and new metadata.
        assert exiftool is not None, "RENAME+TAG requires an ExifTool session"
        _apply_tag(ln, run_dir, exiftool)
        _apply_rename(ln)
    elif ln.action == Action.CONVERT_RENAME_TAG:
        assert exiftool is not None, "CONVERT requires an ExifTool session"
        assert staging_dir is not None, "CONVERT requires a staging directory"
        _apply_convert(
            ln, run_dir, exiftool, staging_dir, meta_cache, prefetcher
        )
    else:
        raise ApplyError(f"action {ln.action.value} not supported")


def _apply_delete(ln: PlanLine, run_dir: Path) -> None:
    """Move the file into the run folder (capture + remove in one move).

    Uses `safe_move` so a `runs_dir` configured onto another volume works
    (cross-volume copy+delete); same-volume stays an atomic rename."""
    if ln.capture_path is None:
        raise ApplyError(f"{ln.line_id}: DELETE missing capture_path")
    safe_move(ln.abs_path, ln.capture_path)


def _apply_stash(ln: PlanLine) -> None:
    """Move the file to its opaque stash path and write the sidecar."""
    if ln.target_path is None:
        raise ApplyError(f"{ln.line_id}: STASH missing target_path")
    stash_file(source=ln.abs_path, target_path=ln.target_path)


def _apply_rename(ln: PlanLine) -> None:
    """Rename the file to its canonical name within the same folder."""
    if ln.target_path is None:
        raise ApplyError(
            f"{ln.line_id}: RENAME missing target_path (no effective date?)"
        )
    target = ln.target_path
    src_name = ln.abs_path.name
    target_name = target.name

    if (
        src_name != target_name
        and src_name.lower() == target_name.lower()
    ):
        # Case-only rename on a case-insensitive filesystem (NTFS, HFS+,
        # APFS-default). A direct `os.rename` can silently no-op because
        # the OS sees src and dst as the same file. Two-step through an
        # intermediate name forces the case change to materialize.
        #
        # Failure semantics:
        # - Each os.rename is itself atomic, so the file bytes are never
        #   lost — at any point in time the file exists at exactly one of
        #   {src, intermediate, target}.
        # - If step B raises in-process, we attempt a best-effort rollback
        #   of step A. If rollback also fails, the file is left at the
        #   intermediate name and we error out clearly.
        # - On hard crash (process killed) between steps A and B, the file
        #   sits at the intermediate name. The next `pix migrate` run
        #   recovers it via `pix.cleanup.cleanup_rename_orphans`.
        intermediate = ln.abs_path.parent / f"{src_name}.__pixrename__"
        if intermediate.exists():
            raise ApplyError(
                f"{ln.line_id}: rename intermediate {intermediate.name} "
                f"already exists (leftover from a prior crash; "
                f"re-run migrate to recover)"
            )
        safe_rename(ln.abs_path, intermediate)  # step A
        try:
            safe_rename(intermediate, target)  # step B
        except Exception as e:
            try:
                safe_rename(intermediate, ln.abs_path)
            except Exception as rollback_err:
                raise ApplyError(
                    f"{ln.line_id}: rename failed and rollback failed; "
                    f"file is at {intermediate.name} "
                    f"(original error: {e}; rollback error: {rollback_err})"
                ) from e
            raise
        return

    if target.exists() and target.resolve() != ln.abs_path.resolve():
        raise ApplyError(
            f"{ln.line_id}: target {target.name} already exists"
        )
    safe_rename(ln.abs_path, target)


def _apply_tag(
    ln: PlanLine, run_dir: Path, exiftool: ExifToolSession
) -> None:
    """Capture prior XMP, then write the new pix:* fields in place."""
    if ln.sidecar_path is None:
        raise ApplyError(f"{ln.line_id}: TAG missing sidecar_path")

    # Capture sidecar with prior XMP contents before mutating.
    exiftool.export_xmp_sidecar(ln.abs_path, ln.sidecar_path)

    if ln.pix_writes:
        exiftool.write_tags(ln.abs_path, dict(ln.pix_writes))


def _apply_convert(
    ln: PlanLine,
    run_dir: Path,
    exiftool: ExifToolSession,
    staging_dir: Path,
    meta_cache: PerFileCache | None,
    prefetcher: "_StagingPrefetcher | None",
) -> None:
    """Execute the 4-step CONVERT sequence (spec/migrate.md → Atomicity).

    1. **Off-library work**: decode source, encode to target format in
       `.pix/staging/`. ExifTool then layers source metadata + pix:* tags
       into the converted file. If any step fails, the staging file is
       discarded; the source is untouched.
    2. **Bring into source as marker**: rename the staging file next to
       the original as `<original-name>.__migrate__.<new-ext>`.
    3. **Capture original**: move source into `runs/<run-id>/`.
    4. **Finalize**: rename the marker to its canonical name.
    5. **Refresh cache** (if `meta_cache` provided): read the new file's
       metadata via the live ExifTool session and write it to the cache.
       Without this, the next migrate would have to ExifTool-read every
       converted output. Best-effort — read or cache write failures don't
       fail the apply.

    All paths are pre-computed by plan-gen and stored on the PlanLine. The
    `run_dir` and `staging_dir` params are kept for signature uniformity
    with the other handlers but unused here.
    """
    del run_dir, staging_dir  # paths are on `ln`

    if (
        ln.staging_path is None
        or ln.marker_path is None
        or ln.capture_path is None
        or ln.target_path is None
    ):
        raise ApplyError(
            f"{ln.line_id}: CONVERT missing one or more pre-computed paths"
        )

    src = ln.abs_path

    # Step 1: off-library conversion. Normally the encode ran ahead of time
    # in the prefetch pool (so several encodes overlap on the CPU while the
    # main thread finalizes earlier lines); `take` blocks until this line's
    # staging file is ready. Falls back to an inline encode when there's no
    # prefetched result — a repair re-run, whose repaired source differs
    # from what was prefetched. ConvertFailed propagates uncaught to
    # apply_plan's loop, where it's logged and the run continues to the next
    # plan line — broken source files are a per-file data issue, not an
    # environmental failure. See spec/migrate.md → Failure handling.
    if prefetcher is None or not prefetcher.take(ln):
        _encode_staging(ln)

    exiftool.copy_metadata_and_write_tags(
        source=src, dest=ln.staging_path, tags=dict(ln.pix_writes)
    )

    # Step 2: bring into source as marker.
    if ln.marker_path.exists():
        raise ApplyError(
            f"{ln.line_id}: marker {ln.marker_path.name} already exists "
            f"(leftover from a prior crash?)"
        )
    safe_rename(ln.staging_path, ln.marker_path)

    # Step 3: capture original into the run folder. safe_move so a
    # relocated runs_dir on another volume works (copy+delete); the
    # source survives until the copy completes, keeping the step crash-safe.
    safe_move(src, ln.capture_path)

    # Step 4: finalize marker to canonical name.
    if ln.target_path.exists():
        raise ApplyError(
            f"{ln.line_id}: target {ln.target_path.name} already exists "
            f"at finalize step"
        )
    safe_rename(ln.marker_path, ln.target_path)

    # Step 5: refresh cache for the new file. Without this, the next
    # migrate sees a file with no cache entry and re-reads it via
    # ExifTool. Best-effort: read/parse/write failures fall through
    # silently — same trust model as the rest of the cache layer.
    if meta_cache is not None:
        try:
            fresh_raw = exiftool.read_metadata(ln.target_path)
        except (ExifToolTimeout, RuntimeError):
            fresh_raw = None
        if fresh_raw is not None:
            meta_cache.add(ln.target_path, fresh_raw)
