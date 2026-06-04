"""`pix dedupe` — remove duplicate files sharing the same content hash.

See spec/dedupe.md for the full design. Hashes are read from the per-
file cache at `.pix/cache/.../<filename>.hash` (populated by
`pix hash`; see spec/hash.md). Dedupe is a pure consumer — it never
computes hashes itself.

This module owns:

- Prerequisite checks (`require_migrated_with_hashes`) — every library
  file must have `pix:OriginalPath` and a valid cached content hash.
- Hash grouping (`group_by_hash`) — index files by cached hash,
  yield only groups of 2+.
- Keeper selection (`select_keeper`) — lex-smallest path (the tag merge
  preserves investment regardless of which file survives).
- Tag merge (`_compute_keeper_merge`) — assembles the best value of each
  tag across the group onto the keeper (earliest date → pix:MergeDate;
  fill-empty event/overrides). See spec/dedupe.md → Tag merge.
- Plan generation (`generate_plan`) — produces a `DedupeResult` with the
  underlying `Plan` (DEDUP line per loser + one MERGE line per keeper
  that gains tags) and the groups for display.
- Plan serialization (`serialize_plan`) — grouped plan.txt format with
  comment headers + `# WARNING` lines per group.
- Apply (`apply_plan`) — write MERGE bundles onto keepers (ExifTool),
  rename each loser into `runs/<run-id>/data/`, then bottom-up
  empty-folder cleanup. MERGE runs before DEDUP for crash safety.

CWD constraint and empty-folder cleanup come from `pix.organize` —
the same checks apply (we'd otherwise fail to clean folders that
become empty after dups go to `data/`).
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO

from pix.dates import (
    PIX_MERGE_DATE,
    derive_date_auto,
    format_pix_datetime,
)
from pix.duration import format_duration_compact, format_size
from pix.events import (
    PIX_EVENT_AUTO,
    PIX_EVENT_OVERRIDE,
    PIX_MERGE_EVENT,
    derive_event_auto,
)
from pix.errors import move_to_errors
from pix.exiftool_session import ExifToolSession, TagWriteFailed
from pix.metadata import FileMetadata
from pix.organize import cleanup_empty_folders  # reused
from pix.timeout import safe_rename
from pix.plan import (
    PIX_DATE_AUTO,
    PIX_DATE_AUTO_PREVIOUS,
    PIX_DATE_OVERRIDE,
    PIX_ORIGINAL_PATH,
    Action,
    Plan,
    PlanLine,
)
from pix.progress import LiveProgress
from pix.telemetry import LineRecord, write_summary
from pix.video_fingerprint import VideoFingerprint, fingerprint_distance


# Video extensions deduped by *perceptual fingerprint* rather than exact
# content hash — the re-encodable canonical containers, where two encodes
# of one source differ byte-wise but match perceptually (the cross-encoder
# gap from the GPU/CPU hybrid). Everything else (images, name-preserving
# .insv/.insp 360 media) stays on the exact-hash path.
_VIDEO_DEDUPE_EXTS: frozenset[str] = frozenset({"mp4", "mov", "m4v"})

# Two videos can only be duplicates if their durations match within this
# tolerance (a re-encode preserves duration to within container rounding /
# a frame). Used as a cheap pre-filter before the fingerprint compare.
_DUR_TOL: float = 0.75

# Default perceptual-distance band (Hamming bits over the frame-hash set).
# 0..30 is the range confirmed by visual review on the real library to be
# all true duplicates; the gray zone begins above it. Overridable per run
# via --min/--max for manual curation of higher bands.
DEFAULT_MIN_DISTANCE: int = 0
DEFAULT_MAX_DISTANCE: int = 30


def is_dedupe_video(path: Path) -> bool:
    """True if `path` is deduped by perceptual fingerprint (not exact hash)."""
    return path.suffix.lower().lstrip(".") in _VIDEO_DEDUPE_EXTS


# --- Errors ------------------------------------------------------------------


class DedupeError(Exception):
    """Base for any dedupe-time problem."""


class UnmigratedFilesError(DedupeError):
    """Raised when one or more files lack `pix:OriginalPath`."""

    def __init__(self, paths: list[Path]) -> None:
        super().__init__(
            f"{len(paths)} file(s) in the library lack pix:OriginalPath. "
            f"Run `pix migrate <library-root>` first."
        )
        self.paths = paths


class MissingHashesError(DedupeError):
    """Raised when one or more files lack a cached content hash."""

    def __init__(self, paths: list[Path]) -> None:
        super().__init__(
            f"{len(paths)} file(s) in the library lack a cached content "
            f"hash. Run `pix hash <library-root>` first."
        )
        self.paths = paths


class DedupeApplyError(Exception):
    """Raised when a DEDUP rename fails during apply."""


# --- Data structures --------------------------------------------------------


@dataclass(frozen=True)
class DedupeGroup:
    """One set of files sharing a `pix:ContentHash`.

    `keeper_writes` is the pix:* field bundle the merge consolidates onto
    the keeper (empty when the keeper already holds the best of every
    tag). `merge_notes` are the per-field human summaries for the MERGE
    plan line's details column; `merge_warnings` are the fill-empty
    divergence notes surfaced as `# WARNING` comments in plan.txt.
    """

    content_hash: str
    keeper: Path  # absolute
    losers: tuple[Path, ...]  # absolute, in plan-line order (lex)
    keeper_writes: dict[str, str] = field(default_factory=lambda: {})
    merge_notes: tuple[str, ...] = ()
    merge_warnings: tuple[str, ...] = ()
    # How the group was matched. "exact" → identical `content_hash` (images
    # and non-video). "perceptual" → video fingerprint within the band;
    # `content_hash` is empty and `distance` is the largest pairwise
    # fingerprint distance inside the group (for the plan/details display).
    kind: str = "exact"
    distance: int = 0


@dataclass(frozen=True)
class DedupeResult:
    """Output of plan-gen.

    `plan` carries one PlanLine (action=DEDUP) per loser; that's what
    apply consumes. `groups` is the same info reshaped for serializing
    the grouped plan.txt format with per-group comment headers.
    """

    plan: Plan
    groups: tuple[DedupeGroup, ...]


# --- Keeper rule ------------------------------------------------------------


def _sort_key(path: Path, library_root: Path) -> str:
    """Library-relative, forward-slash, case-insensitive comparison key."""
    try:
        rel = path.relative_to(library_root)
    except ValueError:
        rel = path
    return rel.as_posix().lower()


def select_keeper(
    library_root: Path,
    members: list[tuple[Path, FileMetadata]],
) -> Path:
    """Lex-smallest library-relative path. Per spec/dedupe.md → Keeper
    selection: no investment tier — the tag merge consolidates every
    file's investment onto whichever file survives, so the keeper is just
    a deterministic survivor."""
    return min((p for p, _ in members), key=lambda p: _sort_key(p, library_root))


# --- Tag merge --------------------------------------------------------------


def _date_override_value(meta: FileMetadata) -> str | None:
    """The file's DateOverride if it actually pins a component, else None.

    An all-`*` override is equivalent to absent (a digit anywhere means at
    least one slot is pinned) — matches `pix.plan._override_has_pinning`.
    """
    v = meta.get_str(PIX_DATE_OVERRIDE)
    if v and any(c.isdigit() for c in v):
        return v
    return None


def _event_override_value(meta: FileMetadata) -> str | None:
    """The file's EventOverride if set (truthy), else None."""
    v = meta.get_str(PIX_EVENT_OVERRIDE)
    return v if v else None


