"""Clips and stills: the APIs the splice page edits them through, the page
itself, the background queue that cuts them losslessly, and the startup hook
that resumes cuts a restart interrupted.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, cast, Sequence

from fastapi import Body, Depends, FastAPI, HTTPException, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel

from pix import datestr
from pix.nas import (
    clips,
    cut,
    decisions,
    destroy as destroy_mod,
    history,
    index as ix,
    paths,
    webroots,
)
from pix.nas.assets import asset
from pix.nas.decisions import Decision
from pix.nas.webapp import logs
from pix.nas.webapp.app import app, db, Principal, require_admin, write_lock
from pix.nas.webapp.marks import mark
from pix.nas.webapp.shell import page
from pix.nas.webapp.text import h as _h, js, q, split
from pix.nas.webapp.writes import Change, decide, did, master_file, recorded


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    """Open the logs, pick up cuts a restart interrupted, then serve."""
    logs.install()
    threading.Thread(target=resume_cuts, name="pix-resume-cuts",
                     daemon=True).start()
    yield


# Attached here rather than when the app is made: what it starts is the cut
# queue, which belongs to the clips, and the app is made before they are.
app.router.lifespan_context = lifespan


class ClipRange(BaseModel):
    """Where a clip starts and ends, in seconds. Equal for a still."""

    start: float
    end: float


class MakeClipsBody(BaseModel):
    folder: str
    source: str
    clips: list[ClipRange]


class ClipRangeBody(BaseModel):
    folder: str
    name: str
    start: float
    end: float


class ClipSplitBody(BaseModel):
    folder: str
    name: str
    at: float


class ClipMergeBody(BaseModel):
    folder: str
    first: str
    second: str


def ms(value: float) -> float:
    """Milliseconds, which is finer than any frame and what the sidecar
    keeps — so a range compared here is the range that will be stored."""
    return round(value, 3)


def clip_conn() -> sqlite3.Connection:
    if not webroots.DB_PATH.is_file():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "the index has not been built")
    return ix.open_rw(webroots.DB_PATH)


def clip_row(conn: sqlite3.Connection, folder: str,
              name: str) -> sqlite3.Row:
    row = ix.one(conn, folder, name)
    if row is None or row["clip_of"] is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such clip")
    return row


def siblings_of(conn: sqlite3.Connection, folder: str, source: str,
              *, besides: Sequence[str] = ()) -> list[tuple[float, float]]:
    """The ranges a clip must not overlap: its living siblings'."""
    return [(float(c["clip_in"]), float(c["clip_out"]))
            for c in ix.clips_of(conn, folder, source)
            if not c["deleted"] and c["name"] not in besides]


def check(start: float, end: float, *, siblings: list[tuple[float, float]],
           duration: float | None) -> None:
    try:
        clips.check(start, end, siblings=siblings, duration=duration)
    except clips.ClipError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e


def content_change(decision: Decision, *, start: float, end: float) -> Change:
    """A whole clip decision as a write: content and range, nothing left
    to what the sidecar said before — because before, there was none."""
    return Change(event=decision.event, date_override=decision.date_override,
                   tags=list(decision.tags), people=list(decision.people),
                   audience=list(decision.audience),
                   clip_in=start, clip_out=end)


def gone_change() -> Change:
    """Every field cleared: what removing a clip writes, so that the log
    holds all of it and reverting brings the whole clip back."""
    return Change(event=None, date_override=None, tags=[], people=[],
                   audience=[], deleted=False, stacked_under=None,
                   no_stack=False, clip_in=None, clip_out=None)


def write_clips(conn: sqlite3.Connection,
                 writes: Sequence[tuple[str, str, Change, bool]]
                 ) -> list[history.Before]:
    """Write each `(folder, name, change, creating)` and say what it did."""
    undo: list[history.Before] = []
    for folder, name, change, creating in writes:
        was, decision, _ = decide(folder, name, change, conn=conn,
                                   creating=creating)
        undo.append(history.Before(
            folder, name, was, did=did(change, recorded(change), decision)))
    return undo


@app.get("/api/clips/{folder}/{source}")
def api_clips(folder: str, source: str,
              user: Annotated[Principal, Depends(require_admin)]
              ) -> JSONResponse:
    """A source's clips and stills, in the order they come in the video."""
    conn = clip_conn()
    try:
        rows = ix.clips_of(conn, folder, source)
    finally:
        conn.close()
    return JSONResponse([clip_json(r) for r in rows])


