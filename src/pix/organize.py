"""`pix organize` — re-shape the library to match a folder template.

See spec/organize.md for the full design. This module owns:

- Template parsing (`parse_template`) — accepts `{token}/{token}/...`
  with literals mixed in, rejects `{time}` and unknown tokens.
- Effective-value computation (`compute_values`) — pulls the
  template-relevant tag values for one file.
- Target-path rendering (`render_target_folder`) — turns a parsed
  template + values into a relative folder path, with per-level
  `null/` placement and trailing-null collapse, plus Windows folder-
  name sanitization.
- Plan generation (`generate_plan`) — walks the cache, computes
  target paths, recomputes canonical filenames against per-target-
  folder peer sets (drops/reapplies `_NNN` suffixes from scratch),
  emits MOVE plan lines for any file that needs to move.
- Apply (`apply_plan`) — sequential renames + bottom-up empty-folder
  cleanup at the end.
- CWD constraint (`check_cwd_not_inside`) — refuses to run if the
  user's CWD is a strict subfolder of the library (Windows holds a
  directory handle on the CWD).
"""

from __future__ import annotations

import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO

from pix.dates import format_pix_datetime
from pix.duration import format_duration_compact, format_size
from pix.events import effective_event
from pix.metadata import FileMetadata
from pix.timeout import safe_rename
from pix.plan import (
    PIX_ORIGINAL_PATH,
    Action,
    Plan,
    PlanLine,
    effective_date,
)
from pix.progress import LiveProgress
from pix.telemetry import LineRecord, write_summary


# --- Errors ------------------------------------------------------------------


class OrganizeError(Exception):
    """Raised for any organize-time problem (template parse, prerequisites)."""


class CwdInsideLibraryError(OrganizeError):
    """Raised when CWD is a strict subfolder of the library root."""


class UnmigratedFilesError(OrganizeError):
    """Raised when one or more files in the library lack pix:OriginalPath."""

    def __init__(self, paths: list[Path]) -> None:
        super().__init__(
            f"{len(paths)} file(s) in the library lack pix:OriginalPath. "
            f"Run `pix migrate <library-root>` first."
        )
        self.paths = paths


class MissingHashesError(OrganizeError):
    """Raised when one or more files lack a cached content hash.

    Organize uses the cached hash as the collision tiebreaker (see
    spec/library.md → Collision handling). Without it the collision
    suffix assignment isn't deterministic across files with identical
    target paths.
    """

    def __init__(self, paths: list[Path]) -> None:
        super().__init__(
            f"{len(paths)} file(s) in the library lack a cached content "
            f"hash. Run `pix hash <library-root>` first."
        )
        self.paths = paths


class OrganizeApplyError(Exception):
    """Raised when a MOVE fails during apply."""


# --- Template parsing --------------------------------------------------------


ALLOWED_TOKENS: frozenset[str] = frozenset(
    {"year", "month", "day", "date", "event"}
)
_TOKEN_RE = re.compile(r"\{([a-zA-Z]+)\}")


@dataclass(frozen=True)
class Token:
    name: str  # one of ALLOWED_TOKENS


@dataclass(frozen=True)
class Literal:
    text: str


@dataclass(frozen=True)
class Level:
    """One folder level: a list of segments (Token | Literal) to concatenate."""

    segments: tuple[Token | Literal, ...]


@dataclass(frozen=True)
class Template:
    """Parsed `pix organize <template>` string, split into folder levels."""

    raw: str  # the original string, for round-tripping back to config
    levels: tuple[Level, ...]


def parse_template(template_str: str) -> Template:
    """Parse `{year}/{month}/{event}`-style strings into a Template.

    Rejects empty templates, empty levels (leading/trailing/consecutive
    `/`), `{time}` (per-second is a foot-gun), and unknown tokens.
    """
    if not template_str.strip():
        raise OrganizeError("template is empty")

    levels: list[Level] = []
    for level_str in template_str.split("/"):
        if not level_str:
            raise OrganizeError(
                f"empty level in template {template_str!r} "
                f"(check for leading, trailing, or consecutive `/`)"
            )

        segments: list[Token | Literal] = []
        pos = 0
        for m in _TOKEN_RE.finditer(level_str):
            if m.start() > pos:
                segments.append(Literal(text=level_str[pos : m.start()]))
            token_name = m.group(1).lower()
            if token_name == "time":
                raise OrganizeError(
                    "{time} is per-second and not useful as a folder "
                    "level; use {year}/{month}/{day} instead."
                )
            if token_name not in ALLOWED_TOKENS:
                raise OrganizeError(
                    f"unknown token {{{token_name}}}; valid tokens: "
                    f"{sorted(ALLOWED_TOKENS)}"
                )
            segments.append(Token(name=token_name))
            pos = m.end()
        if pos < len(level_str):
            segments.append(Literal(text=level_str[pos:]))

        levels.append(Level(segments=tuple(segments)))

    return Template(raw=template_str, levels=tuple(levels))


