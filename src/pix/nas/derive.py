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

- **render** — an H.264 copy of any video master cannot play as-is.

**Whether a render is needed is a codec question for video, not an extension
one** (spec/nas-app.md §5): an `.mp4` containing HEVC still needs one. Measured
across the seeded year, **421 of 724 clips are HEVC against 303 H.264**, so most
of the video library is unplayable in a browser until this runs. Images need
nothing — master is JPG throughout.

Encoding is why `process` is a desktop command: the RS820+'s Atom has no iGPU and
no AVX, while this machine has NVENC.
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
from typing import Any, Callable, ClassVar, Iterator, cast

from PIL import Image

from pix import convert  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pix.exiftool_session import ExifToolSession, ExifToolTimeout
from pix.duration import format_duration_compact
from pix.markers import EXPORT_TMP_SUFFIX
from pix.progress import LiveProgress
from pix.nas import clips
from pix.nas import decisions
from pix.nas import identity
from pix.nas import roundtrip
from pix.nas import ledger
from pix.nas.const import (
    LARGE_DIR, LEDGER_NAME, MASTER_DIR, META_DIR, PREVIEW_DIR, RENDER_DIR,
    STRIP_DIR, THUMB_DIR,
)

# Sizes and layout live in `paths`, which the app imports without this module's
# decoding stack behind it. Re-exported so `derive.THUMB_PX` and
# `derive.derived_path` keep meaning what they always did.
from pix.nas.paths import (  # noqa: F401
    LARGE_PX, PREVIEW_PX, THUMB_PX, derived_path,
)
from pix.nas import paths

#: JPEG quality for derived images. 82 is visually clean at these sizes and
#: keeps both tiers near 20GB for the whole library.
QUALITY: int = 82

#: Where in a clip to grab its poster frame. First frames are routinely black or
#: motion-blurred; a little way in is almost always representative.
FRAME_AT: float = 0.10

WORKERS: int = 8
_FFMPEG_TIMEOUT: float = 120.0

#: Encoding is far heavier than a poster frame, and NVENC has a small number of
#: hardware sessions — running the pool wide against it just queues in the driver.
RENDER_WORKERS: int = 2
_ENCODE_TIMEOUT: float = 3600.0

#: Video codecs that play in a browser as-is. Anything else needs a render.
#: Defined in `paths`, which the app shares.
_PLAYABLE_CODECS: frozenset[str] = paths.PLAYABLE_CODECS

#: 360 footage has no meaningful flat rendition — a plain transcode gives
#: dual-fisheye that nothing displays usefully (spec/nas-app.md §5).
_NO_RENDER_EXTS: frozenset[str] = frozenset({".insv", ".insp"})

#: Constant-quality target for NVENC. Measured on a real 30.5MB HEVC clip from
#: the seeded year, at identical encode time (~4.3s):
#:
#:     cq 23 -> 74.7MB (2.45x source)   cq 28 -> 42.7MB (1.40x)
#:     cq 26 -> 54.2MB (1.78x)          cq 30 -> 33.8MB (1.11x)
#:
#: 26 because a render that is 2.45x larger than the original it derives from is
#: the wrong shape for a disposable tier — and across the 421 HEVC clips that is
#: ~22GB rather than ~31GB. Quality still sits well above what its consumers need
#: (a browser grid, a TV), and the master keeps the original
#: regardless, so this is recoverable if it ever proves too low.
_CQ: str = "26"

_IMAGE_EXTS: frozenset[str] = paths.IMAGE_EXTS
_VIDEO_EXTS: frozenset[str] = paths.VIDEO_EXTS


class ProcessError(Exception):
    """Processing could not start."""