@app.post("/api/clips/make")
def api_clips_make(user: Annotated[Principal, Depends(require_admin)],
                   body: Annotated[MakeClipsBody, Body()]) -> JSONResponse:
    """Cut one or more clips — or stills — out of a video.

    Each starts with a **copy** of the source's content decisions
    (`clips.inherited`), then is its own. The source is untouched: nothing
    about making a clip hides it (spec/clips.md §3).
    """
    if not body.clips:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no clips asked for")
    media = master_file(body.folder, body.source)
    conn = clip_conn()
    try:
        row = ix.one(conn, body.folder, body.source)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "not indexed yet — run pix2 index")
        why = clips.can_splice(body.source, row["kind"])
        if why is None and (row["stacked_under"]
                            or ix.members(conn, f"{body.folder}/{body.source}")):
            # Video does not stack now, but a few stacks from before remain,
            # and cutting a video out from under one is a question for when
            # video stacking is designed (spec/clips.md §4).
            why = "this video is in a stack — take it out first"
        if why is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, why)
        taken = {clips.id_of(str(c["name"]))
                 for c in ix.clips_of(conn, body.folder, body.source)}
        siblings = siblings_of(conn, body.folder, body.source)
        base = clips.inherited(decisions.read(media), row["event"])
        writes: list[tuple[str, str, Change, bool]] = []
        for start, end in snapped_ranges(keyframes_of(body.folder, body.source),
                                   [(ms(a.start), ms(a.end))
                                    for a in body.clips], siblings):
            check(start, end, siblings=siblings, duration=row["duration"])
            siblings.append((start, end))
            clip_id = clips.new_id(taken)
            taken.add(clip_id)
            writes.append((body.folder, clips.name_of(body.source, clip_id),
                           content_change(base, start=start, end=end), True))
        undo = write_clips(conn, writes)
    finally:
        conn.close()
    n = len(undo)
    history.record(user.name, f"cut {n} clip{'s' if n != 1 else ''} from "
                   f"{body.source}", undo)
    for b in undo:
        recut(body.folder, b.name)
    return JSONResponse({"made": [b.name for b in undo]})


def snapped_ranges(keys: tuple[float, ...] | None,
             asked: list[tuple[float, float]],
             siblings: list[tuple[float, float]]
             ) -> list[tuple[float, float]]:
    """The ranges one request asked for, each start on a keyframe.

    A start never snaps back over the clip before it — an existing sibling,
    or an earlier range in this request. Two ranges asked for touching stay
    touching: the first ends wherever the second's start landed, which is
    what makes Split one cut rather than two that nearly meet.
    """
    if not keys:
        return asked
    out: list[tuple[float, float]] = []
    for start, end in asked:
        if start == end:
            out.append((start, end))
            continue
        lo = max([e for s0, e in siblings if s0 != e and e <= start + 0.0005]
                 + [0.0])
        for (a0, b0), (a1, _) in zip(asked, out):
            if a0 == b0:
                continue
            if abs(b0 - start) < 0.0005:
                lo = max(lo, a1 + 0.001)
            elif b0 <= start:
                lo = max(lo, b0)
        out.append((snap_start(keys, start, end, lo), end))
    for i, (a0, b0) in enumerate(asked):
        for j, (c0, _) in enumerate(asked):
            if i != j and a0 != b0 and abs(b0 - c0) < 0.0005:
                out[i] = (out[i][0], out[j][0])
    return out


@app.post("/api/clips/range")
def api_clips_range(user: Annotated[Principal, Depends(require_admin)],
                    body: Annotated[ClipRangeBody, Body()]) -> JSONResponse:
    """Move a clip's ends — or a still's frame. Its id and its decisions stay
    exactly where they were (spec/clips.md §2)."""
    start, end = ms(body.start), ms(body.end)
    conn = clip_conn()
    try:
        row = clip_row(conn, body.folder, body.name)
        if (start == end) != (row["clip_in"] == row["clip_out"]):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "a still stays a still and a clip stays a clip")
        source = ix.one(conn, body.folder, str(row["clip_of"]))
        siblings = siblings_of(conn, body.folder, str(row["clip_of"]),
                             besides=[body.name])
        if start != float(row["clip_in"]):
            lo = max([e for s0, e in siblings
                      if s0 != e and e <= start + 0.0005] + [0.0])
            start = snap_start(keyframes_of(body.folder, str(row["clip_of"])),
                                start, end, lo)
        check(start, end, siblings=siblings,
               duration=source["duration"] if source else None)
        undo = write_clips(conn, [(body.folder, body.name,
                                    Change(clip_in=start, clip_out=end),
                                    False)])
    finally:
        conn.close()
    history.record(user.name, f"moved the ends of {body.name}", undo)
    recut(body.folder, body.name)
    return JSONResponse({"name": body.name, "start": start, "end": end})