# --- Effective values --------------------------------------------------------


def compute_values(meta: FileMetadata) -> dict[str, str | None]:
    """Effective tag values for the template's tokens.

    Returns a dict keyed by token name. `None` means the tag is null
    (no effective value); the renderer routes that level to `null/`.
    """
    date = effective_date(meta)
    return {
        "year": f"{date.year:04d}" if date else None,
        "month": f"{date.month:02d}" if date else None,
        "day": f"{date.day:02d}" if date else None,
        "date": format_pix_datetime(date) if date else None,
        "event": effective_event(meta),
    }


# --- Folder-name sanitization ------------------------------------------------


_ILLEGAL_CHARS_RE = re.compile(r'[<>:"/\\|?*]')
_RESERVED_NAMES: frozenset[str] = (
    frozenset({"CON", "PRN", "AUX", "NUL"})
    | frozenset(f"COM{i}" for i in range(1, 10))
    | frozenset(f"LPT{i}" for i in range(1, 10))
)


def sanitize_folder_name(name: str) -> str:
    """Make a tag value safe to use as a Windows folder name.

    Replaces illegal chars (`<>:"/\\|?*`) with `_`, strips trailing
    whitespace + `.`, and prefixes Windows-reserved DOS names. Empty
    result becomes `_` defensively (shouldn't happen — null tokens
    are routed to `null/` before reaching here).
    """
    out = _ILLEGAL_CHARS_RE.sub("_", name).rstrip(" .")
    if out.upper() in _RESERVED_NAMES:
        out = "_" + out
    return out or "_"


# --- Template rendering ------------------------------------------------------


def render_target_folder(
    template: Template, values: dict[str, str | None]
) -> str:
    """Render the template into a forward-slash-joined relative path.

    Per-level null rule: any null token in a level renders the entire
    level as `null/`. Trailing `null/null/...` chains collapse to a
    single `null/` (so an all-null file lands at `null/foo.jpg`, not
    `null/null/foo.jpg`).
    """
    rendered_levels: list[str] = []
    for level in template.levels:
        if any(
            isinstance(s, Token) and values.get(s.name) is None
            for s in level.segments
        ):
            rendered_levels.append("null")
            continue
        parts: list[str] = []
        for seg in level.segments:
            if isinstance(seg, Token):
                val = values[seg.name]
                assert val is not None, "Token resolved to None despite null check"
                parts.append(sanitize_folder_name(val))
            else:
                parts.append(seg.text)
        rendered_levels.append("".join(parts))

    # Collapse trailing `null` chains.
    while (
        len(rendered_levels) > 1
        and rendered_levels[-1] == "null"
        and rendered_levels[-2] == "null"
    ):
        rendered_levels.pop()

    return "/".join(rendered_levels)


# --- CWD constraint ----------------------------------------------------------


def check_cwd_not_inside(library_root: Path) -> None:
    """Refuse if CWD is a strict subfolder of `library_root`.

    Windows holds a directory handle on the process's CWD, which
    prevents empty-folder cleanup from removing it. We refuse up
    front rather than fail partway through.
    """
    cwd = Path.cwd().resolve()
    if cwd == library_root:
        return
    if library_root in cwd.parents:
        raise CwdInsideLibraryError(
            f"Refusing to organize while CWD is a subfolder of the "
            f"library. cd to {library_root} (or any directory outside "
            f"the library) and re-run."
        )


# --- Plan generation ---------------------------------------------------------


@dataclass
class _CandidateTarget:
    """Per-file intermediate during plan-gen."""

    path: Path  # current absolute path
    target_folder: Path  # absolute target folder
    bare_filename: str  # canonical filename WITHOUT collision suffix
    content_hash: str  # for collision tiebreaker (or path fallback)