class _ExifPool:
    """A serialised, self-healing ExifTool session.

    `ExifToolSession.execute` **kills the process on timeout**, so a single slow
    file leaves the session dead — and every read after it fails, first with
    `RuntimeError: subprocess exited unexpectedly`, then `OSError EINVAL` writing
    to a closed pipe. Observed in the wild: one bad file cost the metadata for
    every file that followed it.

    A Ctrl+C reaches the same state by a different route, because the child
    shares the console's process group and dies with it.

    So the session is treated as disposable: any session-level failure discards
    it, and the next file gets a fresh one. One bad file then costs exactly one
    file.

    Serialised by a lock because ExifTool's `-stay_open` pipe is a single
    conversation — two workers interleaving commands on it would each read the
    other's answer.
    """

    def __init__(self) -> None:
        self._session: ExifToolSession | None = None
        self._lock = threading.Lock()
        self.restarts: int = 0

    def read(self, media: Path) -> dict[str, object] | None:
        """Probe `media`, recreating the session if it has died."""
        with self._lock:
            for last_attempt in (False, True):
                try:
                    # Video in full: its `moov` is often after the footage,
                    # where `-fast2` never looks (`read_metadata`).
                    return self._ensure().read_metadata(
                        media, fast=media.suffix.lower() not in _VIDEO_EXTS)
                except ExifToolTimeout:
                    # execute() already killed it. This file is genuinely slow;
                    # skip it rather than spending the timeout again.
                    self._discard()
                    return None
                except (RuntimeError, OSError, ValueError):
                    self._discard()
                    if last_attempt:
                        raise
        return None

    def close(self) -> None:
        with self._lock:
            self._discard()

    def _ensure(self) -> ExifToolSession:
        if self._session is None:
            self._session = ExifToolSession()
            self.restarts += 1
        return self._session

    def _discard(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception:                 # noqa: BLE001 - already failing
                pass


@dataclass
class ProcessSummary:
    """What one `process` run did."""

    thumbs: int = 0
    larges: int = 0
    previews: int = 0
    metas: int = 0
    renders: int = 0
    strips: int = 0
    stills: int = 0
    skipped: int = 0            # already had both
    unsupported: int = 0        # nothing we know how to render a frame from
    cancelled: bool = False
    failed: list[str] = field(default_factory=lambda: [])
    #: Every file this run worked on, finished or not — what the index has to
    #: catch up on afterwards, and nothing else does.
    handled: list[Path] = field(default_factory=lambda: [])

    #: Every kind of thing a run makes, as `(counter, what to call it)`, in
    #: the order they are reported. The one list both the live line and the
    #: closing summary read, so a new counter cannot be kept and never shown.
    ACTIONS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("thumbs", "thumbnail"), ("larges", "large"),
        ("previews", "preview"), ("metas", "meta"), ("renders", "render"),
        ("strips", "strip"), ("stills", "still"),
    )

    @property
    def made(self) -> int:
        return sum(getattr(self, field_) for field_, _ in self.ACTIONS)

    def actions(self, *, all_: bool = False) -> str:
        """What was made, as `3 thumbnail(s), 1 render(s)` — only what is
        nonzero unless `all_`, so a run says what it did rather than reciting
        every kind of thing it might have."""
        parts = [f"{getattr(self, f)} {label}(s)" for f, label in self.ACTIONS
                 if all_ or getattr(self, f)]
        return ", ".join(parts) if parts else "nothing made"


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


def _placeholder(path: Path) -> bool:
    """Whether a meta record is one the app wrote rather than a probe."""
    try:
        record: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and bool(
        cast("dict[str, Any]", record).get("placeholder"))


def meta_path(media: Path) -> Path:
    """Where `media`'s probed-facts JSON lives, against *this* module's tier."""
    return paths.meta_path(media, META_DIR)


def render_path(media: Path) -> Path:
    """Where `media`'s playable rendition lives, against *this* module's tier."""
    return paths.render_path(media, RENDER_DIR)


def needs_render(media: Path, codec: str | None) -> bool:
    """True if this video cannot be played as-is and a render is possible.

    `codec` is the probed `CompressorID` — already in the meta tier, so this
    costs no extra probe.
    """
    ext = media.suffix.lower()
    if ext not in _VIDEO_EXTS or ext in _NO_RENDER_EXTS:
        return False
    if codec and codec.lower() in _PLAYABLE_CODECS:
        return False
    return not render_path(media).is_file()


def wants_render(media: Path) -> bool:
    """Whether `media` needs an H.264 render, without probing images.

    The extension check comes first so the codec lookup — a meta-tier read — is
    only paid for video.
    """
    ext = media.suffix.lower()
    if ext not in _VIDEO_EXTS or ext in _NO_RENDER_EXTS:
        return False
    if render_path(media).is_file():
        return False
    return needs_render(media, video_codec(media))


#: A filmstrip's frames: this tall, one per `STRIP_EVERY` seconds, between
#: `STRIP_MIN` and `STRIP_MAX` of them. Enough that a long clip can be read
#: along its timeline, few enough that making one is a handful of keyframe
#: seeks rather than a decode of the whole file — each frame is its own seek,
#: which over SMB costs a GOP, not the footage before it.
STRIP_PX: int = 90
STRIP_EVERY: float = 5.0
STRIP_MIN: int = 4
STRIP_MAX: int = 40


def wants_strip(media: Path) -> bool:
    """Whether `media` is a video that should have a filmstrip and has none."""
    ext = media.suffix.lower()
    if ext not in _VIDEO_EXTS or ext in _NO_RENDER_EXTS:
        return False
    return not paths.strip_path(media, STRIP_DIR).is_file()


