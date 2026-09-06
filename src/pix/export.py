"""Export reconcile engine — desired set, target validation, plan.

`pix export` provisions a **delivery tier**: a curated, filtered copy of the
library at a separate path, kept current by a delta reconcile rather than a
wipe-and-redo (spec/export.md). This module is the pure part — it computes
what *should* be there, inspects what *is* there, and produces a plan. It
copies nothing; `pix.commands.export` applies.

Three rules shape everything here:

1. **Export only touches paths its own manifest records.** Every other file
   in the target is foreign: reported, never modified, never deleted. This
   is what makes REMOVE safe even when `path:` points somewhere unexpected.
2. **Unexplained drift stops the run.** A member that changed under us, or a
   foreign file, means pix can't tell a hand-curated target from a
   half-landed sync from a misconfigured path — so it describes and stops
   instead of guessing.
3. **Read-only w.r.t. the library.** The master is a pure source; nothing in
   here writes to it.

**Identity is the content hash**, which is metadata-invariant for JPEG/MP4
(`pix.content_hash`). A tag-only edit in the master — a re-rating, an event
rename that doesn't change the folder — therefore does *not* re-ship the
file. That's deliberate: it keeps sync traffic proportional to real content
change. The cost is that a delivered copy's embedded tags can lag the
master's; membership changes (which is what a rating edit usually causes)
are still picked up, because they change the desired set.
"""

from __future__ import annotations

import os
import shutil
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from pix.config import Distribution
from pix.content_hash import compute_content_hash
from pix.export_manifest import Manifest, Member
from pix.markers import EXPORT_TMP_SUFFIX
from pix.metadata import FileMetadata
from pix.organize import (
    Template,
    compute_values,
    render_target_folder,
    template_filters_out,
)

# Directories a sync client / NAS creates inside a delivery tree that are
# emphatically not ours and equally not drift — flagging them would stop
# every single run against a Synology target.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "@eaDir",  # Synology thumbnail/index sidecar folders
        "#recycle",  # Synology recycle bin
        ".SynologyWorkingDirectory",
        ".pix",  # in case someone points a distribution at a library
    }
)

# Same idea for files: OS/sync junk that appears on its own.
_SKIP_FILES: frozenset[str] = frozenset(
    {"desktop.ini", "thumbs.db", ".ds_store", "synofile_thumb_xl.jpg"}
)


class ExportError(Exception):
    """Export could not proceed."""


class MissingHashesError(ExportError):
    """Library files without a cached content hash.

    Export identifies members by content hash, so it can't reconcile
    without them. Same precedent as organize: refuse and point at
    `pix hash` rather than silently hashing a whole library mid-run.
    """

    def __init__(self, paths: list[Path]) -> None:
        self.paths = paths
        super().__init__(
            f"{len(paths)} file(s) have no cached content hash; "
            f"run `pix hash` first."
        )


class ExportAction(str, Enum):
    """What a plan line does to the delivery target."""

    COPY = "COPY"  # new member (or re-copy of one that went missing)
    REPLACE = "REPLACE"  # source content changed under an existing member
    MOVE = "MOVE"  # same content, new target relpath (template/tag churn)
    REMOVE = "REMOVE"  # member left the filter — plain delete, not staged


_ACTION_WIDTH: int = max(len(a.value) for a in ExportAction)

# Removals sort first so "what am I about to lose" is the top of plan.txt.
_ACTION_ORDER: dict[ExportAction, int] = {
    ExportAction.REMOVE: 0,
    ExportAction.MOVE: 1,
    ExportAction.REPLACE: 2,
    ExportAction.COPY: 3,
}


@dataclass(frozen=True)
class Source:
    """A library file selected for the delivery target."""

    path: Path
    content_hash: str
    size: int


@dataclass(frozen=True)
class ExportLine:
    """One line of an export plan.txt.

    Deleting the line in the editor vetoes just that action, exactly as in
    migrate/organize (`pix.editor.parse_kept_line_ids`).
    """

    line_id: str
    action: ExportAction
    rel_path: str  # target-relative, forward slashes
    details: str
    source_path: Path | None = None  # None for REMOVE
    content_hash: str | None = None
    from_rel_path: str | None = None  # MOVE only: where it is now
    size: int = 0


