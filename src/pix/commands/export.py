"""Implementation of `pix export [<name>]`.

End-to-end flow per spec/export.md: resolve distributions → walk the library
→ compute the desired set → inspect the delivery target → stop on unexplained
drift → plan → editor/apply prompt → apply → persist the manifest.

Bare `pix export` reconciles every distribution — the "reprovision everything
after a curation session" gesture. Named `pix export top` does just that one.
"""

from __future__ import annotations

import time
from pathlib import Path

import typer

from pix import banner, export_manifest
from pix.config import Config, Distribution, new_run_dir, settings_path
from pix.duration import format_duration_precise, format_size
from pix.editor import open_in_editor, parse_kept_line_ids, prompt_apply
from pix.export import (
    Drift,
    ExportAction,
    ExportLine,
    ExportPlan,
    MissingHashesError,
    Source,
    adopt,
    apply_plan,
    build_plan,
    classify,
    desired_members,
    scan_target,
)
from pix.export_manifest import Manifest, Member
from pix.hash_cache import read_all_cached_hashes
from pix.library_lock import LockHeld, acquire as acquire_lock
from pix.metadata import (
    ExifToolFailed,
    ExifToolNotFound,
    FileMetadata,
    filter_cache_misses,
    read_metadata_batched,
)
from pix.metadata_cache import PerFileCache
from pix.metadata_filter import consumed_read_args
from pix.organize import OrganizeError, Template, parse_template
from pix.progress import LiveProgress
from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.scan import walk_source_files

# How many drifted paths to print before eliding.
_SAMPLE = 10


def export_library(
    path: Path, name: str | None = None, no_prompt: bool = False
) -> None:
    """Reconcile one distribution, or every distribution when `name` is None.

    `no_prompt` skips the `Apply?` confirmation, as on the other
    plan-applying ops. It covers removals the manifest fully explains —
    a curation session produces those every time — but **never** covers
    unexplained drift, which stops the run either way.
    """
    path = path.resolve()
    try:
        root = resolve_root(start=path)
    except NoLibraryRoot as e:
        banner()
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    banner()

    config_path = settings_path(root)
    try:
        config = Config.load(config_path)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    if not config.exports:
        typer.echo(
            "No distributions configured. Add an `exports:` section to "
            f"{config_path}:\n"
            "\n"
            "  exports:\n"
            "    general:\n"
            "      path: 'D:\\SynologyDrive\\Photos-General'\n"
            "      filter: 'rating:3,4,5'\n"
            "      template: '{year}/{event}'",
            err=True,
        )
        raise typer.Exit(code=1)

    if name is not None and name not in config.exports:
        typer.echo(
            f"Error: no export named {name!r}. Configured: "
            f"{sorted(config.exports)}",
            err=True,
        )
        raise typer.Exit(code=1)

    chosen = (
        [config.exports[name]]
        if name is not None
        else [config.exports[k] for k in sorted(config.exports)]
    )

    # Parse every template before touching anything — config can't do it
    # (import cycle), so this is where a bad template fails fast, named.
    templates: dict[str, Template] = {}
    for dist in chosen:
        try:
            templates[dist.name] = parse_template(dist.template)
        except OrganizeError as e:
            typer.echo(f"Error: export {dist.name!r} template: {e}", err=True)
            raise typer.Exit(code=1) from e

    try:
        with acquire_lock(root, "export"):
            failures = 0
            for dist in chosen:
                failures += _run_one(
                    root=root,
                    config=config,
                    config_path=config_path,
                    dist=dist,
                    template=templates[dist.name],
                    no_prompt=no_prompt,
                )
    except LockHeld as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    if failures:
        raise typer.Exit(code=1)


