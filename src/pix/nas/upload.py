"""`pix2 upload` — staging to master over SMB (spec/nas-app.md §9).

Takes every pending staging folder and lands it in master as one folder per
upload, `{name}_{upload-time}`, flattening each file's relative path into its
name so the archive stays self-describing when a file is pulled out of the tree.

**This is the only destructive step in the whole pipeline.** Import hardlinks,
process writes derived trees, and neither can lose anything. Upload clears
staging — so that clearing is gated per-folder on verification (every file
present at master at a matching size), not on the run merely finishing. A folder
that fails verification keeps its staging and says so.

Two properties the spec calls for explicitly:

- **Parallel.** Measured against this NAS, single-threaded small-file throughput
  is 11-20 MB/s against 28-34 MB/s at 32 threads. Sequential transfer of ~62k
  files would take over a day, so a worker pool is a schedule-correctness
  concern, not an optimisation.
- **Marker temp, then rename.** A killed copy must never leave a partial that a
  name-and-size check would accept as complete. The `*.__*` marker convention is
  already sync-excluded, so partials cannot leak anywhere either.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from blake3 import blake3

from pix.duration import format_duration_compact, format_size
from pix.ingest import MANIFEST_DIRNAME
from pix.markers import EXPORT_TMP_SUFFIX
from pix.progress import LiveProgress
from pix.nas import ledger, staging as st
from pix.nas.lock import UploadLock
from pix.nas.const import IMPORT_ROOT, LEDGER_NAME, MASTER_DIR

#: Concurrency for the SMB copy. 32 is where measured throughput plateaued;
#: past that the Atom serving the share becomes the limit, not the client.
WORKERS: int = 32

#: Read/write chunk for the hashing copy. Large enough that SMB round trips
#: dominate rather than syscall overhead.
CHUNK: int = 4 * 1024 * 1024

#: Records which master folder an interrupted upload was filling, so a resumed
#: run continues into it instead of starting a second one (the folder name
#: embeds a timestamp, so it cannot simply be recomputed).
TARGET_MARKER: str = ".upload-target"


class UploadError(Exception):
    """Upload could not start."""


@dataclass
class UploadSummary:
    """What one staging folder's upload did."""

    name: str
    master_folder: Path
    copied: int = 0
    skipped: int = 0            # already at master, from an interrupted run
    culled: int = 0             # sidecar with no media: recorded, never copied
    bytes_copied: int = 0
    verified: bool = False
    staging_cleared: bool = False
    cancelled: bool = False     # Ctrl+C: queue dropped, in-flight drained
    failed: list[str] = field(default_factory=lambda: [])


@dataclass(frozen=True)
class _Item:
    """One staged file, resolved to where it will live in master."""

    source: Path        # the staged file (a hardlink into the library)
    rel: str            # path relative to the staging root
    root: str           # the source root it was imported from
    size: int
    flat: str           # its flattened name in master


def pending_folders() -> list[Path]:
    """Every staging folder awaiting upload."""
    if not IMPORT_ROOT.is_dir():
        return []
    return sorted(p for p in IMPORT_ROOT.iterdir() if p.is_dir())


def flatten(rel: str) -> str:
    """Flatten a relative path into a single filename.

    `2015/a/one.jpg` becomes `2015_a_one.jpg`, so the name carries its own
    provenance: pull one file out of master and it still says where it came
    from (spec/nas-app.md §3).
    """
    return rel.replace("/", "_").replace("\\", "_")


def run_upload(*, echo: Callable[[str], None] = lambda _: None) -> list[UploadSummary]:
    """Upload every pending staging folder. Returns one summary per folder.

    Takes a single-writer lock: two uploads appending to one ledger interleave
    their writes and tear lines (see `lock.py`).
    """
    ledger.require_share()
    folders = pending_folders()
    if not folders:
        echo("nothing staged")
        return []
    summaries: list[UploadSummary] = []
    with UploadLock(IMPORT_ROOT):
        for folder in folders:
            summary = _upload_one(folder, echo=echo)
            summaries.append(summary)
            if summary.cancelled:
                remaining = len(folders) - len(summaries)
                if remaining:
                    echo(f"cancelled: {remaining} staging folder(s) not started")
                break
    return summaries