@dataclass(frozen=True)
class Drift:
    """Target state export can't explain from its own manifest."""

    modified: list[str] = field(default_factory=lambda: [])
    foreign: list[str] = field(default_factory=lambda: [])

    def __bool__(self) -> bool:
        return bool(self.modified or self.foreign)


@dataclass(frozen=True)
class ExportPlan:
    """A reconcile plan for one distribution."""

    distribution: str
    target: Path
    generated_at: datetime
    lines: list[ExportLine]
    in_sync: int  # members already correct — the ones we never touch
    adopted: int = 0  # members recovered by hash when the manifest was lost

    def counts(self) -> dict[ExportAction, int]:
        out: dict[ExportAction, int] = {a: 0 for a in ExportAction}
        for line in self.lines:
            out[line.action] += 1
        return out

    def bytes_to_write(self) -> int:
        return sum(
            ln.size
            for ln in self.lines
            if ln.action in (ExportAction.COPY, ExportAction.REPLACE)
        )

    def is_additive(self) -> bool:
        """True when nothing is removed or overwritten.

        The gate on `--no-prompt`-style automation: purely additive runs are
        safe to apply unattended.
        """
        return all(ln.action is ExportAction.COPY for ln in self.lines)

    def to_text(self) -> str:
        """Serialize to plan.txt — same shape as migrate/organize."""
        path_width = max((len(ln.rel_path) for ln in self.lines), default=10)
        header = [
            f"# Export plan: {self.distribution}",
            f"# Target: {self.target}",
            f"# Generated {self.generated_at.strftime('%Y-%m-%d %H:%M')}",
            f"# {self.in_sync} member(s) already in sync — not touched.",
        ]
        if self.adopted:
            header.append(
                f"# {self.adopted} existing file(s) adopted by content hash "
                f"(manifest was missing)."
            )
        header.extend(
            [
                "#",
                "# Delete a line to skip that action this run. "
                'Commented "#" lines are info only.',
                "# REMOVE deletes from the delivery target only — the "
                "library is never touched.",
                "# Format: L<line-id> | ACTION | path | details",
                "",
            ]
        )
        body = [
            f"{ln.line_id} | "
            f"{ln.action.value.ljust(_ACTION_WIDTH)} | "
            f"{ln.rel_path.ljust(path_width)} | "
            f"{ln.details}"
            for ln in self.lines
        ]
        return "\n".join([*header, *body, ""])


# --- Desired set -------------------------------------------------------------


def desired_members(
    library_files: list[Path],
    cache: dict[Path, FileMetadata],
    hashes: dict[Path, str | None],
    sizes: dict[Path, int],
    distribution: Distribution,
    template: Template,
) -> dict[str, Source]:
    """Compute `{target relpath -> source}` for one distribution.

    Selection is the distribution's `extensions` allowlist, then its
    `filter`, then any filters inside its template; excluded files simply
    don't appear (no `(filtered)` folder — that's organize's rule, not
    export's). Raises `MissingHashesError` if a selected file has no cached
    hash.

    The extension gate comes first and is cheap: it's what keeps `.insv`
    360° footage — which has no meaningful delivery rendition — out of a
    tree meant to be played by ordinary clients.
    """
    selected: list[tuple[str, Source]] = []
    missing: list[Path] = []
    for path in library_files:
        if path.suffix.lstrip(".").lower() not in distribution.extensions:
            continue
        meta = cache.get(path)
        if meta is None:
            continue
        values = compute_values(meta)
        if not distribution.filter.matches(values):
            continue
        if template_filters_out(template, values):
            continue
        content_hash = hashes.get(path)
        if content_hash is None:
            missing.append(path)
            continue
        folder = render_target_folder(template, values)
        rel = f"{folder}/{path.name}" if folder else path.name
        selected.append(
            (
                rel,
                Source(
                    path=path,
                    content_hash=content_hash,
                    size=sizes.get(path, 0),
                ),
            )
        )

    if missing:
        raise MissingHashesError(sorted(missing))

    return _resolve_collisions(selected)


