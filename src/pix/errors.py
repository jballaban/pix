"""Errors action — move-aside for files that fail CONVERT.

Per spec/migrate.md → Failure handling: when CONVERT fails on the
conversion step itself (Pillow can't decode, ffmpeg can't read, …),
the source file is moved to `<library>/.pix/errors/` with an opaque
filename + YAML sidecar capturing the original path, the error
message, the run-id, and the **pix version that produced the
failure**. Same on-disk shape as `pix.stash`; the semantic distinction
(intentional set-aside vs. runtime failure) lives in which folder.

Sidecar format:

    original_path: G:\\pix\\raw\\media\\2021\\bad.HEIC
    failed_at: 2026-05-25T15:32:01
    error: "Pillow failed to convert ... truncated (14 bytes not processed)"
    run_id: 2026-05-25_14-52-13
    pix_version: 0.1.85

Auto-retry semantics: migrate's cleanup phase restores any errorinfo
whose `pix_version` doesn't match the current `pix.__version__` (a code
change since the failure means the same input may now succeed). The
restored file goes back to its `original_path`, where the upcoming
plan-gen pass will see it. Sidecars missing `pix_version` (written by
pre-v0.1.86 builds) are also treated as stale and restored.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

import yaml

from pix import __version__ as _PIX_VERSION
from pix.stash import stash_filename  # reuse the opaque-name builder


SIDECAR_SUFFIX: str = ".errorinfo"


@dataclass(frozen=True)
class ErrorSidecar:
    """Per-file provenance for a CONVERT failure."""

    original_path: str
    failed_at: str  # ISO 8601 datetime, second precision
    error: str
    run_id: str
    pix_version: str  # `pix.__version__` at failure time

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            {
                "original_path": self.original_path,
                "failed_at": self.failed_at,
                "error": self.error,
                "run_id": self.run_id,
                "pix_version": self.pix_version,
            },
            default_flow_style=False,
            sort_keys=False,
        )

    @classmethod
    def from_yaml(cls, text: str) -> "ErrorSidecar":
        loaded: object = yaml.safe_load(text) or {}
        if not isinstance(loaded, dict):
            raise ValueError("errorinfo: top-level must be a mapping")
        data = cast("dict[str, object]", loaded)
        original_path = data.get("original_path")
        if not isinstance(original_path, str):
            raise ValueError(
                "errorinfo: 'original_path' must be a string"
            )
        failed_at = data.get("failed_at")
        if not isinstance(failed_at, str):
            raise ValueError("errorinfo: 'failed_at' must be a string")
        error = data.get("error")
        if not isinstance(error, str):
            raise ValueError("errorinfo: 'error' must be a string")
        run_id = data.get("run_id")
        if not isinstance(run_id, str):
            raise ValueError("errorinfo: 'run_id' must be a string")
        # pix_version is optional for backward compatibility with
        # sidecars written by pre-v0.1.86 builds; absent = "unknown
        # older version", which the restore logic treats as stale.
        pix_version_raw = data.get("pix_version", "")
        pix_version = pix_version_raw if isinstance(pix_version_raw, str) else ""
        return cls(
            original_path=original_path,
            failed_at=failed_at,
            error=error,
            run_id=run_id,
            pix_version=pix_version,
        )


def sidecar_path_for(error_file: Path) -> Path:
    """Sidecar lives next to the error file with `.errorinfo` appended."""
    return error_file.parent / (error_file.name + SIDECAR_SUFFIX)


def errors_dir_for(library_root: Path) -> Path:
    return library_root / ".pix" / "errors"


def move_to_errors(
    *,
    source: Path,
    library_root: Path,
    run_id: str,
    line_id: str,
    error: str,
    failed_at: datetime | None = None,
) -> Path:
    """Move `source` into `<library>/.pix/errors/` and write the sidecar.

    Returns the destination path. Filename is the standard opaque
    `<run-id>_<line-id>.<ext>` (shared with stash so a future operator
    only has to learn the one convention).

    Uses `shutil.move` so a cross-volume source (rare — usually source
    and library are same-volume) falls back to copy+delete cleanly.
    """
    errors_dir = errors_dir_for(library_root)
    errors_dir.mkdir(parents=True, exist_ok=True)
    target = errors_dir / stash_filename(run_id, line_id, source)
    original_path = str(source)
    shutil.move(str(source), str(target))
    ts = (failed_at or datetime.now()).isoformat(timespec="seconds")
    sidecar = ErrorSidecar(
        original_path=original_path,
        failed_at=ts,
        error=error,
        run_id=run_id,
        pix_version=_PIX_VERSION,
    )
    sidecar_path_for(target).write_text(
        sidecar.to_yaml(), encoding="utf-8"
    )
    return target


@dataclass(frozen=True)
class RestoredEntry:
    """One entry restored from `.pix/errors/` back to its original path."""

    original_path: Path
    sidecar_pix_version: str  # may be "" for legacy sidecars


@dataclass(frozen=True)
class SkippedEntry:
    """One errorinfo we couldn't or wouldn't restore. Reason is human-readable."""

    entry_path: Path
    reason: str


def restore_stale_errors(library_root: Path) -> tuple[
    list[RestoredEntry], list[SkippedEntry], int
]:
    """Restore every `.pix/errors/` entry whose `pix_version` differs from
    the running `pix.__version__`, plus any legacy sidecar without a
    version recorded.

    Called from migrate's cleanup phase: by the time plan-gen walks the
    source, the restored files are back in place and get the new
    code's chance.

    Returns `(restored, skipped, kept_current_version)`:
    - `restored`: entries successfully moved back to `original_path`
    - `skipped`: entries we couldn't restore (file missing, sidecar
      malformed, target slot occupied, move failed). Reason is logged
      so the user knows what needs manual attention.
    - `kept_current_version`: count of entries left in place because
      their `pix_version` matches the running version (retrying would
      hit the same failure).
    """
    errors_dir = errors_dir_for(library_root)
    if not errors_dir.is_dir():
        return [], [], 0

    restored: list[RestoredEntry] = []
    skipped: list[SkippedEntry] = []
    kept = 0

    for errorinfo in sorted(errors_dir.glob(f"*{SIDECAR_SUFFIX}")):
        data_file = errorinfo.parent / errorinfo.name[: -len(SIDECAR_SUFFIX)]
        if not data_file.is_file():
            skipped.append(
                SkippedEntry(
                    entry_path=errorinfo,
                    reason="quarantined file missing alongside sidecar",
                )
            )
            continue

        try:
            sidecar = ErrorSidecar.from_yaml(
                errorinfo.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, yaml.YAMLError) as e:
            skipped.append(
                SkippedEntry(
                    entry_path=errorinfo,
                    reason=f"errorinfo unreadable: {e}",
                )
            )
            continue

        if sidecar.pix_version == _PIX_VERSION:
            # Same version that quarantined → same code would fail
            # again. Leave in place.
            kept += 1
            continue

        original_path = Path(sidecar.original_path)
        if original_path.exists():
            skipped.append(
                SkippedEntry(
                    entry_path=data_file,
                    reason=(
                        f"target {original_path} already exists — "
                        "not overwriting"
                    ),
                )
            )
            continue

        try:
            original_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(data_file), str(original_path))
        except OSError as e:
            skipped.append(
                SkippedEntry(
                    entry_path=data_file,
                    reason=f"move failed: {e}",
                )
            )
            continue

        # Sidecar cleanup is best-effort. A leftover sidecar with no
        # adjacent data file is harmless — the next cleanup pass skips
        # it with a clear reason.
        try:
            errorinfo.unlink()
        except OSError:
            pass

        restored.append(
            RestoredEntry(
                original_path=original_path,
                sidecar_pix_version=sidecar.pix_version,
            )
        )

    return restored, skipped, kept


def find_orphaned_error_files(library_root: Path) -> list[Path]:
    """Data files in `.pix/errors/` that have no adjacent `.errorinfo`.

    The sidecar is the only record of a quarantined file's original path,
    so a data file without one can't be restored *in place* — but it's
    still a real file we set aside, deserving another processing attempt.
    Typically the residue of an interrupted quarantine: `move_to_errors`
    moves the file, then writes the sidecar — a crash (or a lost sidecar)
    between the two steps leaves the data file standing alone.
    """
    errors_dir = errors_dir_for(library_root)
    if not errors_dir.is_dir():
        return []
    orphans: list[Path] = []
    for entry in sorted(errors_dir.iterdir()):
        if not entry.is_file():
            continue
        if entry.name.endswith(SIDECAR_SUFFIX):
            continue
        if sidecar_path_for(entry).exists():
            continue
        orphans.append(entry)
    return orphans


def restore_orphaned_errors(
    library_root: Path, target_dir: Path
) -> tuple[list[RestoredEntry], list[SkippedEntry]]:
    """Move sidecar-less `.pix/errors/` files into `target_dir` to retry.

    With no sidecar there's no recorded `original_path`, so we can't put
    the file back where it came from. Instead we drop it into the folder
    the current migrate is scanning, where plan-gen picks it up this run.
    If processing fails again, apply re-quarantines it *with* a fresh
    sidecar — so the next run has full provenance. This is the design
    answer to a lost sidecar: just re-attempt, don't nag.

    Never clobbers: a file whose opaque name already exists in
    `target_dir` is skipped (collisions are near-impossible given the
    `<run-id>_<line-id>` naming, but we don't overwrite on the off chance).
    """
    restored: list[RestoredEntry] = []
    skipped: list[SkippedEntry] = []
    for orphan in find_orphaned_error_files(library_root):
        dest = target_dir / orphan.name
        if dest.exists():
            skipped.append(
                SkippedEntry(
                    entry_path=orphan,
                    reason=f"target {dest} already exists — not overwriting",
                )
            )
            continue
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(orphan), str(dest))
        except OSError as e:
            skipped.append(
                SkippedEntry(entry_path=orphan, reason=f"move failed: {e}")
            )
            continue
        restored.append(
            RestoredEntry(original_path=dest, sidecar_pix_version="")
        )
    return restored, skipped