@app.post("/api/clips/split")
def api_clips_split(user: Annotated[Principal, Depends(require_admin)],
                    body: Annotated[ClipSplitBody, Body()]) -> JSONResponse:
    """Cut a clip in two at `at`.

    The first half keeps the id and everything decided about it; the second
    is a new clip that starts with a **copy** of those decisions, because
    both halves were that clip and both start out true to it.
    """
    at = ms(body.at)
    conn = clip_conn()
    try:
        row = clip_row(conn, body.folder, body.name)
        start, end = float(row["clip_in"]), float(row["clip_out"])
        if row["deleted"]:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "that clip is binned")
        if not start < at < end:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{at:g}s is not inside the clip ({start:g}s–{end:g}s)")
        source = str(row["clip_of"])
        keys = keyframes_of(body.folder, source)
        if keys:
            # The second half starts here, so here has to be a keyframe.
            snapped = cut.snap(at, keys, lo=start + 0.001, hi=end)
            if snapped is None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "there is no keyframe inside this clip to split it on")
            at = snapped
        was = decisions.read(master_file(body.folder, body.name)) or Decision()
        taken = {clips.id_of(str(c["name"]))
                 for c in ix.clips_of(conn, body.folder, source)}
        second = clips.name_of(source, clips.new_id(taken))
        undo = write_clips(conn, [
            (body.folder, body.name, Change(clip_out=at), False),
            (body.folder, second, content_change(was, start=at, end=end), True)])
    finally:
        conn.close()
    history.record(user.name, f"split {body.name}", undo)
    recut(body.folder, body.name)
    recut(body.folder, second)
    return JSONResponse({"first": body.name, "second": second, "at": at})


@app.post("/api/clips/merge")
def api_clips_merge(user: Annotated[Principal, Depends(require_admin)],
                    body: Annotated[ClipMergeBody, Body()]) -> JSONResponse:
    """Take away the split between two clips that meet.

    The earlier one survives, with both clips' decisions merged by the
    duplicate ladder (`clips.merged`); the later one is removed — logged in
    full, so reverting the merge brings it back as it was.
    """
    conn = clip_conn()
    try:
        a = clip_row(conn, body.folder, body.first)
        b = clip_row(conn, body.folder, body.second)
        if a["clip_in"] > b["clip_in"]:
            a, b = b, a
        if a["clip_of"] != b["clip_of"]:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "those are clips of two different videos")
        if a["deleted"] or b["deleted"]:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "one of those clips is binned")
        if a["clip_in"] == a["clip_out"] or b["clip_in"] == b["clip_out"]:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "a still cannot be joined to anything")
        # Neighbours, not only clips that meet: taking away the marker at the
        # end of a clip makes it grow to the next one, and the stretch nobody
        # had cut between them comes with it. What may not be absorbed is
        # another clip — joining across one would swallow it.
        between = [c for c in ix.clips_of(conn, body.folder, str(a["clip_of"]))
                   if not c["deleted"] and c["clip_in"] != c["clip_out"]
                   and c["name"] not in (a["name"], b["name"])
                   and float(c["clip_in"]) < float(b["clip_in"])
                   and float(c["clip_out"]) > float(a["clip_out"])]
        if float(a["clip_out"]) > float(b["clip_in"]) + 0.001 or between:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "only neighbouring clips can be joined")
        first = master_file(body.folder, str(a["name"]))
        second = master_file(body.folder, str(b["name"]))
        content = clips.merged(
            decisions.read(first) or Decision(),
            decisions.sidecar_path(first).stat().st_mtime,
            decisions.read(second) or Decision(),
            decisions.sidecar_path(second).stat().st_mtime)
        undo = write_clips(conn, [
            (body.folder, str(a["name"]),
             content_change(content, start=float(a["clip_in"]),
                      end=float(b["clip_out"])), False),
            (body.folder, str(b["name"]), gone_change(), False)])
    finally:
        conn.close()
    history.record(user.name, f"joined {a['name']} and {b['name']}", undo)
    recut(body.folder, str(a["name"]))
    recut(body.folder, str(b["name"]))
    return JSONResponse({"name": a["name"]})


class ClipBoundaryBody(BaseModel):
    folder: str
    first: str
    second: str
    at: float


