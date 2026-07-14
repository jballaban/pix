"""Implementation of `pix tag set` — write a tag override onto specific files.

`pix tag set <tag> <value> <path>...` writes `pix:EventOverride` /
`pix:DateOverride` to each named path. `pix tag clear` is the inverse: for
**date** it removes the override (reverting to the auto date); for **event**
it blanks the *effective* value — writing an `EVENT_NULL` force-null
override when an auto event would otherwise show, so "Clear" means "no
event" even when the event was auto-derived (not a manual override). A path
may be a file or a **folder** — a folder expands to the
taggable media it contains (per `EXTENSION_POLICY`), so a Windows Explorer
selection of mixed files and folders can be handed straight in. It's the
targeted alternative to the `checkout` folder-shuffle: same override the
tag-editing workflow sets, just applied to the paths you pass.
Conservation (prior-XMP capture) and atomicity come from reusing migrate's
TAG apply path.

`set` writes tags only — run `pix organize` afterward to reshape the
library to match (consistent with pix's commit/organize separation). The
content hash is metadata-invariant, so the write's mtime bump is re-keyed
into the hash cache to avoid forcing a needless `pix hash`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer

from pix import banner, cache_db
from pix.apply import ApplyError, apply_plan
from pix.checkout import CheckoutOpen, ensure_no_open_checkout
from pix.config import Config, settings_path
from pix.editor import prompt_proceed
from pix.events import EVENT_NULL, cached_event_names, invalidate_events_cache
from pix.library_lock import LockHeld, acquire as acquire_lock
from pix.metadata import (
    ExifToolFailed,
    ExifToolNotFound,
    FileMetadata,
    read_metadata_batched,
)
from pix.plan import (
    PIX_DATE_OVERRIDE,
    PIX_EVENT_AUTO,
    PIX_EVENT_OVERRIDE,
    Action,
    Plan,
    PlanLine,
    attach_paths,
    lookup_policy,
    valid_date_override,
)
from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.scan import walk_source_files

_OVERRIDE_FIELD: dict[str, str] = {
    "event": PIX_EVENT_OVERRIDE,
    "date": PIX_DATE_OVERRIDE,
}


def _fail(msg: str) -> None:
    typer.echo(f"Error: {msg}", err=True)
    raise typer.Exit(code=1)


def _align_event_case(root: Path, value: str) -> str:
    """Return `value`, but snapped to an existing event's casing when one
    matches case-insensitively.

    NTFS is case-insensitive, so `Karate` and `karate` can't be distinct event
    folders — aligning the tag keeps one canonical casing per event (and the
    events list / autocomplete clean). Best-effort via the cached event list;
    the menu warms it right before set, so there it's reliable."""
    lower = value.lower()
    for existing in cached_event_names(root):
        if existing != value and existing.lower() == lower:
            typer.echo(
                f"Aligning event casing to existing {existing!r} "
                f"(you typed {value!r})."
            )
            return existing
    return value


def _plan_clear(
    field: str, current: str | None, meta: FileMetadata, tag: str
) -> tuple[str, str] | None:
    """Decide the write value + details for a `clear` on one file.

    Returns `(write_value, details)`, or `None` when there's nothing to do.
    A `write_value` of `""` removes the override tag.

    - **Event**: clear means "no event" — it blanks the *effective* value, not
      just a manual override. If an auto event (`pix:EventAuto`) would
      otherwise show, write the `EVENT_NULL` force-null sentinel to beat it;
      if the event came only from an override (no auto), just drop the
      override. Already-eventless files are a no-op.
    - **Other tags (date)**: remove the override, reverting to the auto.
    """
    if field == PIX_EVENT_OVERRIDE:
        auto = meta.get_str(PIX_EVENT_AUTO)
        effective = None if current == EVENT_NULL else (current or auto)
        if effective is None:
            return None  # already no event
        return (EVENT_NULL if auto else "", f'event "{effective}"→(none)')
    if current is None:
        return None
    return ("", f"{tag}_override {current!r}→(cleared)")


