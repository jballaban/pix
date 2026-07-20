"""Implementation of `pix tag checkout` — tag editing via folder-shuffle.

Three actions on one command (see spec/tag-editing.md → CLI surface):

- `pix tag checkout <path> <template>` — start: materialize a scoped
  hard-link workspace + snapshot.
- `pix tag checkout --reset`           — discard the open checkout.
- `pix tag checkout --commit`          — apply tag edits (NOT yet built).
- bare `pix tag checkout`              — status.

Core logic lives in `pix.checkout`; this module is the CLI shell:
argument dispatch, root resolution, metadata loading, and console
output. The materialization itself runs under the library lock.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import typer

from pix import banner
from pix.apply import ApplyError, apply_plan
from pix.config import Config, new_run_dir, settings_path
from pix.checkout import (
    CheckoutError,
    CheckoutExists,
    CheckoutScopeError,
    CheckoutUnmigratedError,
    CommitDiff,
    Snapshot,
    checkout_dir,
    compute_pix_writes,
    create_checkout,
    diff_workspace,
    discard,
    is_open,
    read_snapshot,
    validate_checkout_template,
)
from pix.duration import format_duration_precise
from pix.editor import open_in_editor, parse_kept_line_ids, prompt_apply
from pix.hash_cache import read_cached_hash, write_cached_hash
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
from pix.plan import Action, Plan, PlanLine, attach_paths
from pix.progress import LiveProgress
from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.scan import walk_source_files


def run_checkout(
    path: Path | None,
    template_str: str | None,
    *,
    commit: bool,
    reset: bool,
) -> None:
    """Dispatch the `pix tag checkout` action based on flags + positionals."""
    if commit and reset:
        typer.echo(
            "Error: --commit and --reset are mutually exclusive.", err=True
        )
        raise typer.Exit(code=1)
    if (commit or reset) and (path is not None or template_str is not None):
        typer.echo(
            "Error: --commit and --reset take no positional arguments.",
            err=True,
        )
        raise typer.Exit(code=1)

    if reset:
        _do_reset()
        return
    if commit:
        _do_commit()
        return

    if path is None and template_str is None:
        _do_status()
        return
    if path is None or template_str is None:
        banner()
        typer.echo(
            "Error: starting a checkout needs both <path> and <template>, "
            "e.g. `pix tag checkout . {year}/{event}`. For the whole library, "
            "pass the library root as <path>.",
            err=True,
        )
        raise typer.Exit(code=1)

    _do_start(path, template_str)


# --- status ------------------------------------------------------------------


def _do_status() -> None:
    """Print whether a checkout is open and its details."""
    try:
        root = resolve_root(start=None)
    except NoLibraryRoot as e:
        banner()
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    banner()
    if not is_open(root):
        typer.echo("No checkout open.")
        typer.echo("Start one with `pix tag checkout <path> <template>`.")
        return

    cdir = checkout_dir(root)
    snap = read_snapshot(root)
    typer.echo(f"Checkout open at {cdir}")
    if snap is not None:
        typer.echo(f"  Template: {snap.template}")
        typer.echo(f"  Scope:    {snap.scope}")
        typer.echo(f"  Started:  {snap.created}")
        typer.echo(f"  Links:    {len(snap.links)}")
    else:
        typer.echo("  (snapshot.json missing or unreadable)")
    typer.echo("Run `pix tag checkout --commit` or `pix tag checkout --reset`.")


# --- reset -------------------------------------------------------------------


def _do_reset() -> None:
    """Discard the open checkout (the `--reset` action / unfreeze escape hatch)."""
    try:
        root = resolve_root(start=None)
    except NoLibraryRoot as e:
        banner()
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    banner()
    if discard(root):
        typer.echo("Checkout discarded; the library is no longer frozen.")
    else:
        typer.echo("No checkout to reset.")


# --- commit ------------------------------------------------------------------


def _do_commit() -> None:
    """Apply the open checkout's tag edits, then tear it down."""
    try:
        root = resolve_root(start=None)
    except NoLibraryRoot as e:
        banner()
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    banner()

    if not is_open(root):
        typer.echo("No checkout open; nothing to commit.")
        return

    snap = read_snapshot(root)
    if snap is None:
        typer.echo(
            "Error: the checkout snapshot is missing or unreadable. Run "
            "`pix tag checkout --reset` and start the checkout over.",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        template = parse_template(snap.template)
        validate_checkout_template(template)
    except (OrganizeError, CheckoutError) as e:
        typer.echo(f"Error: invalid template in snapshot: {e}", err=True)
        raise typer.Exit(code=1) from e

    try:
        with acquire_lock(root, "checkout-commit"):
            _run_commit(root, template, snap)
    except LockHeld as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e


def _run_commit(root: Path, template: Template, snap: Snapshot) -> None:
    """Diff → plan → confirm → apply → teardown, under the library lock."""
    diff = diff_workspace(root, template, snap)

    # Re-read current metadata for the files that moved — the override
    # math needs each file's live DateAuto/EventAuto (valid under the
    # freeze). Only the changed files, not the whole library.
    meta_by_path: dict[Path, FileMetadata] = {}
    assign_paths = [a.library_path for a in diff.assigns]
    if assign_paths:
        try:
            meta_by_path = read_metadata_batched(assign_paths, cache=None)
        except ExifToolNotFound as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1) from e
        except ExifToolFailed as e:
            typer.echo(f"Error: exiftool failed.\n{e}", err=True)
            raise typer.Exit(code=1) from e

    run_id, runs_dir = new_run_dir(root, Config.load(settings_path(root)))

    lines: list[PlanLine] = []
    for assign in diff.assigns:
        meta = meta_by_path.get(assign.library_path) or FileMetadata(
            path=assign.library_path,
            raw={"SourceFile": str(assign.library_path)},
        )
        writes, details = compute_pix_writes(assign.token_changes, meta)
        if not writes:
            continue  # move resolved to a no-op (value already effective)
        try:
            rel = assign.library_path.relative_to(root).as_posix()
        except ValueError:
            rel = str(assign.library_path)
        ln = PlanLine(
            line_id=f"L{len(lines) + 1:03d}",
            action=Action.TAG,
            rel_path=rel,
            details=details,
            abs_path=assign.library_path,
            pix_writes=writes,
        )
        lines.append(attach_paths(ln, runs_dir, runs_dir / "staging"))

    _report_skips(diff)

    if not lines:
        typer.echo(
            "Nothing to commit; checkout left open "
            "(use `pix tag checkout --reset` to discard it)."
        )
        return

    plan = Plan(
        source=root,
        run_id=run_id,
        generated_at=datetime.now(),
        lines=lines,
    )
    plan_path = runs_dir / "plan.txt"
    plan_path.write_text(
        _commit_plan_text(root, run_id, snap, lines), encoding="utf-8"
    )

    typer.echo(f"Plan written: {plan_path}")
    typer.echo(f"Summary: {len(lines)} TAG.")

    kept = {ln.line_id for ln in lines}
    while True:
        typer.echo("")
        choice = prompt_apply()
        if choice == "n":
            typer.echo("Aborted; checkout left open.")
            return
        if choice == "e":
            open_in_editor(plan_path)
            kept = parse_kept_line_ids(plan_path.read_text(encoding="utf-8"))
            typer.echo("")
            typer.echo(
                f"After edit: {sum(1 for ln in lines if ln.line_id in kept)} TAG."
            )
            continue
        break  # 'y'

    # A TAG write only touches metadata regions, which the content hash
    # deliberately excludes (see content_hash.py) — so the hash *value* is
    # unchanged. But the write bumps the file's size+mtime, which is what
    # the hash cache validates against, leaving the entry stale → organize
    # would refuse with "run pix hash first". Capture each file's cached
    # hash now (the freeze guarantees it's still valid) and re-key it to the
    # post-write size+mtime after apply, so no needless rehash is forced.
    applied_lines = [ln for ln in lines if ln.line_id in kept]
    pre_hashes: dict[Path, str] = {}
    for ln in applied_lines:
        h = read_cached_hash(root, ln.abs_path)
        if h is not None:
            pre_hashes[ln.abs_path] = h

    apply_log_path = runs_dir / "apply.log"
    try:
        try:
            completed, _ = apply_plan(
                plan=plan,
                plan_path=plan_path,
                run_dir=runs_dir,
                kept_line_ids=kept,
            )
        except ApplyError as e:
            typer.echo(f"Error: apply failed: {e}", err=True)
            raise typer.Exit(code=1) from e

        rekeyed = _rekey_hashes(root, pre_hashes)

        # Success → tear the workspace down and lift the freeze.
        discard(root)
        typer.echo("")
        typer.echo(f"Committed {completed} change(s); checkout closed.")
        if rekeyed:
            typer.echo(
                f"Re-keyed {rekeyed} hash cache entr(ies); no rehash needed."
            )
    finally:
        typer.echo(f"Log: {apply_log_path}")


