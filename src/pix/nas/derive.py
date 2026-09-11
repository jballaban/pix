"""`pix2 process` — thumbnails and previews from master (spec/nas-app.md §9).

Two derived tiers, both disposable and never backed up:

- **thumb** (400px) — the app's grid. It exists because the NAS cannot afford to
  decode originals on demand: a no-AVX Atom serving 62k items needs them
  pre-made (spec/nas-app.md §6).
- **preview** (1600px) — what you actually judge a photo by. A thumbnail cannot
  tell you sharp from soft, and serving the full file instead means pushing
  several MB per photo off that Atom while someone pages through an event.

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
from pix.duration import format_duration_compact
from pix.markers import EXPORT_TMP_SUFFIX
from pix.progress import LiveProgress
from pix.nas import ledger
from pix.nas.const import LEDGER_NAME, MASTER_DIR, PREVIEW_DIR, THUMB_DIR

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
    skipped: int = 0            # already had both
    unsupported: int = 0        # nothing we know how to render a frame from
    cancelled: bool = False
    failed: list[str] = field(default_factory=lambda: [])

    @property
    def made(self) -> int:
        return self.thumbs + self.previews


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


def needs_work(media: Path) -> tuple[bool, bool]:
    """`(needs thumb, needs preview)` — what is missing for this file."""
    return (not derived_path(media, THUMB_DIR).is_file(),
            not derived_path(media, PREVIEW_DIR).is_file())


def sweep_partials() -> int:
    """Delete marker temps left in the derived tiers by an interrupted run.

    Derived images are written temp-then-rename, so a kill can never leave a
    truncated JPEG that `needs_work` would accept as done — but without a sweep
    the temps accumulate in the tiers forever. Poster frames go to local scratch
    and are cleaned there too.
    """
    removed = 0
    for root in (THUMB_DIR, PREVIEW_DIR, _scratch()):
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

    echo(f"{len(pending)} file(s) need thumbnails or previews")
    lock = threading.Lock()
    started = time.monotonic()
    state: dict[str, int] = {"done": 0, "inflight": 0, "cancelling": 0}

    def handle(media: Path) -> None:
        if state["cancelling"]:
            return
        state["inflight"] += 1
        try:
            _derive_one(media, summary, lock)
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
            f"{summary.previews} preview")
    if summary.failed:
        body += f", {len(summary.failed)} failed"
    if done and rate > 0:
        body += f"  {rate:.1f}/s"
        remaining = total - done
        if remaining > 0:
            body += f"  ETA {format_duration_compact(remaining / rate)}"
    return body


def _derive_one(media: Path, summary: ProcessSummary, lock: threading.Lock) -> None:
    """Make whatever `media` is missing."""
    want_thumb, want_preview = needs_work(media)
    if not (want_thumb or want_preview):
        with lock:
            summary.skipped += 1
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
    tmp = media.parent / (media.name + ".poster" + EXPORT_TMP_SUFFIX + ".jpg")
    tmp = Path(str(tmp).replace(str(media.parent), str(_scratch())))
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
    """Local scratch for poster frames.

    Deliberately not beside the media: master is on the NAS and writing temps
    there would put derived churn inside the archive.
    """
    import tempfile

    return Path(tempfile.gettempdir()) / "pix2-process"