def _compute_keeper_merge(
    keeper: Path,
    members: list[tuple[Path, FileMetadata]],
    library_root: Path,
) -> tuple[dict[str, str], tuple[str, ...], tuple[str, ...]]:
    """Assemble the best value of each tag across the group onto the keeper.

    Returns `(writes, notes, warnings)`:
    - `writes`: pix:* field → value, already filtered to fields whose new
      value differs from the keeper's current one (so re-runs that find
      the keeper already consolidated produce no writes → no MERGE line).
    - `notes`: per-field human summaries for the plan's details column.
    - `warnings`: fill-empty divergence notes for `# WARNING` comments.

    See spec/dedupe.md → Tag merge for the per-field rules.
    """
    keeper_meta = next(m for p, m in members if p == keeper)
    writes: dict[str, str] = {}
    notes: list[str] = []
    warnings: list[str] = []

    # --- date: earliest resolved auto across the group -> pix:MergeDate ---
    dated: list[tuple[Path, datetime]] = []
    for p, m in members:
        dt = derive_date_auto(m)
        if dt is not None:
            dated.append((p, dt))
    if dated:
        keeper_dt = derive_date_auto(keeper_meta)
        min_dt = min(dt for _, dt in dated)
        if keeper_dt is None or min_dt < keeper_dt:
            src = min(
                (p for p, dt in dated if dt == min_dt),
                key=lambda p: _sort_key(p, library_root),
            )
            formatted = format_pix_datetime(min_dt)
            writes[PIX_MERGE_DATE] = formatted
            writes[PIX_DATE_AUTO] = formatted
            # Dirty flag, mirroring migrate: if this MergeDate change moves
            # DateAuto while a pinning DateOverride is present, record the
            # prior auto as DateAutoPrevious (see spec/tags.md).
            stored_auto = keeper_meta.get_str(PIX_DATE_AUTO)
            if (
                stored_auto
                and stored_auto != formatted
                and _date_override_value(keeper_meta) is not None
            ):
                writes[PIX_DATE_AUTO_PREVIOUS] = stored_auto
            notes.append(
                f"date_auto →{formatted} "
                f"(merge ←{_rel_or_abs(src, library_root)})"
            )

    # --- event: fill-empty -> pix:MergeEvent ---
    if derive_event_auto(keeper_meta) is None:
        value, src, distinct = _fill_empty(
            keeper, members, library_root, derive_event_auto
        )
        if value is not None and src is not None:
            writes[PIX_MERGE_EVENT] = value
            writes[PIX_EVENT_AUTO] = value
            notes.append(
                f"event_auto →{value!r} "
                f"(merge ←{_rel_or_abs(src, library_root)})"
            )
            if len(distinct) >= 2:
                warnings.append(
                    f"event_auto: losers diverge {sorted(distinct)} "
                    f"— took {value!r}"
                )

    # --- overrides: fill-empty into the real override slot ---
    for label, field_key, getter in (
        ("date_override", PIX_DATE_OVERRIDE, _date_override_value),
        ("event_override", PIX_EVENT_OVERRIDE, _event_override_value),
    ):
        if getter(keeper_meta) is not None:
            continue  # keep the keeper's own user intent
        value, src, distinct = _fill_empty(
            keeper, members, library_root, getter
        )
        if value is not None and src is not None:
            writes[field_key] = value
            notes.append(
                f"{label} →{value} (merge ←{_rel_or_abs(src, library_root)})"
            )
            if len(distinct) >= 2:
                warnings.append(
                    f"{label}: losers diverge {sorted(distinct)} "
                    f"— took {value}"
                )

    # Drop writes that equal the keeper's current value — keeps the merge
    # idempotent (a re-plan of an already-consolidated keeper emits nothing).
    writes = {k: v for k, v in writes.items() if keeper_meta.get_str(k) != v}
    return writes, tuple(notes), tuple(warnings)


