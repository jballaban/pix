"""Implementation of `pix set` — write a tag override onto specific files.

`pix set <tag> <value> <path>...` writes `pix:EventOverride` /
`pix:DateOverride` to each named file. An empty value (`""`) clears the
override. It's the targeted, file-list alternative to the `checkout`
folder-shuffle: same override the tag-editing workflow sets, just applied
to the files you pass. Conservation (prior-XMP capture) and atomicity come
from reusing migrate's TAG apply path.

`set` writes tags only — run `pix organize` afterward to reshape the
library to match (consistent with pix's commit/organize separation). The
content hash is metadata-invariant, so the write's mtime bump is re-keyed
into the hash cache to avoid forcing a needless `pix hash`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer

from pix import banner
from pix.apply import ApplyError, apply_plan
from pix.config import Config, settings_path
from pix.editor import prompt_proceed
from pix.hash_cache import read_cached_hash, write_cached_hash
from pix.metadata import (
    ExifToolFailed,
    ExifToolNotFound,
    FileMetadata,
    read_metadata_batched,
)
from pix.plan import (
    PIX_DATE_OVERRIDE,
    PIX_EVENT_OVERRIDE,
    Action,
    Plan,
    PlanLine,
    attach_paths,
    valid_date_override,
)
from pix.root import NoLibraryRoot, resolve as resolve_root

_OVERRIDE_FIELD: dict[str, str] = {
    "event": PIX_EVENT_OVERRIDE,
    "date": PIX_DATE_OVERRIDE,
}


def _fail(msg: str) -> None:
    typer.echo(f"Error: {msg}", err=True)
    raise typer.Exit(code=1)


def set_override(
    tag: str,
    value: str,
    paths: list[Path],
    no_prompt: bool = False,
    clear: bool = False,
) -> None:
    """Write a tag override on `paths` (or clear it when `clear` is set).

    `value` is ignored when `clear` is True. Exposed as two CLI commands —
    `pix set <tag> <value> <paths>` and `pix clear <tag> <paths>` — so the
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
    resolved = [p.resolve() for p in paths]
    missing = [p for p in resolved if not p.is_file()]
    if missing:
        _fail(f"not a file: {missing[0]}")
        return

    try:
        root = resolve_root(start=resolved[0])
    except NoLibraryRoot as e:
        _fail(str(e))
        return
    outside = [p for p in resolved if root not in p.parents]
    if outside:
        _fail(
            f"{outside[0]} is not inside the library at {root}. All files "
            f"must belong to the same library."
        )
        return

    config = Config.load(settings_path(root))

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
            if current is None:
                noop += 1
                continue
            details = f"{tag}_override {current!r}→(cleared)"
        else:
            if current == value:
                noop += 1
                continue
            details = f"{tag}_override {current or 'null'}→{value}"
        ln = PlanLine(
            line_id=f"L{len(lines) + 1:03d}",
            action=Action.TAG,
            rel_path=p.relative_to(root).as_posix(),
            details=details,
            abs_path=p,
            pix_writes={field: "" if clearing else value},  # "" clears the tag
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
        f"# pix set: {action_desc}\n"
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

    # The override write only touches metadata, which the content hash
    # excludes — but it bumps size+mtime and staleness the hash cache key.
    # Capture each current hash and re-key it post-write so organize doesn't
    # demand a fresh `pix hash`.
    pre_hashes: dict[Path, str] = {}
    for ln in lines:
        h = read_cached_hash(root, ln.abs_path)
        if h is not None:
            pre_hashes[ln.abs_path] = h

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

        rekeyed = 0
        for path, hash_hex in pre_hashes.items():
            try:
                st = path.stat()
            except OSError:
                continue
            write_cached_hash(
                root, path, hash_hex=hash_hex,
                size=st.st_size, mtime_ns=st.st_mtime_ns,
            )
            rekeyed += 1

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
