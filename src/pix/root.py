"""Library-root resolution.

A library root is a directory containing a `.pix/` directory. Every `pix`
command (other than `init`) resolves its root before doing any work.
Resolution order, per spec/library.md:

  1. Walk up from `start` (the path arg the command was given, if any) —
     finds the library when the user pointed at it or at a subfolder.
  2. The `PIX_ROOT` environment variable.
  3. Walk up from CWD — interactive fallback when the user is inside a
     library and didn't bother to pass a path.

After resolving, the schema version is checked and (if needed) the
library is reset to current defaults — see `pix.schema`.
"""

from __future__ import annotations

import os
from pathlib import Path

from pix.schema import SchemaCheckResult, ensure_current


class NoLibraryRoot(Exception):
    """Raised when no library root can be resolved."""


def resolve(
    start: Path | None = None,
) -> tuple[Path, SchemaCheckResult]:
    """Resolve the library root and bring its schema to current.

    Returns `(root, schema_result)`. `schema_result.archived_from` is
    set when the library was reset and the caller may want to surface a
    user-visible notice. Raises `NoLibraryRoot` if no root is found and
    `pix.schema.SchemaTooNew` if the on-disk schema is newer than this
    pix understands.
    """
    if start is not None:
        found = _walk_up(start.resolve())
        if found is not None:
            return found, ensure_current(found)

    env_root = os.environ.get("PIX_ROOT")
    if env_root:
        candidate = Path(env_root).resolve()
        if not (candidate / ".pix").is_dir():
            raise NoLibraryRoot(
                f"PIX_ROOT={candidate} does not contain a .pix directory. "
                f"Run 'pix init {candidate}' to establish one."
            )
        return candidate, ensure_current(candidate)

    found = _walk_up(Path.cwd().resolve())
    if found is not None:
        return found, ensure_current(found)

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
