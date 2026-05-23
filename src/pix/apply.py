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

from collections import deque
from datetime import datetime
from pathlib import Path
from typing import IO

from pix.content_hash import compute_content_hash
from pix.convert import ConvertFailed, convert_to_jpg, convert_to_mp4
from pix.exiftool_session import ExifToolSession
from pix.plan import Action, Plan, PlanLine
from pix.progress import LiveProgress
from pix.stash import stash_file


class ApplyError(Exception):
    """Raised when an action fails mid-apply. Apply halts on this."""


# Actions whose plan line claims a `target_path` and whose `abs_path`
# becomes free at end-of-line. Topo sort orders these so a vacate
# always runs before any claim of the vacated slot.
_RENAME_ACTIONS: frozenset[Action] = frozenset(
    {Action.RENAME, Action.RENAME_TAG, Action.CONVERT_RENAME_TAG}
)


def apply_plan(
    plan: Plan,
    plan_path: Path,
    run_dir: Path,
    kept_line_ids: set[str],
    staging_dir: Path | None = None,
) -> tuple[int, int]:
    """Apply the plan, streaming progress to `<run_dir>/apply.log`.

    Returns `(completed, skipped)`. Raises `ApplyError` if any action fails.
    `plan_path` is unused here — kept in the signature so callers can pass
    the same paths they pass elsewhere; plan.txt is never rewritten.
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
    needs_staging = any(
        ln.action == Action.CONVERT_RENAME_TAG for ln in runnable
    )
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
    completed = 0
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
            for ln in runnable:
                progress.begin(
                    f"{ln.line_id} {ln.action.value}", str(ln.abs_path)
                )
                _log(log, ln, "Started")
                try:
                    _apply_one(ln, run_dir, exiftool, staging_dir)
                except KeyboardInterrupt:
                    _log(log, ln, "Interrupted")
                    raise
                except Exception as e:
                    _log(log, ln, "Failed", detail=str(e))
                    raise ApplyError(
                        f"{ln.line_id} ({ln.rel_path}): {e}"
                    ) from e
                _log(log, ln, "Completed")
                progress.advance()
                completed += 1
        finally:
            if exiftool is not None:
                exiftool.close()

    return completed, 0


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
) -> None:
    """Append one transition line to apply.log and flush."""
    ts = datetime.now().isoformat(timespec="seconds")
    suffix = f": {detail}" if detail else ""
    log.write(
        f"{ts} {ln.line_id} {state:<9} {ln.action.value:<18}  "
        f"{ln.rel_path}{suffix}\n"
    )
    log.flush()


def _apply_one(
    ln: PlanLine,
    run_dir: Path,
    exiftool: ExifToolSession | None,
    staging_dir: Path | None,
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
        _apply_convert(ln, run_dir, exiftool, staging_dir)
    else:
        raise ApplyError(f"action {ln.action.value} not supported")


def _apply_delete(ln: PlanLine, run_dir: Path) -> None:
    """Move the file into the run folder. Single atomic rename = capture+remove."""
    if ln.capture_path is None:
        raise ApplyError(f"{ln.line_id}: DELETE missing capture_path")
    ln.abs_path.rename(ln.capture_path)


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
        ln.abs_path.rename(intermediate)  # step A
        try:
            intermediate.rename(target)  # step B
        except Exception as e:
            try:
                intermediate.rename(ln.abs_path)
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
    ln.abs_path.rename(target)


def _apply_tag(
    ln: PlanLine, run_dir: Path, exiftool: ExifToolSession
) -> None:
    """Capture prior XMP, then write the new pix:* fields in place."""
    if ln.sidecar_path is None:
        raise ApplyError(f"{ln.line_id}: TAG missing sidecar_path")

    # Capture sidecar with prior XMP contents before mutating.
    exiftool.export_xmp_sidecar(ln.abs_path, ln.sidecar_path)

    writes = dict(ln.pix_writes)
    if ln.needs_content_hash:
        # The only value we compute here rather than at plan-gen: format-
        # aware hashing requires a full-file scan, deliberately deferred
        # to apply (per spec/migrate.md → Metadata cache) so an aborted
        # plan doesn't waste hashing time on thousands of files.
        writes["XMP:ContentHash"] = compute_content_hash(ln.abs_path)

    if writes:
        exiftool.write_tags(ln.abs_path, writes)


def _apply_convert(
    ln: PlanLine,
    run_dir: Path,
    exiftool: ExifToolSession,
    staging_dir: Path,
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
    target_ext = ln.target_path.suffix.lstrip(".").lower()

    # Step 1: off-library conversion + metadata copy + pix:* writes
    if ln.staging_path.exists():
        ln.staging_path.unlink()

    try:
        if target_ext == "jpg":
            convert_to_jpg(src, ln.staging_path)
        elif target_ext == "mp4":
            convert_to_mp4(src, ln.staging_path)
        else:
            raise ApplyError(
                f"{ln.line_id}: unsupported CONVERT target extension "
                f"{target_ext!r}"
            )
    except ConvertFailed as e:
        raise ApplyError(
            f"{ln.line_id}: conversion failed: {e}"
        ) from e

    writes = dict(ln.pix_writes)
    if ln.needs_content_hash:
        writes["XMP:ContentHash"] = compute_content_hash(ln.staging_path)
    exiftool.copy_metadata_and_write_tags(
        source=src, dest=ln.staging_path, tags=writes
    )

    # Step 2: bring into source as marker.
    if ln.marker_path.exists():
        raise ApplyError(
            f"{ln.line_id}: marker {ln.marker_path.name} already exists "
            f"(leftover from a prior crash?)"
        )
    ln.staging_path.rename(ln.marker_path)

    # Step 3: capture original into the run folder.
    src.rename(ln.capture_path)

    # Step 4: finalize marker to canonical name.
    if ln.target_path.exists():
        raise ApplyError(
            f"{ln.line_id}: target {ln.target_path.name} already exists "
            f"at finalize step"
        )
    ln.marker_path.rename(ln.target_path)
