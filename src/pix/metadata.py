"""Bulk metadata cache, populated via one ExifTool subprocess call.

Per spec/migrate.md → "Metadata cache": plan generation needs the existing
metadata of every file in the source folder. We bulk-extract via
`exiftool -j -r -G:0 <folder>` in one subprocess, parse the JSON, and index
the result by absolute file path.

Apply-phase writes are out of scope here (they use pyexiftool/`-stay_open`
in a later phase).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pix import exiftool_config_path


class ExifToolNotFound(Exception):
    """Raised when the `exiftool` binary can't be located on PATH."""


class ExifToolFailed(Exception):
    """Raised when an ExifTool subprocess returns non-zero."""


@dataclass(frozen=True)
class FileMetadata:
    """All metadata read from one source file by ExifTool, indexed by raw key.

    `raw` keys are group-prefixed (family-0), e.g. `EXIF:DateTimeOriginal`,
    `XMP:DateCreated`, `QuickTime:CreateDate`, `File:FileModifyDate`.
    """

    path: Path
    raw: dict[str, object]

    def get_str(self, key: str) -> str | None:
        value = self.raw.get(key)
        if isinstance(value, str):
            return value
        return None


def require_exiftool() -> str:
    """Locate `exiftool` on PATH; raise `ExifToolNotFound` if missing."""
    exe = shutil.which("exiftool") or shutil.which("exiftool.exe")
    if exe is None:
        raise ExifToolNotFound(
            "exiftool not found on PATH. Install from https://exiftool.org/ "
            "(rename `exiftool(-k).exe` to `exiftool.exe` and place it on "
            "PATH) or via a package manager (winget, scoop, choco)."
        )
    return exe


def build_cache(
    folder: Path, exiftool: str | None = None
) -> dict[Path, FileMetadata]:
    """Bulk-read metadata for every file under `folder` in one ExifTool call.

    Returns a dict keyed by absolute file path. Entries for non-file results
    (the folder itself, etc.) are dropped.
    """
    exe = exiftool or require_exiftool()

    proc = subprocess.run(
        [
            exe,
            "-config",
            str(exiftool_config_path()),
            "-j",  # JSON output
            "-r",  # recursive
            "-G:0",  # group-prefixed keys (family 0: EXIF, XMP, IPTC, QuickTime, File, ...)
            "-i",
            ".pix",  # skip pix's own state directory at any depth
            "-charset",
            "filename=utf8",
            "-api",
            "largefilesupport=1",
            str(folder),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    # ExifTool exit codes:
    #   0 = success
    #   1 = at least one file had a warning (e.g. "File is empty"); JSON still on stdout
    #   2 = fatal (bad command line, missing args, etc.)
    if proc.returncode >= 2:
        raise ExifToolFailed(
            f"exiftool exited {proc.returncode}.\nstderr:\n{proc.stderr}"
        )

    return parse_exiftool_json(proc.stdout)


def parse_exiftool_json(stdout: str) -> dict[Path, FileMetadata]:
    """Parse ExifTool's `-j` output into a path-indexed cache.

    Pure function — separated from `build_cache` so tests can drive it with
    fixture JSON without needing exiftool installed.
    """
    stripped = stdout.strip()
    if not stripped:
        return {}

    data: object = json.loads(stripped)
    if not isinstance(data, list):
        raise ExifToolFailed(
            f"exiftool returned unexpected JSON type: {type(data).__name__}"
        )
    entries = cast("list[object]", data)

    cache: dict[Path, FileMetadata] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_dict = cast("dict[str, object]", entry)
        source_file = entry_dict.get("SourceFile")
        if not isinstance(source_file, str):
            continue
        path = Path(source_file).resolve()
        if not path.is_file():
            continue
        cache[path] = FileMetadata(path=path, raw=entry_dict)

    return cache
