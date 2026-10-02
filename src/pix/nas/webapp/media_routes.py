"""Serving the files themselves: thumbnails, previews and the larger tiers, a
video streamed for the viewer, a single download and a zip of many — each
only to somebody the file has been shared with.
"""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path
from typing import Annotated, Any, cast, Iterator, Sequence

from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse

from pix.nas import clips, index as ix, paths, webroots
from pix.nas.webapp.app import app, db, Principal, require_user
from pix.nas.webapp.clipping import clip_file
from pix.nas.webapp.session import read_form
from pix.nas.webapp.vocab import mime, ZIP_LIMIT
from pix.nas.webapp.writes import master_file


@app.get("/thumb/{folder}/{name}")
def thumb(folder: str, name: str,
          user: Annotated[Principal, Depends(require_user)]) -> FileResponse:
    allowed(user, folder, name)
    return serve(webroots.THUMB_DIR, folder, name, user)


@app.get("/strip/{folder}/{name}")
def strip(folder: str, name: str,
          user: Annotated[Principal, Depends(require_user)]) -> FileResponse:
    """A video's filmstrip, for the splice page's timeline."""
    allowed(user, folder, name)
    return serve(webroots.STRIP_DIR, folder, name, user)


@app.get("/large/{folder}/{name}")
def large(folder: str, name: str,
          user: Annotated[Principal, Depends(require_user)]) -> FileResponse:
    """The grid's largest setting. Between the two others because a cell that
    stretches to 460px wants 920 device pixels, which `thumb` has not got and
    `preview` has four times too many of."""
    allowed(user, folder, name)
    return serve(webroots.LARGE_DIR, folder, name, user)


@app.get("/preview/{folder}/{name}")
def preview(folder: str, name: str,
            user: Annotated[Principal, Depends(require_user)]) -> FileResponse:
    allowed(user, folder, name)
    return serve(webroots.PREVIEW_DIR, folder, name, user)


def allowed(user: Principal, folder: str, name: str) -> None:
    """Refuse a file this person has not been shared.

    Checked on **every** byte-serving route, not just on the listings. A grid
    that omits a photograph while `/preview/...` still returns it is not
    access control; it is a tidier index. Anyone can type a URL.

    **`unfold` is what makes this the access question rather than the listing
    question.** A bare `Filters` also hides whatever is stacked behind another
    file — which is a rule about what a *grid* shows, not about who may see
    what. Without it every file inside a stack answered 404 to a household
    member: they could open a stack and get a wall of broken thumbnails, while
    an administrator, having no scope, never came through here to find out.

    The deleted stay hidden, and that is the access question: they are in
    nobody's view but an administrator's.

    An admin has no scope and pays nothing for this.
    """
    if user.scope is None:
        return
    conn = db()
    try:
        if not ix.matching(conn, ix.Filters(viewer=user.scope, unfold=True),
                           [(folder, name)]):
            # The same answer as a file that does not exist. Distinguishing
            # them would confirm that a photograph is there to be asked for.
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    finally:
        conn.close()


@app.get("/media/{folder}/{name}")
def media(folder: str, name: str,
          user: Annotated[Principal, Depends(require_user)]) -> FileResponse:
    """Stream the master file itself, for video playback.

    The **only** endpoint that touches master, and strictly read-only — the
    archive is served, never modified.

    Serves the **render** when one exists and master otherwise. For an HEVC
    master the render is the only copy a browser can play — 421 of the seeded
    year's 724 clips — and where both exist they are the same footage.
    """
    allowed(user, folder, name)
    # A clip plays as its source, clamped by the page to its range
    # (spec/clips.md §7). Only a curator gets here: `_allowed` has already
    # refused a viewer, who would otherwise be handed all of the source.
    source = clips.source_of(name)
    if source is not None and not (webroots.MASTER_DIR / folder / name).is_file():
        own = clip_file(folder, name, playable=True)
        if own is not None:
            return FileResponse(own, media_type="video/mp4",
                                headers={"Cache-Control": "private, max-age=3600",
                                         "Accept-Ranges": "bytes"})
        name = source
    # Prefer the render: for an HEVC master it is the only playable copy, and
    # where both exist they are the same footage.
    rendered = (webroots.RENDER_DIR / folder / (name + ".mp4")).resolve()
    target = (webroots.MASTER_DIR / folder / name).resolve()
    if (webroots.MASTER_DIR.resolve() not in target.parents
            or webroots.RENDER_DIR.resolve() not in rendered.parents):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad path")
    if rendered.is_file():
        target = rendered
    elif not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return FileResponse(target, headers={"Cache-Control": "private, max-age=3600",
                                         "Accept-Ranges": "bytes"})


def serve(root: Path, folder: str, name: str,
           user: Principal | None = None) -> FileResponse:
    """Serve a derived image, refusing anything that escapes its tier.

    The path components come from a URL, so they are untrusted: `..` in either
    would otherwise read arbitrary files off the share.

    **A stand-in picture only for someone who may see what it is of.** A clip
    has no pictures of its own until `process` makes them, and a clip made
    into a file of its own none until `process` probes it — so each shows its
    source's meanwhile. That source is footage the clip was cut *out of*, and
    sharing a clip is not sharing it: to anybody who may not see the source,
    a picture of it is a frame they were never given. They get nothing until
    the clip's own pictures exist.
    """
    target = (root / folder / (name + ".jpg")).resolve()
    if root.resolve() not in target.parents:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad path")
    stand_in: str | None = None
    if not target.is_file():
        stand_in = clips.source_of(name)
        if stand_in is None:
            record = ix.record_of(webroots.META_DIR, folder, name) or {}
            raw = record.get("stand_in")
            stand_in = raw if isinstance(raw, str) and raw else None
    if stand_in is not None:
        if not may_see(user, folder, stand_in):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not derived yet")
        target = (root / folder / (stand_in + ".jpg")).resolve()
        if root.resolve() not in target.parents:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad path")
    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not derived yet")
    # A stand-in is not kept: the clip's own picture replaces it, and a day
    # of cache would go on showing the source's after it had.
    cache = "no-store" if stand_in is not None else "public, max-age=86400"
    return FileResponse(target, media_type="image/jpeg",
                        headers={"Cache-Control": cache})