def _rekey_hashes(root: Path, pre_hashes: dict[Path, str]) -> int:
    """Rewrite each captured hash entry against the post-write size+mtime.

    The hash value is metadata-invariant, so we keep the value we captured
    before the TAG write and only refresh the `(size, mtime_ns)` key. A file
    that vanished or can't be stat'd is skipped — the cache entry just stays
    stale and the next `pix hash` recomputes it. Returns the count re-keyed.
    """
    rekeyed = 0
    for path, hash_hex in pre_hashes.items():
        try:
            st = path.stat()
        except OSError:
            continue
        write_cached_hash(
            root,
            path,
            hash_hex=hash_hex,
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
        )
        rekeyed += 1
    return rekeyed


def _report_skips(diff: CommitDiff) -> None:
    """Surface non-edit classifications (informational, to stderr)."""

    def _sample(paths: list[Path], limit: int = 5) -> None:
        for p in paths[:limit]:
            typer.echo(f"  {p}", err=True)
        if len(paths) > limit:
            typer.echo(f"  ... and {len(paths) - limit} more", err=True)

    if diff.skipped_removals:
        typer.echo(
            f"{len(diff.skipped_removals)} file(s) had a tag removed/cleared "
            f"— not supported in v1, skipped:",
            err=True,
        )
        _sample(diff.skipped_removals)
    if diff.foreign:
        typer.echo(
            f"{len(diff.foreign)} file(s) in the workspace aren't part of "
            f"this checkout — ignored:",
            err=True,
        )
        _sample(diff.foreign)
    if diff.ambiguous:
        typer.echo(
            f"{len(diff.ambiguous)} link(s) were ambiguous (duplicate or "
            f"nested too deep) — skipped:",
            err=True,
        )
        _sample(diff.ambiguous)