def _upload_one(staging: Path, *, echo: Callable[[str], None]) -> UploadSummary:
    name = staging.name
    target = _resolve_target(staging, name)
    target.mkdir(parents=True, exist_ok=True)

    summary = UploadSummary(name=name, master_folder=target)
    swept = _sweep_target(target)
    if swept:
        echo(f"swept {swept} partial copy(s) from an interrupted run")
    items, culled = _collect(staging)
    summary.culled = len(culled)

    ledger_path = target / LEDGER_NAME
    already = _already_recorded(ledger_path)
    if not ledger_path.exists():
        _write_header(ledger_path, name,
                  {i.root for i in items} | {c[1] for c in culled})

    echo(f"{name}: {len(items)} file(s) staged, {len(culled)} culled, "
         f"{len(already)} already recorded -> {target.name}")

    lock = threading.Lock()
    total_bytes = sum(i.size for i in items)
    started = time.monotonic()
    digests: dict[str, str] = {}
    # Mutable so the progress thread can read it without the lock. `inflight`
    # is what makes a cancel legible: the operator sees copies closing out
    # rather than a terminal that appears to have hung.
    state: dict[str, int] = {"inflight": 0, "cancelling": 0}

    with ledger_path.open("a", encoding="utf-8") as log:
        progress = LiveProgress(
            total=len(items),
            status_provider=lambda: _status(summary, len(items), total_bytes,
                                            started, state),
        )

        def handle(item: _Item) -> None:
            if state["cancelling"]:
                return
            state["inflight"] += 1
            try:
                _handle_one(item)
            finally:
                state["inflight"] -= 1

        def _handle_one(item: _Item) -> None:
            if item.flat in already:
                with lock:
                    summary.skipped += 1
                progress.advance()
                return
            try:
                moved, digest = _copy_one(item, target)
            except OSError as e:
                with lock:
                    summary.failed.append(f"{item.rel}: {e}")
                progress.advance()
                return
            with lock:
                if moved:
                    summary.copied += 1
                    summary.bytes_copied += item.size
                else:
                    summary.skipped += 1
                digests[item.flat] = digest
                _append(log, {
                    "rel": item.rel, "root": item.root, "size": item.size,
                    "file": item.flat, "blake3": digest, "outcome": "kept",
                })
            progress.advance()

        with progress:
            progress.begin(f"upload {name}")
            pool = ThreadPoolExecutor(max_workers=WORKERS)
            futures = [pool.submit(handle, item) for item in items]
            try:
                for future in as_completed(futures):
                    future.result()
            except KeyboardInterrupt:
                # Drop everything still queued, then let the copies already in
                # progress finish — killing them mid-write would leave marker
                # temps for no gain, and they are nearly done by definition.
                state["cancelling"] = 1
                dropped = sum(1 for f in futures if f.cancel())
                summary.cancelled = True
                echo(f"\ncancelling {name}: {dropped} queued dropped, "
                     f"draining {state['inflight']} in flight")
            finally:
                pool.shutdown(wait=True)

        with lock:
            for rel, root, size in culled:
                if flatten(rel) in already:
                    continue
                _append(log, {"rel": rel, "root": root, "size": size,
                              "outcome": "culled"})

    if summary.cancelled or summary.failed:
        # Never verify a partial batch: it would fail on files that were simply
        # never attempted, and verification is what gates deleting staging.
        summary.verified = False
    else:
        echo(f"{name}: verifying {len(items)} file(s) against master")
        summary.verified = _verify(items, target, digests)
    if summary.verified:
        _clear(staging)
        summary.staging_cleared = True
    return summary


