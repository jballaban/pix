"""Staging sidecars — the pending half of the skip manifest (spec/nas-app.md §9).

`import` lands files under `IMPORT_ROOT/<name>/<relative-path>` and records each
one with an `.importinfo` sidecar in the folder's `.manifest/` child. The sidecar,
**not the media**, is the durable skip record: deleting media to cull it leaves
the record intact, so a culled file is never re-imported, while deleting a whole
staging folder is the deliberate "redo this batch" gesture.

Nothing here writes `.xmp`. That is a different artifact entirely — curation
decisions, written by the app, living in master permanently. These two share
only the word "sidecar" (spec/nas-app.md §4).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

from pix.importer import sanitize_component
from pix.ingest import MANIFEST_DIRNAME
from pix.markers import IMPORT_TMP_SUFFIX

#: Extension for the staging skip record. Matches the device importer's, since
#: the two staging areas are structurally the same thing.
SIDECAR_EXT: str = ".importinfo"

#: Non-media companions a folder import never lands. `.aae` is Apple's
#: edit-instruction sidecar, meaningless without Photos.
SKIP_EXTENSIONS: frozenset[str] = frozenset({".aae"})


def is_bare_dotfile(name: str) -> bool:
    """True for a leading-dot name with no extension (`.nomedia`, `.DS_Store`).

    Device and OS metadata, never user media. A dotfile *with* a real extension
    (`.hidden.jpg`) is not bare — it could be media — and still lands.
    """
    return name.startswith(".") and Path(name).suffix == ""


def is_skippable(name: str) -> bool:
    """True for a filename a folder import should not land at all."""
    return is_bare_dotfile(name) or Path(name).suffix.lower() in SKIP_EXTENSIONS


def source_tag(source: Path) -> str:
    """Flatten an absolute source path into one safe path component.

    `G:\\pix\\2001` becomes `g_pix_2001`. Staging nests each import under its
    source tag, which does two things at once:

    - **No physical collision.** Importing `G:\\pix\\2001` and `G:\\pix\\2022` under
      one name would otherwise drop both `Australia Hockey/` trees into the same
      place.
    - **No key collision.** The skip key is `(rel, size)` with `rel` relative to
      the *staging* root, so including the tag makes it unique across sources.
      Without it, `Australia Hockey/x.jpg` from two different years is one key,
      and the second import would be silently skipped as already-seen.

    It also means [upload](upload.py)'s flattening produces a fully self-describing
    master filename: `g_pix_2001_Australia Hockey_x.jpg`.
    """
    text = str(Path(source).resolve())
    for sep in (os.sep, os.altsep or "/", ":"):
        text = text.replace(sep, "\x00")
    parts = [p for p in text.split("\x00") if p and p not in (".", "..")]
    tag = "_".join(sanitize_component(p) for p in parts if p)
    return tag or "source"


def sidecar_path(landed: Path) -> Path:
    """The `.importinfo` for `landed`, in its folder's `.manifest/` child.

    Deliberately *not* beside the media, so culling media never deletes the
    skip record.
    """
    return landed.parent / MANIFEST_DIRNAME / (landed.name + SIDECAR_EXT)


def write_sidecar(landed: Path, *, name: str, source_root: Path, rel: str,
                  size: int, mtime_ns: int) -> None:
    """Record `landed` as imported, temp-then-rename.

    The atomic rename matters: a process killed mid-write must never leave a
    truncated sidecar that still parses, because its mere presence is what marks
    the file as imported.
    """
    target = sidecar_path(landed)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "source": "folder",
        "name": name,
        "source_root": str(source_root),
        "rel": rel,
        "size": size,
        "mtime_ns": mtime_ns,
    }
    tmp = target.with_name(target.name + IMPORT_TMP_SUFFIX)
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def read_sidecar(path: Path) -> dict[str, Any] | None:
    """Parse one `.importinfo`, or None if it is missing or unreadable."""
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return cast("dict[str, Any]", parsed)


def skip_key(rel: str, size: int) -> tuple[str, int]:
    """Folder-source identity: `(staging-relative path, size)`.

    A folder has no PUID, and path alone is unsafe because a source tree can be
    edited between runs. Size makes the pair stable enough to be idempotent
    while staying free to compute.

    `rel` is relative to the **staging root**, so it carries the
    `source_tag` prefix — that is what keeps two source trees from colliding on
    one key. See `source_tag`.
    """
    return (rel.replace("\\", "/").lower(), size)


def scan_manifest(staging: Path) -> set[tuple[str, int]]:
    """Rebuild the pending skip-set from the `.importinfo` sidecars on disk.

    Recomputed per run rather than cached: the sidecars *are* the record, and a
    cache of them could only drift.
    """
    manifest: set[tuple[str, int]] = set()
    if not staging.is_dir():
        return manifest
    for sidecar in staging.rglob(f"*{SIDECAR_EXT}"):
        data = read_sidecar(sidecar)
        if data is None:
            continue
        rel = data.get("rel")
        size = data.get("size")
        if isinstance(rel, str) and isinstance(size, int):
            manifest.add(skip_key(rel, size))
    return manifest


def sweep_temps(staging: Path) -> int:
    """Delete partial writes left by an interrupted run. Returns the count."""
    if not staging.is_dir():
        return 0
    removed = 0
    for tmp in staging.rglob(f"*{IMPORT_TMP_SUFFIX}"):
        try:
            tmp.unlink()
            removed += 1
        except OSError:
            pass
    return removed