def _resolve_collisions(
    selected: list[tuple[str, Source]],
) -> dict[str, Source]:
    """Give every source a unique target relpath.

    Two library files can land on one relpath when the template flattens
    folders that each held a same-named file. The tiebreak is **content
    hash order**, not library-path order: a file's hash is stable across
    organize moves, so the suffix a member gets doesn't churn just because
    the master rearranged.
    """
    by_rel: dict[str, list[Source]] = defaultdict(list)
    for rel, source in selected:
        by_rel[rel].append(source)

    out: dict[str, Source] = {}
    for rel in sorted(by_rel):
        members = sorted(by_rel[rel], key=lambda s: s.content_hash)
        out[rel] = members[0]
        for idx, source in enumerate(members[1:], start=1):
            candidate = _suffixed(rel, idx)
            while candidate in out or candidate in by_rel:
                idx += 1
                candidate = _suffixed(rel, idx)
            out[candidate] = source
    return out


def _suffixed(rel: str, idx: int) -> str:
    """`2023/a.jpg` + 1 -> `2023/a_001.jpg` (matches the library's style)."""
    head, _, name = rel.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    new_name = f"{stem}_{idx:03d}{'.' + ext if ext else ''}"
    return f"{head}/{new_name}" if head else new_name


# --- Target inspection -------------------------------------------------------


def scan_target(target: Path) -> dict[str, os.stat_result]:
    """Walk the delivery target: `{relpath -> stat}`, forward slashes.

    Sync-client and NAS artifacts (`@eaDir`, `#recycle`, `desktop.ini`, …)
    are skipped — they'd otherwise register as foreign and stop every run
    against a Synology target.
    """
    found: dict[str, os.stat_result] = {}
    if not target.is_dir():
        return found

    stack: list[tuple[Path, str]] = [(target, "")]
    while stack:
        folder, prefix = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            rel = f"{prefix}{name}"
            try:
                if entry.is_dir(follow_symlinks=False):
                    if name not in _SKIP_DIRS:
                        stack.append((Path(entry.path), f"{rel}/"))
                    continue
                if name.lower() in _SKIP_FILES:
                    continue
                found[rel] = entry.stat()
            except OSError:
                continue
    return found


def adopt(
    target: Path,
    actual: dict[str, os.stat_result],
    desired: dict[str, Source],
) -> Manifest:
    """Rebuild a manifest by hashing the target — the lost-manifest recovery.

    A naive full re-provision would be wrong here: export owns nothing it
    can't prove, so every existing file would stay foreign and the copies
    would land beside them as duplicates. Instead, hash each target file
    that sits exactly where a desired member belongs and adopt it when the
    content matches. Anything else stays foreign and surfaces as drift.

    Only runs when the manifest is missing, corrupt, or describes a
    different target path, so the cost is one-time.
    """
    members: dict[str, Member] = {}
    for rel, st in actual.items():
        source = desired.get(rel)
        if source is None:
            continue
        path = target / rel
        try:
            if compute_content_hash(path) != source.content_hash:
                continue
        except OSError:
            continue
        members[rel] = Member(
            source_hash=source.content_hash,
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
        )
    return Manifest(
        distribution="", target=str(target), members=members
    )


def classify(
    manifest_members: dict[str, Member],
    actual: dict[str, os.stat_result],
) -> tuple[set[str], set[str], Drift]:
    """Split the target into (present, missing, drift).

    - **present** — a member we own, still byte-identical to what we wrote.
    - **missing** — a member we own that's gone from the target.
    - **drift.modified** — a member we own that changed under us.
    - **drift.foreign** — a file we never wrote. Never touched.
    """
    present: set[str] = set()
    missing: set[str] = set()
    modified: list[str] = []
    for rel, member in manifest_members.items():
        st = actual.get(rel)
        if st is None:
            missing.add(rel)
        elif member.matches(st):
            present.add(rel)
        else:
            modified.append(rel)

    foreign = [rel for rel in actual if rel not in manifest_members]
    return present, missing, Drift(
        modified=sorted(modified), foreign=sorted(foreign)
    )


