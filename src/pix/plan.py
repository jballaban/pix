"""Migration plan generation.

Walks the metadata cache, decides the action for each file (per the
extension policy and existing pix:* state), and emits a `Plan` that can be
serialized to the plan.txt format from spec/migrate.md.

`PlanLine` carries both a human-readable `details` string (for the plan.txt
the user reviews) AND structured fields (`target_filename`, `pix_writes`,
the various `needs_*` flags) that the apply loop consumes directly. The
text is presentational; the structured data is the source of truth.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from pix import debug
from pix.config import Config, ExtensionAction
from pix.dates import (
    PIX_DATETIME_FORMAT,
    derive_date_auto,
    format_pix_datetime,
    last_derivation_source,
    parse_exiftool_datetime,
)
from pix.events import derive_event_auto
from pix.metadata import FileMetadata
from pix.progress import LiveProgress


# pix:* tag keys (group-prefixed, family-0, as exiftool reports them).
# We register `pix` as an XMP namespace in a later phase; for now this just
# reads existing fields if they're already on the file.
PIX_DATE_AUTO: str = "XMP:DateAuto"
PIX_DATE_AUTO_PREVIOUS: str = "XMP:DateAutoPrevious"
PIX_DATE_OVERRIDE: str = "XMP:DateOverride"
PIX_ORIGINAL_PATH: str = "XMP:OriginalPath"
PIX_EVENT_AUTO: str = "XMP:EventAuto"
PIX_EVENT_AUTO_PREVIOUS: str = "XMP:EventAutoPrevious"
PIX_EVENT_OVERRIDE: str = "XMP:EventOverride"


# Keep-policy extensions that are *not* renamed to the canonical date-based
# filename. Insta360 360 media (.insv video, .insp photo) is kept verbatim:
# a single recording's two lens files share a capture timestamp, so the
# canonical name would collide them and a content-hash tiebreaker would
# scramble lens identity — and Insta360 Studio pairs the lenses by their
# original `VID_<date>_<time>_<lens>_<seq>` filename. These files are still
# tagged (DateAuto/EventAuto/OriginalPath) and organized into folders by
# date; only the rename is suppressed. See spec/library.md → canonical
# filename, spec/migrate.md → name-preserving keep.
NAME_PRESERVING_KEEP: frozenset[str] = frozenset({"insv", "insp"})


class Action(str, Enum):
    """Top-level action label for a plan line.

    Compound labels (CONVERT+RENAME+TAG, RENAME+TAG) are first-class — the
    spec treats the bundle of operations on a file as a single atomic line.
    `MOVE` is organize-only; migrate's apply loop rejects it and vice versa.
    """

    DELETE = "DELETE"
    CONVERT_RENAME_TAG = "CONVERT+RENAME+TAG"
    RENAME_TAG = "RENAME+TAG"
    TAG = "TAG"
    RENAME = "RENAME"
    MOVE = "MOVE"
    DEDUP = "DEDUP"
    MERGE = "MERGE"
    STASH = "STASH"


# Width to which action labels are right-padded in plan.txt.
_ACTION_WIDTH: int = max(len(a.value) for a in Action)


@dataclass(frozen=True)
class PlanLine:
    """One line of plan.txt — all operations on a single file.

    Plan line carries:
    - `details`: human-readable summary for the plan.txt the user reviews.
    - structured fields the apply loop consumes directly.

    Apply is a pure executor: all paths it needs (target / sidecar /
    capture / staging / marker) are pre-computed by plan-gen, and all
    pix:* field values it writes (DateAuto, OriginalPath, ...) are already
    in `pix_writes`.

    User edits to `details` in the editor are ignored at apply time; only
    line deletions (removing whole lines) affect what gets applied.
    """

    line_id: str
    action: Action
    rel_path: str
    details: str
    abs_path: Path
    is_first_migrate: bool = False
    target_filename: str | None = None
    pix_writes: dict[str, str] = field(default_factory=lambda: {})

    # Pre-computed paths (filled in post-plan-gen via `_attach_paths`).
    # All absolute. None for actions that don't use the given path.
    target_path: Path | None = None
    sidecar_path: Path | None = None
    capture_path: Path | None = None
    staging_path: Path | None = None
    marker_path: Path | None = None


@dataclass(frozen=True)
class Plan:
    """A complete migration plan, ready to serialize."""

    source: Path
    run_id: str
    generated_at: datetime
    lines: list[PlanLine]

    def counts(self) -> dict[Action, int]:
        out: dict[Action, int] = {a: 0 for a in Action}
        for line in self.lines:
            out[line.action] += 1
        return out

    def first_migrate_count(self) -> int:
        return sum(1 for ln in self.lines if ln.is_first_migrate)

    def to_text(self) -> str:
        """Serialize to the plan.txt format."""
        path_width = max(
            (len(ln.rel_path) for ln in self.lines), default=10
        )

        header = [
            f"# Migration plan: {self.source}",
            f"# Generated {self.generated_at.strftime('%Y-%m-%d %H:%M')}",
            f"# Run ID: {self.run_id}",
        ]
        first_count = self.first_migrate_count()
        if first_count > 0:
            header.append(
                f"# {first_count} file"
                f"{'s' if first_count != 1 else ''} migrating for the first "
                f"time will have their source path stored in metadata."
            )
        header.extend(
            [
                "#",
                "# Delete a line to skip that file this run. "
                'Commented "#" lines are info only.',
                "# Format: L<line-id> | ACTION | path | details",
                "",
            ]
        )

        body: list[str] = []
        for ln in self.lines:
            body.append(
                f"{ln.line_id} | "
                f"{ln.action.value.ljust(_ACTION_WIDTH)} | "
                f"{ln.rel_path.ljust(path_width)} | "
                f"{ln.details}"
            )

        counts = self.counts()
        convert = counts[Action.CONVERT_RENAME_TAG]
        rename = counts[Action.RENAME] + counts[Action.RENAME_TAG]
        tag = counts[Action.TAG] + counts[Action.RENAME_TAG] + convert
        delete = counts[Action.DELETE]
        summary_parts: list[str] = []
        if convert:
            summary_parts.append(f"{convert} CONVERT")
        if rename:
            summary_parts.append(f"{rename} RENAME")
        if tag:
            summary_parts.append(f"{tag} TAG")
        if delete:
            summary_parts.append(f"{delete} DELETE")
        summary = "# Summary: " + (
            ", ".join(summary_parts) if summary_parts else "nothing to do"
        )

        return "\n".join(header + body + ["", summary, ""])


# --- generation ---


def generate_plan(
    source: Path,
    cache: dict[Path, FileMetadata],
    config: Config,
    run_id: str,
    run_dir: Path,
    staging_dir: Path,
    now: datetime | None = None,
) -> Plan:
    """Build a Plan from the metadata cache and extension policy.

    `run_dir` and `staging_dir` are the destinations for run-folder
    captures and staging conversions respectively. Plan-gen uses them to
    pre-compute every path apply will need, so apply is a pure executor.

    Videos are converted only when their *container* needs normalizing to
    MP4 (the `convert_to_mp4` extension policy); the codec is preserved by a
    lossless remux at apply time (see spec/migrate.md → Video handling). An
    already-`.mp4`/`.m4v` file is kept untouched regardless of its codec.
    """
    generated_at = now or datetime.now()
    lines: list[PlanLine] = []

    paths = sorted(cache.keys())
    run_dir.mkdir(parents=True, exist_ok=True)
    plan_log_path = run_dir / "plan.log"
    with (
        plan_log_path.open("a", encoding="utf-8") as plan_log,
        LiveProgress(total=len(paths)) as progress,
    ):
        for path in paths:
            # Console gets the % style line with the per-file path
            # (rewritten in place via \r); the verbose per-file
            # decision goes to plan.log so the console stays terse.
            progress.begin("planning", str(path))
            meta = cache[path]
            line = _plan_one(
                path=path,
                meta=meta,
                source=source,
                config=config,
            )
            ts = datetime.now().isoformat(timespec="seconds")
            if line is not None:
                lines.append(
                    dataclasses.replace(
                        line, line_id=f"L{len(lines) + 1:03d}"
                    )
                )
                plan_log.write(
                    f"{ts} {path} -> {lines[-1].line_id} "
                    f"{lines[-1].action.value}\n"
                )
            else:
                plan_log.write(f"{ts} {path} -> (skip)\n")
            plan_log.flush()
            progress.advance()

    lines = _resolve_collisions(cache, lines)
    lines = _drop_noop_renames(lines)
    lines = [attach_paths(ln, run_dir, staging_dir) for ln in lines]

    return Plan(
        source=source,
        run_id=run_id,
        generated_at=generated_at,
        lines=lines,
    )


def attach_paths(
    ln: PlanLine, run_dir: Path, staging_dir: Path
) -> PlanLine:
    """Pre-compute every path apply will need for this plan line.

    Apply consumes these directly — no path derivation at apply time.
    """
    sidecar_path: Path | None = None
    capture_path: Path | None = None
    target_path: Path | None = None
    staging_path: Path | None = None
    marker_path: Path | None = None

    base = f"{ln.line_id}_{ln.abs_path.name}"
    data_dir = run_dir / "data"

    if ln.action == Action.DELETE:
        capture_path = data_dir / base
    elif ln.action == Action.STASH:
        # Opaque filename: <run-id>_<line-id>.<source-ext>. Globally
        # unique by construction (run-id is a timestamp); no
        # collision logic. Stash lives at <library>/.pix/stash/;
        # derive library_root from run_dir (`runs/<id>/` parent
        # chain: runs/ -> .pix/ -> library).
        library_root = run_dir.parent.parent.parent
        run_id = run_dir.name
        from pix.stash import stash_filename
        target_path = (
            library_root
            / ".pix"
            / "stash"
            / stash_filename(run_id, ln.line_id, ln.abs_path)
        )
    elif ln.action == Action.RENAME:
        if ln.target_filename is None:
            raise ValueError(
                f"{ln.line_id}: RENAME without target_filename"
            )
        target_path = ln.abs_path.parent / ln.target_filename
    elif ln.action == Action.TAG:
        sidecar_path = data_dir / f"{base}.xmp"
    elif ln.action == Action.RENAME_TAG:
        if ln.target_filename is None:
            raise ValueError(
                f"{ln.line_id}: RENAME+TAG without target_filename"
            )
        sidecar_path = data_dir / f"{base}.xmp"
        target_path = ln.abs_path.parent / ln.target_filename
    elif ln.action == Action.CONVERT_RENAME_TAG:
        if ln.target_filename is None:
            raise ValueError(
                f"{ln.line_id}: CONVERT without target_filename"
            )
        target_ext = ln.target_filename.rsplit(".", 1)[-1].lower()
        staging_path = staging_dir / f"{ln.line_id}_{ln.abs_path.stem}.{target_ext}"
        marker_path = (
            ln.abs_path.parent / f"{ln.abs_path.name}.__migrate__.{target_ext}"
        )
        capture_path = data_dir / base
        target_path = ln.abs_path.parent / ln.target_filename

    return dataclasses.replace(
        ln,
        target_path=target_path,
        sidecar_path=sidecar_path,
        capture_path=capture_path,
        staging_path=staging_path,
        marker_path=marker_path,
    )


def _resolve_collisions(
    cache: dict[Path, FileMetadata], lines: list[PlanLine]
) -> list[PlanLine]:
    """Apply `_NNN` suffixes when multiple files map to the same canonical name.

    Per spec/library.md → Collision handling. The first file in a colliding
    group keeps the bare name; the rest get `_001`, `_002`, ... suffixes
    inserted before the extension.

    Tiebreaker for Phase 3: files already at the bare canonical name win it
    unconditionally; other files in the group sort by source filename
    ascending. (Spec's content-hash tiebreaker lands in Phase 5; for now
    source-filename is a stable proxy.)
    """
    line_by_src: dict[Path, PlanLine] = {ln.abs_path: ln for ln in lines}

    members_by_dest: dict[Path, list[Path]] = {}
    for src_path in cache.keys():
        ln = line_by_src.get(src_path)
        if ln is not None and ln.target_filename is not None:
            dest = src_path.parent / ln.target_filename
        else:
            dest = src_path
        members_by_dest.setdefault(dest, []).append(src_path)

    # Pre-pass: sort each group's members (so the primary is first), then
    # populate `occupied` with every group's primary slot. Without this
    # the original single-loop walk would only learn about a primary slot
    # when its group was reached in dict iteration order — overflow
    # assignment in an earlier group could land on a primary slot of a
    # later group, producing a plan where two files claim the same path.
    occupied: set[Path] = set()
    for dest, srcs in members_by_dest.items():
        # Already-at-bare-name wins, then source-name ASC.
        srcs.sort(key=lambda p: (0 if p == dest else 1, p.name))
        occupied.add(dest)

    updates: dict[Path, str] = {}  # src_path -> new target filename

    for dest, srcs in members_by_dest.items():
        if len(srcs) <= 1:
            continue
        # srcs is already sorted from the pre-pass.
        suffix_idx = 1
        new_targets: dict[Path, str] = {}
        for src_path in srcs[1:]:
            # Skip suffix values already taken — covers both other
            # collision groups' overflow assignments AND any non-renaming
            # cached file whose current path happens to be a suffixed
            # canonical name (its `dest` was its current path, added to
            # `occupied` in the pre-pass).
            while True:
                stem = dest.stem
                ext = dest.suffix
                suffixed_name = f"{stem}_{suffix_idx:03d}{ext}"
                suffixed_path = src_path.parent / suffixed_name
                if suffixed_path not in occupied:
                    break
                suffix_idx += 1
            occupied.add(suffixed_path)
            updates[src_path] = suffixed_name
            new_targets[src_path] = suffixed_name
            suffix_idx += 1

        # Annotate every member's debug log with the collision context.
        for src_path in srcs:
            with debug.for_file(src_path):
                debug.section("Collision resolution")
                debug.log(f"  Canonical destination: {dest.name}")
                debug.log(f"  Competing members ({len(srcs)}):")
                for member in srcs:
                    marker = " <-- this file" if member == src_path else ""
                    debug.log(f"    {member.name}{marker}")
                if src_path == srcs[0]:
                    debug.log("  Result: keeps bare canonical name.")
                else:
                    debug.log(
                        f"  Result: gets suffix → {new_targets[src_path]}"
                    )

    if not updates:
        return lines

    result: list[PlanLine] = []
    for ln in lines:
        new_target = updates.get(ln.abs_path)
        if new_target is None:
            result.append(ln)
            continue
        old_target = ln.target_filename or ""
        new_details = (
            ln.details.replace(f"→{old_target}", f"→{new_target}")
            if old_target
            else ln.details
        )
        result.append(
            dataclasses.replace(
                ln, target_filename=new_target, details=new_details
            )
        )
    return result


def _drop_noop_renames(lines: list[PlanLine]) -> list[PlanLine]:
    """Drop RENAME / demote RENAME+TAG lines whose target == current name.

    Collision resolution gives the bare canonical slot to one file and
    assigns suffixes `_001`, `_002`, … to the others in sort order. When
    a colliding folder already contains the bare-name file plus
    `_001.mp4`, `_002.mp4`, …, the assigned suffixes happen to match each
    file's current suffix — the resulting "rename" would move every file
    onto itself.

    - RENAME with target == source name: drop entirely.
    - RENAME+TAG with target == source name: demote to TAG (the tag
      writes still need to happen) and strip the `→<name>` arrow from
      the details column.

    CONVERT is unaffected: a convert always crosses extensions, so its
    target filename can't equal the source name.
    """
    result: list[PlanLine] = []
    for ln in lines:
        if ln.target_filename != ln.abs_path.name:
            result.append(ln)
            continue
        if ln.action == Action.RENAME:
            with debug.for_file(ln.abs_path):
                debug.section("No-op rename")
                debug.log(
                    "  Collision-resolved target matches current "
                    "filename — dropping plan line."
                )
            continue
        if ln.action == Action.RENAME_TAG:
            new_details = _strip_rename_from_details(
                ln.details, ln.target_filename
            )
            with debug.for_file(ln.abs_path):
                debug.section("No-op rename")
                debug.log(
                    "  Collision-resolved target matches current "
                    "filename — demoting RENAME+TAG to TAG."
                )
            result.append(
                dataclasses.replace(
                    ln,
                    action=Action.TAG,
                    target_filename=None,
                    details=new_details,
                )
            )
            continue
        result.append(ln)
    return result


def _strip_rename_from_details(details: str, target: str | None) -> str:
    """Remove the `→<target>` segment from a `; `-joined details string."""
    if target is None:
        return details
    arrow = f"→{target}"
    parts = [
        p
        for p in (s.strip() for s in details.split(";"))
        if p and p != arrow
    ]
    return "; ".join(parts)


_EXT_ALIASES: dict[str, str] = {
    "jpeg": "jpg",
    "m4v": "mp4",
}


def canonical_extension(ext: str) -> str:
    lowered = ext.lower().lstrip(".")
    return _EXT_ALIASES.get(lowered, lowered)


def _action_for_policy(action: ExtensionAction) -> str | None:
    """Map an extension-policy action to the target canonical extension."""
    if action == "convert_to_jpg":
        return "jpg"
    if action == "convert_to_mp4":
        return "mp4"
    return None


def lookup_policy(
    filename: str, extensions: dict[str, ExtensionAction]
) -> ExtensionAction | None:
    """Return the policy action for a filename, or None if unknown.

    Lookup priority:
    1. Full filename, lowercased, with any leading dot stripped (e.g.
       `.DS_Store` → `ds_store`; `Thumbs.db` → `thumbs.db`).
    2. Extension only, lowercased (e.g. `IMG_001.HEIC` → `heic`).
    """
    name_lower = filename.lower().lstrip(".")
    if name_lower in extensions:
        return extensions[name_lower]
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext and ext in extensions:
        return extensions[ext]
    return None


def is_insta360_lrv(filename: str) -> bool:
    """True if `filename` is an Insta360 LRV low-res proxy.

    Insta360 cameras write a disposable `LRV_*.insv` low-resolution proxy
    alongside each full-res `VID_*.insv` recording (used for fast preview/
    scrubbing in the app). The proxy carries no archival value and the app
    regenerates it on demand, so migrate deletes them rather than keeping
    them. Built-in Insta360 knowledge, not a config extension policy.
    """
    name_lower = filename.lower()
    return name_lower.startswith("lrv_") and name_lower.endswith(".insv")


def _plan_one(
    path: Path,
    meta: FileMetadata,
    source: Path,
    config: Config,
) -> PlanLine | None:
    """Decide the plan line for one file, or return None if no action is needed."""
    rel = path.relative_to(source)
    rel_str = str(rel)

    with debug.for_file(path):
        policy = lookup_policy(path.name, config.extensions)
        debug.section("Extension policy")
        debug.log(f"  Lookup key: {path.name.lower()}")
        debug.log(f"  Action: {policy or '(no match — unreachable)'}")

        if policy is None:
            debug.section("Decision")
            debug.log("  No policy match — no action.")
            return None

        if policy == "delete":
            debug.section("Decision")
            debug.log("  DELETE per extension policy.")
            return PlanLine(
                line_id="",
                action=Action.DELETE,
                rel_path=rel_str,
                details="extension policy: delete",
                abs_path=path,
            )

        if policy == "stash":
            debug.section("Decision")
            debug.log("  STASH per extension policy.")
            return PlanLine(
                line_id="",
                action=Action.STASH,
                rel_path=rel_str,
                details="extension policy: stash",
                abs_path=path,
            )

        target_ext = _action_for_policy(policy)
        original_path_value = meta.get_str(PIX_ORIGINAL_PATH)
        is_first_migrate = original_path_value is None
        debug.section("First-migrate detection")
        debug.log(
            f"  pix:OriginalPath: "
            f"{'(absent)' if is_first_migrate else repr(original_path_value)}"
        )
        debug.log(f"  First migrate: {'yes' if is_first_migrate else 'no'}")

        # Videos converge by *container* only: a `convert_to_mp4`-policy
        # source (.mov/.avi/.mts/.mpg/.mpeg/.vob) is losslessly remuxed into
        # MP4 at apply time, preserving the codec. An already-`.mp4`/`.m4v`
        # file is kept untouched regardless of codec — no re-encode, ever.
        # (This reverses the former HEVC codec-convergence; see
        # spec/migrate.md → Video handling.)
        if target_ext is not None:
            return _plan_convert(
                meta=meta,
                path=path,
                rel_str=rel_str,
                target_ext=target_ext,
                is_first_migrate=is_first_migrate,
            )

        # Built-in Insta360 rule: LRV_*.insv are disposable low-res proxies
        # the camera writes beside the full-res VID_*.insv. They regenerate
        # and carry no archival value, so delete rather than keep. Only
        # reachable on keep-policy .insv (target_ext is None here).
        if is_insta360_lrv(path.name):
            debug.section("Decision")
            debug.log("  DELETE — Insta360 LRV low-res proxy.")
            return PlanLine(
                line_id="",
                action=Action.DELETE,
                rel_path=rel_str,
                details="Insta360 LRV low-res proxy",
                abs_path=path,
            )

        return _plan_keep(
            meta=meta,
            path=path,
            rel_str=rel_str,
            is_first_migrate=is_first_migrate,
        )


def _plan_convert(
    meta: FileMetadata,
    path: Path,
    rel_str: str,
    target_ext: str,
    is_first_migrate: bool,
) -> PlanLine:
    canonical_name = _canonical_filename(meta=meta, ext=target_ext)
    details_parts: list[str] = [
        f"→{canonical_name}" if canonical_name else "→<unknown-date>"
    ]
    pix_writes: dict[str, str] = {}

    if is_first_migrate:
        details_parts.append("original_path init")
        pix_writes["XMP:OriginalPath"] = str(path)

    date_auto = derive_date_auto(meta)
    if date_auto is not None and is_first_migrate:
        formatted = format_pix_datetime(date_auto)
        details_parts.append(f"date_auto null→{formatted}")
        pix_writes[PIX_DATE_AUTO] = formatted

    if is_first_migrate:
        event_auto = derive_event_auto(meta)
        if event_auto is not None:
            details_parts.append(f"event_auto null→{event_auto}")
            pix_writes[PIX_EVENT_AUTO] = event_auto

    debug.section("Decision")
    debug.log(f"  Action: CONVERT+RENAME+TAG (→ .{target_ext})")
    debug.log(f"  Target filename: {canonical_name or '<unknown>'}")
    debug.log(f"  pix:* writes: {pix_writes or '(none)'}")

    return PlanLine(
        line_id="",
        action=Action.CONVERT_RENAME_TAG,
        rel_path=rel_str,
        details="; ".join(details_parts),
        abs_path=path,
        is_first_migrate=is_first_migrate,
        target_filename=canonical_name,
        pix_writes=pix_writes,
    )


def _plan_keep(
    meta: FileMetadata,
    path: Path,
    rel_str: str,
    is_first_migrate: bool,
) -> PlanLine | None:
    # --- DateAuto drift check ---
    # Per spec/tags.md → DateAuto derivation, the candidate list is
    # re-consulted on every migrate. Improving heuristics can change the
    # derived value relative to what's stored; we detect that drift and
    # schedule a TAG write to bring the stored value up to date.
    stored_raw = meta.get_str(PIX_DATE_AUTO)
    stored_auto: datetime | None = None
    if stored_raw:
        stored_auto = parse_exiftool_datetime(
            stored_raw
        ) or _parse_pix_datetime(stored_raw)

    re_derived = derive_date_auto(meta)
    re_derived_source = last_derivation_source()

    # The DateAuto value we'll have after this migrate: prefer the re-derived
    # value; fall back to stored only if re-derivation now returns nothing
    # (don't lose a previously-stored value if heuristics regress).
    new_auto = re_derived if re_derived is not None else stored_auto

    debug.section("DateAuto drift check")
    debug.log(f"  Stored pix:DateAuto: {stored_raw or '(absent)'}")
    if re_derived is not None:
        debug.log(
            f"  Re-derived:          {re_derived.isoformat()}  "
            f"(source: {re_derived_source})"
        )
    else:
        debug.log("  Re-derived:          (none)")

    needs_date_auto_write = False
    if not is_first_migrate and new_auto is not None:
        if stored_auto is None:
            needs_date_auto_write = True
            debug.log(
                "  Drift: stored DateAuto absent — writing re-derived value."
            )
        elif stored_auto != new_auto:
            needs_date_auto_write = True
            debug.log(
                f"  Drift: stored {stored_auto.isoformat()} differs from "
                f"re-derived {new_auto.isoformat()} — writing new value."
            )
        else:
            debug.log("  No drift (stored DateAuto matches re-derived).")
    elif new_auto is None and stored_raw:
        debug.log("  Re-derive returned nothing; keeping stored DateAuto.")

    # AutoPrevious: per spec/tags.md → Auto-previous fields, if the auto
    # value is actually changing AND a DateOverride is pinning some
    # component, capture the prior auto value as a dirty flag for future
    # "review drift" workflows. Spec rationale: when there's no override,
    # the auto change is already visible (filename + canonical name shift);
    # when there IS an override, the change is invisibly masked, which is
    # exactly what we want to flag.
    needs_date_auto_previous = False
    if (
        needs_date_auto_write
        and stored_raw
        and stored_auto is not None
        and _override_has_pinning(meta.get_str(PIX_DATE_OVERRIDE))
    ):
        needs_date_auto_previous = True
        debug.log(
            f"  pix:DateOverride is pinning at least one component — "
            f"writing pix:DateAutoPrevious = {stored_raw!r} (drift flag)."
        )

    # --- EventAuto drift check ---
    # Same pattern as DateAuto: re-derive on every migrate; write when
    # stored differs from re-derived (or stored is absent and we have a
    # value); flag drift via EventAutoPrevious when EventOverride is set.
    stored_event = meta.get_str(PIX_EVENT_AUTO)
    re_derived_event = derive_event_auto(meta)
    new_event = (
        re_derived_event if re_derived_event is not None else stored_event
    )

    needs_event_auto_write = False
    if not is_first_migrate and new_event is not None:
        if stored_event is None:
            needs_event_auto_write = True
        elif stored_event != new_event:
            needs_event_auto_write = True

    needs_event_auto_previous = False
    if (
        needs_event_auto_write
        and stored_event
        and meta.get_str(PIX_EVENT_OVERRIDE)
    ):
        needs_event_auto_previous = True

    # --- Effective date (auto + optional override) for canonical filename ---
    debug.section("Effective date")
    if new_auto is None:
        # No auto base — but a user override pinning a year (with missing
        # parts defaulted) still yields an effective date and a canonical
        # name. See spec/tags.md.
        synth = _date_from_override_only(meta.get_str(PIX_DATE_OVERRIDE))
        effective: datetime | None = synth
        if synth is not None:
            debug.log(
                f"  No DateAuto; effective synthesized from override: "
                f"{synth.isoformat()}"
            )
        else:
            debug.log("  Effective date: (none — no auto available)")
    else:
        override = meta.get_str(PIX_DATE_OVERRIDE)
        if override is None:
            effective = new_auto
            debug.log("  pix:DateOverride: (absent)")
            debug.log(
                f"  Effective date: {new_auto.isoformat()} (auto only)"
            )
        else:
            patched = _apply_override(new_auto, override)
            if patched is None:
                effective = new_auto
                debug.log(
                    f"  pix:DateOverride: {override!r} (unparseable, ignored)"
                )
                debug.log(
                    f"  Effective date: {new_auto.isoformat()} (auto only)"
                )
            else:
                effective = patched
                debug.log(f"  pix:DateOverride: {override!r}")
                debug.log(
                    f"  Effective date: {patched.isoformat()} (override-patched)"
                )

    # --- Canonical filename ---
    # Name-preserving keep formats (Insta360 .insv/.insp) keep their
    # original camera filename — never renamed to the canonical date-based
    # name. See NAME_PRESERVING_KEEP for the rationale (lens-pair identity
    # lives in the filename). Tagging below still proceeds.
    ext = path.suffix.lower().lstrip(".")
    debug.section("Rename check")
    if ext in NAME_PRESERVING_KEEP:
        canonical_name: str | None = None
        needs_rename = False
        debug.log(f"  Name-preserving keep (.{ext}) — rename suppressed.")
        debug.log(f"  Current filename:    {path.name}")
    else:
        canonical_ext = canonical_extension(path.suffix)
        canonical_name = (
            f"{effective.strftime('%Y-%m-%d_%H%M%S')}.{canonical_ext}"
            if effective is not None
            else None
        )
        needs_rename = (
            canonical_name is not None and canonical_name != path.name
        )
        debug.log(f"  Canonical extension: .{canonical_ext}")
        debug.log(f"  Canonical filename:  {canonical_name or '<no date>'}")
        debug.log(f"  Current filename:    {path.name}")
        debug.log(f"  Needs rename: {needs_rename}")

    # --- Decide ---
    details_parts: list[str] = []
    pix_writes: dict[str, str] = {}
    if needs_rename and canonical_name is not None:
        details_parts.append(f"→{canonical_name}")

    needs_tag = False
    if is_first_migrate:
        details_parts.append("original_path init")
        pix_writes["XMP:OriginalPath"] = str(path)
        needs_tag = True
        if new_auto is not None:
            formatted = format_pix_datetime(new_auto)
            details_parts.append(f"date_auto null→{formatted}")
            pix_writes[PIX_DATE_AUTO] = formatted
        if re_derived_event is not None:
            details_parts.append(f"event_auto null→{re_derived_event}")
            pix_writes[PIX_EVENT_AUTO] = re_derived_event
    else:
        if needs_date_auto_write and new_auto is not None:
            formatted = format_pix_datetime(new_auto)
            stored_display = stored_raw or "null"
            details_parts.append(f"date_auto {stored_display}→{formatted}")
            pix_writes[PIX_DATE_AUTO] = formatted
            needs_tag = True
        if needs_date_auto_previous and stored_raw:
            pix_writes[PIX_DATE_AUTO_PREVIOUS] = stored_raw
            details_parts.append(f"date_auto_previous→{stored_raw}")
            needs_tag = True
        if needs_event_auto_write and new_event is not None:
            stored_display = stored_event or "null"
            details_parts.append(f"event_auto {stored_display}→{new_event}")
            pix_writes[PIX_EVENT_AUTO] = new_event
            needs_tag = True
        if needs_event_auto_previous and stored_event:
            pix_writes[PIX_EVENT_AUTO_PREVIOUS] = stored_event
            details_parts.append(f"event_auto_previous→{stored_event}")
            needs_tag = True

    debug.section("Decision")
    if not needs_rename and not needs_tag:
        debug.log("  No action — file already canonical with all expected tags.")
        return None

    if needs_rename and needs_tag:
        action = Action.RENAME_TAG
    elif needs_tag:
        action = Action.TAG
    else:
        action = Action.RENAME

    debug.log(f"  Action: {action.value}")
    debug.log(
        f"  Target filename: "
        f"{canonical_name if needs_rename else '(no rename)'}"
    )
    debug.log(f"  pix:* writes: {pix_writes or '(none)'}")
    debug.log(f"  Needs OriginalPath: {is_first_migrate}")

    return PlanLine(
        line_id="",
        action=action,
        rel_path=rel_str,
        details="; ".join(details_parts),
        abs_path=path,
        is_first_migrate=is_first_migrate,
        target_filename=canonical_name if needs_rename else None,
        pix_writes=pix_writes,
    )


# --- canonical filename + override math ---

_OVERRIDE_RE = re.compile(
    r"^(?P<Y>\*|\d{4})-(?P<M>\*|\d{2})-(?P<D>\*|\d{2})-"
    r"(?P<h>\*|\d{2}):(?P<m>\*|\d{2}):(?P<s>\*|\d{2})$"
)


def valid_date_override(value: str) -> bool:
    """True if `value` is a well-formed `DateOverride` pattern
    (`YYYY-MM-DD-HH:MM:SS`, any component may be `*`). Used by `pix set`
    to validate a date override before writing it."""
    return _OVERRIDE_RE.match(value) is not None


def _override_has_pinning(override: str | None) -> bool:
    """True if `override` actually pins at least one date component.

    A `DateOverride` of all `*` slots (e.g. `*-*-*-*:*:*`) is equivalent to
    no override and should never be stored (tag-editing clears it). This
    helper is defensive: if such a string IS on disk, treat it as
    "no pinning" so we don't flag drift as masked.
    """
    if not override:
        return False
    # Any digit means at least one slot has a real value.
    return any(c.isdigit() for c in override)


def _apply_override(auto: datetime, override: str) -> datetime | None:
    """Patch the `auto` datetime with non-`*` slots from `override`."""
    m = _OVERRIDE_RE.match(override)
    if m is None:
        return None
    parts = m.groupdict()

    def pick(key: str, fallback: int) -> int:
        v = parts[key]
        return fallback if v == "*" else int(v)

    try:
        return datetime(
            year=pick("Y", auto.year),
            month=pick("M", auto.month),
            day=pick("D", auto.day),
            hour=pick("h", auto.hour),
            minute=pick("m", auto.minute),
            second=pick("s", auto.second),
        )
    except ValueError:
        return None


def _date_from_override_only(override: str | None) -> datetime | None:
    """Synthesize an effective date from `DateOverride` alone (no DateAuto).

    Used when a file has no `pix:DateAuto` (un-dated) but the user pinned
    date components via tag-editing. A **year is required** as the anchor;
    any unspecified lower field defaults to its minimum (month/day → 01,
    time → 00:00:00). Without a year there's nothing to anchor, so the
    effective date stays null. The stored override is unchanged — only
    what the user actually set is persisted; the defaults are applied
    here at read time.
    """
    if not override:
        return None
    m = _OVERRIDE_RE.match(override)
    if m is None:
        return None
    parts = m.groupdict()
    if parts["Y"] == "*":
        return None  # no year anchor

    def pick(key: str, default: int) -> int:
        v = parts[key]
        return default if v == "*" else int(v)

    try:
        return datetime(
            year=int(parts["Y"]),
            month=pick("M", 1),
            day=pick("D", 1),
            hour=pick("h", 0),
            minute=pick("m", 0),
            second=pick("s", 0),
        )
    except ValueError:
        return None


def effective_date(meta: FileMetadata) -> datetime | None:
    debug.section("Effective date")
    stored_auto = meta.get_str(PIX_DATE_AUTO)
    if stored_auto is not None:
        auto = parse_exiftool_datetime(stored_auto) or _parse_pix_datetime(
            stored_auto
        )
        debug.log(
            f"  pix:DateAuto (stored): {stored_auto!r} -> "
            f"{auto.isoformat() if auto else 'unparseable'}"
        )
    else:
        debug.log("  pix:DateAuto: (absent) — re-deriving")
        auto = derive_date_auto(meta)

    if auto is None:
        # No auto base. If the user pinned date components via an
        # override, synthesize from it (year required; missing parts
        # default to their minimum). See spec/tags.md.
        synth = _date_from_override_only(meta.get_str(PIX_DATE_OVERRIDE))
        if synth is not None:
            debug.log(
                f"  pix:DateAuto absent; synthesized from override "
                f"(missing parts defaulted): {synth.isoformat()}"
            )
            return synth
        debug.log("  Effective date: (none)")
        return None

    override = meta.get_str(PIX_DATE_OVERRIDE)
    if override is None:
        debug.log("  pix:DateOverride: (absent)")
        debug.log(f"  Effective date: {auto.isoformat()} (auto, no override)")
        return auto
    patched = _apply_override(auto, override)
    if patched is None:
        debug.log(
            f"  pix:DateOverride: {override!r} (unparseable, ignored)"
        )
        debug.log(f"  Effective date: {auto.isoformat()} (auto)")
        return auto
    debug.log(f"  pix:DateOverride: {override!r}")
    debug.log(
        f"  Effective date: {patched.isoformat()} (auto + override)"
    )
    return patched


def _parse_pix_datetime(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, PIX_DATETIME_FORMAT)
    except ValueError:
        return None


def _canonical_filename(meta: FileMetadata, ext: str) -> str | None:
    """Compute the canonical filename for a file given its target extension."""
    effective = effective_date(meta)
    if effective is None:
        return None
    stem = effective.strftime("%Y-%m-%d_%H%M%S")
    return f"{stem}.{ext}"
