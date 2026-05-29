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
- Keeper selection (`select_keeper`) — investment-tier rule, lex
  tie-break per spec.
- Plan generation (`generate_plan`) — produces a `DedupeResult` with
  the underlying `Plan` (PlanLines for each loser, action=DEDUP) and
  the groups for display.
- Plan serialization (`serialize_plan`) — grouped plan.txt format with
  comment headers per group.
- Apply (`apply_plan`) — sequential rename of each loser into
  `runs/<run-id>/data/`, then bottom-up empty-folder cleanup.

CWD constraint and empty-folder cleanup come from `pix.organize` —
the same checks apply (we'd otherwise fail to clean folders that
become empty after dups go to `data/`).
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO

from pix.duration import format_duration_compact, format_size
from pix.events import PIX_EVENT_OVERRIDE
from pix.metadata import FileMetadata
from pix.organize import cleanup_empty_folders  # reused
from pix.timeout import safe_rename
from pix.plan import (
    PIX_DATE_OVERRIDE,
    PIX_ORIGINAL_PATH,
    Action,
    Plan,
    PlanLine,
)
from pix.progress import LiveProgress
from pix.telemetry import LineRecord, write_summary


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
    """One set of files sharing a `pix:ContentHash`."""

    content_hash: str
    keeper: Path  # absolute
    losers: tuple[Path, ...]  # absolute, in plan-line order (lex)


@dataclass(frozen=True)
class DedupeResult:
    """Output of plan-gen.

    `plan` carries one PlanLine (action=DEDUP) per loser; that's what
    apply consumes. `groups` is the same info reshaped for serializing
    the grouped plan.txt format with per-group comment headers.
    """

    plan: Plan
    groups: tuple[DedupeGroup, ...]


# --- Investment / keeper rule ------------------------------------------------


def _is_invested(meta: FileMetadata) -> bool:
    """A file is 'invested' if user-set DateOverride or EventOverride is present.

    Face regions become a third investment signal once face detection
    ships; not relevant yet.
    """
    # Match `pix.plan._override_has_pinning`: an all-`*` override is
    # equivalent to absent, so a digit anywhere means at least one slot
    # is pinned.
    date_override = meta.get_str(PIX_DATE_OVERRIDE)
    if date_override and any(c.isdigit() for c in date_override):
        return True
    event_override = meta.get_str(PIX_EVENT_OVERRIDE)
    if event_override:
        return True
    return False


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
    """Per spec: invested-tier first, then lex-smallest within tier."""
    invested: list[Path] = []
    pristine: list[Path] = []
    for path, meta in members:
        (invested if _is_invested(meta) else pristine).append(path)

    pool = invested if invested else pristine
    return min(pool, key=lambda p: _sort_key(p, library_root))


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
        groups.append(
            DedupeGroup(
                content_hash=content_hash,
                keeper=keeper,
                losers=tuple(losers_sorted),
            )
        )

    # Sort groups by keeper path so plan ordering is stable.
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
) -> DedupeResult:
    """Build a dedupe plan from the library cache and precomputed hash map."""
    require_migrated_with_hashes(cache, hashes)

    groups = group_by_hash(library_root, cache, hashes)

    # Build PlanLines with stable IDs and pre-computed capture paths.
    # Capture path lives at runs/<run-id>/data/L<NNN>_<filename>; the
    # L<NNN> prefix disambiguates losers with the same on-disk filename
    # (common — duplicates often share a name).
    lines: list[PlanLine] = []
    data_dir = run_dir / "data"
    line_count_total = sum(len(g.losers) for g in groups)
    with LiveProgress(total=line_count_total) as progress:
        for group in groups:
            for loser in group.losers:
                progress.begin("dedupe", str(loser))
                line_id = f"L{len(lines) + 1:03d}"
                capture_name = f"{line_id}_{loser.name}"
                short_hash = group.content_hash[:12]
                line = PlanLine(
                    line_id=line_id,
                    action=Action.DEDUP,
                    rel_path=loser.relative_to(library_root).as_posix(),
                    details=f"hash {short_hash}…",
                    abs_path=loser,
                    capture_path=data_dir / capture_name,
                )
                lines.append(line)
                if plan_log is not None:
                    ts = datetime.now().isoformat(timespec="seconds")
                    plan_log.write(
                        f"{ts} {loser} -> {line_id} DEDUP "
                        f"(keeper={group.keeper}, hash={short_hash}…)\n"
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
        ln.abs_path: ln for ln in plan.lines
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
        short_hash = group.content_hash[:12]
        keeper_rel = _rel_or_abs(group.keeper, library_root)
        body.append(
            f"# Group {i} — hash {short_hash}…, "
            f"{len(group.losers) + 1} files"
        )
        body.append(f"# Keeper: {keeper_rel}")
        for loser in group.losers:
            ln = lines_by_loser[loser]
            body.append(
                f"{ln.line_id} | "
                f"{ln.action.value.ljust(_ACTION_WIDTH)} | "
                f"{ln.rel_path.ljust(path_width)} | "
                f"{ln.details}"
            )
        body.append("")  # blank line between groups

    summary = (
        f"# Summary: {len(plan.lines)} DEDUP across "
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
) -> int:
    """Move each runnable loser into the run folder's data/, then sweep
    empty library folders. Returns count of successful removals."""
    runnable = [ln for ln in plan.lines if ln.line_id in kept_line_ids]
    log_path = run_dir / "apply.log"
    data_dir = run_dir / "data"
    if runnable:
        data_dir.mkdir(parents=True, exist_ok=True)

    completed = 0
    records: list[LineRecord] = []
    with (
        log_path.open("a", encoding="utf-8") as log,
        LiveProgress(total=len(runnable)) as progress,
    ):
        for ln in runnable:
            progress.begin(
                f"{ln.line_id} {ln.action.value}", str(ln.abs_path)
            )
            t_start = time.monotonic()
            _log(log, ln, "Started")
            try:
                _apply_dedup(ln)
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
            completed += 1

        write_summary(log, records)

    cleanup_empty_folders(library_root)
    return completed


def _apply_dedup(ln: PlanLine) -> None:
    if ln.capture_path is None:
        raise ValueError(f"{ln.line_id}: DEDUP missing capture_path")
    if ln.capture_path.exists():
        raise DedupeApplyError(
            f"capture path {ln.capture_path} already exists"
        )
    safe_rename(ln.abs_path, ln.capture_path)


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