def _status(summary: "UploadSummary", total_files: int, total_bytes: int,
            started: float, state: dict[str, int] | None = None) -> str:
    """The live body: done, transferred, rate, and an ETA.

    Read **without the lock**, deliberately. It is called from the progress
    thread once a second as well as from workers, so taking the lock here could
    deadlock against a worker mid-append — and a display that is momentarily one
    file stale costs nothing. Int reads are atomic in CPython.

    An ETA is worth the arithmetic here specifically: a single year folder is
    up to 946GB, so "how long is this going to take" is the question the line
    exists to answer.
    """
    done = summary.copied + summary.skipped + len(summary.failed)
    sent = summary.bytes_copied
    elapsed = max(time.monotonic() - started, 0.001)
    rate = sent / elapsed

    if state is not None and state["cancelling"]:
        return f"CANCELLING - {state['inflight']} copy(s) closing out"

    body = f"{done}/{total_files}  {format_size(sent)} of {format_size(total_bytes)}"
    if rate > 0 and sent > 0:
        body += f"  {format_size(int(rate))}/s"
        remaining = total_bytes - sent
        if remaining > 0:
            body += f"  ETA {format_duration_compact(remaining / rate)}"
    return body


def _resolve_target(staging: Path, name: str) -> Path:
    """The master folder this staging goes to, stable across a resumed run.

    The folder name embeds an upload timestamp, so a resumed run cannot
    recompute it — it would start a second folder and split one batch in two.
    The marker records it instead, and is removed when staging is cleared.
    """
    marker = staging / TARGET_MARKER
    if marker.is_file():
        recorded = marker.read_text(encoding="utf-8").strip()
        if recorded:
            return MASTER_DIR / recorded
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    folder = f"{name}_{stamp}"
    # Two uploads in the same second would otherwise merge into one folder,
    # silently joining two batches that have nothing to do with each other.
    n = 2
    while (MASTER_DIR / folder).exists():
        folder = f"{name}_{stamp}_{n}"
        n += 1
    marker.write_text(folder, encoding="utf-8")
    return MASTER_DIR / folder


def _collect(staging: Path) -> tuple[list[_Item], list[tuple[str, str, int]]]:
    """Split staging into files to upload and culled records to note.

    A sidecar whose media is gone is a **cull**: the user deleted it deliberately
    before upload, and the record has to travel to master anyway so the file is
    never re-imported (spec/nas-app.md §9).
    """
    items: list[_Item] = []
    culled: list[tuple[str, str, int]] = []
    used: dict[str, str] = {}

    for sidecar in sorted(staging.rglob(f"*{st.SIDECAR_EXT}")):
        data = st.read_sidecar(sidecar)
        if data is None:
            continue
        rel = data.get("rel")
        root = data.get("source_root")
        size = data.get("size")
        if not isinstance(rel, str) or not isinstance(root, str):
            continue

        media = staging / Path(rel)
        if not media.is_file():
            # The media is gone but its size is in the sidecar — and it must
            # reach the ledger, because the skip key is (rel, size). A culled
            # entry without a size would never join the manifest, and the file
            # would be re-imported on the next run.
            if isinstance(size, int):
                culled.append((rel, root, size))
            continue

        flat = _unique(flatten(rel), rel, used)
        items.append(_Item(source=media, rel=rel, root=root,
                           size=size if isinstance(size, int) else media.stat().st_size,
                           flat=flat))
    return items, culled


def _unique(flat: str, rel: str, used: dict[str, str]) -> str:
    """Disambiguate two different paths that flatten to one name.

    `a/b_c.jpg` and `a_b/c.jpg` both flatten to `a_b_c.jpg`. Rare, but silent
    overwriting in an archive is not an acceptable way to find that out.
    """
    if used.get(flat) in (None, rel):
        used[flat] = rel
        return flat
    stem, dot, ext = flat.partition(".")
    n = 2
    while True:
        candidate = f"{stem}~{n}{dot}{ext}"
        if used.get(candidate) in (None, rel):
            used[candidate] = rel
            return candidate
        n += 1