def may_see(user: Principal | None, folder: str, name: str) -> bool:
    """Whether this person may see one file — an administrator may, and a
    caller with nobody in particular in mind is one."""
    if user is None or user.scope is None:
        return True
    conn = db()
    try:
        return ix.sees(conn, user.scope, folder, name)
    finally:
        conn.close()


def to_send(folder: str, name: str, original: bool) -> tuple[Path, str]:
    """The file to hand over and what to call it.

    Two things can be meant by *the file*. The **original** is what came off
    the camera and is what master holds; the playable copy is the H.264
    rendition the app made of it, which exists only where the original is
    something a browser will not play. For everything else — every photograph
    here, and the third of the clips that were already H.264 — they are the
    same file, and offering a choice between them would be offering a choice
    that is not there.
    """
    media = master_file(folder, name)
    if not media.is_file():
        # A clip: *original* is its lossless cut, and the playable copy is its
        # render where it has one (spec/clips.md §7). Never its source —
        # downloading a clip and receiving the whole video is the wrong file.
        own = clip_file(folder, name, playable=not original)
        if own is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "this clip has not been cut yet")
        return own, f"{name}{own.suffix}"
    if not original:
        render = paths.render_path(media, webroots.RENDER_DIR)
        if render.is_file():
            return render, Path(name).with_suffix(render.suffix).name
    return media, name


@app.get("/download/{folder}/{name}")
def download(folder: str, name: str,
             user: Annotated[Principal, Depends(require_user)],
             original: Annotated[str | None, Query()] = None) -> FileResponse:
    """One file, as a download rather than as something to look at.

    The same access check as every other byte-serving route: a grid that omits
    a photograph while this hands it over is not access control.

    **Named as what it is, and still an attachment.** `filename=` sets a
    `Content-Disposition: attachment`, which outranks the type in every browser
    — so this still downloads on a desktop exactly as it did when it claimed
    everything was octet-stream. What the honest type buys is the phone: see
    `_mime`.
    """
    allowed(user, folder, name)
    target, called = to_send(folder, name, bool(original))
    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such file")
    return FileResponse(target, filename=called, media_type=mime(called))


@app.post("/download.zip")
async def download_zip(
    request: Request,
    user: Annotated[Principal, Depends(require_user)],
) -> StreamingResponse:
    """A selection, as one zip.

    **A form post rather than a fetch**, because the browser has to own this:
    a fetch would hold every byte in memory before the file appeared, and a
    selection of video runs to gigabytes. Posted rather than linked because
    five hundred names do not fit in an address.

    **Stored, not deflated.** Every file in here is already compressed — JPEG
    or H.264 — so deflating spends the processor to save nothing, on the one
    path where throughput is the whole experience.
    """
    form = await read_form(request)
    original = bool(form.get("original"))
    try:
        raw: object = json.loads(form.get("files", "[]"))
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad file list") from None
    wanted = cast("list[dict[str, str]]", raw) if isinstance(raw, list) else []
    if not wanted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "nothing chosen")
    if len(wanted) > ZIP_LIMIT:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{len(wanted):,} files in one download — "
            f"take at most {ZIP_LIMIT:,} at a time")

    picked: list[tuple[str, Path]] = []
    for item in wanted:
        folder, name = str(item.get("folder", "")), str(item.get("name", ""))
        allowed(user, folder, name)
        target, called = to_send(folder, name, original)
        if target.is_file():
            # Foldered inside the zip, because two master folders can hold the
            # same name and a flat zip would quietly keep one of them.
            picked.append((f"{folder}/{called}", target))
    if not picked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "nothing to send")

    stamp = time.strftime("%Y-%m-%d")
    return StreamingResponse(
        zipped(picked), media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="pix-{stamp}.zip"'})


class Sink:
    """A file object for `zipfile` that hands back what it is given.

    `zipfile` writes; the generator below drains. Nothing is held but the
    chunk in flight, which is what lets a thirty-gigabyte selection through a
    process that must also still be serving pages.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._at = 0

    def write(self, data: bytes) -> int:
        self._buf += data
        self._at += len(data)
        return len(data)

    def tell(self) -> int:
        return self._at

    def flush(self) -> None:
        return None

    def drain(self) -> bytes:
        out = bytes(self._buf)
        del self._buf[:]
        return out


def zipped(picked: Sequence[tuple[str, Path]]) -> Iterator[bytes]:
    """The zip, a chunk at a time."""
    sink = Sink()
    # `zipfile` wants a file; `_Sink` is one in every way it uses — write,
    # tell, flush — and in none of the ways the type says.
    with zipfile.ZipFile(cast("Any", sink), "w", zipfile.ZIP_STORED) as zf:
        for arcname, path in picked:
            try:
                with zf.open(arcname, "w") as into, path.open("rb") as src:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        into.write(chunk)
                        got = sink.drain()
                        if got:
                            yield got
            except OSError:
                # One unreadable file does not cost the other four hundred.
                continue
            got = sink.drain()
            if got:
                yield got
    yield sink.drain()