@app.post("/api/clips/boundary")
def api_clips_boundary(user: Annotated[Principal, Depends(require_admin)],
                       body: Annotated[ClipBoundaryBody, Body()]
                       ) -> JSONResponse:
    """Move the marker two touching clips share: the end of one and the start
    of the next, together, as one edit.

    Two range writes would leave the clips overlapping or apart between them,
    and would be two lines in History for one gesture. The marker is a start,
    so it lands on a keyframe.
    """
    at = ms(body.at)
    conn = clip_conn()
    try:
        a = clip_row(conn, body.folder, body.first)
        b = clip_row(conn, body.folder, body.second)
        if float(a["clip_in"]) > float(b["clip_in"]):
            a, b = b, a
        if (a["clip_of"] != b["clip_of"]
                or abs(float(a["clip_out"]) - float(b["clip_in"])) > 0.001):
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "those clips do not share a marker")
        lo, hi = float(a["clip_in"]), float(b["clip_out"])
        keys = keyframes_of(body.folder, str(a["clip_of"]))
        if keys:
            snapped = cut.snap(at, keys, lo=lo + 0.001, hi=hi)
            if snapped is None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "there is no keyframe between them to move it to")
            at = snapped
        if not lo < at < hi:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "a marker cannot move past its clips' ends")
        # Shrink before growing, so the two never overlap on the way.
        order = [(str(b["name"]), Change(clip_in=at, clip_out=float(b["clip_out"]))),
                 (str(a["name"]), Change(clip_in=lo, clip_out=at))]
        if at < float(a["clip_out"]):
            order.reverse()
        undo = write_clips(conn, [(body.folder, n, c, False)
                                   for n, c in order])
    finally:
        conn.close()
    history.record(user.name, f"moved the cut between {a['name']} and "
                   f"{b['name']}", undo)
    recut(body.folder, str(a["name"]))
    recut(body.folder, str(b["name"]))
    return JSONResponse({"at": at})


class DraftClip(BaseModel):
    """One clip as the splice page ends with it.

    `id` is the clip's name for one already saved, or anything starting
    `new` for one made in this draft. `copy_of` names the clip a split part
    was cut from, so it starts with that clip's tags; `absorbs` the clips
    joined into this one, whose tags merge into it.
    """

    id: str
    start: float
    end: float
    copy_of: str | None = None
    absorbs: list[str] = []


class SaveClipsBody(BaseModel):
    folder: str
    source: str
    clips: list[DraftClip]
    deleted: list[str] = []