def make_strip(media: Path) -> bool:
    """A row of frames from `media`, evenly across it, for the splice page.

    Each frame is its own `-ss` before `-i` — a keyframe seek, so a frame
    costs what it takes to reach the nearest keyframe and decode one picture
    — and they are laid side by side in one JPEG with a small JSON beside it
    saying how many there are. The page shows as many as fit.
    """
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    duration = _duration(media)
    if ffmpeg is None or not duration or duration <= 0:
        return False
    import math

    n = max(STRIP_MIN, min(STRIP_MAX, math.ceil(duration / STRIP_EVERY)))
    times = [round((i + 0.5) * duration / n, 3) for i in range(n)]
    hdr = is_hdr(media)
    frames: list[Image.Image] = []
    stem = f"{media.parent.name}_{media.name}"
    try:
        for i, when in enumerate(times):
            tmp = _scratch() / f"{stem}.strip{i}{EXPORT_TMP_SUFFIX}.jpg"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            scale = f"scale=-2:{STRIP_PX}"
            head = [ffmpeg, "-v", "error", "-ss", f"{when:.3f}", "-i", str(media)]
            tail = ["-frames:v", "1", "-q:v", "4", "-y", str(tmp)]
            tries = ([[*head, "-vf", f"{_TONEMAP},{scale}", *tail],
                      [*head, "-vf", scale, *tail]]
                     if hdr else [[*head, "-vf", scale, *tail]])
            for cmd in tries:
                try:
                    done = subprocess.run(cmd, capture_output=True, text=True,
                                          timeout=_FFMPEG_TIMEOUT)
                except (OSError, subprocess.TimeoutExpired):
                    continue
                if done.returncode == 0 and tmp.is_file():
                    break
            if not tmp.is_file():
                # Past the last picture. The container's length is the
                # longest stream's, and in a short phone clip that is the
                # sound — so the last time asked for can fall after the video
                # has ended, where a player holds the final picture. The strip
                # does the same, rather than giving up on the clip and having
                # every run try it again.
                if not frames:
                    return False
                frames.append(frames[-1].copy())
                continue
            with Image.open(tmp) as im:
                frames.append(im.convert("RGB").copy())
            tmp.unlink(missing_ok=True)
        width, height = frames[0].size
        sprite = Image.new("RGB", (width * n, height))
        for i, frame in enumerate(frames):
            if frame.size != (width, height):
                frame = frame.resize((width, height))
            sprite.paste(frame, (i * width, 0))
        dest = paths.strip_path(media, STRIP_DIR)
        info = paths.strip_info_path(media, STRIP_DIR)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + EXPORT_TMP_SUFFIX)
        sprite.save(part, "JPEG", quality=70)
        os.replace(part, dest)
        info_part = info.with_name(info.name + EXPORT_TMP_SUFFIX)
        info_part.write_text(json.dumps(
            {"n": n, "w": width, "h": height, "times": times}),
            encoding="utf-8")
        os.replace(info_part, info)
        return True
    finally:
        for frame in frames:
            frame.close()


def needs_work(media: Path) -> tuple[bool, bool, bool, bool]:
    """`(needs thumb, needs large, needs preview, needs meta)` — what is missing.

    Per-file, so it costs four `stat`s. `pending_files` answers the same
    question for a whole run with directory listings instead; use that for
    anything at scale.
    """
    return (not derived_path(media, THUMB_DIR).is_file(),
            not derived_path(media, LARGE_DIR).is_file(),
            not derived_path(media, PREVIEW_DIR).is_file(),
            not meta_path(media).is_file())


def _names_in(folder: Path) -> set[str]:
    """The filenames directly in `folder`, or empty if it does not exist.

    `os.scandir` rather than `iterdir` so no `stat` is issued per entry — only
    the names are wanted, and over SMB a stat per derived file is exactly the
    cost this scan exists to avoid.
    """
    try:
        with os.scandir(folder) as entries:
            return {e.name for e in entries}
    except OSError:
        return set()


def pending_files(echo: Callable[[str], None] = lambda _: None) -> list[Path]:
    """Every master file missing at least one derived artefact.

    **One listing per tier per master folder**, rather than three `stat`s per
    file. Over SMB that is the difference between four round trips and ~19,000
    for a single 6,400-file folder — the scan was taking longer than the work.
    """
    folders = ([p for p in sorted(MASTER_DIR.iterdir()) if p.is_dir()]
               if MASTER_DIR.is_dir() else [])
    if not folders:
        return []

    scanned = {"n": 0, "found": 0}
    progress = LiveProgress(
        status_provider=lambda: (
            f"scanning {scanned['n']}/{len(folders)} folder(s), "
            f"{scanned['found']} to do"),
    )

    pending: list[Path] = []
    with progress:
        progress.begin("scan")
        for folder in folders:
            thumbs = _names_in(THUMB_DIR / folder.name)
            larges = _names_in(LARGE_DIR / folder.name)
            previews = _names_in(PREVIEW_DIR / folder.name)
            metas = _names_in(META_DIR / folder.name)

            # `scandir` again: its `is_file()` is answered from the directory
            # entry the OS already returned, where `Path.is_file()` would be a
            # fresh round trip for every one of 6,400 files.
            try:
                with os.scandir(folder) as entries:
                    listing = sorted(entries, key=lambda e: e.name)
            except OSError:
                scanned["n"] += 1
                continue

            renders = _names_in(RENDER_DIR / folder.name)
            strips = _names_in(STRIP_DIR / folder.name)

            present = {e.name for e in listing if e.is_file()}
            for entry in listing:
                if not entry.is_file():
                    continue
                name = entry.name
                if name.lower().endswith(".xmp"):
                    # A clip's sidecar is the one master entry a clip has, so
                    # it is how the scan finds clips wanting pictures of their
                    # own (spec/clips.md §7) — without which a viewer sees
                    # none, rather than a frame of footage they were not
                    # given.
                    clip = name[: -len(".xmp")]
                    of = clips.source_of(clip)
                    if of is not None and of in present and (
                            clip + ".jpg" not in thumbs
                            or clip + ".jpg" not in larges
                            or clip + ".jpg" not in previews
                            or _clip_wants_file(folder / clip, renders)):
                        pending.append(folder / clip)
                        scanned["found"] += 1
                    continue
                if name == LEDGER_NAME:
                    continue
                derived = name + ".jpg"
                want = (derived not in thumbs or derived not in larges
                        or derived not in previews
                        or name + ".json" not in metas)
                # A video may be complete on every image tier and still need a
                # render — the codec question the extension cannot answer.
                if not want and name + ".mp4" not in renders:
                    want = wants_render(folder / name)
                # And a filmstrip, which only a video has.
                if (not want and derived not in strips
                        and Path(name).suffix.lower() in _VIDEO_EXTS
                        and Path(name).suffix.lower() not in _NO_RENDER_EXTS):
                    want = True
                if want:
                    pending.append(folder / name)
                    scanned["found"] += 1
            scanned["n"] += 1
    return pending


