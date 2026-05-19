"""Apply loop for `pix migrate`.

Per spec/migrate.md → Workflow step 7: process plan lines sequentially.
Each destructive operation captures the data it replaces into the run
folder before destroying anything. Plan.txt is updated in place with
`[time Started]` / `[time Completed]` annotations as each line progresses.

Phase 3 scope: handles RENAME, DELETE, TAG, RENAME+TAG. CONVERT(+RENAME+TAG)
lines are detected up-front and reported as skipped (Phase 4 lands the
conversion implementations).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer

from pix.exiftool_session import ExifToolSession
from pix.plan import Action, Plan, PlanLine


class ApplyError(Exception):
    """Raised when an action fails mid-apply. Apply halts on this."""


def apply_plan(
    plan: Plan,
    plan_path: Path,
    run_dir: Path,
    kept_line_ids: set[str],
) -> tuple[int, int]:
    """Apply the plan, updating plan.txt in place.

    Returns `(completed, skipped)`. Raises `ApplyError` if any action fails.
    """
    selected = [
        ln for ln in plan.lines if ln.line_id in kept_line_ids
    ]

    convert_lines = [
        ln for ln in selected if ln.action == Action.CONVERT_RENAME_TAG
    ]
    runnable = [
        ln for ln in selected if ln.action != Action.CONVERT_RENAME_TAG
    ]

    if convert_lines:
        typer.echo(
            f"Warning: {len(convert_lines)} CONVERT line(s) skipped — "
            "format conversion lands in Phase 4. The following files will "
            "remain unchanged:"
        )
        for ln in convert_lines:
            typer.echo(f"  {ln.line_id} {ln.rel_path}")

    annotations: dict[str, str] = {}
    for ln in convert_lines:
        annotations[ln.line_id] = "[skipped: CONVERT not yet implemented]"
    if annotations:
        # Persist the skip annotations even if there are no runnable lines.
        _write_plan(plan, plan_path, annotations)

    needs_exiftool = any(
        ln.action in (Action.TAG, Action.RENAME_TAG) for ln in runnable
    )
    exiftool: ExifToolSession | None = None
    try:
        if needs_exiftool:
            exiftool = ExifToolSession()
        completed = 0
        for ln in runnable:
            annotations[ln.line_id] = _stamp("Started")
            _write_plan(plan, plan_path, annotations)
            try:
                _apply_one(ln, run_dir, exiftool)
            except Exception as e:
                annotations[ln.line_id] = _stamp(f"Failed: {e}")
                _write_plan(plan, plan_path, annotations)
                raise ApplyError(
                    f"{ln.line_id} ({ln.rel_path}): {e}"
                ) from e
            annotations[ln.line_id] = _stamp("Completed")
            _write_plan(plan, plan_path, annotations)
            completed += 1
    finally:
        if exiftool is not None:
            exiftool.close()

    return completed, len(convert_lines)


def _stamp(state: str) -> str:
    return f"[{datetime.now().strftime('%H:%M:%S')} {state}]"


def _write_plan(
    plan: Plan, plan_path: Path, annotations: dict[str, str]
) -> None:
    plan_path.write_text(plan.to_text(annotations), encoding="utf-8")


def _apply_one(
    ln: PlanLine, run_dir: Path, exiftool: ExifToolSession | None
) -> None:
    """Dispatch one plan line to its action handler."""
    if ln.action == Action.DELETE:
        _apply_delete(ln, run_dir)
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
    else:
        # CONVERT cases are filtered out before we get here.
        raise ApplyError(f"action {ln.action.value} not supported in this phase")


def _apply_delete(ln: PlanLine, run_dir: Path) -> None:
    """Move the file into the run folder. Single atomic rename = capture+remove."""
    capture_path = run_dir / f"{ln.line_id}_{ln.abs_path.name}"
    ln.abs_path.rename(capture_path)


def _apply_rename(ln: PlanLine) -> None:
    """Rename the file to its canonical name within the same folder."""
    if ln.target_filename is None:
        raise ApplyError(
            f"{ln.line_id}: RENAME requires a target_filename but none "
            f"was computed (no effective date?)"
        )
    target = ln.abs_path.parent / ln.target_filename
    src_name = ln.abs_path.name

    if (
        src_name != ln.target_filename
        and src_name.lower() == ln.target_filename.lower()
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
    # Capture sidecar with prior XMP contents before mutating.
    sidecar = run_dir / f"{ln.line_id}_{ln.abs_path.name}.xmp"
    exiftool.export_xmp_sidecar(ln.abs_path, sidecar)

    writes = dict(ln.pix_writes)
    if ln.needs_original_path:
        # Stored as the source path at the time of first migrate. We use
        # the absolute path before any rename in this same line.
        writes["XMP:OriginalPath"] = str(ln.abs_path)
    # `needs_content_hash` is honored in Phase 5 (blake3 + format-aware
    # framing). Phase 3 deliberately skips it; the next migrate will
    # re-propose `content_hash compute` for any file still missing the
    # hash.

    if writes:
        exiftool.write_tags(ln.abs_path, writes)