@app.post("/api/clips/save")
def api_clips_save(user: Annotated[Principal, Depends(require_admin)],
                   body: Annotated[SaveClipsBody, Body()]) -> JSONResponse:
    """Save the splice page's draft: the clips it ends with, **by identity**.

    Nothing is inferred. Every clip carries who it is through the edit — a
    trimmed clip is the same clip, a split part says which clip it was cut
    from, a join says which clips it absorbed, and a deletion names the clip
    deleted — because the page asked about each of those when it happened.
    So this applies exactly that, and a clip that was only moved about keeps
    everything decided about it, however many times its markers moved.

    Every saved clip must be accounted for — kept, absorbed or deleted. One
    that is not means the clips changed since the page loaded, and the draft
    was made against something that is no longer there.

    One History entry for the whole save, and reverting it puts every clip
    back as it was.
    """
    media = master_file(body.folder, body.source)
    conn = clip_conn()
    try:
        row = ix.one(conn, body.folder, body.source)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not indexed")
        live = {str(c["name"]): c for c in ix.clips_of(conn, body.folder,
                                                        body.source)
                if not c["deleted"]}
        kept = {c.id for c in body.clips if c.id in live}
        absorbed = {a for c in body.clips for a in c.absorbs}
        gone = set(body.deleted)
        for c in body.clips:
            if not c.id.startswith("new") and c.id not in live:
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    f"{c.id} is not a clip of this video "
                                    "any more — reload the page")
        missing = set(live) - kept - absorbed - gone
        if missing or (absorbed | gone) - set(live) or kept & (absorbed | gone):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "the clips changed since this page loaded — reload it")
        # The ranges, checked as the routes check one: forwards, inside the
        # video, never over each other — and each start on a keyframe.
        keys = keyframes_of(body.folder, body.source)
        ordered = sorted(body.clips, key=lambda c: (c.start, c.end))
        placed: list[tuple[float, float]] = []
        final: dict[str, tuple[float, float]] = {}
        for c in ordered:
            start, end = ms(c.start), ms(c.end)
            if start != end:
                lo = max([e for s0, e in placed
                          if s0 != e and e <= start + 0.0005] + [0.0])
                start = snap_start(keys, start, end, lo)
            check(start, end, siblings=placed, duration=row["duration"])
            placed.append((start, end))
            final[c.id] = (start, end)

        def said(name: str) -> tuple[Decision, float]:
            path = master_file(body.folder, name)
            try:
                at = decisions.sidecar_path(path).stat().st_mtime
            except OSError:
                at = 0.0
            return decisions.read(path) or Decision(), at

        base = clips.inherited(decisions.read(media), row["event"])
        taken = {clips.id_of(str(c["name"]))
                 for c in ix.clips_of(conn, body.folder, body.source)}
        names: dict[str, str] = {}
        content: dict[str, Decision] = {}
        writes: list[tuple[str, str, Change, bool]] = []
        # Saved clips first, so a split part copying one copies what it says.
        for c in body.clips:
            if c.id not in live:
                continue
            start, end = final[c.id]
            if c.absorbs:
                merged, at = said(c.id)
                for other in c.absorbs:
                    theirs, their_at = said(other)
                    merged = clips.merged(merged, at, theirs, their_at)
                    at = max(at, their_at)
                content[c.id] = merged
                writes.append((body.folder, c.id,
                               content_change(merged, start=start, end=end), False))
            else:
                content[c.id] = said(c.id)[0]
                was = live[c.id]
                if (float(was["clip_in"]), float(was["clip_out"])) != (start, end):
                    writes.append((body.folder, c.id,
                                   Change(clip_in=start, clip_out=end), False))
            names[c.id] = c.id
        pending = [c for c in body.clips if c.id not in live]
        while pending:
            progressed = False
            for c in list(pending):
                parent = c.copy_of
                if parent is not None and parent not in content:
                    continue
                start, end = final[c.id]
                start_with = content[parent] if parent else base
                # A new clip can absorb one too — a split part joined to its
                # neighbour — and takes on its tags the same way.
                at = 0.0
                for other in c.absorbs:
                    theirs, their_at = said(other)
                    start_with = clips.merged(start_with, at, theirs, their_at)
                    at = max(at, their_at)
                clip_id = clips.new_id(taken)
                taken.add(clip_id)
                name = clips.name_of(body.source, clip_id)
                names[c.id] = name
                content[c.id] = start_with
                writes.append((body.folder, name,
                               content_change(start_with, start=start, end=end),
                               True))
                pending.remove(c)
                progressed = True
            if not progressed:
                raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                    "a split part copies a clip that is not "
                                    "in the draft")
        for other in sorted(absorbed):
            writes.append((body.folder, other, gone_change(), False))
        for other in sorted(gone):
            writes.append((body.folder, other, Change(deleted=True), False))
        undo = write_clips(conn, writes)
    finally:
        conn.close()
    if undo:
        history.record(user.name, f"edited the clips of {body.source}", undo)
    for b in undo:
        if b.name not in gone:
            recut(body.folder, b.name)
    return JSONResponse({"names": names, "changes": len(undo)})


class FreeBody(BaseModel):
    folder: str
    source: str


@app.post("/api/clips/free")
def api_clips_free(user: Annotated[Principal, Depends(require_admin)],
                   body: Annotated[FreeBody, Body()]) -> JSONResponse:
    """Bin a source and keep its clips, each as a file of its own
    (spec/clips.md §5).

    Each clip's cut is copied into master beside the source as an ordinary
    video with its decisions, and stops being a clip. A copy, not an encode —
    the cut is the source's own samples — so the NAS does it. Then the source
    is binned, which is the one part of this History can take back: the new
    files are real files now, and removing them is an ordinary delete.

    Offered only once every clip has its cut; stills have no file until the
    desktop makes one, so a source with stills waits for that.
    """
    media = master_file(body.folder, body.source)
    conn = clip_conn()
    freed: list[str] = []
    try:
        live = [c for c in ix.clips_of(conn, body.folder, body.source)
                if not c["deleted"]]
        if not live:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"{body.source} has no clips")
        made: list[tuple[sqlite3.Row, Path]] = []
        for c in live:
            clip_media = webroots.MASTER_DIR / body.folder / str(c["name"])
            if c["clip_in"] == c["clip_out"]:
                file = paths.still_path(clip_media, webroots.RENDER_DIR,
                                        float(c["clip_in"]))
                if not file.is_file():
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        "its photos have no files yet — pix2 process makes "
                        "them. Archive the video instead for now.")
            else:
                file = paths.cut_path(clip_media, webroots.RENDER_DIR,
                                      float(c["clip_in"]), float(c["clip_out"]))
                if not file.is_file():
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        "its clips are still being cut — try again in a moment")
            made.append((c, file))
        record = ix.record_of(webroots.META_DIR, body.folder, body.source) or {}
        for c, file in made:
            name = str(c["name"])
            clip = webroots.MASTER_DIR / body.folder / name
            own = webroots.MASTER_DIR / body.folder / f"{name}{file.suffix}"
            if own.exists():
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    f"{own.name} is already in master")
            with write_lock:
                tmp = own.with_name(own.name + ".tmp")
                shutil.copyfile(file, tmp)
                os.replace(tmp, own)
                was = decisions.read(clip) or Decision()
                decisions.write(own, Decision(
                    event=was.event, date_override=was.date_override,
                    tags=was.tags, people=was.people, audience=was.audience))
                stand_in(body.folder, own, c, record, body.source)
                destroy_mod.destroy(clip, conn=conn, folder=body.folder,
                                    name=name)
            ix.refresh(conn, body.folder, own.name, meta_dir=webroots.META_DIR,
                       master_dir=webroots.MASTER_DIR)
            freed.append(own.name)
        binned = Change(deleted=True)
        was, decision, _ = decide(body.folder, body.source, binned,
                                   conn=conn)
        undo = [history.Before(body.folder, body.source, was,
                               did=did(binned, recorded(binned), decision))]
    finally:
        conn.close()
    del media
    n = len(freed)
    history.record(user.name, f"binned {body.source}, keeping its {n} "
                   f"clip{'s' if n != 1 else ''} as files of their own", undo)
    return JSONResponse({"freed": freed})