def sweep_partials() -> int:
    """Delete marker temps left in the derived tiers by an interrupted run.

    Derived images are written temp-then-rename, so a kill can never leave a
    truncated JPEG that `needs_work` would accept as done — but without a sweep
    the temps accumulate in the tiers forever. Poster frames go to local scratch
    and are cleaned there too.
    """
    # Reaping a previous run's scratch is routine housekeeping, not evidence of
    # an interrupted run — counting it here made every run claim it had swept a
    # partial, forever.
    _reap_dead_scratch()

    removed = 0
    for root in (THUMB_DIR, LARGE_DIR, PREVIEW_DIR, META_DIR, RENDER_DIR,
                 _scratch()):
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

    pending = pending_files(echo)
    if not pending:
        echo("nothing to process")
        return summary

    echo(f"{len(pending)} file(s) need thumbnails, previews or metadata")
    lock = threading.Lock()
    started = time.monotonic()
    state: dict[str, int] = {"done": 0, "inflight": 0, "cancelling": 0}

    # One ExifTool process for the whole run — spawning one per file would cost
    # more than the reads — but replaced whenever it dies. See `_ExifPool`.
    exif = _ExifPool()

    def handle(media: Path) -> None:
        if state["cancelling"]:
            return
        state["inflight"] += 1
        try:
            _derive_one(media, summary, lock, exif, state)
        finally:
            state["inflight"] -= 1
            state["done"] += 1
            with lock:
                summary.handled.append(media)

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
            if exif.restarts > 1:
                echo(f"exiftool was restarted {exif.restarts - 1} time(s)")

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

    body = f"{done}/{total}  {summary.actions(all_=True)}"
    if summary.failed:
        body += f", {len(summary.failed)} failed"
    if done and rate > 0:
        body += f"  {rate:.1f}/s"
        remaining = total - done
        if remaining > 0:
            body += f"  ETA {format_duration_compact(remaining / rate)}"
    return body


def _derive_one(media: Path, summary: ProcessSummary, lock: threading.Lock,
                exif: "_ExifPool", state: dict[str, int]) -> None:
    """Make whatever `media` is missing."""
    if clips.source_of(media.name) is not None and not media.is_file():
        _derive_clip(media, summary, lock)
        return
    want_thumb, want_large, want_preview, want_meta = needs_work(media)
    # A record the app wrote for a clip it made into a file (spec/clips.md
    # §5) is a placeholder: what the clip's row knew, standing in until this
    # probes the file itself. Only asked of a file already here for work —
    # it has no thumbnail of its own — so it costs one small read.
    want_meta = want_meta or _placeholder(meta_path(media))
    # A video can be complete on every image tier and still need a render — the
    # codec question an extension cannot answer. Leaving this out of the early
    # return dismissed all 421 HEVC clips as "already done".
    want_render = wants_render(media)
    want_strip = wants_strip(media)
    if not (want_thumb or want_large or want_preview or want_meta
            or want_render or want_strip):
        with lock:
            summary.skipped += 1
        return

    # Metadata first, and independently of the image work: a file whose pixels
    # cannot be decoded still has facts worth recording, and an unsupported
    # extension is not a reason to know nothing about it.
    if want_meta:
        try:
            if _write_meta(media, exif):
                with lock:
                    summary.metas += 1
        except Exception as e:                # noqa: BLE001
            # A failure while shutting down is an artefact of the interrupt, not
            # a fact about the file — and the next run redoes it anyway. Listing
            # it would bury any real failure under a wall of noise.
            if not state["cancelling"]:
                with lock:
                    summary.failed.append(
                        f"{media.name}: meta: {type(e).__name__}: {e}")

    if want_strip and not state["cancelling"]:
        try:
            if make_strip(media):
                with lock:
                    summary.strips += 1
            elif not state["cancelling"]:
                # Said, because the scan will ask for it again next run and a
                # file retried forever in silence is how this went unnoticed.
                with lock:
                    summary.failed.append(
                        f"{media.name}: strip: no frame could be read")
        except Exception as e:                # noqa: BLE001
            if not state["cancelling"]:
                with lock:
                    summary.failed.append(
                        f"{media.name}: strip: {type(e).__name__}: {e}")

    # A render is the expensive item, so it goes after the cheap ones: a
    # cancelled run still leaves the thumbnails and metadata it managed.
    if want_render and not state["cancelling"]:
        try:
            if render_video(media):
                with lock:
                    summary.renders += 1
            else:
                with lock:
                    summary.failed.append(f"{media.name}: render failed")
        except Exception as e:                # noqa: BLE001
            if not state["cancelling"]:
                with lock:
                    summary.failed.append(
                        f"{media.name}: render: {type(e).__name__}: {e}")

    if not (want_thumb or want_large or want_preview):
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
            if want_large:
                _resize(source, derived_path(media, LARGE_DIR), LARGE_PX)
                with lock:
                    summary.larges += 1
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


