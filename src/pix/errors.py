"""Errors action — move-aside for files that fail CONVERT.

Per spec/migrate.md → Failure handling: when CONVERT fails on the
conversion step itself (Pillow truncated-image error, ffmpeg can't
decode, …), the source file is moved to `<library>/.pix/errors/` with
an opaque filename + YAML sidecar capturing the original path, the
error message, and the run-id. Same on-disk shape as `pix.stash`; the
semantic distinction (intentional set-aside vs. runtime failure) lives
in which folder, not in the layout.

Sidecar format:

    original_path: G:\\pix\\raw\\media\\2021\\bad.HEIC
    failed_at: 2026-05-25T15:32:01
    error: "Pillow failed to convert ... truncated (14 bytes not processed)"
    run_id: 2026-05-25_14-52-13

Recovery: the user can either restore the file at `original_path` from
backup (next migrate run will pick it up), or hard-delete the
`.pix/errors/<...>` entry to forget it. The errors folder accumulates
across runs; a future `pix errors clean` command can prune entries
older than N days if needed.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

import yaml

from pix.stash import stash_filename  # reuse the opaque-name builder


SIDECAR_SUFFIX: str = ".errorinfo"


@dataclass(frozen=True)
class ErrorSidecar:
    """Per-file provenance for a CONVERT failure."""

    original_path: str
    failed_at: str  # ISO 8601 datetime, second precision
    error: str
    run_id: str

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            {
                "original_path": self.original_path,
                "failed_at": self.failed_at,
                "error": self.error,
                "run_id": self.run_id,
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
        return cls(
            original_path=original_path,
            failed_at=failed_at,
            error=error,
            run_id=run_id,
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
    )
    sidecar_path_for(target).write_text(
        sidecar.to_yaml(), encoding="utf-8"
    )
    return target
