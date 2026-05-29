"""`pix checkout` — tag editing via folder-shuffle.

See spec/tag-editing.md for the full design. This module owns the
*core* of checkout (no CLI concerns — that's commands/checkout.py):

- The freeze guard (`ensure_no_open_checkout`) every other write-mode
  command calls: while `<library>/.pix/checkout/` exists, only
  `pix checkout --commit` / `--reset` may run.
- The on-disk snapshot (`Snapshot`, `read_snapshot`, `write_snapshot`)
  — the baseline commit diffs the shuffled workspace against. Keyed by
  NTFS file-ID so a shuffled link can be matched back to its library
  file regardless of where the user dragged it.
- Workspace materialization (`create_checkout`) — scoped hard links
  rendered by an organize-style template, plus the snapshot.
- Teardown (`discard`) — remove the workspace (the `--reset` action,
  and the cleanup `--commit` will reuse once it lands).

`--commit` (diff + apply tag writes) is not implemented yet.

Identity is by inode, not content hash, so checkout — unlike organize
— needs no `pix hash` prerequisite.
"""

from __future__ import annotations

import json
import os
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from pix.metadata import FileMetadata
from pix.organize import (
    Template,
    Token,
    compute_values,
    sanitize_folder_name,
)
from pix.plan import PIX_ORIGINAL_PATH, effective_date

CHECKOUT_DIRNAME: str = "checkout"
SNAPSHOT_FILENAME: str = "snapshot.json"


# --- Errors ------------------------------------------------------------------


class CheckoutError(Exception):
    """Base class for checkout-time problems."""


class CheckoutExists(CheckoutError):
    """Raised when `pix checkout <path> <template>` is run with one already open."""

    def __init__(self, checkout_path: Path) -> None:
        super().__init__(
            f"A checkout is already open at {checkout_path}. Run "
            f"`pix checkout --commit` or `pix checkout --reset` first."
        )


class CheckoutScopeError(CheckoutError):
    """Raised when `<path>` isn't a usable scope (inside .pix, or not a dir)."""


class CheckoutUnmigratedError(CheckoutError):
    """Raised when one or more files under the scope lack pix:OriginalPath."""

    def __init__(self, paths: list[Path]) -> None:
        super().__init__(
            f"{len(paths)} file(s) under the checkout scope lack "
            f"pix:OriginalPath. Run `pix migrate` on them first."
        )
        self.paths = paths


class CheckoutOpen(Exception):
    """Raised by the freeze guard when an op is blocked by an open checkout."""

    def __init__(self, snapshot: "Snapshot | None") -> None:
        if snapshot is not None:
            detail = (
                f" (template: {snapshot.template}, scope: {snapshot.scope}, "
                f"started {snapshot.created})"
            )
        else:
            detail = ""
        super().__init__(
            f"A checkout is open{detail}. Run `pix checkout --commit` or "
            f"`pix checkout --reset` before any other operation."
        )


# --- Paths -------------------------------------------------------------------


def checkout_dir(library_root: Path) -> Path:
    """The single checkout workspace, `<library>/.pix/checkout/`."""
    return library_root / ".pix" / CHECKOUT_DIRNAME


def _snapshot_path(library_root: Path) -> Path:
    return checkout_dir(library_root) / SNAPSHOT_FILENAME


def is_open(library_root: Path) -> bool:
    return checkout_dir(library_root).exists()


# --- Snapshot ----------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotLink:
    """One materialized link's baseline state at checkout time."""

    ino: str  # NTFS file-ID join key (st_dev + st_ino)
    library_path: str  # forward-slash absolute path of the source file
    values: dict[str, str | None]  # effective values of the template's tokens


@dataclass(frozen=True)
class Snapshot:
    """The `.pix/checkout/snapshot.json` baseline."""

    template: str
    scope: str
    created: str  # ISO-8601, seconds precision
    links: list[SnapshotLink]