def _commit_plan_text(
    root: Path, run_id: str, snap: Snapshot, lines: list[PlanLine]
) -> str:
    """Serialize the commit plan.txt (mirrors migrate's editable format)."""
    header = [
        f"# Commit plan: {root}",
        f"# Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"# Run ID: {run_id}",
        f"# Template: {snap.template}",
        f"# Source: checkout started {snap.created}",
        "#",
        "# Delete a line to skip that file this commit. "
        'Commented "#" lines are info only.',
        "# Format: L<line-id> | ACTION | path | details",
        "",
    ]
    body = [
        f"{ln.line_id} | {ln.action.value:<3} | {ln.rel_path} | {ln.details}"
        for ln in lines
    ]
    return "\n".join(header + body + ["", f"# Summary: {len(lines)} TAG", ""])


# --- start -------------------------------------------------------------------


def _do_start(path: Path, template_str: str) -> None:
    """Materialize a new scoped checkout workspace."""
    scope = path.resolve()
    try:
        root = resolve_root(start=scope)
    except NoLibraryRoot as e:
        banner()
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    banner()

    if is_open(root):
        typer.echo(f"Error: {CheckoutExists(checkout_dir(root))}", err=True)
        raise typer.Exit(code=1)

    # Scope validation: must be a real directory, and not inside .pix/
    # (tool scaffolding — nothing to check out). `resolve_root` already
    # guarantees scope is at or under the library root.
    pix_dir = (root / ".pix").resolve()
    if scope == pix_dir or pix_dir in scope.parents:
        typer.echo(
            f"Error: {scope} is inside .pix/ (tool state); pick a media "
            f"folder under the library root.",
            err=True,
        )
        raise typer.Exit(code=1)
    if not scope.is_dir():
        typer.echo(f"Error: {scope} is not a directory.", err=True)
        raise typer.Exit(code=1)

    try:
        template = parse_template(template_str)
        validate_checkout_template(template)
    except OrganizeError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    except CheckoutError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    try:
        with acquire_lock(root, "checkout"):
            _materialize(root, scope, template, template_str)
    except LockHeld as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e