def stand_in(folder: str, own: Path, clip: sqlite3.Row,
              source_record: dict[str, Any], source: str) -> None:
    """A meta record for a clip made into a file, until `process` probes it.

    The index sees only what the meta tier describes, and `process` runs on
    the desktop — so without this the clips would leave the grid the moment
    they became real. What it says is what the clip's row already knew,
    marked as a placeholder so `process` replaces it rather than trusting it,
    and naming the source whose pictures stand in meanwhile.
    """
    raw: object = source_record.get("exif")
    had = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    keep = ("CompressorID", "VideoFrameRate", "ImageWidth", "ImageHeight",
            "Model", "Make", "Rotation")
    exif: dict[str, Any] = {k: v for k, v in had.items()
                            if k.split(":")[-1] in keep}
    if clip["capture_date"]:
        exif["EXIF:DateTimeOriginal"] = clip["capture_date"]
    if clip["clip_out"] != clip["clip_in"]:
        exif["QuickTime:Duration"] = (
            f'{float(clip["clip_out"]) - float(clip["clip_in"]):.2f} s')
    else:
        # A photograph now: its codec and length are its video's, not its own.
        exif = {k: v for k, v in exif.items()
                if k.split(":")[-1] in ("ImageWidth", "ImageHeight", "Model",
                                        "Make", "DateTimeOriginal")}
    st = own.stat()
    record = {"file": own.name, "folder": folder, "size": st.st_size,
              "mtime_ns": st.st_mtime_ns, "exif": exif,
              "placeholder": True, "stand_in": source}
    path = paths.meta_path(own, webroots.META_DIR)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    os.replace(tmp, path)


@app.get("/splice/{folder}/{name}", response_class=HTMLResponse)
def splice(folder: str, name: str,
           user: Annotated[Principal, Depends(require_admin)]) -> Response:
    """Cut a video into clips and stills, on a timeline under it.

    **A clip is always cut on its source's timeline**, so asking to splice a
    clip opens its source, standing on that clip. Nothing else can be done
    here — tagging a clip happens in the grid like any other file, and each
    bar links to it.

    The video plays from its render where it has one, and the cuts are made
    against the master; the two share their timestamps, so a range read off
    one is the same range in the other.
    """
    source = clips.source_of(name)
    if source is not None and not (webroots.MASTER_DIR / folder / name).is_file():
        return RedirectResponse(
            f"/splice/{q(folder)}/{q(source)}#{q(name)}",
            status_code=status.HTTP_303_SEE_OTHER)
    media = master_file(folder, name)
    conn = db()
    try:
        row = ix.one(conn, folder, name)
        cut = ix.clips_of(conn, folder, name) if row is not None else []
        stacked = bool(row is not None and (
            row["stacked_under"] or ix.members(conn, f"{folder}/{name}")))
    finally:
        conn.close()
    title = f"Splice — {name}"
    if row is None:
        return page(title, '<p class="empty">Not indexed yet — run '
                     '<code>pix2 index</code>.</p>', user=user)
    why = clips.can_splice(name, str(row["kind"]))
    if why is None and stacked:
        why = ("this video is in a stack — take it out first. Video "
               "stacking is still to be designed, and cutting a video out "
               "from under one is part of that question.")
    record = ix.record_of(webroots.META_DIR, folder, name) or {}
    raw: object = record.get("exif")
    exif = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    codec = str(exif.get("QuickTime:CompressorID") or "").lower()
    playable = (paths.render_path(media, webroots.RENDER_DIR).is_file()
                or codec in paths.PLAYABLE_CODECS)
    if why is None and not playable:
        why = ("waiting for processing — this video will not play in a "
               "browser until pix2 process has made its playable copy.")
    if why is not None:
        return page(title, f'<p class="empty">{_h(why)}</p>', user=user)
    try:
        fps = float(exif.get("QuickTime:VideoFrameRate") or 0) or 30.0
    except (TypeError, ValueError):
        fps = 30.0
    state = {
        "folder": folder, "source": name,
        "duration": row["duration"], "fps": fps,
        "hidden": decisions.ARCHIVED in split(row["audience"]),
        "hiddenName": decisions.ARCHIVED,
        "clips": [clip_json(c) for c in cut if not c["deleted"]],
        "strip": strip_of(media, folder, name),
    }
    src = f"/media/{q(folder)}/{q(name)}"
    return page(title, splice_html().replace("{src}", src),
                 user=user,
                 script=(f"<style>{SPLICE_CSS}</style>"
                         f"<script>const SPLICE={js(state)};</script>"
                         f"<script>{SPLICE_JS}</script>"))