# --- Plan --------------------------------------------------------------------


def build_plan(
    distribution: str,
    target: Path,
    desired: dict[str, Source],
    manifest_members: dict[str, Member],
    present: set[str],
    missing: set[str],
    generated_at: datetime | None = None,
    adopted: int = 0,
) -> ExportPlan:
    """Diff desired-vs-owned into COPY / REPLACE / MOVE / REMOVE lines."""
    copies: dict[str, Source] = {}
    replaces: dict[str, Source] = {}
    removes: list[str] = []

    for rel, source in desired.items():
        member = manifest_members.get(rel)
        if member is None:
            copies[rel] = source
        elif member.source_hash != source.content_hash:
            replaces[rel] = source
        elif rel in missing:
            copies[rel] = source  # we own it, but it's gone — re-provision
        # else: in `present` and unchanged — never touched.

    for rel in manifest_members:
        if rel not in desired:
            removes.append(rel)

    moves = _pair_moves(copies, removes, manifest_members)

    lines: list[ExportLine] = []
    for rel, source, from_rel in moves:
        lines.append(
            ExportLine(
                line_id="",
                action=ExportAction.MOVE,
                rel_path=rel,
                details=f"from {from_rel}",
                source_path=source.path,
                content_hash=source.content_hash,
                from_rel_path=from_rel,
                size=source.size,
            )
        )
    for rel in sorted(removes):
        lines.append(
            ExportLine(
                line_id="",
                action=ExportAction.REMOVE,
                rel_path=rel,
                details="no longer selected by this distribution",
            )
        )
    for rel in sorted(replaces):
        source = replaces[rel]
        lines.append(
            ExportLine(
                line_id="",
                action=ExportAction.REPLACE,
                rel_path=rel,
                details=f"source content changed ({source.path.name})",
                source_path=source.path,
                content_hash=source.content_hash,
                size=source.size,
            )
        )
    for rel in sorted(copies):
        source = copies[rel]
        details = (
            "re-provision (missing from target)"
            if rel in missing
            else f"from {source.path.name}"
        )
        lines.append(
            ExportLine(
                line_id="",
                action=ExportAction.COPY,
                rel_path=rel,
                details=details,
                source_path=source.path,
                content_hash=source.content_hash,
                size=source.size,
            )
        )

    lines.sort(key=lambda ln: (_ACTION_ORDER[ln.action], ln.rel_path))
    numbered = [
        ExportLine(
            line_id=f"L{i:03d}",
            action=ln.action,
            rel_path=ln.rel_path,
            details=ln.details,
            source_path=ln.source_path,
            content_hash=ln.content_hash,
            from_rel_path=ln.from_rel_path,
            size=ln.size,
        )
        for i, ln in enumerate(lines, start=1)
    ]

    return ExportPlan(
        distribution=distribution,
        target=target,
        generated_at=generated_at or datetime.now(),
        lines=numbered,
        in_sync=len(present & set(desired)),
        adopted=adopted,
    )


def _pair_moves(
    copies: dict[str, Source],
    removes: list[str],
    manifest_members: dict[str, Member],
) -> list[tuple[str, Source, str]]:
    """Turn a REMOVE+COPY of identical content into one MOVE line.

    Path churn (an event renamed, a template changed) otherwise reads as
    "deleted 400 files, added 400 files", which is exactly the alarming
    shape a reviewer shouldn't have to decode. Pairing is only done when
    it's unambiguous — one removal and one copy sharing a content hash.
    Mutates `copies`/`removes` to consume what it pairs.

    Honest caveat: on a sync client a move within the tree still re-uploads
    (it's a delete+create to the client). MOVE saves re-reading the master
    and the local copy, not the upload.
    """
    removes_by_hash: dict[str, list[str]] = defaultdict(list)
    for rel in removes:
        removes_by_hash[manifest_members[rel].source_hash].append(rel)
    copies_by_hash: dict[str, list[str]] = defaultdict(list)
    for rel, source in copies.items():
        copies_by_hash[source.content_hash].append(rel)

    paired: list[tuple[str, Source, str]] = []
    for content_hash, to_rels in sorted(copies_by_hash.items()):
        from_rels = removes_by_hash.get(content_hash, [])
        if len(to_rels) != 1 or len(from_rels) != 1:
            continue
        to_rel, from_rel = to_rels[0], from_rels[0]
        paired.append((to_rel, copies[to_rel], from_rel))
        del copies[to_rel]
        removes.remove(from_rel)
    return paired