def _expand_paths(raw: list[Path], root: Path, config: Config) -> list[Path]:
    """Resolve a mix of files and folders to a deduped list of taggable files.

    A path that is a **file** passes through verbatim (the user named it
    explicitly, so any extension is honored). A path that is a **folder** is
    walked recursively (`walk_source_files` already skips `.pix/`) and kept
    only where `EXTENSION_POLICY` says pix actually tags the file — the
    `keep` and `convert_to_*` types, never `delete` junk or unknown formats.
    This lets the Explorer context menu hand us whatever was selected.

    Order is preserved and the first occurrence of a path wins, so an
    overlapping selection (a file and the folder that contains it) never
    double-writes. Anything under `.pix/` is dropped — that's tool
    scaffolding, never library media.
    """
    out: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        if p in seen or ".pix" in p.relative_to(root).parts:
            return
        seen.add(p)
        out.append(p)

    for p in raw:
        if p.is_dir():
            for fp, _size, _mtime in walk_source_files(p):
                action = lookup_policy(fp.name, config.extensions)
                if action is not None and action != "delete":
                    _add(fp)
        else:
            _add(p)
    return out


def set_override(
    tag: str,
    value: str,
    paths: list[Path],
    no_prompt: bool = False,
    clear: bool = False,
) -> None:
    """Write a tag override on `paths` (or clear it when `clear` is set).

    `value` is ignored when `clear` is True. Exposed as two CLI commands —
    `pix tag set <tag> <value> <paths>` and `pix tag clear <tag> <paths>` — so the
    value is always an explicit positional and never an empty-string arg
    (which the shell drops)."""
    banner()

    field = _OVERRIDE_FIELD.get(tag.lower())
    if field is None:
        _fail(f"unknown tag {tag!r}; expected one of: {', '.join(_OVERRIDE_FIELD)}.")
        return
    clearing = clear
    if tag.lower() == "date" and not clearing and not valid_date_override(value):
        _fail(
            f"invalid date override {value!r}. Expected YYYY-MM-DD-HH:MM:SS "
            f"with `*` for any unpinned part, e.g. 2022-*-*-*:*:* (pin year) "
            f"or 2022-08-15-*:*:* (pin the day)."
        )
        return

    if not paths:
        _fail("no files given.")
        return
    raw = [p.resolve() for p in paths]
    bad = [p for p in raw if not p.is_file() and not p.is_dir()]
    if bad:
        _fail(f"not a file or folder: {bad[0]}")
        return

    try:
        root = resolve_root(start=raw[0])
    except NoLibraryRoot as e:
        _fail(str(e))
        return
    outside = [p for p in raw if p != root and root not in p.parents]
    if outside:
        _fail(
            f"{outside[0]} is not inside the library at {root}. All paths "
            f"must belong to the same library."
        )
        return

    # Snap a new event's casing onto any existing same-spelling event so NTFS
    # never ends up with case-variant event folders (Karate vs karate).
    if field == PIX_EVENT_OVERRIDE and not clearing and value:
        value = _align_event_case(root, value)

    # A checkout freeze forbids inode-mutating ops: an override write goes
    # through ExifTool -overwrite_original (temp + rename → new inode), which
    # would orphan the open checkout's hard links. Refuse, like every other
    # mutating command.
    try:
        ensure_no_open_checkout(root)
    except CheckoutOpen as e:
        _fail(str(e))
        return

    # All file mutations run under the library lock so set/clear can't race a
    # concurrent migrate/organize/dedupe (or each other) on the same files or
    # the hash cache.
    try:
        with acquire_lock(root, "clear" if clearing else "set"):
            _apply_overrides(
                tag=tag,
                value=value,
                field=field,
                clearing=clearing,
                raw=raw,
                root=root,
                no_prompt=no_prompt,
            )
    except LockHeld as e:
        _fail(str(e))


