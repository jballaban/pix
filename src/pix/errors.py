"""Errors action — move-aside for files that fail CONVERT.

Per spec/migrate.md → Failure handling: when CONVERT fails on the
conversion step itself (Pillow can't decode, ffmpeg can't read, …),
the source file is moved into `<library>/.pix/errors/` with a YAML
sidecar capturing the error message, the run-id, and the **pix version
that produced the failure**.

The moved file **mirrors its source path** under the errors tree, the
same drive-folding scheme the cache uses (see `pix.cache_base`):

    source:   G:\\pix\\2014\\foo.mp4
    errors:   <library>/.pix/errors/G/pix/2014/foo.mp4
    sidecar:  <library>/.pix/errors/G/pix/2014/foo.mp4.errorinfo

This diverges from `pix.stash`'s opaque flat naming on purpose: the
error message in the sidecar is regeneratable, but the **original path
is not** — encoding it in the file's location means a lost or corrupt
sidecar no longer loses provenance. The path is reversible via
`original_path_from_errors_file`, so the file can always be restored to
exactly where it came from.

Sidecar format (`original_path` is redundant with the location — kept
for human readability and as a cross-check):

    original_path: G:\\pix\\2014\\foo.mp4
    failed_at: 2026-05-25T15:32:01
    error: "ffmpeg failed converting ... exit 1"
    run_id: 2026-05-25_14-52-13
    pix_version: 0.1.85

Auto-retry semantics: migrate's cleanup phase restores any entry whose
`pix_version` differs from the current `pix.__version__` (a code change
since the failure means the same input may now succeed), back to its
source path. Entries quarantined by the current version are left in
place. A sidecar-less entry has no recorded version, so it's treated as
stale and restored (to its mirrored location for new-layout entries, or
— for legacy flat entries with no recoverable origin — reprocessed via
`restore_orphaned_errors`).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

import yaml

from pix import __version__ as _PIX_VERSION
from pix import cache_base


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


def errors_path_for(library_root: Path, original_path: Path) -> Path:
    """Mirror `original_path` under `<library>/.pix/errors/` (no suffix).

    The moved file keeps its own name; its location encodes the source
    path. Reverse with `original_path_from_errors_file`.
    """
    return cache_base.mirror_under(
        errors_dir_for(library_root), original_path, ""
    )


def original_path_from_errors_file(
    library_root: Path, errors_file: Path
) -> Path | None:
    """Recover the source path a mirrored errors file came from.

    Returns None for legacy flat entries (directly under `errors/`, with
    no drive folder to reverse) — those carry their origin only in the
    sidecar, if at all.
    """
    return cache_base.unmirror_under(
        errors_dir_for(library_root), errors_file, ("",)
    )


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

    The destination mirrors the source path under the errors tree (see
    module docstring), so the original path is recorded by the file's
    *location*, not just the sidecar. Returns the destination path.

    Uses `shutil.move` so a cross-volume source (rare — usually source
    and library are same-volume) falls back to copy+delete cleanly.
    """
    del line_id  # recorded in the sidecar's run_id context; not in the name
    target = errors_path_for(library_root, source)
    target.parent.mkdir(parents=True, exist_ok=True)
    # A stale copy from a prior failure of the *same* source path can only
    # exist if a previous restore didn't clear it; it's the same file, so
    # replacing it is safe (and `shutil.move` would otherwise nest/raise).
    if target.exists():
        try:
            target.unlink()
        except OSError:
            pass
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
    _write_sidecar_atomic(sidecar_path_for(target), sidecar)
    return target


def _write_sidecar_atomic(path: Path, sidecar: "ErrorSidecar") -> None:
    """Write the sidecar via temp + replace so a crash can't truncate it."""
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(sidecar.to_yaml(), encoding="utf-8")
    tmp.replace(path)


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


def _iter_data_files(errors_dir: Path) -> list[Path]:
    """Quarantined media files under the errors tree (recursive).

    Excludes `.errorinfo` sidecars and `.tmp` write-in-progress files.
    Sorted for deterministic output.
    """
    return sorted(
        p
        for p in errors_dir.rglob("*")
        if p.is_file()
        and not p.name.endswith(SIDECAR_SUFFIX)
        and not p.name.endswith(".tmp")
    )


def _read_sidecar(data_file: Path) -> ErrorSidecar | None:
    """Read the sidecar next to `data_file`; None if absent or unreadable."""
    try:
        return ErrorSidecar.from_yaml(
            sidecar_path_for(data_file).read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError, yaml.YAMLError):
        return None


def _prune_empty_dirs(start: Path, stop: Path) -> None:
    """Remove now-empty errors subdirs from `start` up to (not incl.) `stop`."""
    d = start
    while d != stop and stop in d.parents:
        try:
            d.rmdir()  # only succeeds while empty
        except OSError:
            break
        d = d.parent


