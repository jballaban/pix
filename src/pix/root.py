"""Library-root resolution.

A library root is a directory containing a `.pix/` directory. Every `pix`
command (other than `init`) resolves its root before doing any work. Resolution
order, per spec/library.md:

  1. Explicit override (typically from a `--root` flag).
  2. The `PIX_ROOT` environment variable.
  3. Walk up from the start path (defaulting to CWD), first `.pix/` wins.

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
    override: Path | None = None,
) -> tuple[Path, SchemaCheckResult]:
    """Resolve the library root and bring its schema to current.

    Returns `(root, schema_result)`. `schema_result.archived_from` is set
    when the library was reset and the caller may want to surface a
    user-visible notice. Raises `NoLibraryRoot` if no root is found and
    `pix.schema.SchemaTooNew` if the on-disk schema is newer than this
    pix understands.
    """
    if override is not None:
        candidate = override.resolve()
        if not (candidate / ".pix").is_dir():
            raise NoLibraryRoot(
                f"--root {candidate} does not contain a .pix directory. "
                f"Run 'pix init {candidate}' to establish one."
            )
        return candidate, ensure_current(candidate)

    env_root = os.environ.get("PIX_ROOT")
    if env_root:
        candidate = Path(env_root).resolve()
        if not (candidate / ".pix").is_dir():
            raise NoLibraryRoot(
                f"PIX_ROOT={candidate} does not contain a .pix directory. "
                f"Run 'pix init {candidate}' to establish one."
            )
        return candidate, ensure_current(candidate)

    cwd = (start or Path.cwd()).resolve()
    for parent in (cwd, *cwd.parents):
        if (parent / ".pix").is_dir():
            return parent, ensure_current(parent)

    raise NoLibraryRoot(
        "No pix library root found. Run 'pix init <path>' to establish one."
    )
