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

import re
from dataclasses import dataclass, field
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


# Width to which action labels are right-padded in plan.txt.
_ACTION_WIDTH: int = max(len(a.value) for a in Action)


@dataclass(frozen=True)
class PlanLine:
    """One line of plan.txt — all operations on a single file.

    Plan line carries:
    - `details`: human-readable summary for the plan.txt the user reviews.
    - structured fields the apply loop consumes (`abs_path`,
      `target_filename`, `pix_writes`, the `needs_*` flags).

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
    needs_original_path: bool = False


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

    def to_text(
        self, annotations: dict[str, str] | None = None
    ) -> str:
        """Serialize to the plan.txt format.

        `annotations` maps line_id -> annotation suffix (e.g. `[14:32:01
        Started]`). Used during apply to update plan.txt in place.
        """
        annot = annotations or {}
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
            line = (
                f"{ln.line_id} | "
                f"{ln.action.value.ljust(_ACTION_WIDTH)} | "
                f"{ln.rel_path.ljust(path_width)} | "
                f"{ln.details}"
            )
            suffix = annot.get(ln.line_id)
            if suffix:
                line = f"{line}    {suffix}"
            body.append(line)

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
        lines.append(
            PlanLine(
                line_id=f"L{len(lines) + 1:03d}",
                action=line.action,
                rel_path=line.rel_path,
                details=line.details,
                abs_path=line.abs_path,
                is_first_migrate=line.is_first_migrate,
                target_filename=line.target_filename,
                pix_writes=line.pix_writes,
                needs_content_hash=line.needs_content_hash,
                needs_original_path=line.needs_original_path,
            )
        )

    return Plan(
        source=source,
        run_id=run_id,
        generated_at=generated_at,
        lines=lines,
    )


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
            abs_path=path,
        )

    target_ext = _action_for_policy(policy)
    is_first_migrate = meta.get_str(PIX_ORIGINAL_PATH) is None

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

    date_auto = derive_date_auto(meta)
    if date_auto is not None and is_first_migrate:
        formatted = format_pix_datetime(date_auto)
        details_parts.append(f"date_auto null→{formatted}")
        pix_writes[PIX_DATE_AUTO] = formatted

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
        needs_original_path=is_first_migrate,
    )


def _plan_keep(
    meta: FileMetadata,
    path: Path,
    rel_str: str,
    is_first_migrate: bool,
) -> PlanLine | None:
    canonical_ext = _canonical_extension(path.suffix)
    canonical_name = _canonical_filename(meta=meta, ext=canonical_ext)
    needs_rename = (
        canonical_name is not None and canonical_name != path.name
    )

    details_parts: list[str] = []
    pix_writes: dict[str, str] = {}
    if needs_rename and canonical_name is not None:
        details_parts.append(f"→{canonical_name}")

    needs_tag = False
    needs_hash = False
    needs_op = False
    if is_first_migrate:
        details_parts.append("original_path init")
        details_parts.append("content_hash compute")
        needs_tag = True
        needs_hash = True
        needs_op = True
        date_auto = derive_date_auto(meta)
        if date_auto is not None:
            formatted = format_pix_datetime(date_auto)
            details_parts.append(f"date_auto null→{formatted}")
            pix_writes[PIX_DATE_AUTO] = formatted
    elif meta.get_str(PIX_CONTENT_HASH) is None:
        details_parts.append("content_hash compute")
        needs_tag = True
        needs_hash = True

    if not needs_rename and not needs_tag:
        return None

    if needs_rename and needs_tag:
        action = Action.RENAME_TAG
    elif needs_tag:
        action = Action.TAG
    else:
        action = Action.RENAME

    # silence unused-var; `needs_op` is preserved for future readers
    _ = needs_op

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
        needs_original_path=is_first_migrate,
    )


# --- canonical filename + override math ---

_OVERRIDE_RE = re.compile(
    r"^(?P<Y>\*|\d{4})-(?P<M>\*|\d{2})-(?P<D>\*|\d{2})-"
    r"(?P<h>\*|\d{2}):(?P<m>\*|\d{2}):(?P<s>\*|\d{2})$"
)


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
    """Compute the canonical filename for a file given its target extension."""
    effective = effective_date(meta)
    if effective is None:
        return None
    stem = effective.strftime("%Y-%m-%d_%H%M%S")
    return f"{stem}.{ext}"
