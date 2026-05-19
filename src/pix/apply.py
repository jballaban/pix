"""Apply loop for `pix migrate`.

Per spec/migrate.md → Workflow step 7: process plan lines sequentially.
Each destructive operation captures the data it replaces into the run
folder before destroying anything. Plan.txt is updated in place with
`[time Started]` / `[time Completed]` annotations as each line progresses.

Handles RENAME, DELETE, TAG, RENAME+TAG, and (since v0.1.8) CONVERT+
RENAME+TAG. CONVERT uses the 4-step marker sequence from
spec/migrate.md → Atomicity to keep the source folder recoverable
through any crash point.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pix.content_hash import compute_content_hash
from pix.convert import ConvertFailed, convert_to_jpg, convert_to_mp4
from pix.exiftool_session import ExifToolSession
from pix.plan import Action, Plan, PlanLine


class ApplyError(Exception):
    """Raised when an action fails mid-apply. Apply halts on this."""


def apply_plan(
    plan: Plan,
    plan_path: Path,
    run_dir: Path,
    kept_line_ids: set[str],
    staging_dir: Path | None = None,
) -> tuple[int, int]:
    """Apply the plan, updating plan.txt in place.

    Returns `(completed, skipped)`. Raises `ApplyError` if any action fails.
    """
    runnable = [
        ln for ln in plan.lines if ln.line_id in kept_line_ids
    ]

    needs_exiftool = any(
        ln.action
        in (Action.TAG, Action.RENAME_TAG, Action.CONVERT_RENAME_TAG)
        for ln in runnable
    )
    needs_staging = any(
        ln.action == Action.CONVERT_RENAME_TAG for ln in runnable
    )

    annotations: dict[str, str] = {}
    exiftool: ExifToolSession | None = None
    try:
        if needs_exiftool:
            exiftool = ExifToolSession()
        if needs_staging:
            if staging_dir is None:
                raise ApplyError(
                    "CONVERT actions require a staging directory but none "
                    "was provided"
                )
            staging_dir.mkdir(parents=True, exist_ok=True)
        completed = 0
        for ln in runnable:
            annotations[ln.line_id] = _stamp("Started")
            _write_plan(plan, plan_path, annotations)
            try:
                _apply_one(ln, run_dir, exiftool, staging_dir)
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

    return completed, 0


def _stamp(state: str) -> str:
    return f"[{datetime.now().strftime('%H:%M:%S')} {state}]"


def _write_plan(
    plan: Plan, plan_path: Path, annotations: dict[str, str]
) -> None:
    plan_path.write_text(plan.to_text(annotations), encoding="utf-8")


def _apply_one(
    ln: PlanLine,
    run_dir: Path,
    exiftool: ExifToolSession | None,
    staging_dir: Path | None,
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
    elif ln.action == Action.CONVERT_RENAME_TAG:
        assert exiftool is not None, "CONVERT requires an ExifTool session"
        assert staging_dir is not None, "CONVERT requires a staging directory"
        _apply_convert(ln, run_dir, exiftool, staging_dir)
    else:
        raise ApplyError(f"action {ln.action.value} not supported")


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
    if ln.needs_content_hash:
        # Compute on the current in-place file *before* the ExifTool
        # write. Format-aware framing makes the hash invariant under
        # the metadata edit that's about to happen — so the value we
        # compute here is the same value the next migrate would compute.
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

    Each step after (1) is a single same-volume rename. Crash recovery is
    handled by `pix.cleanup` on the next migrate.
    """
    if ln.target_filename is None:
        raise ApplyError(
            f"{ln.line_id}: CONVERT requires a target_filename"
        )

    src = ln.abs_path
    target_ext = ln.target_filename.rsplit(".", 1)[-1].lower()

    # Step 1: off-library conversion + metadata copy + pix:* writes
    staging_path = staging_dir / f"{ln.line_id}_{src.stem}.{target_ext}"
    if staging_path.exists():
        staging_path.unlink()

    try:
        if target_ext == "jpg":
            convert_to_jpg(src, staging_path)
        elif target_ext == "mp4":
            convert_to_mp4(src, staging_path)
        else:
            raise ApplyError(
                f"{ln.line_id}: unsupported CONVERT target extension "
                f"{target_ext!r}"
            )
    except ConvertFailed as e:
        raise ApplyError(
            f"{ln.line_id}: conversion failed: {e}"
        ) from e

    # Copy all source metadata onto the converted file + write pix:* tags.
    writes = dict(ln.pix_writes)
    if ln.needs_original_path:
        writes["XMP:OriginalPath"] = str(src)
    if ln.needs_content_hash:
        # Hash the freshly-converted bytes in staging. Same as TAG: the
        # format-aware hash is invariant under the metadata layer-up
        # that happens in the next exiftool call.
        writes["XMP:ContentHash"] = compute_content_hash(staging_path)
    exiftool.copy_metadata_and_write_tags(
        source=src, dest=staging_path, tags=writes
    )

    # Step 2: bring into source as marker. Marker name per spec/migrate.md
    # → Marker conventions: `{full-source-name}.__migrate__.{new-ext}`.
    marker_path = src.parent / f"{src.name}.__migrate__.{target_ext}"
    if marker_path.exists():
        raise ApplyError(
            f"{ln.line_id}: marker {marker_path.name} already exists "
            f"(leftover from a prior crash?)"
        )
    staging_path.rename(marker_path)

    # Step 3: capture original into the run folder.
    capture_path = run_dir / f"{ln.line_id}_{src.name}"
    src.rename(capture_path)

    # Step 4: finalize marker to canonical name.
    target = src.parent / ln.target_filename
    if target.exists():
        # Shouldn't happen — collision resolution ran during plan-gen.
        # But fail clearly rather than overwrite something unrelated.
        raise ApplyError(
            f"{ln.line_id}: target {target.name} already exists at "
            f"finalize step"
        )
    marker_path.rename(target)
