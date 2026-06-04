"""Implementation of `pix dedupe`.

Three modes share one pipeline (walk → cache → hashes → fingerprints →
group → plan):

- **Auto** (`pix dedupe <path>`): build the plan and apply it (prompt unless
  `--no-prompt`). Images group by exact content hash; videos group by
  perceptual fingerprint within `[--min, --max]` (default 0–30). Removed
  files are conserved to the run folder.
- **Checkout** (`--checkout <dir>`): write a montage + manifest per *video
  perceptual* group into `<dir>` for human review; delete nothing. Locks
  only while scanning/grouping, then releases (montages render lock-free).
- **Commit** (`--commit <dir>`): re-group the library, keep the groups whose
  montage still exists in `<dir>`, and apply those.

See spec/dedupe.md.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import typer

from pix import banner, debug, dedupe_review
from pix.cache_base import prune_orphans, remove_all
from pix.checkout import CheckoutOpen, ensure_no_open_checkout
from pix.dedupe import (
    DEFAULT_MAX_DISTANCE,
    DEFAULT_MIN_DISTANCE,
    DedupeApplyError,
    DedupeGroup,
    DedupeResult,
    MissingHashesError,
    UnmigratedFilesError,
    apply_plan,
    generate_plan,
    is_dedupe_video,
    serialize_plan,
)
from pix.duration import format_duration_precise
from pix.editor import open_in_editor, parse_kept_line_ids, prompt_apply
from pix.hash_cache import (
    read_all_cached_hashes,
    read_cached_hash,
    write_cached_hash,
)
from pix.library_lock import LockHeld, acquire as acquire_lock
from pix.metadata import (
    ExifToolFailed,
    ExifToolNotFound,
    FileMetadata,
    filter_cache_misses,
    read_metadata_batched,
)
from pix.metadata_cache import PerFileCache
from pix.organize import CwdInsideLibraryError, check_cwd_not_inside
from pix.plan import Action
from pix.progress import LiveProgress
from pix.root import NoLibraryRoot, resolve as resolve_root
from pix.scan import walk_source_files
from pix.vfp_cache import read_all_cached_fingerprints, write_cached_fingerprint
from pix.video_fingerprint import (
    FingerprintFailed,
    VideoFingerprint,
    compute_fingerprint,
)


_FINGERPRINT_WORKERS = 14
_MONTAGE_WORKERS = 12


def _make_run_dir(root: Path) -> tuple[str, Path]:
    """Create a fresh `runs/<id>` folder, uniquified if two runs land in the
    same second (e.g. a fast checkout→commit)."""
    base = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    runs = root / ".pix" / "runs"
    run_id, n = base, 2
    while (runs / run_id).exists():
        run_id, n = f"{base}_{n}", n + 1
    (runs / run_id).mkdir(parents=True)
    return run_id, runs / run_id


def dedupe_library(
    path: Path | None = None,
    no_prompt: bool = False,
    min_distance: int = DEFAULT_MIN_DISTANCE,
    max_distance: int = DEFAULT_MAX_DISTANCE,
    checkout: Path | None = None,
    commit: Path | None = None,
    videos_only: bool = False,
) -> None:
    """Dispatch to the auto / checkout / commit mode (see module docstring)."""
    try:
        if commit is not None:
            _run_commit(commit.resolve())
            return

        if path is None:
            typer.echo("Error: a library path is required.", err=True)
            raise typer.Exit(code=1)
        path = path.resolve()
        try:
            root = resolve_root(start=path)
        except NoLibraryRoot as e:
            banner()
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1) from e
        banner()
        try:
            ensure_no_open_checkout(root)
            check_cwd_not_inside(root)
        except (CheckoutOpen, CwdInsideLibraryError) as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1) from e

        if checkout is not None:
            _run_checkout(root, checkout.resolve(), min_distance, max_distance)
        else:
            with acquire_lock(root, "dedupe"):
                _run_dedupe(
                    root, no_prompt, min_distance, max_distance, videos_only
                )
    except LockHeld as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e


# --- shared build ----------------------------------------------------------


def _build_result(
    root: Path,
    run_id: str,
    runs_dir: Path,
    plan_log_path: Path,
    min_distance: int,
    max_distance: int,
    videos_only: bool = False,
) -> tuple[DedupeResult, dict[Path, VideoFingerprint | None]]:
    """Walk → cache → hashes → fingerprints → group → plan. Shared by all
    three modes. Echoes + exits cleanly on an empty library; raises
    typer.Exit on prerequisite/ExifTool errors."""
    _plog(plan_log_path, f"Library root: {root}")
    t0 = time.monotonic()
    scanned = walk_source_files(root)
    _plog(
        plan_log_path,
        f"Found {len(scanned)} file(s) in "
        f"{format_duration_precise(time.monotonic() - t0)}.",
    )
    if not scanned:
        typer.echo("Library is empty; nothing to dedupe.")
        raise typer.Exit()

    library_files = [p for p, _, _ in scanned]
    prune_stats = prune_orphans(root, set(library_files))
    if prune_stats.orphans_removed or prune_stats.legacy_removed:
        _plog(
            plan_log_path,
            f"Pruned {prune_stats.orphans_removed} orphan + "
            f"{prune_stats.legacy_removed} legacy cache sidecar(s).",
        )

    t0 = time.monotonic()
    meta_cache = PerFileCache.for_library(root)
    with LiveProgress(total=len(scanned)) as check_progress:
        check_progress.begin("Loading cache")
        hits, misses = filter_cache_misses(
            scanned, meta_cache, on_batch=lambda n: check_progress.advance(by=n)
        )
    fresh: dict[Path, FileMetadata] = {}
    if misses:
        try:
            with LiveProgress(total=len(misses)) as read_progress:
                read_progress.begin("Filling missing cache")
                fresh = read_metadata_batched(
                    misses, cache=meta_cache,
                    on_batch=lambda n: read_progress.advance(by=n),
                )
        except ExifToolNotFound as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1) from e
        except ExifToolFailed as e:
            typer.echo(f"Error: exiftool failed.\n{e}", err=True)
            raise typer.Exit(code=1) from e
    cache = {**hits, **fresh}
    for p in library_files:
        if p not in cache:
            cache[p] = FileMetadata(path=p, raw={"SourceFile": str(p)})

    t0 = time.monotonic()
    with LiveProgress(total=len(scanned)) as hash_progress:
        hash_progress.begin("Reading hashes")
        hashes = read_all_cached_hashes(
            root, scanned, on_batch=lambda n: hash_progress.advance(by=n)
        )

    fingerprints = _load_fingerprints(root, scanned, plan_log_path)

    _plog(plan_log_path, "Generating plan...")
    t0 = time.monotonic()
    try:
        with (
            debug.writing_to(runs_dir),
            plan_log_path.open("a", encoding="utf-8") as plan_log,
        ):
            result = generate_plan(
                library_root=root,
                cache=cache,
                hashes=hashes,
                run_id=run_id,
                run_dir=runs_dir,
                plan_log=plan_log,
                fingerprints=fingerprints,
                min_distance=min_distance,
                max_distance=max_distance,
                videos_only=videos_only,
            )
    except UnmigratedFilesError as e:
        typer.echo(f"Error: {e}", err=True)
        _echo_sample(e.paths)
        raise typer.Exit(code=1) from e
    except MissingHashesError as e:
        typer.echo(f"Error: {e}", err=True)
        _echo_sample(e.paths)
        raise typer.Exit(code=1) from e
    _plog(
        plan_log_path,
        f"Plan generated in {format_duration_precise(time.monotonic() - t0)}.",
    )
    return result, fingerprints


def _load_fingerprints(
    root: Path,
    scanned: list[tuple[Path, int, int]],
    plan_log_path: Path,
) -> dict[Path, VideoFingerprint | None]:
    """Cached `.vfp` read for every video; compute+cache any missing.

    The compute step decodes a handful of frames per video — the one-time
    cost paid on the first dedupe/sync after a video is added/re-encoded;
    later runs hit the cache. A video that can't be fingerprinted maps to
    None and is never grouped (so never deleted)."""
    video_meta = [(p, sz, mt) for p, sz, mt in scanned if is_dedupe_video(p)]
    if not video_meta:
        return {}
    cached = read_all_cached_fingerprints(root, video_meta)
    result: dict[Path, VideoFingerprint | None] = {
        p: fp for p, fp in cached.items() if fp is not None
    }
    mt_by_path = {p: (sz, mt) for p, sz, mt in video_meta}
    missing = [p for p, _, _ in video_meta if cached.get(p) is None]
    if not missing:
        return result

    def _work(p: Path) -> tuple[Path, VideoFingerprint | None]:
        try:
            return p, compute_fingerprint(str(p))
        except FingerprintFailed:
            return p, None

    _plog(plan_log_path, f"Fingerprinting {len(missing)} video(s)...")
    t0 = time.monotonic()
    with LiveProgress(total=len(missing)) as prog:
        prog.begin("Fingerprinting videos")
        with ThreadPoolExecutor(max_workers=_FINGERPRINT_WORKERS) as ex:
            for p, fp in ex.map(_work, missing):
                if fp is not None:
                    sz, mt = mt_by_path[p]
                    write_cached_fingerprint(
                        root, p, fingerprint=fp, size=sz, mtime_ns=mt
                    )
                result[p] = fp
                prog.advance()
    _plog(
        plan_log_path,
        f"Fingerprinted {len(missing)} video(s) in "
        f"{format_duration_precise(time.monotonic() - t0)}.",
    )
    return result


# --- auto mode -------------------------------------------------------------


def _run_dedupe(
    root: Path, no_prompt: bool, min_distance: int, max_distance: int,
    videos_only: bool = False,
) -> None:
    run_id, runs_dir = _make_run_dir(root)
    plan_log_path = runs_dir / "plan.log"

    result, _fps = _build_result(
        root, run_id, runs_dir, plan_log_path, min_distance, max_distance,
        videos_only,
    )

    plan_path = runs_dir / "plan.txt"
    plan_path.write_text(
        serialize_plan(source=root, result=result, library_root=root),
        encoding="utf-8",
    )
    if not result.plan.lines:
        typer.echo("No duplicates found; nothing to do.")
        return

    dedup_total = sum(1 for ln in result.plan.lines if ln.action == Action.DEDUP)
    merge_total = sum(1 for ln in result.plan.lines if ln.action == Action.MERGE)
    exact_groups = sum(1 for g in result.groups if g.kind == "exact")
    perc_groups = sum(1 for g in result.groups if g.kind == "perceptual")
    typer.echo(f"Plan written: {plan_path}")
    typer.echo(
        f"Summary: {dedup_total} DEDUP, {merge_total} MERGE across "
        f"{len(result.groups)} group(s) "
        f"({exact_groups} exact image, {perc_groups} perceptual video "
        f"[{min_distance}, {max_distance}])."
    )

    kept_line_ids = {ln.line_id for ln in result.plan.lines}
    while not no_prompt:
        typer.echo("")
        choice = prompt_apply()
        if choice == "n":
            typer.echo("Aborted; plan file left in place.")
            return
        if choice == "e":
            open_in_editor(plan_path)
            kept_line_ids = parse_kept_line_ids(
                plan_path.read_text(encoding="utf-8")
            )
            kept_dedup = sum(
                1 for ln in result.plan.lines
                if ln.line_id in kept_line_ids and ln.action == Action.DEDUP
            )
            kept_merge = sum(
                1 for ln in result.plan.lines
                if ln.line_id in kept_line_ids and ln.action == Action.MERGE
            )
            typer.echo("")
            typer.echo(
                f"After edit: {kept_dedup} DEDUP, {kept_merge} MERGE "
                f"(of {dedup_total} / {merge_total})."
            )
            continue
        break  # 'y'

    _apply_result(root, runs_dir, result, kept_line_ids)


# --- checkout mode ---------------------------------------------------------


def _run_checkout(
    root: Path, review_dir: Path, min_distance: int, max_distance: int
) -> None:
    """Stage video perceptual groups into `review_dir` for human review.

    Locks only for the scan/group/manifest write; releases before rendering
    montages (a file moving out from under a montage is harmless — the
    manifest is authoritative). Deletes nothing."""
    run_id, runs_dir = _make_run_dir(root)
    plan_log_path = runs_dir / "plan.log"

    with acquire_lock(root, "dedupe-checkout"):
        result, fingerprints = _build_result(
            root, run_id, runs_dir, plan_log_path, min_distance, max_distance,
            videos_only=True,  # review tool: perceptual video groups only
        )
        groups = [g for g in result.groups if g.kind == "perceptual"]
        if not groups:
            typer.echo(
                f"No perceptual video duplicates found in distance band "
                f"[{min_distance}, {max_distance}]."
            )
            return
        id_groups = [(f"g{i:04d}", g) for i, g in enumerate(groups, start=1)]
        dedupe_review.write_manifest(
            review_dir, root, id_groups, min_distance, max_distance
        )
        durations = {
            p: fp.duration for p, fp in fingerprints.items() if fp is not None
        }
    # lock released — render montages lock-free
    typer.echo(
        f"Reviewing {len(id_groups)} perceptual group(s) "
        f"(band [{min_distance}, {max_distance}]) → {review_dir}"
    )
    def _render(item: tuple[str, DedupeGroup]) -> None:
        gid, group = item
        members = [group.keeper, *group.losers]
        dedupe_review.render_montage(
            review_dir, gid, group.distance, members, durations
        )

    # Rendering is process-spawn/IO-light, so it parallelizes well — a serial
    # loop leaves the machine idle waiting on ffmpeg startup.
    with LiveProgress(total=len(id_groups)) as prog:
        prog.begin("Rendering montages")
        with ThreadPoolExecutor(max_workers=_MONTAGE_WORKERS) as ex:
            for _ in ex.map(_render, id_groups):
                prog.advance()
    typer.echo("")
    typer.echo(
        f"Delete the montage of any group you DON'T want deduped, then run:\n"
        f"  pix dedupe --commit \"{review_dir}\""
    )


# --- commit mode -----------------------------------------------------------


def _run_commit(review_dir: Path) -> None:
    """Apply the groups whose montage still exists in `review_dir`.

    Re-groups the library fresh (so files are re-validated against current
    bytes) and keeps only groups whose member set still matches a surviving
    montage — a group changed since checkout simply won't match and is
    skipped."""
    banner()
    try:
        manifest = dedupe_review.read_manifest(review_dir)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        typer.echo(
            f"Error: {review_dir} is not a dedupe checkout folder "
            f"(no readable {dedupe_review.MANIFEST_NAME}).",
            err=True,
        )
        raise typer.Exit(code=1) from e

    root = Path(str(manifest.get("library_root", "")))
    if not root.is_dir():
        typer.echo(f"Error: library root {root} not found.", err=True)
        raise typer.Exit(code=1)
    min_d = int(manifest.get("min_distance", DEFAULT_MIN_DISTANCE))
    max_d = int(manifest.get("max_distance", DEFAULT_MAX_DISTANCE))

    survivors = set(dedupe_review.surviving_member_sets(review_dir))
    if not survivors:
        typer.echo(
            "No groups selected (all montages were removed); nothing to do."
        )
        return

    try:
        ensure_no_open_checkout(root)
    except CheckoutOpen as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    run_id, runs_dir = _make_run_dir(root)
    plan_log_path = runs_dir / "plan.log"

    with acquire_lock(root, "dedupe-commit"):
        result, _fps = _build_result(
            root, run_id, runs_dir, plan_log_path, min_d, max_d,
            videos_only=True,  # commit only applies perceptual survivors
        )
        dedup_by_path = {
            ln.abs_path: ln.line_id
            for ln in result.plan.lines if ln.action == Action.DEDUP
        }
        merge_by_path = {
            ln.abs_path: ln.line_id
            for ln in result.plan.lines if ln.action == Action.MERGE
        }
        kept_line_ids: set[str] = set()
        matched = 0
        for group in result.groups:
            if group.kind != "perceptual":
                continue
            members = frozenset(
                _rel(m, root) for m in [group.keeper, *group.losers]
            )
            if members not in survivors:
                continue
            matched += 1
            for loser in group.losers:
                if loser in dedup_by_path:
                    kept_line_ids.add(dedup_by_path[loser])
            if group.keeper in merge_by_path:
                kept_line_ids.add(merge_by_path[group.keeper])

        skipped = len(survivors) - matched
        if skipped:
            typer.echo(
                f"{skipped} selected group(s) no longer match the library "
                f"(changed since checkout) — skipping them.",
                err=True,
            )
        if not kept_line_ids:
            typer.echo("Nothing to apply (no selected group still matches).")
            return
        typer.echo(f"Committing {matched} reviewed group(s).")
        _apply_result(root, runs_dir, result, kept_line_ids)


# --- shared apply ----------------------------------------------------------


def _apply_result(
    root: Path,
    runs_dir: Path,
    result: DedupeResult,
    kept_line_ids: set[str],
) -> None:
    """Apply `kept_line_ids` of `result`: conserve+remove losers, MERGE tags
    onto keepers, update caches (incl. re-stamping each merged keeper's
    metadata-invariant content hash). Shared by auto + commit."""
    # Capture each surviving keeper's content hash before apply mutates it
    # (a MERGE tag-write bumps mtime → stale (size,mtime) key, but the
    # content hash is metadata-invariant). Re-stamped post-apply so organize
    # still finds a valid cached hash. See spec/dedupe.md.
    merge_keeper_hashes: dict[Path, str | None] = {
        ln.abs_path: read_cached_hash(root, ln.abs_path)
        for ln in result.plan.lines
        if ln.line_id in kept_line_ids and ln.action == Action.MERGE
    }

    apply_log_path = runs_dir / "apply.log"
    try:
        try:
            removed, merged, quarantined = apply_plan(
                plan=result.plan,
                kept_line_ids=kept_line_ids,
                run_dir=runs_dir,
                library_root=root,
            )
        except DedupeApplyError as e:
            typer.echo(f"Error: apply failed: {e}", err=True)
            raise typer.Exit(code=1) from e

        for ln in result.plan.lines:
            if ln.line_id in kept_line_ids:
                remove_all(root, ln.abs_path)
        for keeper, hash_hex in merge_keeper_hashes.items():
            if hash_hex is None:
                continue
            try:
                st = keeper.stat()
            except OSError:
                continue
            write_cached_hash(
                root, keeper, hash_hex=hash_hex,
                size=st.st_size, mtime_ns=st.st_mtime_ns,
            )

        typer.echo("")
        typer.echo(f"Removed {removed} duplicate(s).")
        if merged:
            typer.echo(f"Merged tags onto {merged} keeper(s).")
        if quarantined:
            errors_dir = root / ".pix" / "errors"
            typer.echo("")
            typer.echo(
                f"{len(quarantined)} file(s) could not be processed "
                f"and were moved to {errors_dir}",
                err=True,
            )
            raise typer.Exit(code=1)
    finally:
        typer.echo(f"Log: {apply_log_path}")


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _echo_sample(paths: list[Path], limit: int = 10) -> None:
    """Print up to `limit` offending paths to stderr, then an elision note."""
    for p in paths[:limit]:
        typer.echo(f"  {p}", err=True)
    if len(paths) > limit:
        typer.echo(f"  ... and {len(paths) - limit} more", err=True)


def _plog(plan_log_path: Path, msg: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    with plan_log_path.open("a", encoding="utf-8") as f:
        f.write(f"{ts} {msg}\n")