def _fill_empty(
    keeper: Path,
    members: list[tuple[Path, FileMetadata]],
    library_root: Path,
    getter: Callable[[FileMetadata], str | None],
) -> tuple[str | None, Path | None, set[str]]:
    """Pick a loser's value for a fill-empty field: lex-smallest contributor.

    Returns `(value, source_path, distinct_values)`. `value`/`source` are
    None when no loser contributes. `distinct_values` lets the caller warn
    on divergence (≥2 distinct contributed values).
    """
    contributors: list[tuple[Path, str]] = []
    for p, m in members:
        if p == keeper:
            continue
        v = getter(m)
        if v is not None:
            contributors.append((p, v))
    if not contributors:
        return None, None, set()
    contributors.sort(key=lambda pv: _sort_key(pv[0], library_root))
    src, value = contributors[0]
    distinct = {v for _, v in contributors}
    return value, src, distinct


# --- Prerequisites ----------------------------------------------------------


def require_migrated_with_hashes(
    cache: dict[Path, FileMetadata],
    hashes: dict[Path, str | None],
) -> None:
    """Refuse if any file lacks pix:OriginalPath or a cached content hash.

    Both checks consume already-computed inputs — the cache (from the
    metadata bulk read) and the hashes (from one parallel pass over
    `hash_cache.read_all_cached_hashes`). No per-file syscalls here.
    """
    unmigrated = [
        p for p, m in cache.items() if m.get_str(PIX_ORIGINAL_PATH) is None
    ]
    if unmigrated:
        raise UnmigratedFilesError(sorted(unmigrated))

    no_hash = [p for p in cache if hashes.get(p) is None]
    if no_hash:
        raise MissingHashesError(sorted(no_hash))


