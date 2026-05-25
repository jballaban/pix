"""Metadata extraction for the plan-gen phase.

Per spec/migrate.md → "Metadata cache": plan generation needs the
existing metadata of every file it'll consider. We bulk-extract via
one ExifTool subprocess and parse the JSON. Now optionally backed by
a per-file persistent cache (see `pix.metadata_cache`); when present,
already-cached files skip ExifTool entirely.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, cast

from pix import exiftool_config_path
from pix.metadata_cache import PerFileCache


# Misses are read in batches of this size. Picked so a crash mid-bulk-read
# loses at most ~1000 files' worth of progress (the cache stores completed
# batches immediately) and so memory stays bounded — one ExifTool call
# returning JSON for 1000 files is a few MB, vs. tens-to-hundreds of MB
# for an unbounded library-wide read.
BATCH_SIZE: int = 1000

# Cache lookup is I/O-bound (stat + small file read + parse, per file).
# A thread pool parallelizes the per-file disk operations; on SSD/NVMe
# this is roughly an order-of-magnitude speedup over sequential, since
# each stat/read spends most of its time blocked on the disk. 32 keeps
# us well under typical NTFS handle limits while saturating modern
# storage.
CACHE_LOOKUP_WORKERS: int = 32


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


def filter_cache_misses(
    paths_with_sizes: list[tuple[Path, int]],
    cache: PerFileCache | None,
    on_batch: Callable[[int], None] | None = None,
    batch_size: int = BATCH_SIZE,
    max_workers: int = CACHE_LOOKUP_WORKERS,
) -> tuple[dict[Path, FileMetadata], list[Path]]:
    """Split `(path, size)` pairs into (cache_hits, misses).

    Sizes come from the scandir-based walk in `pix.scan.walk_source_files`,
    where they're free from the dirent. They're passed through to
    `cache.get()` for validation so the cache lookup is a pure file read
    with no additional `stat()`.

    If `cache` is None, every path is a miss.

    Lookups run in a thread pool (`max_workers` default 32) — each
    per-file check is I/O-bound (read + parse) and independent, so
    concurrent execution on SSD/NVMe pushes the total phase time
    ~10× lower than sequential.

    `on_batch(batch_size)` fires every `batch_size` files (default
    1000) from the consumer thread (after results are collected in
    submission order via `ThreadPoolExecutor.map`) — no locking
    needed for the callback.
    """
    if cache is None:
        return {}, [p for p, _ in paths_with_sizes]

    hits: dict[Path, FileMetadata] = {}
    misses: list[Path] = []
    in_batch = 0

    def check_one(
        item: tuple[Path, int],
    ) -> tuple[Path, dict[str, object] | None]:
        path, size = item
        return path, cache.get(path, expected_size=size)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for path, cached in executor.map(check_one, paths_with_sizes):
            if cached is not None:
                hits[path] = FileMetadata(path=path, raw=cached)
            else:
                misses.append(path)
            in_batch += 1
            if in_batch >= batch_size:
                if on_batch is not None:
                    on_batch(in_batch)
                in_batch = 0

    if in_batch > 0 and on_batch is not None:
        on_batch(in_batch)
    return hits, misses


def read_metadata_batched(
    misses: list[Path],
    cache: PerFileCache | None = None,
    exiftool: str | None = None,
    on_batch: Callable[[int], None] | None = None,
    batch_size: int = BATCH_SIZE,
) -> dict[Path, FileMetadata]:
    """Read metadata for the listed files via batched ExifTool calls.

    Misses are read in `batch_size`-chunked subprocess calls (default
    1000). Each batch's results are written to `cache` as soon as the
    batch completes — incremental persistence so a crash mid-read
    keeps completed batches. `on_batch(batch_size)` fires after each
    batch; callers wire this to `LiveProgress.advance(by=n)`.

    Returns a dict keyed by absolute file path.
    """
    if not misses:
        return {}
    result: dict[Path, FileMetadata] = {}
    for i in range(0, len(misses), batch_size):
        batch = misses[i : i + batch_size]
        fresh = _exiftool_bulk_read(batch, exiftool=exiftool)
        for path, meta in fresh.items():
            result[path] = meta
            if cache is not None:
                cache.add(path, meta.raw)
        if on_batch is not None:
            on_batch(len(batch))
    return result


def build_cache(
    paths_with_sizes: list[tuple[Path, int]],
    cache: PerFileCache | None = None,
    exiftool: str | None = None,
    on_batch: Callable[[int], None] | None = None,
    batch_size: int = BATCH_SIZE,
) -> dict[Path, FileMetadata]:
    """Convenience: cache lookup + batched read in one call.

    Accepts the `(path, size)` shape returned by
    `pix.scan.walk_source_files` so callers don't have to massage it.

    For commands that want per-batch progress, prefer the lower-level
    split: `filter_cache_misses` then `read_metadata_batched`, so the
    caller can put a determinate `LiveProgress` around the read.

    Returns a dict keyed by absolute file path.
    """
    if not paths_with_sizes:
        return {}
    hits, misses = filter_cache_misses(paths_with_sizes, cache)
    fresh = read_metadata_batched(
        misses,
        cache=cache,
        exiftool=exiftool,
        on_batch=on_batch,
        batch_size=batch_size,
    )
    return {**hits, **fresh}


def _exiftool_bulk_read(
    paths: list[Path], exiftool: str | None = None
) -> dict[Path, FileMetadata]:
    """Run ExifTool against a list of paths via the `-@ <listfile>` flag.

    Avoids both Windows command-line length limits and ExifTool's
    redundant `-r` walk (the caller has already enumerated the
    relevant files via `pix.scan.walk_source_files`).
    """
    if not paths:
        return {}

    exe = exiftool or require_exiftool()

    listfile = Path(
        tempfile.NamedTemporaryFile(
            mode="w",
            suffix="_pix_paths.txt",
            delete=False,
            encoding="utf-8",
        ).name
    )
    try:
        with listfile.open("w", encoding="utf-8") as f:
            for p in paths:
                f.write(str(p) + "\n")

        # -fast2 skips the JPEG trailer scan (trailing IPTC/Photoshop
        # blocks) and the MakerNote extraction. pix never reads either:
        # consumed tags live in EXIF (IFD0/ExifIFD), XMP (separate
        # packet), QuickTime, or File:* basics. The skipped data is
        # exactly what makes ExifTool slow on rich JPEGs.
        proc = subprocess.run(
            [
                exe,
                "-config",
                str(exiftool_config_path()),
                "-fast2",  # skip JPEG trailer + MakerNote extraction
                "-j",  # JSON output
                "-G:0",  # group-prefixed keys (family 0)
                "-charset",
                "filename=utf8",
                "-api",
                "largefilesupport=1",
                "-@",
                str(listfile),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    finally:
        try:
            listfile.unlink()
        except OSError:
            pass

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

    Pure function — separated so tests can drive it with fixture JSON
    without needing exiftool installed.

    No `resolve()` or `is_file()` on each result: ExifTool emits a
    `SourceFile` entry only for files it successfully read, and the
    string is already the absolute path we passed in via `-@ <listfile>`.
    Re-validating each entry is two redundant stats per file at scale.
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
        path = Path(source_file)
        cache[path] = FileMetadata(path=path, raw=entry_dict)

    return cache
