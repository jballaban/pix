"""Source-folder cleanup pass run at the start of every `pix migrate`.

Handles orphan files left behind by atomic operations that crashed
mid-flight in a prior run. Currently:

- `*.__pixrename__` — intermediate from an interrupted case-only rename
  (see `pix.apply._apply_rename`). Revert to the original name so the
  next plan re-proposes the rename.

Phase 4 will extend this for `*.__migrate__.*` markers from interrupted
CONVERT / TAG operations (see spec/migrate.md → Marker cleanup).
"""

from __future__ import annotations

from pathlib import Path

_RENAME_SUFFIX: str = ".__pixrename__"


def cleanup_rename_orphans(folder: Path) -> list[Path]:
    """Revert any `*.__pixrename__` intermediates back to their original names.

    For each orphan:
    - If the original name slot is empty → rename intermediate to original.
    - If the original slot is occupied (the intermediate is a stale dup;
      original came back somehow) → delete the intermediate.

    Returns the list of original-target paths that were resolved (one per
    intermediate encountered).
    """
    resolved: list[Path] = []
    for path in folder.rglob(f"*{_RENAME_SUFFIX}"):
        if not path.is_file():
            continue
        # Skip pix's own state directory — these orphans only ever live in
        # the user's source folder.
        if any(part == ".pix" for part in path.parts):
            continue

        original_name = path.name[: -len(_RENAME_SUFFIX)]
        original = path.parent / original_name
        if original.exists():
            path.unlink()
        else:
            path.rename(original)
        resolved.append(original)
    return resolved