def _clip_wants_file(clip: Path, renders: set[str]) -> bool:
    """Whether a clip still lacks the file a viewer is given: a still its
    JPEG, and a clip of footage a browser will not play its H.264 render.
    A clip of H.264 needs nothing — its cut plays as it is.

    Reads the clip's sidecar and its source's record, which is two small
    reads per clip; clips are few beside the files the scan lists."""
    decision = decisions.read(clip)
    if decision is None or not decision.is_clip:
        return False
    assert decision.clip_in is not None and decision.clip_out is not None
    if _clip_files(clip, decision, renders) != _clip_hashed(clip):
        return True
    if decision.is_still:
        return paths.still_path(clip, RENDER_DIR, decision.clip_in).name \
            not in renders
    if _playable_source(clip):
        return False
    return paths.play_path(clip, RENDER_DIR, decision.clip_in,
                           decision.clip_out).name not in renders


def _clip_files(clip: Path, decision: "decisions.Decision",
                renders: set[str] | None = None) -> dict[str, Path]:
    """A clip's own files that exist, as `role -> path`: its cut, its
    playback render, its still."""
    assert decision.clip_in is not None and decision.clip_out is not None
    if decision.is_still:
        wanted = {"content": paths.still_path(clip, RENDER_DIR,
                                              decision.clip_in)}
    else:
        wanted = {"content": paths.cut_path(clip, RENDER_DIR, decision.clip_in,
                                            decision.clip_out),
                  "render": paths.play_path(clip, RENDER_DIR, decision.clip_in,
                                            decision.clip_out)}
    return {role: path for role, path in wanted.items()
            if (path.name in renders if renders is not None else path.is_file())}


def _clip_hashed(clip: Path) -> dict[str, Path]:
    """Which of a clip's files its record has hashes of."""
    record = _record_of(clip) or {}
    raw: object = record.get("hashed")
    names = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    folder = RENDER_DIR / clip.parent.name
    return {str(role): folder / str(name) for role, name in names.items()}


def _note_clip(clip: Path, decision: "decisions.Decision") -> bool:
    """Record the content hashes of a clip's own files, so an import can
    tell a copy of one for what it is (`roundtrip`) — the cut or the still as
    its content, the playback render as its render.

    A meta record of its own, marked as a clip's so the index does not take it
    for a file; rewritten whenever the files it names are not the files there
    are, which is how a new cut from the NAS, or a range that moved, is
    noticed."""
    files = _clip_files(clip, decision)
    if files == _clip_hashed(clip):
        return False
    record: dict[str, Any] = {"file": clip.name, "folder": clip.parent.name,
                              "clip": True,
                              "hashed": {r: p.name for r, p in files.items()}}
    for role, path in files.items():
        digest = identity.content_hash(path)
        if digest:
            record[f"{role}_hash"] = digest
    dest = meta_path(clip)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + EXPORT_TMP_SUFFIX)
    part.write_text(json.dumps(record), encoding="utf-8")
    os.replace(part, dest)
    return True


def _playable_source(clip: Path) -> bool:
    """Whether a clip's source plays in a browser as it is."""
    source = clips.source_of(clip.name)
    if source is None:
        return True
    codec = video_codec(clip.parent / source)
    return bool(codec) and codec.lower() in _PLAYABLE_CODECS


