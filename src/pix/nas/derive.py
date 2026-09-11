"""`pix2 process` — thumbnails and previews from master (spec/nas-app.md §9).

Two derived tiers, both disposable and never backed up:

- **thumb** (400px) — the app's grid. It exists because the NAS cannot afford to
  decode originals on demand: a no-AVX Atom serving 62k items needs them
  pre-made (spec/nas-app.md §6).
- **preview** (1600px) — what you actually judge a photo by. A thumbnail cannot
  tell you sharp from soft, and serving the full file instead means pushing
  several MB per photo off that Atom while someone pages through an event.
- **meta** — the probed facts, one JSON per file. Section 4 of the spec accepts
  that an index rebuild re-probes every media file, which on spinning disks
  behind an Atom is hours. This makes that cheap instead: `process` is already
  opening each file to decode it, so extracting its metadata at the same time is
  near-free, and rebuilding the index becomes a read of small JSON rather than
  62k media opens.

**Runs desktop-side**, like everything that decodes. The cost is reading the bulk
of master back over SMB once; the alternative is a second implementation inside
the app for a job the desktop does faster.

Renders (format conversion) are deliberately **not** here yet. Master is seeded
from the already-normalised library, so it is JPG and MP4 throughout and almost
nothing needs converting — the exceptions are legacy HEVC clips needing H.264 for
delivery, which drag in codec probing and the encode path for nearly no work
today.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from PIL import Image

from pix import convert  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pix.exiftool_session import ExifToolSession, ExifToolTimeout
from pix.duration import format_duration_compact
from pix.markers import EXPORT_TMP_SUFFIX
from pix.progress import LiveProgress
from pix.nas import ledger
from pix.nas.const import (
    LEDGER_NAME, MASTER_DIR, META_DIR, PREVIEW_DIR, THUMB_DIR,
)

#: Long-edge pixels. Both are regenerable, but regenerating 62k files is an
#: afternoon, so they are worth getting roughly right rather than discovering
#: mid-curation.
THUMB_PX: int = 400
PREVIEW_PX: int = 1600

#: JPEG quality for derived images. 82 is visually clean at these sizes and
#: keeps both tiers near 20GB for the whole library.
QUALITY: int = 82

#: Where in a clip to grab its poster frame. First frames are routinely black or
#: motion-blurred; a little way in is almost always representative.
FRAME_AT: float = 0.10

WORKERS: int = 8
_FFMPEG_TIMEOUT: float = 120.0

_IMAGE_EXTS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif",
    ".tif", ".tiff", ".webp", ".bmp",
})
_VIDEO_EXTS: frozenset[str] = frozenset({
    ".mov", ".mp4", ".m4v", ".avi", ".mkv", ".wmv",
    ".webm", ".3gp", ".mts", ".mpg", ".mpeg", ".insv", ".insp",
})


class ProcessError(Exception):
    """Processing could not start."""


@dataclass
class ProcessSummary:
    """What one `process` run did."""

    thumbs: int = 0
    previews: int = 0
    metas: int = 0
    skipped: int = 0            # already had both
    unsupported: int = 0        # nothing we know how to render a frame from
    cancelled: bool = False
    failed: list[str] = field(default_factory=lambda: [])

    @property
    def made(self) -> int:
        return self.thumbs + self.previews + self.metas


def master_files() -> Iterator[Path]:
    """Every media file in master, ledgers and decision sidecars excluded."""
    if not MASTER_DIR.is_dir():
        return
    for folder in sorted(p for p in MASTER_DIR.iterdir() if p.is_dir()):
        for path in sorted(folder.iterdir()):
            if not path.is_file():
                continue
            if path.name == LEDGER_NAME or path.suffix.lower() == ".xmp":
                continue
            yield path


def derived_path(media: Path, root: Path) -> Path:
    """Where `media`'s derived image lives, mirroring master's folder layout.

    Always `.jpg`: the derived tiers are for looking at, so a HEIC's thumbnail
    and an MP4's poster frame are both just JPEGs.
    """
    return root / media.parent.name / (media.name + ".jpg")


def meta_path(media: Path) -> Path:
    """Where `media`'s probed-facts JSON lives, mirroring master's layout."""
    return META_DIR / media.parent.name / (media.name + ".json")


def needs_work(media: Path) -> tuple[bool, bool, bool]:
    """`(needs thumb, needs preview, needs meta)` — what is missing."""
    return (not derived_path(media, THUMB_DIR).is_file(),
            not derived_path(media, PREVIEW_DIR).is_file(),
            not meta_path(media).is_file())


def sweep_partials() -> int:
    """Delete marker temps left in the derived tiers by an interrupted run.

    Derived images are written temp-then-rename, so a kill can never leave a
    truncated JPEG that `needs_work` would accept as done — but without a sweep
    the temps accumulate in the tiers forever. Poster frames go to local scratch
    and are cleaned there too.
    """
    removed = _reap_dead_scratch()
    for root in (THUMB_DIR, PREVIEW_DIR, META_DIR, _scratch()):
        if not root.is_dir():
            continue
        for tmp in root.rglob(f"*{EXPORT_TMP_SUFFIX}*"):
            try:
                tmp.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def run_process(*, echo: Callable[[str], None] = lambda _: None) -> ProcessSummary:
    """Generate every missing thumbnail and preview.

    **Cancel and restart freely.** Resumable by construction: it makes what is
    missing, and missing is recomputed every run, so there is no state to
    corrupt and nothing to resume *from*. A Ctrl+C drops the queue, drains the
    files already in flight where the operator can watch the count fall, and
    leaves the tiers in a state the next run simply continues.
    """
    ledger.require_share()
    summary = ProcessSummary()

    swept = sweep_partials()
    if swept:
        echo(f"swept {swept} partial(s) from an interrupted run")

    pending = [p for p in master_files() if any(needs_work(p))]
    if not pending:
        echo("nothing to process")
        return summary

    echo(f"{len(pending)} file(s) need thumbnails, previews or metadata")
    lock = threading.Lock()
    started = time.monotonic()
    state: dict[str, int] = {"done": 0, "inflight": 0, "cancelling": 0}

    # One persistent ExifTool process for the whole run — spawning one per file
    # would cost more than the reads. It is not thread-safe, so it is guarded by
    # its own lock rather than the summary's.
    exif = ExifToolSession()
    exif_lock = threading.Lock()

    def handle(media: Path) -> None:
        if state["cancelling"]:
            return
        state["inflight"] += 1
        try:
            _derive_one(media, summary, lock, exif, exif_lock)
        finally:
            state["inflight"] -= 1
            state["done"] += 1

    progress = LiveProgress(
        total=len(pending),
        status_provider=lambda: _status(summary, len(pending), started, state),
    )

    def tracked(media: Path) -> None:
        handle(media)
        progress.advance()

    with progress:
        progress.begin("process")
        pool = ThreadPoolExecutor(max_workers=WORKERS)
        futures = [pool.submit(tracked, p) for p in pending]
        try:
            for future in as_completed(futures):
                future.result()
        except KeyboardInterrupt:
            state["cancelling"] = 1
            dropped = sum(1 for f in futures if f.cancel())
            summary.cancelled = True
            echo(f"\ncancelling: {dropped} queued dropped, "
                 f"draining {state['inflight']} in flight")
        finally:
            pool.shutdown(wait=True)
            exif.close()

    elapsed = time.monotonic() - started
    echo(f"done in {format_duration_compact(elapsed)}")
    return summary


def _status(summary: ProcessSummary, total: int, started: float,
            state: dict[str, int]) -> str:
    """The live body: files resolved, what was made, rate and an ETA.

    Read **without the lock**, deliberately — it is called from the progress
    thread once a second as well as from workers, so taking it could deadlock a
    worker mid-update, and a line one file stale costs nothing.

    Counted in **files rather than bytes**: decode cost varies enormously
    between a 3MB JPEG and a keyframe seek into a 2.6GB clip, so bytes-per-second
    would be a number that jumps around without meaning anything.
    """
    if state["cancelling"]:
        return f"CANCELLING - {state['inflight']} file(s) closing out"

    done = state["done"]
    elapsed = max(time.monotonic() - started, 0.001)
    rate = done / elapsed

    body = (f"{done}/{total}  {summary.thumbs} thumb, "
            f"{summary.previews} preview, {summary.metas} meta")
    if summary.failed:
        body += f", {len(summary.failed)} failed"
    if done and rate > 0:
        body += f"  {rate:.1f}/s"
        remaining = total - done
        if remaining > 0:
            body += f"  ETA {format_duration_compact(remaining / rate)}"
    return body


def _derive_one(media: Path, summary: ProcessSummary, lock: threading.Lock,
                exif: ExifToolSession, exif_lock: threading.Lock) -> None:
    """Make whatever `media` is missing."""
    want_thumb, want_preview, want_meta = needs_work(media)
    if not (want_thumb or want_preview or want_meta):
        with lock:
            summary.skipped += 1
        return

    # Metadata first, and independently of the image work: a file whose pixels
    # cannot be decoded still has facts worth recording, and an unsupported
    # extension is not a reason to know nothing about it.
    if want_meta:
        try:
            if _write_meta(media, exif, exif_lock):
                with lock:
                    summary.metas += 1
        except Exception as e:                # noqa: BLE001
            with lock:
                summary.failed.append(f"{media.name}: meta: {type(e).__name__}: {e}")

    if not (want_thumb or want_preview):
        return

    ext = media.suffix.lower()
    try:
        if ext in _IMAGE_EXTS:
            source = media
            temp: Path | None = None
        elif ext in _VIDEO_EXTS:
            temp = _poster_frame(media)
            if temp is None:
                with lock:
                    summary.unsupported += 1
                return
            source = temp
        else:
            with lock:
                summary.unsupported += 1
            return

        try:
            if want_thumb:
                _resize(source, derived_path(media, THUMB_DIR), THUMB_PX)
                with lock:
                    summary.thumbs += 1
            if want_preview:
                _resize(source, derived_path(media, PREVIEW_DIR), PREVIEW_PX)
                with lock:
                    summary.previews += 1
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
    except Exception as e:                    # noqa: BLE001 - one bad file must
        with lock:                            # not stop 62k others
            summary.failed.append(f"{media.name}: {type(e).__name__}: {e}")


def _write_meta(media: Path, exif: ExifToolSession,
                exif_lock: threading.Lock) -> bool:
    """Write `media`'s probed facts as JSON. True if one was written.

    **Facts, not interpretations** (spec/nas-app.md §4). This records what
    ExifTool read — `EXIF:DateTimeOriginal`, dimensions, codec — never the
    effective value after pix's heuristics. Facts cannot go stale, because master
    files are immutable; interpretations go stale the moment a heuristic
    improves, and persisting them would resurrect the `_auto` re-derivation
    treadmill the old architecture had.

    Also records the file's size and mtime, so the index can tell a stale entry
    from a current one with a `stat` rather than a re-read.
    """
    with exif_lock:                          # ExifTool session is not thread-safe
        try:
            data = exif.read_metadata(media)
        except ExifToolTimeout:
            return False
    if data is None:
        return False

    try:
        stat = media.stat()
    except OSError:
        return False

    payload: dict[str, object] = {
        "file": media.name,
        "folder": media.parent.name,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "exif": data,
    }
    dest = meta_path(media)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + EXPORT_TMP_SUFFIX)
    tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    tmp.replace(dest)
    return True


def _resize(source: Path, dest: Path, long_edge: int) -> None:
    """Write a JPEG of `source` fitted to `long_edge`, temp-then-rename.

    EXIF orientation is applied rather than carried, because the derived image
    has no metadata to carry it in — a sideways thumbnail is worse than useless
    in a grid you are scanning for one photo.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + EXPORT_TMP_SUFFIX)
    with Image.open(source) as img:
        from PIL import ImageOps

        img = ImageOps.exif_transpose(img) or img
        img.thumbnail((long_edge, long_edge))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(tmp, "JPEG", quality=QUALITY, optimize=True)
    tmp.replace(dest)