def strip_of(media: Path, folder: str, name: str) -> dict[str, Any] | None:
    """The filmstrip `process` made for this video, as the page draws it —
    or None, and the timeline is plain until there is one."""
    try:
        raw: object = json.loads(paths.strip_info_path(media, webroots.STRIP_DIR)
                                 .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    info = cast("dict[str, Any]", raw)
    try:
        n, w, h = int(info["n"]), int(info["w"]), int(info["h"])
    except (KeyError, TypeError, ValueError):
        return None
    return {"n": n, "w": w, "h": h,
            "url": f"/strip/{q(folder)}/{q(name)}"}


def clip_json(row: sqlite3.Row) -> dict[str, Any]:
    """One clip as the splice page and the clip listing read it."""
    start, end = row["clip_in"], row["clip_out"]
    media = webroots.MASTER_DIR / str(row["folder"]) / str(row["name"])
    made = (start is not None and end is not None and (
        paths.still_path(media, webroots.RENDER_DIR, float(start)).is_file()
        if start == end else
        paths.cut_path(media, webroots.RENDER_DIR, float(start), float(end)).is_file()))
    return {"name": row["name"], "start": start, "end": end,
            "deleted": bool(row["deleted"]), "event": row["event"],
            "date": row["effective_date"], "cut": bool(made)}


def clip_file(folder: str, name: str, *, playable: bool) -> Path | None:
    """A clip's own file: its playback render first where `playable` is
    asked for, then its cut — or None while it has neither."""
    media = webroots.MASTER_DIR / folder / name
    decision = decisions.read(media)
    if decision is None or not decision.is_clip:
        return None
    assert decision.clip_in is not None and decision.clip_out is not None
    if decision.is_still:
        # A still's one file is its JPEG, original and playable alike.
        still = paths.still_path(media, webroots.RENDER_DIR, decision.clip_in)
        return still if still.is_file() else None
    render = paths.play_path(media, webroots.RENDER_DIR, decision.clip_in,
                             decision.clip_out)
    cut_file = paths.cut_path(media, webroots.RENDER_DIR, decision.clip_in,
                              decision.clip_out)
    for candidate in ((render, cut_file) if playable else (cut_file,)):
        if candidate.is_file():
            return candidate
    return None


def first_frame(record: dict[str, Any] | None, offset: float) -> str | None:
    """When a clip's first frame was taken, as a container stamps it.

    The source's own QuickTime clock, which is UTC, plus where the clip
    starts. A date override is not applied here — the cut is a piece of the
    source as recorded, and the override is baked into what is delivered
    (spec/clips.md §7), like any file's.
    """
    raw: object = (record or {}).get("exif")
    exif = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    value = exif.get("QuickTime:CreateDate")
    moment = datestr.parse_exiftool(str(value)) if value else None
    if moment is None:
        return None
    from datetime import timedelta

    return (moment + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def cut_one(folder: str, name: str) -> None:
    """Make one clip's cut, if it has none for its current range, then bring
    its row up to date — which is what lets a viewer see it."""
    media = webroots.MASTER_DIR / folder / name
    source_name = clips.source_of(name)
    if source_name is None:
        return
    decision = decisions.read(media)
    if decision is None or not decision.is_clip:
        cut.sweep(webroots.RENDER_DIR / folder, name, keep=None)
        return
    if decision.is_still:
        return
    assert decision.clip_in is not None and decision.clip_out is not None
    source = webroots.MASTER_DIR / folder / source_name
    if not source.is_file():
        return
    dest = paths.cut_path(media, webroots.RENDER_DIR, decision.clip_in,
                          decision.clip_out)
    if not dest.is_file():
        record = ix.record_of(webroots.META_DIR, folder, source_name)
        cut.make(source, decision.clip_in, decision.clip_out, dest,
                 created=first_frame(record, decision.clip_in))
    cut.sweep(dest.parent, name, keep=dest, kind=".cut.mp4")
    if webroots.DB_PATH.is_file():
        conn = ix.open_rw(webroots.DB_PATH)
        try:
            ix.refresh(conn, folder, name, meta_dir=webroots.META_DIR,
                       master_dir=webroots.MASTER_DIR)
        finally:
            conn.close()


#: The clips waiting to be cut. One worker, a few seconds behind the last
#: change to each (spec/clips.md §6).
CUTS: cut.Queue = cut.Queue(cut_one)


def recut(folder: str, name: str) -> None:
    """A clip's range has changed, or it was just made: what it had is stale.

    The desktop's files for it — thumbnail, preview, playback render — go at
    once, because they show footage that is no longer the clip (§6); the cut
    is replaced by the worker, which keeps the old one only until the new one
    lands. Neither is needed for a curator, who watches the source.
    """
    media = webroots.MASTER_DIR / folder / name
    # The desktop's files for the old range or moment. Their names carry it,
    # so every one of them is stale; `process` makes the new ones.
    for kind in (".play.mp4", ".still.jpg"):
        cut.sweep(webroots.RENDER_DIR / folder, name, keep=None, kind=kind)
    for stale in (paths.render_path(media, webroots.RENDER_DIR),
                  paths.derived_path(media, webroots.THUMB_DIR),
                  paths.derived_path(media, webroots.LARGE_DIR),
                  paths.derived_path(media, webroots.PREVIEW_DIR)):
        try:
            stale.unlink(missing_ok=True)
        except OSError:
            continue
    CUTS.schedule(folder, name)


def resume_cuts() -> None:
    """Schedule every living clip that has no cut for its range — the ones a
    restart interrupted, and any made while ffmpeg was missing."""
    if cut.ffmpeg() is None or not webroots.DB_PATH.is_file():
        return
    try:
        conn = ix.open_ro(webroots.DB_PATH)
    except Exception:                            # noqa: BLE001
        return
    try:
        rows = conn.execute(
            "SELECT folder, name, clip_in, clip_out FROM files "
            "WHERE clip_of IS NOT NULL AND deleted = 0 "
            "AND clip_in < clip_out").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    for row in rows:
        media = webroots.MASTER_DIR / str(row["folder"]) / str(row["name"])
        if not paths.cut_path(media, webroots.RENDER_DIR, float(row["clip_in"]),
                              float(row["clip_out"])).is_file():
            CUTS.schedule(str(row["folder"]), str(row["name"]))


def keyframes_of(folder: str, source: str) -> tuple[float, ...] | None:
    return cut.keyframes(webroots.MASTER_DIR / folder / source)


def snap_start(keys: tuple[float, ...] | None, start: float, end: float,
                lo: float) -> float:
    """A clip's start moved onto a keyframe, since a cut cannot begin
    between them (spec/clips.md §6). Unsnapped where the keyframes are not
    known, and for a still, which is decoded rather than cut."""
    if not keys or start == end:
        return start
    snapped = cut.snap(start, keys, lo=lo, hi=end)
    return start if snapped is None else snapped


@app.get("/api/keyframes/{folder}/{name}")
def api_keyframes(folder: str, name: str,
                  user: Annotated[Principal, Depends(require_admin)]
                  ) -> JSONResponse:
    """Where a video can be cut from — `null` when that is not known."""
    media = master_file(folder, name)
    keys = cut.keyframes(media)
    return JSONResponse({"keys": list(keys) if keys is not None else None})


SPLICE_HTML: str = asset("html/splice.html")


def splice_html() -> str:
    """The page, with its drawings put in — `@name@` for each, so the markup
    reads as a layout rather than as a wall of paths."""
    import re as _re

    return _re.sub(r"@(sp_\w+)@", lambda m: mark(m.group(1), 20),
                   SPLICE_HTML)


SPLICE_CSS: str = asset("css/splice.css")


SPLICE_JS: str = asset("js/splice.js")