def make_still(source: Path, at: float, dest: Path, *,
               taken: str | None = None, clip_id: str | None = None) -> bool:
    """A still's own file: the frame at `at`, full size, as a JPEG.

    **JPEG, like every photograph in the library** (spec/clips.md §6),
    however it was made. Near the top of JPEG's quality with no colour
    subsampling, since this is the file that stands in for a photograph
    somebody chose. HDR is brought down the way poster frames are; the
    orientation is applied, since a JPEG has nowhere to carry the video's.
    Dated as the frame was taken, which is the source's own clock plus the
    moment.
    """
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if ffmpeg is None:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + EXPORT_TMP_SUFFIX + ".jpg")
    head = [ffmpeg, "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", str(source)]
    tail = ["-frames:v", "1", "-pix_fmt", "yuvj444p", "-q:v", "1", str(tmp)]
    tries = ([[*head, "-vf", _TONEMAP, *tail], [*head, *tail]]
             if is_hdr(source) else [[*head, *tail]])
    for cmd in tries:
        try:
            done = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=_FFMPEG_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            tmp.unlink(missing_ok=True)
            continue
        if done.returncode == 0 and tmp.is_file() and tmp.stat().st_size:
            break
        tmp.unlink(missing_ok=True)
    if not tmp.is_file():
        return False
    exiftool = shutil.which("exiftool") or shutil.which("exiftool.exe")
    if exiftool is not None:
        # Dated as the frame was taken, and stamped as pix's own
        # (`roundtrip`), so a copy that comes back is known for what it is.
        from pix import exiftool_config_path

        tags = [f"-XMP-pix:SourceFile={source.parent.name}/{source.name}",
                f"-XMP-pix:ClipRange={paths.seconds(at)}"]
        if clip_id:
            tags.append(f"-XMP-pix:ClipId={clip_id}")
        if taken:
            tags += [f"-DateTimeOriginal={taken}", f"-CreateDate={taken}"]
        subprocess.run([exiftool, "-config", str(exiftool_config_path()),
                        "-q", "-overwrite_original", *tags, str(tmp)],
                       capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT)
    os.replace(tmp, dest)
    return True


def render_clip(source: Path, clip_in: float, clip_out: float, dest: Path,
                *, timeout: float = _ENCODE_TIMEOUT) -> bool:
    """A clip's playback render: its stretch of the source, as H.264.

    For a clip whose cut keeps a codec a browser will not play — HEVC, most
    of the phone footage — so that it can be watched and shared at all. The
    same encode as a whole video's render, over the clip's range only.
    """
    return _encode(source, dest, start=clip_in, length=clip_out - clip_in,
                   stamp=roundtrip.stamp_args(
                       f"{source.parent.name}/{source.name}",
                       clip_id=clips.id_of(dest.name.partition("@")[0]),
                       clip_range=f"{paths.seconds(clip_in)}-"
                                  f"{paths.seconds(clip_out)}"),
                   timeout=timeout)


def _derive_clip(clip: Path, summary: ProcessSummary,
                 lock: threading.Lock) -> None:
    """A clip's own pictures, from a frame inside it.

    Until these exist a clip shows its source's pictures, and only to someone
    who may see the source — so for everyone else, this is the difference
    between no picture and one they are entitled to. The frame is a tenth of
    the way in, for the reason poster frames are; a still's is its own frame.

    Nothing else: a clip has no meta record (its facts are its source's), no
    render yet, and no filmstrip — it is edited on its source's timeline.
    """
    decision = decisions.read(clip)
    source_name = clips.source_of(clip.name)
    if (decision is None or not decision.is_clip or source_name is None
            or decision.clip_in is None or decision.clip_out is None):
        return
    source = clip.parent / source_name
    at = decision.clip_in + FRAME_AT * (decision.clip_out - decision.clip_in)
    # The file a viewer is given first, since it is what lets them see the
    # clip at all; the pictures after.
    try:
        if decision.is_still:
            dest = paths.still_path(clip, RENDER_DIR, decision.clip_in)
            if not dest.is_file():
                from pix.nas import index as ix

                capture, _, _ = clips.date(
                    ix.capture_of(_record_of(source)), None,
                    decision.clip_in, None)
                if make_still(source, decision.clip_in, dest, taken=capture,
                              clip_id=clips.id_of(clip.name)):
                    with lock:
                        summary.stills += 1
        elif not _playable_source(clip):
            dest = paths.play_path(clip, RENDER_DIR, decision.clip_in,
                                   decision.clip_out)
            if not dest.is_file():
                if render_clip(source, decision.clip_in, decision.clip_out,
                               dest):
                    with lock:
                        summary.renders += 1
                else:
                    with lock:
                        summary.failed.append(f"{clip.name}: render failed")
    except Exception as e:                    # noqa: BLE001
        with lock:
            summary.failed.append(f"{clip.name}: {type(e).__name__}: {e}")
    try:
        if _note_clip(clip, decision):
            with lock:
                summary.metas += 1
    except Exception as e:                    # noqa: BLE001
        with lock:
            summary.failed.append(f"{clip.name}: hash: {type(e).__name__}: {e}")
    want_thumb, want_large, want_preview, _ = needs_work(clip)
    if not (want_thumb or want_large or want_preview):
        return
    temp = _poster_frame(source, at=at)
    if temp is None:
        with lock:
            summary.unsupported += 1
        return
    try:
        if want_thumb:
            _resize(temp, derived_path(clip, THUMB_DIR), THUMB_PX)
            with lock:
                summary.thumbs += 1
        if want_large:
            _resize(temp, derived_path(clip, LARGE_DIR), LARGE_PX)
            with lock:
                summary.larges += 1
        if want_preview:
            _resize(temp, derived_path(clip, PREVIEW_DIR), PREVIEW_PX)
            with lock:
                summary.previews += 1
    except Exception as e:                    # noqa: BLE001
        with lock:
            summary.failed.append(f"{clip.name}: {type(e).__name__}: {e}")
    finally:
        temp.unlink(missing_ok=True)