def _copy_one(item: _Item, target: Path) -> tuple[bool, str]:
    """Copy one staged file into master, hashing it on the way through.

    Returns `(bytes moved, blake3 digest)`. Staged through a marker temp and
    renamed into place, so a kill mid-copy leaves something that can never be
    mistaken for a finished file.

    **The hash costs nothing extra.** The bytes are already streaming through
    this process, so digesting them is free — and it turns the ledger into a
    permanent integrity record. Size alone catches truncation but not a flipped
    bit, and for an archive of originals "corrupt but the right length" is the
    failure that goes unnoticed for years.
    """
    dest = target / item.flat
    if dest.is_file() and dest.stat().st_size == item.size:
        # Present but unrecorded — a run killed between the rename and the
        # ledger flush. Hash what is there so the record is still complete.
        return False, _digest(dest)
    tmp = dest.with_name(dest.name + EXPORT_TMP_SUFFIX)
    digest = _copy_hashing(item.source, tmp)
    os.replace(tmp, dest)
    return True, digest


def _copy_hashing(src: Path, dst: Path) -> str:
    """Stream `src` to `dst`, returning the blake3 of the bytes written."""
    h = blake3()
    with src.open("rb") as fin, dst.open("wb") as fout:
        while True:
            chunk = fin.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            fout.write(chunk)
    shutil.copystat(src, dst)
    return h.hexdigest()


def _digest(path: Path) -> str:
    """blake3 of a file already on disk."""
    h = blake3()
    with path.open("rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _sweep_target(target: Path) -> int:
    """Delete partial copies left in master by an interrupted run.

    Nothing else removes them: they are marker-named so they can never be
    mistaken for real files, but without a sweep they accumulate in the archive
    forever.
    """
    if not target.is_dir():
        return 0
    removed = 0
    for tmp in target.glob(f"*{EXPORT_TMP_SUFFIX}*"):
        try:
            tmp.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _verify(items: list[_Item], target: Path,
            digests: dict[str, str]) -> bool:
    """Every uploaded file present at master, at size, and byte-identical.

    **Reads the files back off the NAS and hashes them.** Size alone catches a
    truncated or interrupted copy but not a flipped bit in transit, and this
    check is what gates clearing staging — the one destructive step in the
    pipeline. An unverified "probably fine" is not worth the staging it deletes.

    The read-back roughly adds a third to a batch's wall time (writes run near
    30 MB/s, reads near 100, and recently-written files often come back out of
    the NAS's own cache). For a one-time seed of originals that cannot be
    regenerated, that is a good trade.
    """
    for item in items:
        dest = target / item.flat
        try:
            if not dest.is_file() or dest.stat().st_size != item.size:
                return False
        except OSError:
            return False
        expected = digests.get(item.flat)
        if expected is not None and _digest(dest) != expected:
            return False
    return True


def _clear(staging: Path) -> None:
    """Remove a verified staging folder — the one destructive act here."""
    shutil.rmtree(staging, ignore_errors=True)


def _write_header(path: Path, name: str, roots: set[str]) -> None:
    """The ledger's first line: which source produced this master folder.

    This is what makes the known-device registry derivable rather than stored,
    and what lets a lookup skip folders belonging to other sources without
    opening their bodies.
    """
    header: dict[str, Any] = {
        "name": name,
        "source": "folder",
        "source_roots": sorted(roots),
        "uploaded": datetime.now().isoformat(timespec="seconds"),
    }
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n")


def _append(log: Any, entry: dict[str, Any]) -> None:
    """Append one ledger line and flush.

    Written during the upload rather than at the end, so a crash leaves a
    consistent partial record instead of an empty one.
    """
    log.write(json.dumps(entry) + "\n")
    log.flush()


def _already_recorded(ledger_path: Path) -> set[str]:
    """Flattened names already in the ledger, so a resume adds no duplicates."""
    recorded: set[str] = set()
    for entry in ledger.iter_entries(ledger_path):
        flat = entry.get("file")
        if isinstance(flat, str):
            recorded.add(flat)
        else:
            rel = entry.get("rel")
            if isinstance(rel, str):
                recorded.add(flatten(rel))
    return recorded


def iter_staged_sidecars(staging: Path) -> Iterator[Path]:
    """Every `.importinfo` under a staging folder (its `.manifest/` children)."""
    yield from staging.rglob(f"*{st.SIDECAR_EXT}")


__all__ = [
    "MANIFEST_DIRNAME", "TARGET_MARKER", "UploadError", "UploadSummary",
    "flatten", "pending_folders", "run_upload",
]
