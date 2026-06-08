"""Path-mirroring helpers shared by the errors tree and the cache import.

The cache itself now lives in a single SQLite DB (`pix.cache_db`). What
remains here is the path-mirror scheme that maps an absolute media path to a
location under a root directory:

    media:  G:\\pix\\raw\\2023\\foo.jpg
    under:  <root>/G/pix/raw/2023/foo.jpg[.<suffix>]

Two consumers use it:

- `pix.errors` — the `<library>/.pix/errors/` tree, where a quarantined file
  keeps its name and its *location* records the source path (suffix `""`).
- `pix.cache_db` — the one-time import reads the legacy `.pix/cache/`
  sidecars (`.meta`/`.hash`/`.vfp`) via this same mirror before reaping them.

`read_json` is the tiny JSON reader the import uses for those legacy sidecars.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast


def cache_root_for(library_root: Path) -> Path:
    """Return the legacy sidecar directory: `<library>/.pix/cache/`.

    Only the one-time import (`pix.cache_db`) still references this — to find
    and reap the old tree. New caching goes to `<library>/.pix/cache.db`.
    """
    return library_root / ".pix" / "cache"


def mirror_under(root: Path, file_path: Path, suffix: str = "") -> Path:
    """Mirror `file_path` under `root`, appending `suffix` to the filename.

    Drive letters (Windows) fold into the first folder name because NTFS dir
    names can't contain `:` (`G:\\pix\\foo.jpg` → `root/G/pix/foo.jpg`). Caller
    passes an absolute path — every pix caller goes through
    `pix.scan.walk_source_files`, which returns absolute canonical paths.
    """
    parts = file_path.parts
    if not parts:
        return root / (file_path.name + suffix)
    drive = parts[0].rstrip("\\/").rstrip(":")
    rest = parts[1:]
    if rest:
        mirrored = Path(drive, *rest)
    else:
        mirrored = Path(drive)
    return root / mirrored.with_name(mirrored.name + suffix)


def cache_path_for(library_root: Path, file_path: Path, suffix: str) -> Path:
    """Mirror `file_path` under the legacy `<library>/.pix/cache/` with
    `suffix`. Used by the one-time import to locate old sidecars."""
    return mirror_under(cache_root_for(library_root), file_path, suffix)


def read_json(cache_path: Path) -> dict[str, object] | None:
    """Read + parse a JSON file. None on miss / unreadable / bad type."""
    try:
        loaded: object = json.loads(cache_path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    return cast("dict[str, object]", loaded)


def unmirror_under(
    root: Path, mirrored: Path, suffixes: tuple[str, ...]
) -> Path | None:
    """Reverse `mirror_under`: recover the absolute source path that
    `mirrored` (a file under `root`) mirrors. Returns None if `mirrored`
    isn't under `root`, is too shallow to carry a drive folder, or its name
    doesn't end in one of `suffixes`.

    The mirror only mutates the first path component (drive letter folds to a
    bare letter folder) and appends a suffix to the filename. Pass `("",)`
    for the errors tree, whose files carry no suffix.
    """
    try:
        rel = mirrored.relative_to(root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 2:
        return None
    name = parts[-1]
    for suffix in suffixes:
        if name.endswith(suffix):
            stem = name[: len(name) - len(suffix)] if suffix else name
            drive = parts[0]
            interior = parts[1:-1]
            # Restore the colon + root-slash to form an absolute Windows path.
            # `Path("G:")` alone is "current dir on G:"; `Path("G:\\")` is the
            # drive root.
            return Path(f"{drive}:\\", *interior, stem)
    return None