def _write_meta(media: Path, exif: "_ExifPool") -> bool:
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
    data = exif.read(media)
    if data is None:
        return False

    try:
        stat = media.stat()
    except OSError:
        return False

    # Identity, taken here because this is the one moment the whole library is
    # being read anyway (spec/nas-app.md §15). The perceptual hash is for
    # images only: a clip needs a frame extracted before it has pixels to
    # compare, which is ffmpeg work this path deliberately does not do, and
    # `video_fingerprint` already answers that question its own way.
    payload: dict[str, object] = {
        "file": media.name,
        "folder": media.parent.name,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "content_hash": identity.content_hash(media),
        "exif": data,
    }
    if media.suffix.lower() in _IMAGE_EXTS:
        payload["phash"] = identity.perceptual_hash(media)
    # A render already made is hashed here; one made later patches this record
    # on its way out. Either way the two identities of one photograph sit in the
    # same place.
    rendered = render_path(media)
    if rendered.is_file():
        payload["render_hash"] = identity.content_hash(rendered)
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


def _note_render(media: Path) -> None:
    """Record a freshly made render's identity on the metadata already written.

    **A render is a duplicate waiting to happen** (spec/nas-app.md §15): it is
    the file the app hands out, so it is the one that comes back — downloaded,
    passed around, and re-imported by somebody who no longer remembers where it
    came from. Its bytes are a re-encode, so the master's content hash cannot
    recognise it and only what pix itself recorded can.

    That is also why this is a hash and not only the `pix:ArtifactId` stamp the
    render carries. A stamp is metadata, and metadata is exactly what a
    messaging app strips on the way through; the hash is the file, and survives
    anything that does not re-encode it.

    Written by patching rather than by rebuilding the record, because the
    metadata was written before the render existed — meta comes first in a run
    so that a file whose pixels will not decode still has its facts recorded.
    A record that is not there yet is not an error: the next run writes it, and
    picks the render up while it does.
    """
    dest = meta_path(media)
    try:
        raw: object = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict):
        return
    payload = cast("dict[str, object]", raw)
    payload["render_hash"] = identity.content_hash(render_path(media))
    tmp = dest.with_name(dest.name + EXPORT_TMP_SUFFIX)
    try:
        tmp.write_text(json.dumps(payload, indent=1, default=str),
                       encoding="utf-8")
        tmp.replace(dest)
    except OSError:
        tmp.unlink(missing_ok=True)


def render_video(media: Path, *, timeout: float = _ENCODE_TIMEOUT) -> bool:
    """Encode `media` to browser-playable H.264. True if a render was written.

    **NVENC first, libx264 as the fallback.** The GPU is the reason this runs on
    the desktop rather than the NAS; the CPU path exists so a machine without one
    still works rather than failing.

    Rotation is *baked in*, not carried: ffmpeg autorotates on decode by default
    and the re-encode drops the display matrix, so a phone clip shot sideways
    plays the right way up. The retired GPU pipeline got this wrong
    (spec/video-redesign.md), which is worth not repeating.

    Audio is copied when it is already AAC and re-encoded when it is not, which
    is the same rule `convert.convert_to_mp4` uses — the video bitstream is the
    part worth protecting.
    """
    if _encode(media, render_path(media), timeout=timeout):
        _note_render(media)
        return True
    return False


def _encode(media: Path, dest: Path, *, start: float | None = None,
            length: float | None = None, stamp: list[str] | None = None,
            timeout: float = _ENCODE_TIMEOUT) -> bool:
    """Encode `media` — or `length` seconds of it from `start` — to H.264 at
    `dest`. NVENC first, libx264 after; see `render_video`.

    **Stamped as pix's own** (`roundtrip`): the master it came from, and for a
    clip which clip — so a copy downloaded and imported again is known."""
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if ffmpeg is None:
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + EXPORT_TMP_SUFFIX + ".mp4")

    seek = ["-ss", f"{start:.3f}"] if start is not None else []
    span = ["-t", f"{length:.3f}"] if length is not None else []
    base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *seek,
            "-i", str(media), *span]
    # H.264 in a browser is shown as ordinary picture, so an HDR clip has to
    # be brought down to that before it is encoded — or the render is the same
    # washed-out grey the thumbnails were, and playing it back would disagree
    # with the original on every screen.
    tone = ["-vf", _TONEMAP] if is_hdr(media) else []
    stamped = stamp if stamp is not None else roundtrip.stamp_args(
        f"{media.parent.name}/{media.name}")
    tail = ["-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart+use_metadata_tags",
            "-map_metadata", "0", *stamped, str(tmp)]
    attempts = [
        [*base, *tone, "-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
         "-cq", _CQ, "-b:v", "0", "-pix_fmt", "yuv420p", *tail],
        [*base, *tone, "-c:v", "libx264", "-preset", "medium", "-crf", _CQ,
         "-pix_fmt", "yuv420p", *tail],
        # Without the tone map, if this build has no zimg in it.
        [*base, "-c:v", "libx264", "-preset", "medium", "-crf", _CQ,
         "-pix_fmt", "yuv420p", *tail],
    ]

    for cmd in attempts:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            tmp.unlink(missing_ok=True)
            continue
        if proc.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
            tmp.replace(dest)
            return True
        tmp.unlink(missing_ok=True)
    return False