def _run_one(
    root: Path,
    config: Config,
    config_path: Path,
    dist: Distribution,
    template: Template,
    no_prompt: bool,
) -> int:
    """Reconcile one distribution. Returns a non-zero count on failure."""
    typer.echo("")
    typer.echo(f"=== export: {dist.name} -> {dist.path}")
    typer.echo(
        f"    filter: {dist.filter.raw or '(everything)'}   "
        f"extensions: {','.join(sorted(dist.extensions))}   "
        f"template: {dist.template}"
    )

    target = Path(dist.path)
    desired = _desired(root, dist, template)
    if desired is None:
        return 1

    actual = scan_target(target)
    manifest = export_manifest.load(root, dist.name)

    adopted = 0
    if manifest is None or manifest.target != str(target):
        if actual:
            # Lost/repointed manifest with files already there: adopt what
            # matches by content hash so we don't duplicate beside them.
            typer.echo(
                f"No manifest for this target; checking {len(actual)} "
                f"existing file(s) by content hash..."
            )
            recovered = adopt(target, actual, desired)
            adopted = len(recovered.members)
            manifest = Manifest(
                distribution=dist.name,
                target=str(target),
                members=recovered.members,
            )
            typer.echo(f"Adopted {adopted} existing file(s).")
        else:
            manifest = Manifest(
                distribution=dist.name, target=str(target), members={}
            )

    present, missing, drift = classify(manifest.members, actual)

    if drift:
        _report_drift(dist, target, drift)
        return 1

    plan = build_plan(
        distribution=dist.name,
        target=target,
        desired=desired,
        manifest_members=manifest.members,
        present=present,
        missing=missing,
        adopted=adopted,
    )

    if not plan.lines:
        typer.echo(
            f"Already in sync: {plan.in_sync} member(s); nothing to do."
        )
        _save(root, dist, target, manifest.members)
        return 0

    _run_id, runs_dir = new_run_dir(root, config)
    plan_path = runs_dir / f"export-{dist.name}.txt"
    plan_path.write_text(plan.to_text(), encoding="utf-8")

    typer.echo(f"Plan written: {plan_path}")
    typer.echo(f"Summary: {_summarize(plan)}")

    kept_line_ids = {ln.line_id for ln in plan.lines}
    while not no_prompt:
        typer.echo("")
        choice = prompt_apply()
        if choice == "n":
            typer.echo("Aborted; plan file left in place.")
            return 0
        if choice == "e":
            open_in_editor(plan_path)
            kept_line_ids = parse_kept_line_ids(
                plan_path.read_text(encoding="utf-8")
            )
            kept = [ln for ln in plan.lines if ln.line_id in kept_line_ids]
            typer.echo("")
            typer.echo(f"After edit: {_summarize(plan, kept)}")
            continue
        break  # 'y'

    apply_log_path = runs_dir / f"export-{dist.name}.log"
    runnable = sum(1 for ln in plan.lines if ln.line_id in kept_line_ids)
    t0 = time.monotonic()
    try:
        with (
            apply_log_path.open("a", encoding="utf-8") as apply_log,
            LiveProgress(total=runnable) as progress,
        ):
            progress.begin(f"Provisioning {dist.name}")

            def _log(message: str) -> None:
                apply_log.write(f"{message}\n")

            result = apply_plan(
                plan=plan,
                kept_line_ids=kept_line_ids,
                members=manifest.members,
                log=_log,
                on_progress=progress.advance,
            )
    finally:
        # Persist whatever landed, even on interrupt: a copy we made but
        # didn't record reads as foreign next run and stops the reconcile.
        _save(root, dist, target, manifest.members)

    typer.echo("")
    typer.echo(
        f"Provisioned {result.completed} change(s) in "
        f"{format_duration_precise(time.monotonic() - t0)}"
        + (f"; {result.pruned_folders} empty folder(s) removed" if result.pruned_folders else "")
        + "."
    )
    if result.failed:
        typer.echo(
            f"Error: {result.failed} action(s) failed; see {apply_log_path}",
            err=True,
        )
        return 1
    typer.echo(f"Log: {apply_log_path}")
    return 0