def write_snapshot(library_root: Path, snapshot: Snapshot) -> None:
    """Serialize `snapshot` to `.pix/checkout/snapshot.json`."""
    payload = {
        "template": snapshot.template,
        "scope": snapshot.scope,
        "created": snapshot.created,
        "links": [
            {
                "ino": ln.ino,
                "library_path": ln.library_path,
                "values": ln.values,
            }
            for ln in snapshot.links
        ],
    }
    _snapshot_path(library_root).write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def read_snapshot(library_root: Path) -> Snapshot | None:
    """Parse the snapshot, or return None if missing/unreadable/malformed.

    Tolerant by design: the freeze guard reads this only to enrich its
    error message, and must never itself fail because the snapshot is
    absent or corrupt.
    """
    path = _snapshot_path(library_root)
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    raw = cast("dict[str, object]", loaded)
    try:
        links: list[SnapshotLink] = []
        for item in cast("list[object]", raw["links"]):
            if not isinstance(item, dict):
                continue
            d = cast("dict[str, object]", item)
            links.append(
                SnapshotLink(
                    ino=str(d["ino"]),
                    library_path=str(d["library_path"]),
                    values=cast("dict[str, str | None]", d["values"]),
                )
            )
        return Snapshot(
            template=str(raw["template"]),
            scope=str(raw["scope"]),
            created=str(raw["created"]),
            links=links,
        )
    except (KeyError, TypeError):
        return None


# --- Freeze guard ------------------------------------------------------------


def ensure_no_open_checkout(library_root: Path) -> None:
    """Refuse if a checkout is open (the library-wide freeze).

    Every write-mode command except `pix checkout --commit`/`--reset`
    calls this after resolving the root. See spec/tag-editing.md →
    The freeze.
    """
    if is_open(library_root):
        raise CheckoutOpen(read_snapshot(library_root))


# --- Identity ----------------------------------------------------------------


def file_id(st: os.stat_result) -> str:
    """Stable NTFS file-ID string from a stat result (`st_dev` + `st_ino`).

    Shared by every hard link to one file and stable across rename/move,
    so it's the join key between a shuffled workspace link and its
    snapshot record.
    """
    return f"0x{st.st_dev:x}_{st.st_ino:016x}"


# --- Template helpers --------------------------------------------------------


def template_token_names(template: Template) -> list[str]:
    """Distinct token names in the template, in first-seen order."""
    names: list[str] = []
    seen: set[str] = set()
    for level in template.levels:
        for seg in level.segments:
            if isinstance(seg, Token) and seg.name not in seen:
                seen.add(seg.name)
                names.append(seg.name)
    return names


def validate_checkout_template(template: Template) -> None:
    """Reject templates whose levels aren't a single bare tag.

    Commit reverses each folder name back into a tag value, so a level
    must be exactly one token with no literal text — `{year}/{event}` ✅,
    `{year}-archive/{event}` ❌. (The fuller organize grammar — literals,
    multiple tokens per level — isn't reversible and isn't supported in
    checkout.)
    """
    for level in template.levels:
        if len(level.segments) != 1 or not isinstance(level.segments[0], Token):
            raise CheckoutError(
                "checkout template levels must each be a single tag with no "
                "literal text (e.g. `{year}/{event}`). Multi-tag or literal-"
                "bearing levels aren't supported in checkout."
            )


def _level_token_names(template: Template) -> list[str]:
    """One token name per level, in order (assumes a validated template)."""
    return [
        seg.name
        for level in template.levels
        for seg in level.segments
        if isinstance(seg, Token)
    ]


def render_checkout_path(
    template: Template, values: dict[str, str | None]
) -> str:
    """Workspace-relative folder for a file (single-tag-per-level template).

    Uniform rule: render each level's value as a folder and **stop at the
    first level the file has no value for** — the file rests in whatever
    folder's been built so far, which is the workspace **root** when the
    very first level is missing (e.g. a no-event file in `{event}/{year}`).
    Because we stop at the first gap, a later level's folder never appears
    where it could be mistaken for an earlier token. See spec/tag-editing.md
    → Workspace layout.
    """
    parts: list[str] = []
    for name in _level_token_names(template):
        val = values.get(name)
        if val is None:
            break
        parts.append(sanitize_folder_name(val))
    return "/".join(parts)


