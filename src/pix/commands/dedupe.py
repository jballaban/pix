"""Implementation of `pix dedupe <path>`.

End-to-end flow per spec/dedupe.md: resolve root → CWD check → walk
library → bulk-read metadata → check prerequisites → group by hash →
plan → editor/apply prompt → apply (move losers to data/) → sweep
empty folders.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import typer

from pix import banner, debug
from pix.dedupe import (
    DedupeApplyError,
    MissingHashesError,
    UnmigratedFilesError,
    apply_plan,
    generate_plan,
    serialize_plan,
)
from pix.editor import open_in_editor, parse_kept_line_ids, prompt_apply
from pix.metadata import (
    ExifToolFailed,
    ExifToolNotFound,
    FileMetadata,
    build_cache,
)
from pix.organize import CwdInsideLibraryError, check_cwd_not_inside
from pix.progress import LiveProgress
from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.scan import walk_source_files
from pix.schema import SCHEMA_VERSION, SchemaTooNew, SchemaUpgradeRequired


def dedupe_library(path: Path) -> None:
    """End-to-end dedupe: resolve, plan, edit, confirm, apply."""
    user_path = str(path)
    path = path.resolve()
    try:
        root = resolve_root(start=path)
    except SchemaUpgradeRequired as e:
        banner()
        typer.echo(f"{e} Run `pix upgrade {user_path}`", err=True)
        raise typer.Exit(code=1) from e
    except (NoLibraryRoot, SchemaTooNew) as e:
        banner()
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    banner(schema_version=SCHEMA_VERSION)

    try:
        check_cwd_not_inside(root)
    except CwdInsideLibraryError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    runs_dir = root / ".pix" / "runs" / run_id
    runs_dir.mkdir(parents=True)
    plan_log_path = runs_dir / "plan.log"

    _plog(plan_log_path, f"Library root: {root}")

    with LiveProgress() as silent_progress:
        t0 = time.monotonic()
        silent_progress.begin("Walking library...")
        library_files = walk_source_files(root)
        _plog(
            plan_log_path,
            f"Found {len(library_files)} file(s) in "
            f"{time.monotonic() - t0:.1f}s.",
        )

        if not library_files:
            typer.echo("Library is empty; nothing to dedupe.")
            return

        t0 = time.monotonic()
        silent_progress.begin(
            f"Reading metadata from {len(library_files)} file(s) "
            f"(one ExifTool call; can take a while)..."
        )
        try:
            cache = build_cache(root)
        except ExifToolNotFound as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1) from e
        except ExifToolFailed as e:
            typer.echo(f"Error: exiftool failed.\n{e}", err=True)
            raise typer.Exit(code=1) from e
        for p in library_files:
            if p not in cache:
                cache[p] = FileMetadata(path=p, raw={"SourceFile": str(p)})
        _plog(
            plan_log_path,
            f"Read {len(cache)} file(s) in {time.monotonic() - t0:.1f}s.",
        )

    t0 = time.monotonic()
    _plog(plan_log_path, "Generating plan...")
    try:
        with (
            debug.writing_to(runs_dir),
            plan_log_path.open("a", encoding="utf-8") as plan_log,
        ):
            result = generate_plan(
                library_root=root,
                cache=cache,
                run_id=run_id,
                run_dir=runs_dir,
                plan_log=plan_log,
            )
    except UnmigratedFilesError as e:
        typer.echo(f"Error: {e}", err=True)
        for p in e.paths:
            typer.echo(f"  {p}", err=True)
        raise typer.Exit(code=1) from e
    except MissingHashesError as e:
        typer.echo(f"Error: {e}", err=True)
        for p in e.paths:
            typer.echo(f"  {p}", err=True)
        raise typer.Exit(code=1) from e
    _plog(
        plan_log_path,
        f"Plan generated in {time.monotonic() - t0:.1f}s.",
    )

    plan_path = runs_dir / "plan.txt"
    plan_text = serialize_plan(
        source=root, result=result, library_root=root
    )
    plan_path.write_text(plan_text, encoding="utf-8")

    typer.echo(f"Plan written: {plan_path}")
    typer.echo(
        f"Summary: {len(result.plan.lines)} DEDUP across "
        f"{len(result.groups)} group(s)."
    )

    if not result.plan.lines:
        typer.echo("No duplicates found; nothing to do.")
        return

    kept_line_ids = {ln.line_id for ln in result.plan.lines}
    while True:
        typer.echo("")
        choice = prompt_apply()
        if choice == "n":
            typer.echo("Aborted; plan file left in place.")
            return
        if choice == "e":
            open_in_editor(plan_path)
            edited_text = plan_path.read_text(encoding="utf-8")
            kept_line_ids = parse_kept_line_ids(edited_text)
            kept = sum(
                1 for ln in result.plan.lines if ln.line_id in kept_line_ids
            )
            typer.echo("")
            typer.echo(
                f"After edit: {kept} DEDUP "
                f"(of {len(result.plan.lines)})."
            )
            continue
        break  # 'y'

    try:
        completed = apply_plan(
            plan=result.plan,
            kept_line_ids=kept_line_ids,
            run_dir=runs_dir,
            library_root=root,
        )
    except DedupeApplyError as e:
        typer.echo(f"Error: apply failed: {e}", err=True)
        raise typer.Exit(code=1) from e

    typer.echo("")
    typer.echo(
        f"Removed {completed} duplicate(s) across "
        f"{len(result.groups)} group(s)."
    )


def _plog(plan_log_path: Path, msg: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    with plan_log_path.open("a", encoding="utf-8") as f:
        f.write(f"{ts} {msg}\n")