# --- Grouping ---------------------------------------------------------------


def group_by_hash(
    library_root: Path,
    cache: dict[Path, FileMetadata],
    hashes: dict[Path, str | None],
) -> list[DedupeGroup]:
    """Group files by cached content hash; yield only groups of 2+.

    Consumes the precomputed `hashes` dict (built once via
    `read_all_cached_hashes`) instead of re-reading per file.
    """
    by_hash: dict[str, list[tuple[Path, FileMetadata]]] = defaultdict(list)
    for path, meta in cache.items():
        h = hashes.get(path)
        if h is None:
            continue  # require_migrated_with_hashes should have caught
        by_hash[h].append((path, meta))

    groups: list[DedupeGroup] = []
    for content_hash, members in by_hash.items():
        if len(members) < 2:
            continue
        keeper = select_keeper(library_root, members)
        losers_sorted = sorted(
            (p for p, _ in members if p != keeper),
            key=lambda p: _sort_key(p, library_root),
        )
        keeper_writes, merge_notes, merge_warnings = _compute_keeper_merge(
            keeper, members, library_root
        )
        groups.append(
            DedupeGroup(
                content_hash=content_hash,
                keeper=keeper,
                losers=tuple(losers_sorted),
                keeper_writes=keeper_writes,
                merge_notes=merge_notes,
                merge_warnings=merge_warnings,
            )
        )

    # Sort groups by keeper path so plan ordering is stable.
    groups.sort(key=lambda g: _sort_key(g.keeper, library_root))
    return groups


def select_video_keeper(
    library_root: Path,
    paths: list[Path],
    fingerprints: dict[Path, VideoFingerprint],
) -> Path:
    """Pick the best copy to keep from a perceptual video group.

    Unlike exact dedupe (where every member is byte-identical, so the
    keeper is an arbitrary deterministic survivor), perceptual matches
    differ in quality — so we keep the best by: highest resolution →
    highest bitrate (size ÷ duration, a proxy for fidelity / fewest
    re-encode generations) → longest duration (completeness) → lex-smallest
    path (stable tie-break). See spec/dedupe.md → Keeper selection."""
    def rank(p: Path) -> tuple[int, float, float, str]:
        fp = fingerprints[p]
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        bitrate = size / fp.duration if fp.duration > 0 else 0.0
        return (
            -(fp.width * fp.height),   # higher resolution first
            -bitrate,                  # higher bitrate first
            -fp.duration,              # longer first
            _sort_key(p, library_root),
        )
    return min(paths, key=rank)


