"""Lossless cuts — a clip's own file, made on the NAS (spec/clips.md §6).

A clip is its source's bytes plus a range, and the **cut** is those bytes: a
stream copy of the source's own samples between the clip's ends, into an MP4.
Nothing is decoded and nothing is encoded, which is why the NAS may do it —
its Atom has no AVX and cannot encode (spec/nas-app.md §10), but copying is
I/O, which it is fine at.

**Copy only, enforced.** Every command is built here and checked before it
runs: any codec option that is not `copy`, and any filter, is refused. The
app's first media tool must not be the way encoding drifts onto the Atom.

**A cut starts on a keyframe.** A stream copy cannot begin between them, so
the splice page and the routes snap a clip's start to one (`snap`), and the
stored start *is* where the file begins — in every player, with no edit list
some of them ignore. The end is exact to the frame.

Cuts are made **in the background, a few seconds after the last change**
(`Queue`): dragging an edge is many ranges, and only the last one is wanted.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

#: How long a cut may take. A long clip off spinning disks is a minute or two;
#: this is the point at which it is stuck rather than slow.
TIMEOUT: float = 1800.0
_PROBE_TIMEOUT: float = 300.0

#: How long a clip has to sit still before it is cut. Long enough that
#: dragging an edge does not cut at every position it passed through.
DELAY: float = 3.0


class CutError(Exception):
    """A cut that could not be made."""


def ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def ffprobe() -> str | None:
    return shutil.which("ffprobe")


# --- keyframes ---------------------------------------------------------------

_keys: dict[tuple[str, int, int], tuple[float, ...]] = {}
_keys_lock = threading.Lock()


def keyframes(media: Path) -> tuple[float, ...] | None:
    """Where `media`'s video can be cut from, in seconds; None if unknown.

    Read from the packet flags — the container's index says which samples
    are keyframes, and no frame is decoded. Kept for the life of the process,
    keyed by size and mtime, because the page asks on every open and a master
    file never changes.
    """
    probe = ffprobe()
    if probe is None:
        return None
    try:
        st = media.stat()
    except OSError:
        return None
    key = (str(media), st.st_size, st.st_mtime_ns)
    with _keys_lock:
        if key in _keys:
            return _keys[key]
    try:
        done = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0",
             str(media)],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    found: set[float] = set()
    for line in done.stdout.splitlines():
        when, _, flags = line.partition(",")
        if "K" not in flags:
            continue
        try:
            found.add(round(float(when), 3))
        except ValueError:
            continue
    keys = tuple(sorted(found))
    with _keys_lock:
        _keys[key] = keys
    return keys


def snap(t: float, keys: tuple[float, ...], *, lo: float = 0.0,
         hi: float = float("inf")) -> float | None:
    """The keyframe nearest `t` in `[lo, hi)`, or None if there is none.

    `lo` is the end of the clip before, so snapping never pulls a start back
    over its neighbour: clips may touch, but never overlap.
    """
    inside = [k for k in keys if lo - 0.0005 <= k < hi - 0.0005]
    if not inside:
        return None
    return min(inside, key=lambda k: (abs(k - t), k))


# --- the cut -------------------------------------------------------------------

#: Options that would make ffmpeg do something other than copy.
_NOT_COPY: frozenset[str] = frozenset({
    "-vf", "-af", "-filter", "-filter:v", "-filter:a", "-filter_complex",
    "-lavfi", "-crf", "-qp", "-preset", "-b:v", "-b:a", "-s", "-r",
})


def command(tool: str, media: Path, clip_in: float, clip_out: float,
            dest: Path, *, created: str | None) -> list[str]:
    """The ffmpeg command for one cut — and the only way one is built.

    `-ss` before `-i` seeks the container to the keyframe at or before the
    start, which is the start itself once it has been snapped. Only the video
    and audio are kept: phones write data tracks alongside them that MP4 will
    not hold, and nothing downstream reads them.

    Stamped as it is written (spec/clips.md §6): which clip this is and the
    range it was cut from, and — where the source's own clock is known — the
    moment its first frame was taken, so a downloaded clip does not claim its
    source's start time.
    """
    from pix.nas import clips
    from pix.nas.paths import seconds

    args = [tool, "-v", "error", "-y", "-nostdin",
            "-ss", f"{clip_in:.3f}", "-i", str(media),
            "-t", f"{clip_out - clip_in:.3f}",
            "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
            "-avoid_negative_ts", "make_zero", "-map_metadata", "0",
            "-movflags", "+faststart+use_metadata_tags",
            "-metadata", f"pix:ClipId={clips.id_of(dest_name(dest))}",
            "-metadata", f"pix:ClipRange={seconds(clip_in)}-{seconds(clip_out)}",
            "-metadata", f"pix:SourceFile={media.parent.name}/{media.name}"]
    if created:
        args += ["-metadata", f"creation_time={created}"]
    args += ["-f", "mp4", str(dest)]
    copy_only(args)
    return args


def dest_name(dest: Path) -> str:
    """The clip's name, from the name of one of its cuts."""
    return dest.name.partition("@")[0]