def restore_stale_errors(library_root: Path) -> tuple[
    list[RestoredEntry], list[SkippedEntry], int
]:
    """Restore `.pix/errors/` entries whose origin is known and which a
    code change may now handle, back to their source path.

    Called from migrate's cleanup phase: by the time plan-gen walks the
    source, the restored files are back in place and get the new code's
    chance. An entry's source path comes from its mirrored *location*
    (new layout) or, for legacy flat entries, its sidecar's
    `original_path`. Entries with neither are left for
    `restore_orphaned_errors`.

    Restore decision per entry:
    - **Keep** if a readable sidecar records the running `pix_version`
      (the same code would re-fail — no point retrying).
    - Otherwise **restore** (different/absent version → may now succeed).
      A mirrored entry restores even with no/corrupt sidecar, since its
      location alone pins the origin.

    Returns `(restored, skipped, kept_current_version)`:
    - `restored`: entries moved back to their source path.
    - `skipped`: entries we couldn't restore (target slot occupied, move
      failed) plus lone sidecars whose data file is gone (data loss worth
      surfacing). Reason is human-readable.
    - `kept_current_version`: count left in place (current-version quarantine).
    """
    errors_dir = errors_dir_for(library_root)
    if not errors_dir.is_dir():
        return [], [], 0

    restored: list[RestoredEntry] = []
    skipped: list[SkippedEntry] = []
    kept = 0

    for data_file in _iter_data_files(errors_dir):
        loc = original_path_from_errors_file(library_root, data_file)
        sidecar = _read_sidecar(data_file)

        if loc is not None:
            original = loc
        elif sidecar is not None:
            original = Path(sidecar.original_path)
        else:
            continue  # legacy flat, no recoverable origin → orphan path

        if sidecar is not None and sidecar.pix_version == _PIX_VERSION:
            kept += 1  # same code would fail again; leave in place
            continue

        if original.exists():
            skipped.append(
                SkippedEntry(
                    entry_path=data_file,
                    reason=f"target {original} already exists — not overwriting",
                )
            )
            continue

        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(data_file), str(original))
        except OSError as e:
            skipped.append(
                SkippedEntry(entry_path=data_file, reason=f"move failed: {e}")
            )
            continue

        # Best-effort cleanup: drop the sidecar and any now-empty dirs.
        try:
            sidecar_path_for(data_file).unlink(missing_ok=True)
        except OSError:
            pass
        _prune_empty_dirs(data_file.parent, errors_dir)

        restored.append(
            RestoredEntry(
                original_path=original,
                sidecar_pix_version=sidecar.pix_version if sidecar else "",
            )
        )

    # Lone sidecars (data file already gone) — data loss worth surfacing.
    for errorinfo in sorted(errors_dir.rglob(f"*{SIDECAR_SUFFIX}")):
        data_file = errorinfo.parent / errorinfo.name[: -len(SIDECAR_SUFFIX)]
        if not data_file.exists():
            skipped.append(
                SkippedEntry(
                    entry_path=errorinfo,
                    reason="quarantined file missing alongside sidecar",
                )
            )

    return restored, skipped, kept


def find_orphaned_error_files(library_root: Path) -> list[Path]:
    """Errors files whose origin can't be recovered — true orphans.

    A file qualifies only if it's *neither* mirrored (no source path
    derivable from its location) *nor* accompanied by a usable sidecar.
    In practice these are legacy flat entries from before the mirrored
    layout whose sidecar was lost. Mirrored sidecar-less files are not
    orphans: `restore_stale_errors` sends them home via their location.
    """
    errors_dir = errors_dir_for(library_root)
    if not errors_dir.is_dir():
        return []
    orphans: list[Path] = []
    for data_file in _iter_data_files(errors_dir):
        if original_path_from_errors_file(library_root, data_file) is not None:
            continue  # mirrored — origin known
        if _read_sidecar(data_file) is not None:
            continue  # legacy but sidecar carries the origin
        orphans.append(data_file)
    return orphans


def restore_orphaned_errors(
    library_root: Path, target_dir: Path
) -> tuple[list[RestoredEntry], list[SkippedEntry]]:
    """Move origin-less `.pix/errors/` files into `target_dir` to retry.

    Only legacy flat entries with no recoverable origin reach here (see
    `find_orphaned_error_files`). With no source path we can't put them
    back where they came from, so we drop them into the folder the
    current migrate is scanning, where plan-gen picks them up this run.
    If processing fails again, apply re-quarantines them *with* a fresh
    sidecar in the mirrored layout — so provenance is restored. This is
    the design answer to a lost sidecar: just re-attempt, don't nag.

    Never clobbers a file already present in `target_dir`.
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