def group_by_fingerprint(
    library_root: Path,
    cache: dict[Path, FileMetadata],
    fingerprints: Mapping[Path, VideoFingerprint | None],
    min_distance: int,
    max_distance: int,
) -> list[DedupeGroup]:
    """Group videos by perceptual fingerprint within `[min,max]` distance.

    Two videos are candidates only if they share resolution and durations
    within `_DUR_TOL` (cheap pre-filter); among those, an edge is drawn when
    their fingerprint distance is in `[min_distance, max_distance]`.
    Connected components of 2+ become groups. Keeper is the best copy
    (`select_video_keeper`); the rest are losers. Files with no usable
    fingerprint are skipped (never grouped → never deleted).
    """
    valid: dict[Path, VideoFingerprint] = {
        p: fp for p, fp in fingerprints.items()
        if fp is not None and p in cache
    }

    parent: dict[Path, Path] = {}

    def find(x: Path) -> Path:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: Path, b: Path) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_res: dict[tuple[int, int], list[Path]] = defaultdict(list)
    for p, fp in valid.items():
        by_res[(fp.width, fp.height)].append(p)
    for paths in by_res.values():
        paths.sort(key=lambda p: valid[p].duration)
        n = len(paths)
        for i in range(n):
            di = valid[paths[i]].duration
            j = i + 1
            while j < n and valid[paths[j]].duration - di <= _DUR_TOL:
                d = fingerprint_distance(valid[paths[i]].frames, valid[paths[j]].frames)
                if min_distance <= d <= max_distance:
                    union(paths[i], paths[j])
                j += 1

    comps: dict[Path, list[Path]] = defaultdict(list)
    for p in parent:
        comps[find(p)].append(p)
    # Group distance = the WORST pair within the component, not just the
    # linking edges. Grouping is transitive (union-find): A~B and B~C put
    # A,B,C together even if A~C exceeds the band, so the honest "how loose
    # is this group" is the max over *all* pairs. Components are tiny, so the
    # O(k^2) recompute is cheap. A chained group can thus report a distance
    # above --max — exactly the signal a reviewer wants.
    comp_max_dist: dict[Path, int] = {}
    for root, members_paths in comps.items():
        mx = 0
        for i in range(len(members_paths)):
            fi = valid[members_paths[i]].frames
            for j in range(i + 1, len(members_paths)):
                d = fingerprint_distance(fi, valid[members_paths[j]].frames)
                if d > mx:
                    mx = d
        comp_max_dist[root] = mx

    groups: list[DedupeGroup] = []
    for root, members_paths in comps.items():
        if len(members_paths) < 2:
            continue
        members = [(p, cache[p]) for p in members_paths]
        keeper = select_video_keeper(library_root, members_paths, valid)
        losers_sorted = sorted(
            (p for p in members_paths if p != keeper),
            key=lambda p: _sort_key(p, library_root),
        )
        keeper_writes, merge_notes, merge_warnings = _compute_keeper_merge(
            keeper, members, library_root
        )
        groups.append(
            DedupeGroup(
                content_hash="",
                keeper=keeper,
                losers=tuple(losers_sorted),
                keeper_writes=keeper_writes,
                merge_notes=merge_notes,
                merge_warnings=merge_warnings,
                kind="perceptual",
                distance=comp_max_dist[root],
            )
        )
    groups.sort(key=lambda g: _sort_key(g.keeper, library_root))
    return groups


# --- Plan generation --------------------------------------------------------