def copy_only(args: list[str]) -> None:
    """Refuse any command that would decode or encode."""
    for i, arg in enumerate(args):
        if arg in _NOT_COPY:
            raise CutError(f"{arg} is not a copy")
        codec = arg.split(":")[0]
        if codec in ("-c", "-codec", "-vcodec", "-acodec"):
            if i + 1 >= len(args) or args[i + 1] != "copy":
                raise CutError(f"{arg} must be copy")


def make(media: Path, clip_in: float, clip_out: float, dest: Path, *,
         created: str | None = None) -> None:
    """Cut `media` between the two ends into `dest`, atomically.

    Written beside its destination and renamed into place, so a cut that is
    there is a whole one — a killed ffmpeg leaves a temp file, never a clip
    that stops half way.
    """
    tool = ffmpeg()
    if tool is None:
        raise CutError("ffmpeg is not installed")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    # The temp shares the destination's name up to its range, which is where
    # the clip's id is read from, so the stamp is the same either way.
    args = command(tool, media, clip_in, clip_out, tmp, created=created)
    try:
        done = subprocess.run(args, capture_output=True, text=True,
                              timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        tmp.unlink(missing_ok=True)
        raise CutError(f"ffmpeg did not finish: {e}") from e
    if done.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise CutError((done.stderr or "ffmpeg failed").strip()[-300:])
    os.replace(tmp, dest)


def sweep(folder: Path, clip_name: str, *, keep: Path | None,
          kind: str | None = None) -> int:
    """Remove every file of `clip_name` but `keep` — the stale ones, whose
    range is not the clip's any more — and any temp a killed one left.

    `kind` narrows it to one sort (`.cut.mp4`, `.play.mp4`, `.still.jpg`),
    because they share the prefix and each is made by a different party: the
    cut by the NAS, the other two by the desktop. Without it — a purge —
    every one goes."""
    removed = 0
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return 0
    prefix = f"{clip_name}@"
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        if kind is not None and kind not in entry.name:
            continue
        if keep is not None and entry.name == keep.name:
            continue
        try:
            os.unlink(entry.path)
            removed += 1
        except OSError:
            continue
    return removed


# --- the queue -------------------------------------------------------------------

class Queue:
    """Cuts waiting to be made, each a few seconds after it was last asked for.

    One worker, because the job is disk-bound and the NAS has other things to
    do. Asking again for a clip already waiting moves its time back rather
    than queueing it twice — that is the debounce.

    `immediate` runs the work in the caller instead, which is what tests
    want: a cut that has happened by the time the request returns.
    """

    def __init__(self, work: Callable[[str, str], None], *,
                 delay: float = DELAY) -> None:
        self.work = work
        self.delay = delay
        self.immediate = False
        self._due: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def schedule(self, folder: str, name: str) -> None:
        if self.immediate:
            self._run(folder, name)
            return
        with self._lock:
            self._due[(folder, name)] = time.monotonic() + self.delay
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop,
                                                name="pix-cuts", daemon=True)
                self._thread.start()
        self._wake.set()

    def pending(self) -> int:
        with self._lock:
            return len(self._due)

    def _loop(self) -> None:
        while True:
            with self._lock:
                if not self._due:
                    self._thread = None
                    return
                now = time.monotonic()
                ready = [k for k, at in self._due.items() if at <= now]
                wait = (0.0 if ready
                        else min(self._due.values()) - now)
                for k in ready:
                    del self._due[k]
            if not ready:
                self._wake.clear()
                self._wake.wait(timeout=max(0.05, wait))
                continue
            for folder, name in ready:
                self._run(folder, name)

    def _run(self, folder: str, name: str) -> None:
        try:
            self.work(folder, name)
        except Exception:                        # noqa: BLE001
            # One clip that will not cut must not stop the rest. The clip
            # stays without files, which a viewer sees as not there yet and a
            # curator sees on the splice page; the next change retries it.
            log.exception("cutting %s/%s", folder, name)
