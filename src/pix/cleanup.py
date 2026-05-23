"""Source-folder cleanup pass run at the start of every `pix migrate`.

Handles orphan files left behind by atomic operations that crashed
mid-flight in a prior run:

- `*.__pixrename__` — intermediate from an interrupted case-only rename
  (see `pix.apply._apply_rename`). Revert to the original name so the
  next plan re-proposes the rename.
- `*.__migrate__.*` — marker from an interrupted CONVERT step (see
  `pix.apply._apply_convert`). Resolve per spec/migrate.md → Marker
  cleanup: if the original is still in source, delete the marker (the
  new plan re-proposes the convert); if the original is already captured
  (marker only), finalize the marker to its canonical name.
- `*_exiftool_tmp` — leftover from ExifTool's own atomic-write machinery
  if a TAG write was interrupted. Per ExifTool's protocol the original
  file is untouched whenever the `_exiftool_tmp` is present, so deletion
  is safe; the new plan will re-propose the TAG.

See spec/migrate.md → Marker cleanup for the full state diagram.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from pix.dates import parse_exiftool_datetime
from pix.exiftool_session import ExifToolSession

_RENAME_SUFFIX: str = ".__pixrename__"
_MIGRATE_INFIX: str = ".__migrate__."
_EXIFTOOL_TMP_SUFFIX: str = "_exiftool_tmp"


class CleanupError(Exception):
    """Raised when a marker can't be safely resolved during cleanup."""


def cleanup_rename_orphans(folder: Path) -> list[Path]:
    """Revert any `*.__pixrename__` intermediates back to their original names.

    For each orphan:
    - If the original name slot is empty → rename intermediate to original.
    - If the original slot is occupied (the intermediate is a stale dup;
      original came back somehow) → delete the intermediate.

    Returns the list of original-target paths that were resolved (one per
    intermediate encountered).
    """
    resolved: list[Path] = []
    for path in folder.rglob(f"*{_RENAME_SUFFIX}"):
        if not path.is_file():
            continue
        if _under_pix_dir(path):
            continue

        original_name = path.name[: -len(_RENAME_SUFFIX)]
        original = path.parent / original_name
        if original.exists():
            path.unlink()
        else:
            path.rename(original)
        resolved.append(original)
    return resolved


def cleanup_migrate_markers(folder: Path) -> list[str]:
    """Resolve any `*.__migrate__.*` markers from interrupted CONVERT runs.

    For each marker `{original-name}.__migrate__.{new-ext}`:
    - If `{original-name}` is still in source → original survived; delete
      the marker (next plan re-proposes the CONVERT).
    - Else → original was already captured to a prior run folder. Read the
      marker's `pix:DateAuto`, compute its canonical filename, and rename
      the marker to it.

    Opens a short-lived ExifTool session only if markers are found. Raises
    `CleanupError` if a marker can't be safely resolved (e.g. malformed
    name, missing pix:DateAuto, canonical-name collision).
    """
    markers = [
        p
        for p in folder.rglob(f"*{_MIGRATE_INFIX}*")
        if p.is_file() and not _under_pix_dir(p)
    ]
    if not markers:
        return []

    notes: list[str] = []
    with ExifToolSession() as session:
        for marker in markers:
            try:
                original_name, new_ext = _split_marker_name(marker.name)
            except ValueError as e:
                raise CleanupError(
                    f"unrecognized marker filename {marker.name!r}: {e}"
                ) from e

            original_path = marker.parent / original_name
            if original_path.exists():
                marker.unlink()
                notes.append(
                    f"deleted marker {marker.name} (original still present)"
                )
                continue

            target = _finalize_convert_marker(marker, new_ext, session)
            notes.append(
                f"finalized marker {marker.name} -> {target.name}"
            )

    return notes


def _split_marker_name(name: str) -> tuple[str, str]:
    """Split `{original}.__migrate__.{new-ext}` into (original, new-ext)."""
    idx = name.rfind(_MIGRATE_INFIX)
    if idx < 0:
        raise ValueError("missing __migrate__ infix")
    original = name[:idx]
    new_ext = name[idx + len(_MIGRATE_INFIX):]
    if not original or not new_ext:
        raise ValueError("empty original or extension component")
    if "." in new_ext:
        # New extension shouldn't itself contain `.`; reject to avoid
        # accidentally picking up nested suffixes.
        raise ValueError(f"new-ext {new_ext!r} contains a `.`")
    return original, new_ext


def _finalize_convert_marker(
    marker: Path, new_ext: str, session: ExifToolSession
) -> Path:
    """Read marker's pix:DateAuto, compute canonical name, rename."""
    # Read just the value of pix:DateAuto. `-s3` strips tag name + group.
    output = session.execute(
        "-s3", "-XMP-pix:DateAuto", str(marker)
    )
    date_auto_str = output.strip()
    if not date_auto_str:
        raise CleanupError(
            f"marker {marker.name}: no pix:DateAuto stored, can't compute "
            f"canonical filename (manual recovery needed; original is in a "
            f"prior runs/<run-id>/ folder)"
        )

    dt = _parse_pix_dt(date_auto_str) or parse_exiftool_datetime(date_auto_str)
    if dt is None:
        raise CleanupError(
            f"marker {marker.name}: pix:DateAuto={date_auto_str!r} "
            f"unparseable"
        )

    canonical_stem = dt.strftime("%Y-%m-%d_%H%M%S")
    canonical_name = f"{canonical_stem}.{new_ext.lower()}"
    target = marker.parent / canonical_name
    if target.exists():
        raise CleanupError(
            f"marker {marker.name}: finalize target {canonical_name} "
            f"already exists"
        )
    marker.rename(target)
    return target


def _parse_pix_dt(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d-%H:%M:%S")
    except ValueError:
        return None


def cleanup_exiftool_tmp(folder: Path) -> list[Path]:
    """Delete any `*_exiftool_tmp` leftovers under `folder`.

    These are ExifTool's atomic-write tmp files; if one exists then the
    original was never replaced (ExifTool's protocol), so deletion is
    safe. Per spec/migrate.md → Marker cleanup. Returns the list of
    deleted paths.

    Skips anything under `.pix/` — that's pix-managed working state and
    not part of source's atomic-write story.
    """
    deleted: list[Path] = []
    for path in folder.rglob(f"*{_EXIFTOOL_TMP_SUFFIX}"):
        if not path.is_file():
            continue
        if _under_pix_dir(path):
            continue
        try:
            path.unlink()
        except OSError:
            # Best-effort. If we can't delete it, the next plan will
            # surface it as an unknown extension and the user can
            # remove it manually.
            continue
        deleted.append(path)
    return deleted


def wipe_staging(staging_dir: Path) -> int:
    """Wipe `.pix/staging/` contents at the start of a migrate run.

    Per spec/migrate.md → Workflow step 1. Removes everything inside
    `staging_dir` (but not the directory itself). Returns the count of
    entries removed. Safe if the directory doesn't exist (returns 0).
    """
    if not staging_dir.exists():
        return 0
    count = 0
    for entry in staging_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            try:
                entry.unlink()
            except OSError:
                continue
        count += 1
    return count


def _under_pix_dir(path: Path) -> bool:
    return any(part == ".pix" for part in path.parts)