def generate_plan(
    *,
    library_root: Path,
    cache: dict[Path, FileMetadata],
    hashes: dict[Path, str | None],
    run_id: str,
    run_dir: Path,
    plan_log: IO[str] | None = None,
    fingerprints: dict[Path, VideoFingerprint | None] | None = None,
    min_distance: int = DEFAULT_MIN_DISTANCE,
    max_distance: int = DEFAULT_MAX_DISTANCE,
    videos_only: bool = False,
) -> DedupeResult:
    """Build a dedupe plan from the library cache and precomputed hash map.

    When `fingerprints` is provided, videos (`is_dedupe_video`) are grouped
    by *perceptual fingerprint* within `[min_distance, max_distance]` and
    everything else by exact content hash; when it's None, all files group
    by exact hash (the pre-perceptual behavior, kept for callers/tests that
    don't supply fingerprints).

    `videos_only` drops the exact-hash (image/byte) grouping entirely, so the
    plan contains *only* perceptual video groups — what `--videos-only`,
    `--checkout`, and `--commit` operate on.
    """
    require_migrated_with_hashes(cache, hashes)

    if fingerprints is None:
        groups = group_by_hash(library_root, cache, hashes)
    else:
        video = {p: m for p, m in cache.items() if is_dedupe_video(p)}
        groups = group_by_fingerprint(
            library_root, video, fingerprints, min_distance, max_distance
        )
        if not videos_only:
            non_video = {
                p: m for p, m in cache.items() if not is_dedupe_video(p)
            }
            groups += group_by_hash(library_root, non_video, hashes)
        groups.sort(key=lambda g: _sort_key(g.keeper, library_root))

    # Build PlanLines with stable IDs and pre-computed capture paths.
    # Capture path lives at runs/<run-id>/data/L<NNN>_<filename>; the
    # L<NNN> prefix disambiguates losers with the same on-disk filename
    # (common — duplicates often share a name).
    lines: list[PlanLine] = []
    data_dir = run_dir / "data"
    line_count_total = sum(len(g.losers) for g in groups) + sum(
        1 for g in groups if g.keeper_writes
    )
    with LiveProgress(total=line_count_total) as progress:
        for group in groups:
            for loser in group.losers:
                progress.begin("dedupe", str(loser))
                line_id = f"L{len(lines) + 1:03d}"
                capture_name = f"{line_id}_{loser.name}"
                if group.kind == "perceptual":
                    detail = f"perceptual d={group.distance}"
                else:
                    detail = f"hash {group.content_hash[:12]}…"
                line = PlanLine(
                    line_id=line_id,
                    action=Action.DEDUP,
                    rel_path=loser.relative_to(library_root).as_posix(),
                    details=detail,
                    abs_path=loser,
                    capture_path=data_dir / capture_name,
                )
                lines.append(line)
                if plan_log is not None:
                    ts = datetime.now().isoformat(timespec="seconds")
                    plan_log.write(
                        f"{ts} {loser} -> {line_id} DEDUP "
                        f"(keeper={group.keeper}, {detail})\n"
                    )
                    plan_log.flush()
                progress.advance()

            # One MERGE line per group whose keeper gains consolidated
            # tags. The sidecar captures the keeper's pre-merge XMP for
            # rollback (the keeper file itself is not moved).
            if group.keeper_writes:
                progress.begin("merge", str(group.keeper))
                line_id = f"L{len(lines) + 1:03d}"
                merge_line = PlanLine(
                    line_id=line_id,
                    action=Action.MERGE,
                    rel_path=group.keeper.relative_to(library_root).as_posix(),
                    details="; ".join(group.merge_notes),
                    abs_path=group.keeper,
                    pix_writes=dict(group.keeper_writes),
                    sidecar_path=data_dir / f"{line_id}_{group.keeper.name}.xmp",
                )
                lines.append(merge_line)
                if plan_log is not None:
                    ts = datetime.now().isoformat(timespec="seconds")
                    plan_log.write(
                        f"{ts} {group.keeper} -> {line_id} MERGE "
                        f"({group.keeper_writes})\n"
                    )
                    plan_log.flush()
                progress.advance()

    plan = Plan(
        source=library_root,
        run_id=run_id,
        generated_at=datetime.now(),
        lines=lines,
    )
    return DedupeResult(plan=plan, groups=tuple(groups))


# --- Plan serialization -----------------------------------------------------


_ACTION_WIDTH: int = max(len(a.value) for a in Action)