# --- Materialization ---------------------------------------------------------


@dataclass
class _Candidate:
    library_path: Path
    target_rel: str  # forward-slash relative folder under the workspace
    bare: str  # canonical filename without collision suffix
    values: dict[str, str | None]


def _assign_workspace_names(
    candidates: list[_Candidate],
) -> dict[Path, str]:
    """Resolve per-target-folder filename collisions for the workspace.

    Cosmetic only — identity is by inode, not name — so the tiebreaker
    is library-path order (deterministic, and needs no content hash,
    which checkout deliberately doesn't require). First member keeps the
    bare name; the rest get `_001`, `_002`, … before the extension.
    """
    groups: dict[tuple[str, str], list[_Candidate]] = defaultdict(list)
    for c in candidates:
        groups[(c.target_rel, c.bare)].append(c)

    result: dict[Path, str] = {}
    for (_target_rel, bare), members in groups.items():
        members.sort(key=lambda c: str(c.library_path))
        result[members[0].library_path] = bare
        if len(members) == 1:
            continue
        stem, dot, ext = bare.rpartition(".")
        if not dot:  # no extension
            stem = bare
        for i, c in enumerate(members[1:], start=1):
            result[c.library_path] = (
                f"{stem}_{i:03d}{dot}{ext}" if dot else f"{bare}_{i:03d}"
            )
    return result


def create_checkout(
    *,
    library_root: Path,
    scope: Path,
    template: Template,
    cache: dict[Path, FileMetadata],
) -> int:
    """Materialize the checkout workspace + snapshot. Returns the link count.

    `cache` holds metadata for every file under `scope`. Raises
    `CheckoutUnmigratedError` if any lack `pix:OriginalPath`. On any
    failure the partial workspace is removed so the library is never
    left frozen by a half-built checkout.
    """
    unmigrated = [
        p for p, m in cache.items() if m.get_str(PIX_ORIGINAL_PATH) is None
    ]
    if unmigrated:
        raise CheckoutUnmigratedError(sorted(unmigrated)[:10])

    token_names = template_token_names(template)
    candidates: list[_Candidate] = []
    for path in sorted(cache.keys()):
        meta = cache[path]
        values = compute_values(meta)
        target_rel = render_checkout_path(template, values)
        date = effective_date(meta)
        ext = path.suffix.lower().lstrip(".") or "bin"
        bare = (
            f"{date.strftime('%Y-%m-%d_%H%M%S')}.{ext}"
            if date is not None
            else path.name
        )
        candidates.append(
            _Candidate(
                library_path=path,
                target_rel=target_rel,
                bare=bare,
                values={n: values.get(n) for n in token_names},
            )
        )

    final_names = _assign_workspace_names(candidates)

    cdir = checkout_dir(library_root)
    try:
        cdir.mkdir(parents=True)

        links: list[SnapshotLink] = []
        for cand in candidates:
            dest_folder = cdir / cand.target_rel
            dest_folder.mkdir(parents=True, exist_ok=True)
            dest = dest_folder / final_names[cand.library_path]
            os.link(cand.library_path, dest)
            st = cand.library_path.stat()
            links.append(
                SnapshotLink(
                    ino=file_id(st),
                    library_path=cand.library_path.as_posix(),
                    values=cand.values,
                )
            )

        write_snapshot(
            library_root,
            Snapshot(
                template=template.raw,
                scope=scope.as_posix(),
                created=datetime.now().isoformat(timespec="seconds"),
                links=links,
            ),
        )
    except BaseException:
        # Never leave a half-built workspace — it would freeze the
        # library with nothing to commit. Tear down and re-raise.
        shutil.rmtree(cdir, ignore_errors=True)
        raise

    return len(links)


def discard(library_root: Path) -> bool:
    """Remove the checkout workspace. Returns False if none was open.

    Hard links are just directory entries — removing them never touches
    the library files. This is the `--reset` action.
    """
    cdir = checkout_dir(library_root)
    if not cdir.exists():
        return False
    shutil.rmtree(cdir)
    return True
