"""Library-root resolution.

A library root is a directory containing a `.pix/` directory. Every `pix`
command (other than `init`) resolves its root before doing any work.
Resolution order, per spec/library.md:

  1. Walk up from `start` (the path arg the command was given, if any) —
     finds the library when the user pointed at it or at a subfolder.
  2. The `PIX_ROOT` environment variable.
  3. Walk up from CWD — interactive fallback when the user is inside a
     library and didn't bother to pass a path.

After resolving, the schema version is checked via `pix.schema.ensure_current`
unless `check_schema=False` (only `pix upgrade` passes False, since
upgrade is the command that fixes the schema mismatch).
"""

from __future__ import annotations

import os
from pathlib import Path

from pix.schema import ensure_current


class NoLibraryRoot(Exception):
    """Raised when no library root can be resolved."""


def resolve(
    start: Path | None = None,
    *,
    check_schema: bool = True,
) -> Path:
    """Resolve the library root.

    Raises `NoLibraryRoot` if no root is found. When `check_schema=True`
    (the default), may also raise `pix.schema.SchemaUpgradeRequired`
    or `pix.schema.SchemaTooNew`.
    """
    if start is not None:
        found = _walk_up(start.resolve())
        if found is not None:
            if check_schema:
                ensure_current(found)
            return found

    env_root = os.environ.get("PIX_ROOT")
    if env_root:
        candidate = Path(env_root).resolve()
        if not (candidate / ".pix").is_dir():
            raise NoLibraryRoot(
                f"PIX_ROOT={candidate} does not contain a .pix directory. "
                f"Run 'pix init {candidate}' to establish one."
            )
        if check_schema:
            ensure_current(candidate)
        return candidate

    found = _walk_up(Path.cwd().resolve())
    if found is not None:
        if check_schema:
            ensure_current(found)
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