def _materialize(
    root: Path, scope: Path, template: Template, template_str: str
) -> None:
    """Load scoped metadata, then build the workspace under the lock."""
    t0 = time.monotonic()
    scanned = walk_source_files(scope)
    if not scanned:
        typer.echo(f"No files under {scope}; nothing to check out.")
        return

    cache = _load_metadata(root, scanned)
    typer.echo(
        f"Read {len(cache)} file(s) in "
        f"{format_duration_precise(time.monotonic() - t0)}."
    )

    try:
        with LiveProgress() as progress:
            progress.begin("Linking checkout")
            count = create_checkout(
                library_root=root,
                scope=scope,
                template=template,
                cache=cache,
            )
    except CheckoutUnmigratedError as e:
        typer.echo(f"Error: {e}", err=True)
        for p in e.paths:
            typer.echo(f"  {p}", err=True)
        raise typer.Exit(code=1) from e
    except (CheckoutScopeError, CheckoutExists, CheckoutError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    cdir = checkout_dir(root)
    typer.echo("")
    typer.echo(f"Checked out {count} file(s) to {cdir}")
    typer.echo(f"Template: {template_str}")
    typer.echo(
        "Shuffle the links in your file explorer, then "
        "`pix tag checkout --commit` (or `--reset` to discard)."
    )


def _load_metadata(
    root: Path, scanned: list[tuple[Path, int, int]]
) -> dict[Path, FileMetadata]:
    """Cache lookup + ExifTool fill for the scoped file set.

    Same shape as organize/dedupe, minus the hash and video passes
    (checkout needs neither). Files with no metadata at all get an
    empty record so the prereq check can flag them as un-migrated.
    """
    meta_cache = PerFileCache.for_library(root)

    with LiveProgress(total=len(scanned)) as check_progress:
        check_progress.begin("Loading cache")

        def _on_check_batch(n: int) -> None:
            check_progress.advance(by=n)

        hits, misses = filter_cache_misses(
            scanned, meta_cache, on_batch=_on_check_batch
        )

    fresh: dict[Path, FileMetadata] = {}
    if misses:
        try:
            with LiveProgress(total=len(misses)) as read_progress:
                read_progress.begin("Filling missing cache")

                def _on_batch(n: int) -> None:
                    read_progress.advance(by=n)

                fresh = read_metadata_batched(
                    misses, cache=meta_cache, on_batch=_on_batch,
                    tags=consumed_read_args(),
                )
        except ExifToolNotFound as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1) from e
        except ExifToolFailed as e:
            typer.echo(f"Error: exiftool failed.\n{e}", err=True)
            raise typer.Exit(code=1) from e

    cache = {**hits, **fresh}
    for path, _size, _mtime in scanned:
        if path not in cache:
            cache[path] = FileMetadata(
                path=path, raw={"SourceFile": str(path)}
            )
    return cache