def _poster_frame(media: Path) -> Path | None:
    """Extract a representative frame to a temp JPEG, or None if we cannot.

    Seeks to `FRAME_AT` of the duration. `-ss` before `-i` makes it a keyframe
    seek, which costs a fraction of decoding up to that point — the difference
    between minutes and milliseconds on a long clip.
    """
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if ffmpeg is None:
        return None

    duration = _duration(media)
    offset = max(duration * FRAME_AT, 0.0) if duration else 0.0
    # Named with the master folder as well as the file: two folders can hold the
    # same flattened name, and a shared temp would have one worker deleting what
    # another is reading.
    stem = f"{media.parent.name}_{media.name}"
    tmp = _scratch() / (stem + ".poster" + EXPORT_TMP_SUFFIX + ".jpg")
    tmp.parent.mkdir(parents=True, exist_ok=True)

    cmd = [ffmpeg, "-v", "error", "-ss", f"{offset:.3f}", "-i", str(media),
           "-frames:v", "1", "-y", str(tmp)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_FFMPEG_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return None
    return tmp


def _duration(media: Path) -> float | None:
    """Clip duration in seconds, or None if ffprobe cannot say."""
    ffprobe = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if ffprobe is None:
        return None
    cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=nw=1:nk=1", str(media)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def _scratch() -> Path:
    """Local scratch for poster frames, **private to this process**.

    Deliberately not beside the media: master is on the NAS and writing temps
    there would put derived churn inside the archive.

    Per-PID because it is swept at startup. A single shared directory means any
    other run — including a test suite — wipes the frames a live run is in the
    middle of reading, which surfaces as a `FileNotFoundError` on a file that
    demonstrably existed a moment earlier. Observed exactly that.
    """
    import tempfile

    return Path(tempfile.gettempdir()) / "pix2-process" / str(os.getpid())


def _reap_dead_scratch() -> int:
    """Remove scratch directories belonging to processes that are gone.

    Per-PID scratch would otherwise accumulate forever after a hard kill. Only
    dead owners are touched: a live run's directory is never anyone else's to
    delete, which is the whole point of splitting them up.
    """
    import psutil

    parent = _scratch().parent
    if not parent.is_dir():
        return 0
    removed = 0
    for child in parent.iterdir():
        if not child.is_dir() or child.name == _scratch().name:
            continue
        try:
            pid = int(child.name)
        except ValueError:
            continue
        if psutil.pid_exists(pid):
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed += 1
    return removed