def generate_plan(
    *,
    library_root: Path,
    template: Template,
    cache: dict[Path, FileMetadata],
    hashes: dict[Path, str | None],
    run_id: str,
    run_dir: Path,
    plan_log: IO[str] | None = None,
) -> Plan:
    """Build an organize Plan from the cache and precomputed hash map.

    Raises `UnmigratedFilesError` if any file lacks `pix:OriginalPath`
    — the library invariant is that every file has been migrated, and
    organize templates read effective tag values. Raises
    `MissingHashesError` if any file lacks a cached content hash
    (needed as the collision-resolution tiebreaker).

    Both checks consume already-computed inputs — no per-file syscalls.
    """
    unmigrated = [
        p for p, m in cache.items() if m.get_str(PIX_ORIGINAL_PATH) is None
    ]
    if unmigrated:
        raise UnmigratedFilesError(sorted(unmigrated)[:10])

    no_hash = [p for p in cache if hashes.get(p) is None]
    if no_hash:
        raise MissingHashesError(sorted(no_hash)[:10])

    paths = sorted(cache.keys())
    candidates: list[_CandidateTarget] = []

    with LiveProgress(total=len(paths)) as progress:
        for path in paths:
            progress.begin("organizing", str(path))
            meta = cache[path]
            values = compute_values(meta)
            target_rel = render_target_folder(template, values)
            target_folder = library_root / target_rel

            # Bare canonical filename. The current filename's `_NNN`
            # is irrelevant — we recompute from effective date.
            date = effective_date(meta)
            ext = path.suffix.lower().lstrip(".") or "bin"
            if date is not None:
                bare = f"{date.strftime('%Y-%m-%d_%H%M%S')}.{ext}"
            else:
                # No effective date: keep current filename. The file
                # lands in null/ anyway (date tokens render null).
                bare = path.name

            # Prereq check above guarantees a cached hash exists.
            content_hash = hashes.get(path) or str(path)
            candidates.append(
                _CandidateTarget(
                    path=path,
                    target_folder=target_folder,
                    bare_filename=bare,
                    content_hash=content_hash,
                )
            )

            if plan_log is not None:
                ts = datetime.now().isoformat(timespec="seconds")
                plan_log.write(
                    f"{ts} {path} -> tentative "
                    f"{target_folder / bare}\n"
                )
                plan_log.flush()
            progress.advance()

    # Resolve collisions per target folder.
    final_names = _resolve_collisions(candidates)

    # Build plan lines.
    lines: list[PlanLine] = []
    for cand in candidates:
        final_name = final_names[cand.path]
        target_path = cand.target_folder / final_name
        if cand.path == target_path:
            continue  # idempotent — file already at its target

        line_id = f"L{len(lines) + 1:03d}"
        rel_source = cand.path.relative_to(library_root)
        rel_target = target_path.relative_to(library_root)
        details = f"→{rel_target.as_posix()}"
        lines.append(
            PlanLine(
                line_id=line_id,
                action=Action.MOVE,
                rel_path=rel_source.as_posix(),
                details=details,
                abs_path=cand.path,
                target_filename=final_name,
                target_path=target_path,
            )
        )

    return Plan(
        source=library_root,
        run_id=run_id,
        generated_at=datetime.now(),
        lines=lines,
    )


def _resolve_collisions(
    candidates: list[_CandidateTarget],
) -> dict[Path, str]:
    """Assign final filenames per the library.md collision rule.

    Groups candidates by `(target_folder, bare_filename)`. Within each
    group: sort by `content_hash` ascending; first member keeps the
    bare name, others get `_001`, `_002`, ... suffixes inserted before
    the extension.
    """
    groups: dict[
        tuple[Path, str], list[_CandidateTarget]
    ] = defaultdict(list)
    for cand in candidates:
        groups[(cand.target_folder, cand.bare_filename)].append(cand)

    result: dict[Path, str] = {}
    for (_target_folder, bare), members in groups.items():
        if len(members) == 1:
            result[members[0].path] = bare
            continue
        members.sort(key=lambda c: c.content_hash)
        result[members[0].path] = bare
        if "." in bare:
            stem, dot, ext = bare.rpartition(".")
        else:
            stem, dot, ext = bare, "", ""
        for i, cand in enumerate(members[1:], start=1):
            result[cand.path] = f"{stem}_{i:03d}{dot}{ext}"
    return result


# --- Apply -------------------------------------------------------------------


def apply_plan(
    *,
    plan: Plan,
    kept_line_ids: set[str],
    run_dir: Path,
    library_root: Path,
) -> int:
    """Execute MOVE lines and then sweep empty folders. Returns the count."""
    runnable = [
        ln for ln in plan.lines if ln.line_id in kept_line_ids
    ]
    log_path = run_dir / "apply.log"
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
                _apply_move(ln)
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
                raise OrganizeApplyError(
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


def _apply_move(ln: PlanLine) -> None:
    if ln.target_path is None:
        raise ValueError(f"{ln.line_id}: MOVE missing target_path")
    target = ln.target_path
    if target.exists():
        # Either it's actually our source (case-only rename — rare for
        # MOVE since folder structure usually differs) or a real conflict.
        if target.resolve() != ln.abs_path.resolve():
            raise OrganizeApplyError(
                f"target {target} already exists"
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_rename(ln.abs_path, target)


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


# --- Empty-folder cleanup ----------------------------------------------------


def cleanup_empty_folders(library_root: Path) -> int:
    """Bottom-up sweep — remove empty folders under `library_root`.

    Never touches `.pix/` or its subtree, and never removes the
    library root itself. Returns the count of folders removed.
    """
    pix_dir = (library_root / ".pix").resolve()
    library_root = library_root.resolve()

    # Walk bottom-up by sorting by path depth descending.
    candidates: list[Path] = []
    for dirpath, _dirnames, _filenames in os.walk(library_root):
        d = Path(dirpath).resolve()
        if d == library_root:
            continue
        if d == pix_dir or pix_dir in d.parents:
            continue
        candidates.append(d)
    candidates.sort(key=lambda p: -len(p.parts))

    removed = 0
    for d in candidates:
        try:
            d.rmdir()
            removed += 1
        except OSError:
            # Not empty, or in use — skip.
            pass
    return removed