# --- Apply -------------------------------------------------------------------


@dataclass
class ApplyResult:
    """Outcome of applying an export plan."""

    completed: int = 0
    failed: int = 0
    pruned_folders: int = 0
    members: dict[str, Member] = field(default_factory=lambda: {})


def apply_plan(
    plan: ExportPlan,
    kept_line_ids: set[str],
    members: dict[str, Member],
    log: Callable[[str], None],
    on_progress: Callable[[int], None] | None = None,
) -> ApplyResult:
    """Execute an export plan against the delivery target.

    `members` is the manifest's member map; it's updated in place as each
    line lands, so the caller can persist it even after a failure — anything
    we copied but didn't record would look foreign (and stop) next run.

    A failed line is logged and the run continues: a network blip on one
    file shouldn't strand the other 500. The count comes back in
    `ApplyResult.failed` for the caller to surface and exit non-zero.

    Copies land on a `.__export__` temp first and are then renamed into
    place, so an interrupted copy never leaves a plausible-looking partial
    in the delivery tree — and because the name carries pix's marker infix,
    a sync client excluding `*.__*` won't upload the partial either.
    """
    result = ApplyResult(members=members)
    emptied: set[Path] = set()

    for line in plan.lines:
        if line.line_id not in kept_line_ids:
            continue
        dest = plan.target / line.rel_path
        try:
            if line.action is ExportAction.REMOVE:
                _remove(dest)
                members.pop(line.rel_path, None)
                emptied.add(dest.parent)
            elif line.action is ExportAction.MOVE:
                assert line.from_rel_path is not None
                origin = plan.target / line.from_rel_path
                _move_within_target(origin, dest, line.source_path)
                members.pop(line.from_rel_path, None)
                members[line.rel_path] = _member_for(dest, line)
                emptied.add(origin.parent)
            else:  # COPY / REPLACE
                assert line.source_path is not None
                _copy_into(line.source_path, dest)
                members[line.rel_path] = _member_for(dest, line)
            result.completed += 1
            log(f"{line.line_id} {line.action.value} {line.rel_path}")
        except OSError as exc:
            result.failed += 1
            log(f"{line.line_id} FAILED {line.action.value} {line.rel_path}: {exc}")
        if on_progress is not None:
            on_progress(1)

    result.pruned_folders = _prune_empty(emptied, plan.target)
    return result


def _member_for(dest: Path, line: ExportLine) -> Member:
    """Record what we just wrote, as written (size + mtime_ns)."""
    st = dest.stat()
    return Member(
        source_hash=line.content_hash or "",
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
    )


def _copy_into(source: Path, dest: Path) -> None:
    """Copy `source` to `dest` via a marker-named temp, then rename."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + EXPORT_TMP_SUFFIX)
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _move_within_target(
    origin: Path, dest: Path, source: Path | None
) -> None:
    """Relocate a member inside the target; re-copy if it isn't there."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(origin, dest)
    except OSError:
        if source is None:
            raise
        _copy_into(source, dest)


def _remove(dest: Path) -> None:
    """Delete a member from the target. Already gone is success.

    A plain delete, not a soft-delete: the conservation invariant protects
    the *master*, and a delivery mirror is regenerable by construction
    (spec/export.md → Provisioning).
    """
    try:
        dest.unlink()
    except FileNotFoundError:
        pass


def _prune_empty(folders: set[Path], target: Path) -> int:
    """Remove folders emptied by removals/moves, walking up to the target."""
    pruned = 0
    for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
        current = folder
        while current != target and target in current.parents:
            try:
                current.rmdir()
            except OSError:
                break
            pruned += 1
            current = current.parent
    return pruned