def serialize_plan(
    *, source: Path, result: DedupeResult, library_root: Path
) -> str:
    """Render the grouped dedupe plan.txt format."""
    plan = result.plan
    lines_by_loser: dict[Path, PlanLine] = {
        ln.abs_path: ln for ln in plan.lines if ln.action == Action.DEDUP
    }
    merge_by_keeper: dict[Path, PlanLine] = {
        ln.abs_path: ln for ln in plan.lines if ln.action == Action.MERGE
    }

    path_width = max(
        (len(ln.rel_path) for ln in plan.lines), default=10
    )

    header: list[str] = [
        f"# Dedupe plan: {source}",
        f"# Generated {plan.generated_at.strftime('%Y-%m-%d %H:%M')}",
        f"# Run ID: {plan.run_id}",
        "#",
        "# Delete a line to skip that file this run. "
        'Commented "#" lines are info only.',
        "# To pick a different keeper for a group, delete the line(s) "
        "for the file you want to keep — the survivor becomes the keeper.",
        "# Format: L<line-id> | ACTION | path | details",
        "",
    ]

    body: list[str] = []
    for i, group in enumerate(result.groups, start=1):
        keeper_rel = _rel_or_abs(group.keeper, library_root)
        if group.kind == "perceptual":
            match_desc = f"perceptual, max dist {group.distance}"
        else:
            match_desc = f"hash {group.content_hash[:12]}…"
        body.append(
            f"# Group {i} — {match_desc}, "
            f"{len(group.losers) + 1} files"
        )
        body.append(f"# Keeper: {keeper_rel}")
        for warning in group.merge_warnings:
            body.append(f"# WARNING: {warning}")
        for loser in group.losers:
            ln = lines_by_loser[loser]
            body.append(
                f"{ln.line_id} | "
                f"{ln.action.value.ljust(_ACTION_WIDTH)} | "
                f"{ln.rel_path.ljust(path_width)} | "
                f"{ln.details}"
            )
        merge_line = merge_by_keeper.get(group.keeper)
        if merge_line is not None:
            body.append(
                f"{merge_line.line_id} | "
                f"{merge_line.action.value.ljust(_ACTION_WIDTH)} | "
                f"{merge_line.rel_path.ljust(path_width)} | "
                f"{merge_line.details}"
            )
        body.append("")  # blank line between groups

    dedup_count = sum(
        1 for ln in plan.lines if ln.action == Action.DEDUP
    )
    merge_count = sum(
        1 for ln in plan.lines if ln.action == Action.MERGE
    )
    summary = (
        f"# Summary: {dedup_count} DEDUP, {merge_count} MERGE across "
        f"{len(result.groups)} group(s)."
    )
    return "\n".join(header + body + [summary, ""])


def _rel_or_abs(path: Path, library_root: Path) -> str:
    try:
        return path.relative_to(library_root).as_posix()
    except ValueError:
        return str(path)


# --- Apply -------------------------------------------------------------------


def apply_plan(
    *,
    plan: Plan,
    kept_line_ids: set[str],
    run_dir: Path,
    library_root: Path,
) -> tuple[int, int, list[tuple[PlanLine, str]]]:
    """Apply MERGE writes onto keepers, move losers into the run folder's
    data/, then sweep empty library folders. Returns
    `(removed, merged, quarantined)` where `quarantined` is the list of
    `(merge_line, error)` for keepers whose MERGE write didn't persist.

    MERGE lines run **before** DEDUP lines (spec/dedupe.md → Atomicity): a
    crash after the merge but before the removals re-plans cleanly (the
    keeper already holds the consolidated values), whereas removing losers
    first would strand the to-be-merged values on gone files.

    A MERGE write that doesn't persist (a damaged/truncated keeper ExifTool
    can't write to — `TagWriteFailed`) quarantines that keeper to
    `.pix/errors/` and continues, rather than halting: its bytes are intact
    (ExifTool didn't touch it), and surfacing it stops a file that can't
    hold `pix:*` tags from later tripping organize's migrated-files check.
    """
    runnable = [ln for ln in plan.lines if ln.line_id in kept_line_ids]
    # Stable sort, MERGE first.
    runnable.sort(key=lambda ln: 0 if ln.action == Action.MERGE else 1)
    log_path = run_dir / "apply.log"
    run_id = run_dir.name
    data_dir = run_dir / "data"
    if runnable:
        data_dir.mkdir(parents=True, exist_ok=True)

    needs_exiftool = any(ln.action == Action.MERGE for ln in runnable)

    removed = 0
    merged = 0
    quarantined: list[tuple[PlanLine, str]] = []
    records: list[LineRecord] = []
    exiftool: ExifToolSession | None = None
    with (
        log_path.open("a", encoding="utf-8") as log,
        LiveProgress(total=len(runnable)) as progress,
    ):
        try:
            if needs_exiftool:
                exiftool = ExifToolSession()
            for ln in runnable:
                progress.begin(
                    f"{ln.line_id} {ln.action.value}", str(ln.abs_path)
                )
                t_start = time.monotonic()
                _log(log, ln, "Started")
                try:
                    if ln.action == Action.MERGE:
                        assert exiftool is not None
                        _apply_merge(ln, exiftool)
                    else:
                        _apply_dedup(ln)
                except TagWriteFailed as e:
                    dur = time.monotonic() - t_start
                    _log(log, ln, "Failed", detail=str(e), dur_seconds=dur)
                    try:
                        dest = move_to_errors(
                            source=ln.abs_path,
                            library_root=library_root,
                            run_id=run_id,
                            line_id=ln.line_id,
                            error=str(e),
                        )
                    except Exception as move_err:
                        _log(
                            log, ln, "Failed",
                            detail=f"move to .pix/errors/ failed: {move_err}",
                        )
                        records.append(
                            LineRecord(
                                line_id=ln.line_id,
                                action=ln.action.value,
                                duration_seconds=dur,
                                rel_path=ln.rel_path,
                                failed=True,
                            )
                        )
                        write_summary(log, records)
                        raise DedupeApplyError(
                            f"{ln.line_id} ({ln.rel_path}): merge write "
                            f"failed and move to .pix/errors/ also failed: "
                            f"{move_err}"
                        ) from move_err
                    try:
                        rel_dest = dest.relative_to(library_root / ".pix")
                    except ValueError:
                        rel_dest = Path(dest.name)
                    _log(
                        log, ln, "Quarantined",
                        detail=str(rel_dest).replace("\\", "/"),
                    )
                    quarantined.append((ln, str(e)))
                    records.append(
                        LineRecord(
                            line_id=ln.line_id,
                            action=ln.action.value,
                            duration_seconds=dur,
                            rel_path=ln.rel_path,
                            failed=True,
                        )
                    )
                    progress.advance()
                    continue
                except Exception as e:
                    dur = time.monotonic() - t_start
                    _log(log, ln, "Failed", detail=str(e), dur_seconds=dur)
                    records.append(
                        LineRecord(
                            line_id=ln.line_id,
                            action=ln.action.value,
                            duration_seconds=dur,
                            rel_path=ln.rel_path,
                            failed=True,
                        )
                    )
                    write_summary(log, records)
                    raise DedupeApplyError(
                        f"{ln.line_id} ({ln.rel_path}): {e}"
                    ) from e
                dur = time.monotonic() - t_start
                _log(log, ln, "Completed", dur_seconds=dur)
                records.append(
                    LineRecord(
                        line_id=ln.line_id,
                        action=ln.action.value,
                        duration_seconds=dur,
                        rel_path=ln.rel_path,
                    )
                )
                progress.advance()
                if ln.action == Action.MERGE:
                    merged += 1
                else:
                    removed += 1

            write_summary(log, records)
        finally:
            if exiftool is not None:
                exiftool.close()

    cleanup_empty_folders(library_root)
    return removed, merged, quarantined


