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
from pix.metadata import FileMetadata


# pix:* tag keys (group-prefixed, family-0, as exiftool reports them).
# We register `pix` as an XMP namespace in a later phase; for now this just
# reads existing fields if they're already on the file.
PIX_DATE_AUTO: str = "XMP:DateAuto"
PIX_DATE_AUTO_PREVIOUS: str = "XMP:DateAutoPrevious"
PIX_DATE_OVERRIDE: str = "XMP:DateOverride"
PIX_ORIGINAL_PATH: str = "XMP:OriginalPath"
PIX_CONTENT_HASH: str = "XMP:ContentHash"
PIX_EVENT_AUTO: str = "XMP:EventAuto"
PIX_EVENT_OVERRIDE: str = "XMP:EventOverride"


class Action(str, Enum):
    """Top-level action label for a plan line.

    Compound labels (CONVERT+RENAME+TAG, RENAME+TAG) are first-class — the
    spec treats the bundle of operations on a file as a single atomic line.
    """

    DELETE = "DELETE"
    CONVERT_RENAME_TAG = "CONVERT+RENAME+TAG"
    RENAME_TAG = "RENAME+TAG"
    TAG = "TAG"
    RENAME = "RENAME"


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
    in `pix_writes`. The one exception is `pix:ContentHash` — the value
    requires a full-file scan that we deliberately defer to apply (so an
    aborted plan doesn't waste hashing time on thousands of files); when
    `needs_content_hash` is True, apply computes the hash on the
    appropriate file and writes it alongside the other `pix_writes`.

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
    needs_content_hash: bool = False

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
    """
    generated_at = now or datetime.now()
    lines: list[PlanLine] = []

    for path in sorted(cache.keys()):
        meta = cache[path]
        line = _plan_one(path=path, meta=meta, source=source, config=config)
        if line is None:
            continue
        lines.append(
            dataclasses.replace(line, line_id=f"L{len(lines) + 1:03d}")
        )

    lines = _resolve_collisions(cache, lines)
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

    if ln.action == Action.DELETE:
        capture_path = run_dir / base
    elif ln.action == Action.RENAME:
        if ln.target_filename is None:
            raise ValueError(
                f"{ln.line_id}: RENAME without target_filename"
            )
        target_path = ln.abs_path.parent / ln.target_filename
    elif ln.action == Action.TAG:
        sidecar_path = run_dir / f"{base}.xmp"
    elif ln.action == Action.RENAME_TAG:
        if ln.target_filename is None:
            raise ValueError(
                f"{ln.line_id}: RENAME+TAG without target_filename"
            )
        sidecar_path = run_dir / f"{base}.xmp"
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
        capture_path = run_dir / base
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

    occupied: set[Path] = set()
    updates: dict[Path, str] = {}  # src_path -> new target filename

    for dest, srcs in members_by_dest.items():
        if len(srcs) <= 1:
            occupied.add(dest)
            continue
        # Already-at-bare-name wins, then source-name ASC.
        srcs.sort(key=lambda p: (0 if p == dest else 1, p.name))
        occupied.add(dest)  # first member keeps the bare slot
        suffix_idx = 1
        new_targets: dict[Path, str] = {}
        for src_path in srcs[1:]:
            # Skip suffix values already taken (in case prior groups
            # claimed `_001` etc. at the same canonical destination).
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


_EXT_ALIASES: dict[str, str] = {
    "jpeg": "jpg",
}


def _canonical_extension(ext: str) -> str:
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


def _plan_one(
    path: Path, meta: FileMetadata, source: Path, config: Config
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

        target_ext = _action_for_policy(policy)
        original_path_value = meta.get_str(PIX_ORIGINAL_PATH)
        is_first_migrate = original_path_value is None
        debug.section("First-migrate detection")
        debug.log(
            f"  pix:OriginalPath: "
            f"{'(absent)' if is_first_migrate else repr(original_path_value)}"
        )
        debug.log(f"  First migrate: {'yes' if is_first_migrate else 'no'}")

        if target_ext is not None:
            return _plan_convert(
                meta=meta,
                path=path,
                rel_str=rel_str,
                target_ext=target_ext,
                is_first_migrate=is_first_migrate,
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
        details_parts.append("content_hash compute")
        pix_writes["XMP:OriginalPath"] = str(path)

    date_auto = derive_date_auto(meta)
    if date_auto is not None and is_first_migrate:
        formatted = format_pix_datetime(date_auto)
        details_parts.append(f"date_auto null→{formatted}")
        pix_writes[PIX_DATE_AUTO] = formatted

    debug.section("Decision")
    debug.log(f"  Action: CONVERT+RENAME+TAG (→ .{target_ext})")
    debug.log(f"  Target filename: {canonical_name or '<unknown>'}")
    debug.log(f"  pix:* writes: {pix_writes or '(none)'}")
    debug.log(
        f"  Content hash: compute (always written on CONVERT)"
    )

    return PlanLine(
        line_id="",
        action=Action.CONVERT_RENAME_TAG,
        rel_path=rel_str,
        details="; ".join(details_parts),
        abs_path=path,
        is_first_migrate=is_first_migrate,
        target_filename=canonical_name,
        pix_writes=pix_writes,
        needs_content_hash=True,
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

    # --- Effective date (auto + optional override) for canonical filename ---
    debug.section("Effective date")
    if new_auto is None:
        effective: datetime | None = None
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
    canonical_ext = _canonical_extension(path.suffix)
    canonical_name = (
        f"{effective.strftime('%Y-%m-%d_%H%M%S')}.{canonical_ext}"
        if effective is not None
        else None
    )
    needs_rename = canonical_name is not None and canonical_name != path.name

    debug.section("Rename check")
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
    needs_hash = False
    if is_first_migrate:
        details_parts.append("original_path init")
        details_parts.append("content_hash compute")
        pix_writes["XMP:OriginalPath"] = str(path)
        needs_tag = True
        needs_hash = True
        if new_auto is not None:
            formatted = format_pix_datetime(new_auto)
            details_parts.append(f"date_auto null→{formatted}")
            pix_writes[PIX_DATE_AUTO] = formatted
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
        if meta.get_str(PIX_CONTENT_HASH) is None:
            details_parts.append("content_hash compute")
            needs_tag = True
            needs_hash = True
            debug.log("  Hash check: pix:ContentHash absent — needs compute")
        else:
            debug.log(
                "  Hash check: pix:ContentHash present — no recompute needed"
            )

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
    debug.log(f"  Needs content hash: {needs_hash}")
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
        needs_content_hash=needs_hash,
    )


# --- canonical filename + override math ---

_OVERRIDE_RE = re.compile(
    r"^(?P<Y>\*|\d{4})-(?P<M>\*|\d{2})-(?P<D>\*|\d{2})-"
    r"(?P<h>\*|\d{2}):(?P<m>\*|\d{2}):(?P<s>\*|\d{2})$"
)


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