#: Transfer functions that are not display-referred. The picture in the file
#: is stored for a screen far brighter than the one a JPEG assumes, so reading
#: it as though it were ordinary gives a flat, grey, washed-out frame — which
#: is what 196 of this library's 1,069 clips were getting, nearly all of them
#: from one phone.
_HDR: tuple[str, ...] = ("hlg", "arib", "2100", "2084", "pq", "bt2020")

#: Bring an HDR picture down to the range a JPEG and a browser can show.
#:
#: Linear light first, because tone mapping is arithmetic on brightness and
#: the stored signal is a curve; then the tone map itself; then back to the
#: ordinary transfer, primaries and range. Hable because it keeps highlights
#: rather than clipping them, and `desat=0` because desaturating on the way
#: down is the washed-out look this exists to fix.
_TONEMAP: str = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p")


def is_hdr(media: Path) -> bool:
    """Whether this clip stores more brightness than a screen will show.

    Read from the meta tier where it is there, the way `video_codec` does and
    for the same reason: the common case is a small local JSON rather than
    opening a multi-gigabyte file over SMB.
    """
    meta = meta_path(media)
    if meta.is_file():
        try:
            parsed: object = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            exif = cast("dict[str, Any]", parsed).get("exif")
            if isinstance(exif, dict):
                said = " ".join(
                    str(v) for k, v in cast("dict[str, Any]", exif).items()
                    if "TransferCharacteristics" in k or "ColorPrimaries" in k)
                if said:
                    return any(w in said.lower() for w in _HDR)
    return _probed_hdr(media)


def _probed_hdr(media: Path) -> bool:
    """The same question asked of the file itself."""
    ffprobe = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if ffprobe is None:
        return False
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer,color_primaries",
             "-of", "default=nw=1:nk=1", str(media)],
            capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(w in proc.stdout.lower() for w in _HDR)


def _poster_frame(media: Path, *, at: float | None = None) -> Path | None:
    """Extract a representative frame to a temp JPEG, or None if we cannot.

    Seeks to `FRAME_AT` of the duration, or to `at` seconds where that is
    given — a clip's frame is inside the clip, not inside its source. `-ss` before `-i` makes it a keyframe
    seek, which costs a fraction of decoding up to that point — the difference
    between minutes and milliseconds on a long clip.
    """
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if ffmpeg is None:
        return None

    if at is not None:
        offset = max(at, 0.0)
    else:
        duration = _duration(media)
        offset = max(duration * FRAME_AT, 0.0) if duration else 0.0
    # Named with the master folder as well as the file: two folders can hold the
    # same flattened name, and a shared temp would have one worker deleting what
    # another is reading.
    stem = f"{media.parent.name}_{media.name}"
    if at is not None:
        # Several clips of one video are several frames of one file, made at
        # once by different workers.
        stem += f"@{at:.3f}"
    tmp = _scratch() / (stem + ".poster" + EXPORT_TMP_SUFFIX + ".jpg")
    tmp.parent.mkdir(parents=True, exist_ok=True)

    head = [ffmpeg, "-v", "error", "-ss", f"{offset:.3f}", "-i", str(media)]
    tail = ["-frames:v", "1", "-y", str(tmp)]
    # The plain command second, not instead: `zscale` needs an ffmpeg built
    # with zimg, and a washed-out thumbnail beats none at all.
    tries = ([[*head, "-vf", _TONEMAP, *tail], [*head, *tail]]
             if is_hdr(media) else [[*head, *tail]])
    for cmd in tries:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=_FFMPEG_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            tmp.unlink(missing_ok=True)
            continue
        if proc.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
            return tmp
        tmp.unlink(missing_ok=True)
    return None


def _record_of(media: Path) -> dict[str, Any] | None:
    """`media`'s meta record, or None."""
    try:
        parsed: object = json.loads(meta_path(media).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else None


def video_codec(media: Path) -> str | None:
    """The video codec, from the meta tier if it is there and ffprobe if not.

    Preferring the meta tier means the common case costs a small JSON read
    rather than opening a multi-gigabyte file over SMB.
    """
    meta = meta_path(media)
    if meta.is_file():
        parsed: object = None
        try:
            parsed = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            parsed = None
        rec = cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else {}
        raw: object = rec.get("exif")
        if isinstance(raw, dict):
            exif = cast("dict[str, Any]", raw)
            for key in ("CompressorID", "VideoCodec"):
                for full, value in exif.items():
                    if full.split(":")[-1] == key and value:
                        return str(value)

    ffprobe = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if ffprobe is None:
        return None
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=codec_name", "-of",
           "default=nw=1:nk=1", str(media)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None


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
