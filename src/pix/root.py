"""Library-root resolution.

A library root is a directory containing a `.pix/` directory. Every `pix`
command (other than `init`) resolves its root before doing any work.
Resolution order, per spec/library.md:

  1. Walk up from `start` (the path arg the command was given, if any) —
     finds the library when the user pointed at it or at a subfolder.
  2. The `PIX_ROOT` environment variable.
  3. Walk up from CWD — interactive fallback when the user is inside a
     library and didn't bother to pass a path.

The library is version-less — there's no schema check or upgrade step.
Format drift in `.pix/` is handled structurally (regenerable caches are
rebuilt; only-copy provenance is restored from its stable path field;
run folders are left as-is). See spec/library.md.
"""

from __future__ import annotations

import os
from pathlib import Path


class NoLibraryRoot(Exception):
    """Raised when no library root can be resolved."""


def resolve(start: Path | None = None) -> Path:
    """Resolve the library root. Raises `NoLibraryRoot` if none is found."""
    if start is not None:
        found = _walk_up(start.resolve())
        if found is not None:
            return found

    env_root = os.environ.get("PIX_ROOT")
    if env_root:
        candidate = Path(env_root).resolve()
        if not (candidate / ".pix").is_dir():
            raise NoLibraryRoot(
                f"PIX_ROOT={candidate} does not contain a .pix directory. "
                f"Run 'pix init {candidate}' to establish one."
            )
        return candidate

    found = _walk_up(Path.cwd().resolve())
    if found is not None:
        return found

    raise NoLibraryRoot(
        "No pix library root found. Pass a path inside a library, set "
        "PIX_ROOT, or run 'pix init <path>' to establish one."
    )


def _walk_up(start: Path) -> Path | None:
    """Walk up from `start` looking for a `.pix/` directory; first match wins."""
    for parent in (start, *start.parents):
        if (parent / ".pix").is_dir():
            return parent
    return None