def _apply_dedup(ln: PlanLine) -> None:
    if ln.capture_path is None:
        raise ValueError(f"{ln.line_id}: DEDUP missing capture_path")
    if ln.capture_path.exists():
        raise DedupeApplyError(
            f"capture path {ln.capture_path} already exists"
        )
    safe_rename(ln.abs_path, ln.capture_path)


def _apply_merge(ln: PlanLine, exiftool: ExifToolSession) -> None:
    """Capture the keeper's prior XMP to its sidecar, then write the merged
    pix:* fields in place. The keeper file is not moved."""
    if ln.sidecar_path is None:
        raise ValueError(f"{ln.line_id}: MERGE missing sidecar_path")
    exiftool.export_xmp_sidecar(ln.abs_path, ln.sidecar_path)
    if ln.pix_writes:
        exiftool.write_tags(ln.abs_path, dict(ln.pix_writes))


def _log(
    log: IO[str],
    ln: PlanLine,
    state: str,
    detail: str | None = None,
    *,
    dur_seconds: float | None = None,
    size_bytes: int | None = None,
) -> None:
    ts = datetime.now().isoformat(timespec="milliseconds")
    extras: list[str] = []
    if dur_seconds is not None:
        extras.append(f"dur={format_duration_compact(dur_seconds)}")
    if size_bytes is not None:
        extras.append(f"size={format_size(size_bytes)}")
    extras_str = f"  [{' '.join(extras)}]" if extras else ""
    detail_str = f": {detail}" if detail else ""
    log.write(
        f"{ts} {ln.line_id} {state:<9} {ln.action.value:<18}  "
        f"{ln.rel_path}{extras_str}{detail_str}\n"
    )
    log.flush()