def _desired(
    root: Path, dist: Distribution, template: Template
) -> dict[str, Source] | None:
    """Library-side half: walk, read metadata + hashes, select, render."""
    t0 = time.monotonic()
    scanned = walk_source_files(root)
    if not scanned:
        typer.echo("Library is empty; nothing to export.")
        return {}

    meta_cache = PerFileCache.for_library(root)
    with LiveProgress(total=len(scanned)) as progress:
        progress.begin("Loading cache")
        hits, misses = filter_cache_misses(
            scanned, meta_cache, on_batch=progress.advance
        )

    fresh: dict[Path, FileMetadata] = {}
    if misses:
        try:
            with LiveProgress(total=len(misses)) as progress:
                progress.begin("Filling missing cache")
                fresh = read_metadata_batched(
                    misses,
                    cache=meta_cache,
                    on_batch=progress.advance,
                    tags=consumed_read_args(),
                )
        except ExifToolNotFound as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1) from e
        except ExifToolFailed as e:
            typer.echo(f"Error: exiftool failed.\n{e}", err=True)
            raise typer.Exit(code=1) from e

    cache = {**hits, **fresh}
    with LiveProgress(total=len(scanned)) as progress:
        progress.begin("Reading hashes")
        hashes = read_all_cached_hashes(root, scanned, on_batch=progress.advance)

    sizes = {p: size for p, size, _mtime in scanned}
    try:
        desired = desired_members(
            [p for p, _s, _m in scanned],
            cache,
            hashes,
            sizes,
            dist,
            template,
        )
    except MissingHashesError as e:
        typer.echo(f"Error: {e}", err=True)
        for p in e.paths[:_SAMPLE]:
            typer.echo(f"  {p}", err=True)
        if len(e.paths) > _SAMPLE:
            typer.echo(f"  ... and {len(e.paths) - _SAMPLE} more", err=True)
        return None

    typer.echo(
        f"Selected {len(desired)} of {len(scanned)} file(s) in "
        f"{format_duration_precise(time.monotonic() - t0)}."
    )
    return desired


def _report_drift(dist: Distribution, target: Path, drift: Drift) -> None:
    """Describe an unexplained target and stop — never guess."""
    typer.echo("", err=True)
    typer.echo(
        f"Error: the delivery target for {dist.name!r} has changed in ways "
        f"pix can't explain, so nothing was touched.",
        err=True,
    )
    typer.echo(f"  Target: {target}", err=True)
    if drift.modified:
        typer.echo(
            f"\n  {len(drift.modified)} file(s) pix provisioned have been "
            f"modified or replaced since:",
            err=True,
        )
        _sample(drift.modified)
    if drift.foreign:
        typer.echo(
            f"\n  {len(drift.foreign)} file(s) in the target were not put "
            f"there by this distribution:",
            err=True,
        )
        _sample(drift.foreign)
    typer.echo(
        "\n  pix never modifies or deletes files it didn't provision. "
        "Check that\n"
        f"  `path:` points where you expect, then either remove the "
        f"unexpected\n  files or delete the manifest to re-adopt the target:\n"
        f"    {export_manifest.manifest_path(Path('<library>'), dist.name)}",
        err=True,
    )


def _sample(paths: list[str]) -> None:
    for rel in paths[:_SAMPLE]:
        typer.echo(f"    {rel}", err=True)
    if len(paths) > _SAMPLE:
        typer.echo(f"    ... and {len(paths) - _SAMPLE} more", err=True)


def _save(
    root: Path, dist: Distribution, target: Path, members: dict[str, Member]
) -> None:
    export_manifest.save(
        root,
        Manifest(
            distribution=dist.name, target=str(target), members=members
        ),
    )


def _summarize(
    plan: ExportPlan, lines: list[ExportLine] | None = None
) -> str:
    """`2 REMOVE, 3 COPY, 1.2 GB to write` — removals first."""
    subset = plan.lines if lines is None else lines
    counts: dict[ExportAction, int] = {a: 0 for a in ExportAction}
    for line in subset:
        counts[line.action] += 1
    parts = [
        f"{counts[action]} {action.value}"
        for action in (
            ExportAction.REMOVE,
            ExportAction.MOVE,
            ExportAction.REPLACE,
            ExportAction.COPY,
        )
        if counts[action]
    ]
    written = sum(
        ln.size
        for ln in subset
        if ln.action in (ExportAction.COPY, ExportAction.REPLACE)
    )
    if written:
        parts.append(f"{format_size(written)} to write")
    return ", ".join(parts) if parts else "nothing to do"