def _apply_overrides(
    *,
    tag: str,
    value: str,
    field: str,
    clearing: bool,
    raw: list[Path],
    root: Path,
    no_prompt: bool,
) -> None:
    """Plan and write the override changes. Runs under the library lock."""
    config = Config.load(settings_path(root))

    # A folder argument expands to the taggable media it contains, so the
    # Explorer context menu can pass a mix of files and folders. Files pass
    # through unchanged. An empty expansion (a folder with no media) is an
    # error rather than a silent no-op.
    resolved = _expand_paths(raw, root, config)
    if not resolved:
        _fail("no taggable media found in the given files/folders.")
        return

    # Read current overrides so a set that's already in effect is a no-op,
    # and a clear only touches files that actually have the override (an
    # empty-value write on a file without it would be a no-op ExifTool
    # reports as "0 updated" — which the apply path treats as a failure).
    try:
        metas = read_metadata_batched(resolved)
    except ExifToolNotFound as e:
        _fail(str(e))
        return
    except ExifToolFailed as e:
        _fail(f"exiftool failed reading current tags.\n{e}")
        return

    run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    runs_dir = config.runs_base(root) / run_id
    runs_dir.mkdir(parents=True, exist_ok=True)

    lines: list[PlanLine] = []
    noop = 0
    for p in resolved:
        meta = metas.get(p) or FileMetadata(path=p, raw={"SourceFile": str(p)})
        current = meta.get_str(field)
        if clearing:
            decision = _plan_clear(field, current, meta, tag)
            if decision is None:
                noop += 1
                continue
            write_value, details = decision
        else:
            if current == value:
                noop += 1
                continue
            write_value, details = value, f"{tag}_override {current or 'null'}→{value}"
        ln = PlanLine(
            line_id=f"L{len(lines) + 1:03d}",
            action=Action.TAG,
            rel_path=p.relative_to(root).as_posix(),
            details=details,
            abs_path=p,
            pix_writes={field: write_value},  # "" removes the tag
        )
        lines.append(attach_paths(ln, runs_dir, runs_dir / "staging"))

    if not lines:
        verb = "cleared" if clearing else "set"
        typer.echo(f"Nothing to do — all {noop} file(s) already {verb}.")
        return

    plan_path = runs_dir / "plan.txt"
    action_desc = (
        f"clear {tag} override"
        if clearing
        else f"set {tag} override → {value!r}"
    )
    plan_path.write_text(
        f"# pix tag set: {action_desc}\n"
        f"# Run ID: {run_id}\n#\n"
        + "\n".join(f"{ln.line_id} | TAG | {ln.rel_path} | {ln.details}" for ln in lines)
        + "\n",
        encoding="utf-8",
    )
    plan = Plan(source=root, run_id=run_id, generated_at=datetime.now(), lines=lines)

    typer.echo(f"{action_desc} on {len(lines)} file(s).")
    if noop:
        typer.echo(f"({noop} already at the requested state — skipped.)")
    if not no_prompt:
        if not prompt_proceed():
            typer.echo("Aborted; no changes made.")
            return

    apply_log_path = runs_dir / "apply.log"
    try:
        try:
            completed, failures = apply_plan(
                plan=plan,
                plan_path=plan_path,
                run_dir=runs_dir,
                kept_line_ids={ln.line_id for ln in lines},
                library_root=root,
            )
        except ApplyError as e:
            typer.echo(f"Error: apply failed: {e}", err=True)
            raise typer.Exit(code=1) from e

        # The override write only touches metadata: the file's content — and
        # so its content hash and perceptual fingerprint — is unchanged, but
        # the write bumps (size, mtime_ns) and stales the cache stamp. Reflect
        # the new tag value into the cached meta, re-stamp, and carry the hash
        # + fingerprint forward so organize/dedupe don't demand a fresh
        # `pix hash` / re-fingerprint. Files that failed (moved to errors/)
        # fail the stat and are skipped.
        for ln in lines:
            try:
                st = ln.abs_path.stat()
            except OSError:
                continue
            cache_db.note_inplace_metadata_change(
                root,
                ln.abs_path,
                meta_updates=dict(ln.pix_writes),
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
            )

        # An event change can add/remove a unique event, so drop the cached
        # event list — the next autocomplete fetch then reflects this edit.
        if field == PIX_EVENT_OVERRIDE:
            invalidate_events_cache(root)

        typer.echo("")
        typer.echo(f"Updated {completed} file(s).")
        if failures:
            typer.echo(
                f"{len(failures)} file(s) could not be written; see "
                f"{root / '.pix' / 'errors'}.",
                err=True,
            )
        typer.echo("Run `pix organize` to reshape the library to match.")
    finally:
        typer.echo(f"Log: {apply_log_path}")
