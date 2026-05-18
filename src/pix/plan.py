"""Migration plan generation.

Walks the metadata cache, decides the action for each file (per the
extension policy and existing pix:* state), and emits a `Plan` that can be
serialized to the plan.txt format from spec/migrate.md.

Phase 2 scope: plan generation only. Apply, marker handling, content-hash
computation, AutoPrevious side-effects, and editor integration land in
later phases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from pix.config import Config, ExtensionAction
from pix.dates import (
    PIX_DATETIME_FORMAT,
    derive_date_auto,
    format_pix_datetime,
    parse_exiftool_datetime,
)
from pix.metadata import FileMetadata


# pix:* tag keys (group-prefixed, family-0, as exiftool reports them).
# We register `pix` as an XMP namespace in a later phase; for now this just
# reads existing fields if they're already on the file.
PIX_DATE_AUTO: str = "XMP:DateAuto"
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


# Width to which action labels are right-padded in plan.txt. Equals the
# longest label so columns line up.
_ACTION_WIDTH: int = max(len(a.value) for a in Action)


@dataclass(frozen=True)
class PlanLine:
    """One line of plan.txt — all operations on a single file."""

    line_id: str
    action: Action
    rel_path: str
    details: str
    is_first_migrate: bool = False  # True if `original_path init` is in details


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

        body = [
            (
                f"{ln.line_id} | "
                f"{ln.action.value.ljust(_ACTION_WIDTH)} | "
                f"{ln.rel_path.ljust(path_width)} | "
                f"{ln.details}"
            )
            for ln in self.lines
        ]

        counts = self.counts()
        summary_parts = [
            f"{counts[Action.CONVERT_RENAME_TAG]} CONVERT",
            f"{counts[Action.RENAME] + counts[Action.RENAME_TAG]} RENAME",
            f"{counts[Action.TAG] + counts[Action.RENAME_TAG] + counts[Action.CONVERT_RENAME_TAG]} TAG",
            f"{counts[Action.DELETE]} DELETE",
        ]
        summary = "# Summary: " + ", ".join(summary_parts)

        return "\n".join(header + body + ["", summary, ""])


# --- generation ---


def generate_plan(
    source: Path,
    cache: dict[Path, FileMetadata],
    config: Config,
    run_id: str,
    now: datetime | None = None,
) -> Plan:
    """Build a Plan from the metadata cache and extension policy."""
    generated_at = now or datetime.now()
    lines: list[PlanLine] = []

    for path in sorted(cache.keys()):
        meta = cache[path]
        line = _plan_one(path=path, meta=meta, source=source, config=config)
        if line is None:
            continue
        # Stamp line_id sequentially over lines we actually emit.
        lines.append(
            PlanLine(
                line_id=f"L{len(lines) + 1:03d}",
                action=line.action,
                rel_path=line.rel_path,
                details=line.details,
                is_first_migrate=line.is_first_migrate,
            )
        )

    return Plan(
        source=source,
        run_id=run_id,
        generated_at=generated_at,
        lines=lines,
    )


# Extension canonicalization, per spec/migrate.md.
_EXT_ALIASES: dict[str, str] = {
    "jpeg": "jpg",
}


def _canonical_extension(ext: str) -> str:
    lowered = ext.lower().lstrip(".")
    return _EXT_ALIASES.get(lowered, lowered)


def _action_for_policy(action: ExtensionAction) -> str | None:
    """Map an extension-policy action to the target canonical extension.

    Returns the new extension (for convert_to_*), or None for keep/delete.
    """
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

    policy = lookup_policy(path.name, config.extensions)
    if policy is None:
        # Shouldn't happen — caller validates extensions before plan-gen.
        return None

    if policy == "delete":
        return PlanLine(
            line_id="",
            action=Action.DELETE,
            rel_path=rel_str,
            details="extension policy: delete",
        )

    target_ext = _action_for_policy(policy)
    is_first_migrate = meta.get_str(PIX_ORIGINAL_PATH) is None

    if target_ext is not None:
        # CONVERT case — always implies +RENAME, always +TAG on first migrate.
        canonical_name = _canonical_filename(meta=meta, ext=target_ext)
        details_parts: list[str] = [f"→{canonical_name}"]
        if is_first_migrate:
            details_parts.append("original_path init")
            details_parts.append("content_hash compute")
        date_auto = derive_date_auto(meta)
        if date_auto is not None and is_first_migrate:
            details_parts.append(
                f"date_auto null→{format_pix_datetime(date_auto)}"
            )
        return PlanLine(
            line_id="",
            action=Action.CONVERT_RENAME_TAG,
            rel_path=rel_str,
            details="; ".join(details_parts),
            is_first_migrate=is_first_migrate,
        )

    # `keep` case: figure out what (if anything) needs to change.
    canonical_ext = _canonical_extension(path.suffix)
    canonical_name = _canonical_filename(meta=meta, ext=canonical_ext)
    needs_rename = canonical_name is not None and canonical_name != path.name

    details_parts: list[str] = []
    if needs_rename and canonical_name is not None:
        details_parts.append(f"→{canonical_name}")

    needs_tag = False
    if is_first_migrate:
        details_parts.append("original_path init")
        details_parts.append("content_hash compute")
        needs_tag = True
        date_auto = derive_date_auto(meta)
        if date_auto is not None:
            details_parts.append(
                f"date_auto null→{format_pix_datetime(date_auto)}"
            )
    elif meta.get_str(PIX_CONTENT_HASH) is None:
        # Previously-migrated file predating the hash feature.
        details_parts.append("content_hash compute")
        needs_tag = True

    if not needs_rename and not needs_tag:
        # File is already canonical; no plan line.
        return None

    if needs_rename and needs_tag:
        action = Action.RENAME_TAG
    elif needs_tag:
        action = Action.TAG
    else:
        action = Action.RENAME

    return PlanLine(
        line_id="",
        action=action,
        rel_path=rel_str,
        details="; ".join(details_parts),
        is_first_migrate=is_first_migrate,
    )


# --- canonical filename ---

# Override slot patterns: YYYY-MM-DD-HH:MM:SS with `*` allowed in any field.
_OVERRIDE_RE = re.compile(
    r"^(?P<Y>\*|\d{4})-(?P<M>\*|\d{2})-(?P<D>\*|\d{2})-"
    r"(?P<h>\*|\d{2}):(?P<m>\*|\d{2}):(?P<s>\*|\d{2})$"
)


def _apply_override(
    auto: datetime, override: str
) -> datetime | None:
    """Patch the `auto` datetime with non-`*` slots from `override`.

    Returns None if the override string doesn't parse.
    """
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
    """Compute the effective `date` for a file (auto patched by override).

    Reads `pix:DateAuto` and `pix:DateOverride` from the file's metadata if
    present; falls back to re-deriving `DateAuto` from EXIF/XMP/filename/etc.
    if not stored.
    """
    stored_auto = meta.get_str(PIX_DATE_AUTO)
    if stored_auto is not None:
        auto = parse_exiftool_datetime(stored_auto) or _parse_pix_datetime(
            stored_auto
        )
    else:
        auto = derive_date_auto(meta)

    if auto is None:
        return None

    override = meta.get_str(PIX_DATE_OVERRIDE)
    if override is None:
        return auto
    patched = _apply_override(auto, override)
    return patched if patched is not None else auto


def _parse_pix_datetime(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, PIX_DATETIME_FORMAT)
    except ValueError:
        return None


def _canonical_filename(meta: FileMetadata, ext: str) -> str | None:
    """Compute the canonical filename for a file given its target extension.

    Returns None if no effective date can be derived (file lands in
    null-date territory; spec says it goes into `null/` for date-based
    templates, but there's no canonical name we can compute).
    """
    effective = effective_date(meta)
    if effective is None:
        return None
    stem = effective.strftime("%Y-%m-%d_%H%M%S")
    return f"{stem}.{ext}"
