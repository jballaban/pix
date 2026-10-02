"""The JSON the page script talks to: listing and paging files, suggestions for
a menu, one file's details, a stack's members, and the writes — one file,
many, and the purge.
"""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from dataclasses import replace
from typing import Annotated, Any, cast, Sequence

from fastapi import Body, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from pix.nas import (
    decisions,
    destroy as destroy_mod,
    history,
    index as ix,
    webroots,
)
from pix.nas.decisions import Unset
from pix.nas.webapp.address import filters
from pix.nas.webapp.app import (
    app,
    db,
    Principal,
    require_admin,
    require_user,
    write_lock,
)
from pix.nas.webapp.grid import cell, sections, totals
from pix.nas.webapp.media_routes import allowed, may_see
from pix.nas.webapp.text import q, split, under
from pix.nas.webapp.vocab import (
    BULK_LIMIT,
    FIXED,
    groupings,
    NEXT_PAGE,
    PAGE_LIMIT,
)
from pix.nas.webapp.writes import (
    behind,
    cascade,
    change_from,
    clip_rules,
    decide,
    DecideBody,
    DecideBulkBody,
    did as _did,
    master_file,
    one_kind,
    only_mine,
    promote,
    recorded,
    records_for,
    summary,
    Target,
    writable,
)


@app.get("/api/files")
def api_files(user: Annotated[Principal, Depends(require_user)],
              view: Annotated[ix.Filters, Depends(filters)],
              limit: Annotated[int, Query(le=2000)] = 500,
              offset: Annotated[int, Query(ge=0)] = 0) -> JSONResponse:
    rows = ix.files(db(), view, limit=limit, offset=offset)
    return JSONResponse([dict(r) for r in rows])


@app.get("/api/page")
def api_page(user: Annotated[Principal, Depends(require_user)],
             view: Annotated[ix.Filters, Depends(filters)],
             group: Annotated[str, Query()] = "day",
             offset: Annotated[int, Query(ge=0)] = 0,
             limit: Annotated[int, Query(ge=1, le=2000)] = NEXT_PAGE
             ) -> JSONResponse:
    """The grid's next page, as the sections it is drawn in.

    The same rows `browse` would have drawn there, in the same order and the
    same markup, so a page that arrives later is indistinguishable from one
    that came with the grid. Its first section may carry on the one already at
    the bottom of the screen; the page script joins them by `data-key`.

    `offset` is how many rows the page has been given and still holds — the
    script counts down the ones a write took out of the view, or the next
    page would start that many rows late and they would never be seen.
    """
    del user
    conn = db()
    groups = groupings(group)
    rows = ix.files(conn, view, groups=groups, limit=limit, offset=offset)
    total = ix.count(conn, view)
    return JSONResponse({
        "html": sections(rows, groups, view, totals(conn, view, groups),
                          total) if rows else "",
        "served": offset + len(rows),
        "total": total,
    })


@app.get("/api/suggest")
def api_suggest(user: Annotated[Principal, Depends(require_user)],
                view: Annotated[ix.Filters, Depends(filters)],
                column: Annotated[str, Query()],
                near_from: Annotated[str | None, Query()] = None,
                near_to: Annotated[str | None, Query()] = None) -> JSONResponse:
    """Existing values for a column, most relevant to the current view first.

    A library ends up with hundreds of events and tags, and an alphabetical
    list of all of them buries the handful that apply to what is on screen.
    Ranking by how much of the current view already uses a value puts the
    likely answer in the first few rows — see `index.suggest`.

    `near_from`/`near_to` are the dates the *selection* spans, which the page
    knows and the server does not. They add the strongest band of all: events
    already covering those days. Both or neither — half a range is not a range,
    and guessing the missing end would propose events on evidence nobody gave.
    """
    if column not in ("event", "tag", "person", "audience", "date", "kind",
                      "band", "camera", "source", "stacks", "deleted"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"cannot suggest values for {column!r}")
    conn = db()
    if column in FIXED:
        return JSONResponse(fixed_counts(conn, column, view))
    near = (near_from, near_to) if near_from and near_to else None
    out = [{"value": s.value, "n": s.n, "scope": s.scope}
           for s in ix.suggest(conn, column, view, near=near)]
    if column == "audience":
        # The two that are states rather than names, which no row of
        # `file_audience` carries — so they are counted, not listed.
        have = {o["value"] for o in out}
        for value in (ix.UNREVIEWED, decisions.ARCHIVED):
            if value not in have:
                out.extend(o for o in fixed_counts(conn, column, view,
                                                    (value,)) if o["n"])
    return JSONResponse(out)


def fixed_counts(conn: sqlite3.Connection, column: str, view: ix.Filters,
                  values: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """How many files carry each value of a filter whose values are a fixed
    list — across the library, and whether any are in the view as it stands.

    A filter offers only what is there. Type offered *Other* to a library
    with none, and picking it was an empty grid; the list is fixed, but what
    is worth offering from it is not. Counted the way the grid counts, so
    `_always` and the viewer's scope hold here too.
    """
    out: list[dict[str, Any]] = []
    base = ix.Filters(viewer=view.viewer, apart=view.apart)
    for value in values or [v for v, _ in FIXED[column]]:
        if column == "deleted":
            change: dict[str, Any] = {
                "deleted": "only" if value == "gone" else None}
        else:
            change = {column: value}
        n = ix.count(conn, replace(base, **change))
        if not n:
            out.append({"value": value, "n": 0, "scope": "other"})
            continue
        here = ix.count(conn, replace(view, **change))
        out.append({"value": value, "n": n, "scope": "all" if here else "other"})
    return out


#: The readings worth surfacing, in the order a person asks for them. The full
#: set runs to ~176 keys per file and is available underneath; this is the part
#: that answers "what is this photograph".
FACTS: tuple[tuple[str, str], ...] = (
    ("Camera", "Model"), ("Make", "Make"), ("Lens", "LensID"),
    ("Exposure", "ExposureTime"), ("Aperture", "FNumber"), ("ISO", "ISO"),
    ("Focal length", "FocalLength"), ("Flash", "Flash"),
    ("Codec", "CompressorID"), ("Frame rate", "VideoFrameRate"),
    ("Location", "GPSPosition"), ("Software", "Software"),
    ("Original path", "OriginalPath"),
)


#: Where a value came from before anyone decided anything. These are the legacy
#: `pix:*` tags embedded in seeded files — the inherited half of the read-through
#: in §4, which is what lets the rail show *inherited* apart from *decided*.
INHERITED: tuple[tuple[str, str], ...] = (
    ("event", "EventOverride"), ("event_auto", "EventAuto"),
    ("date_override", "DateOverride"),
)


@app.get("/api/file/{folder}/{name}")
def api_file(folder: str, name: str,
             user: Annotated[Principal, Depends(require_user)],
             view: Annotated[ix.Filters, Depends(filters)]) -> JSONResponse:
    """Everything known about one file, with fact and judgement kept apart.

    The rail's whole job is that separation. `capture_date` is what the camera
    wrote and can never change; `date_override` is what a person decided;
    `effective_date` is the composition of the two. Collapsing them into one
    "date" would hide the only interesting question — whether this is what the
    file says or what somebody chose.
    """
    allowed(user, folder, name)
    row = ix.one(db(), folder, name)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not indexed")

    media = webroots.MASTER_DIR / folder / name
    decision = decisions.read(media) if under(webroots.MASTER_DIR, media) else None
    record = ix.record_for(folder, name, meta_dir=webroots.META_DIR) or {}
    raw: object = record.get("exif")
    exif: dict[str, Any] = (
        cast("dict[str, Any]", raw) if isinstance(raw, dict) else {})

    facts = [{"label": label, "value": ix.tag(exif, key)}
             for label, key in FACTS if ix.tag(exif, key)]
    inherited = {field: ix.tag(exif, key) for field, key in INHERITED}

    return JSONResponse({
        "folder": folder,
        "name": name,
        "size": row["size"],
        "kind": row["kind"],
        "width": row["width"],
        "height": row["height"],
        "duration": row["duration"],
        "band": row["band"],
        "camera": row["camera"],
        # Fact, judgement, and the composition of the two — kept apart.
        "capture_date": row["capture_date"],
        "date_override": row["date_override"],
        "effective_date": row["effective_date"],
        "year": row["year"],
        "event": row["event"],
        "tags": split(row["tags"]),
        "people": split(row["people"]),
        "audience": split(row["audience"]),
        "has_sidecar": bool(row["has_sidecar"]),
        "decided": ({"event": decision.event,
                     "date_override": decision.date_override,
                     "tags": list(decision.tags),
                     "audience": list(decision.audience)}
                    if decision else None),
        "inherited": inherited,
        "facts": facts,
        "exif": {k: str(v) for k, v in sorted(exif.items())},
        "has_render": (webroots.RENDER_DIR / folder / (name + ".mp4")).is_file(),
        "clip": clip_from(user, row, view),
        "clips": clips_list(user, row, view),
    })


def clips_list(user: Principal, row: sqlite3.Row,
                view: ix.Filters) -> list[dict[str, Any]]:
    """The clips and stills cut from this video that this person may see,
    each with the way to its preview. Only on the video itself — a clip's
    details name its source, and the source lists the rest."""
    folder = str(row["folder"])
    # A clip's details point to its source and no further: the siblings are
    # listed on the source, which is where anybody going looking for them
    # goes, and a list on every clip would be the same list many times over.
    if ("clip_of" in row.keys() and row["clip_of"] is not None)             or row["kind"] != "video":
        return []
    source = str(row["name"])
    conn = db()
    try:
        cut = [c for c in ix.clips_of(conn, folder, source)
               if not c["deleted"] and c["name"] != row["name"]]
        keys = [(folder, str(c["name"])) for c in cut]
        seen = (set(keys) if user.scope is None else
                ix.matching(conn, ix.Filters(viewer=user.scope, unfold=True),
                            keys))
        here = ix.matching(conn, replace(view, within=None, chosen=None,
                                         unfold=True), keys)
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for c in cut:
        key = (folder, str(c["name"]))
        if key not in seen:
            continue
        day = str(c["effective_date"] or "")[:10]
        out.append({
            "name": c["name"], "key": f"{folder}/{c['name']}",
            "start": c["clip_in"], "end": c["clip_out"],
            "in_view": key in here,
            "open": ("/browse" + (f"?date={q(day)}" if len(day) == 10 else "")
                     + "#open:" + q(f"{folder}/{c['name']}")),
        })
    return out


def clip_from(user: Principal, row: sqlite3.Row,
               view: ix.Filters | None = None) -> dict[str, Any] | None:
    """Where a clip was cut from, for someone who may see that — and nothing
    at all for anyone else, to whom a clip is simply a video.

    `in_view` says whether the source is in the view the clip was opened
    from, so the page can go to it without leaving that view — the filters
    somebody built up are not the link's to throw away. Only when the source
    is not in it does `open` fall back to the source's own day.
    """
    source = row["clip_of"] if "clip_of" in row.keys() else None
    if source is None:
        return None
    folder = str(row["folder"])
    if not may_see(user, folder, str(source)):
        return None
    conn = db()
    try:
        parent = ix.one(conn, folder, str(source))
        in_view = view is not None and bool(ix.matching(
            conn, replace(view, within=None, chosen=None, unfold=True),
            [(folder, str(source))]))
    finally:
        conn.close()
    day = str(parent["effective_date"] or "")[:10] if parent else ""
    query = [f"date={q(day)}"] if len(day) == 10 else []
    # A hidden source is out of the administrator's own grid too, so the way
    # to it asks for hidden files by name.
    if parent is not None and decisions.ARCHIVED in split(parent["audience"]):
        query.append(f"audience={q(decisions.ARCHIVED)}")
    return {
        "source": source,
        "key": f"{folder}/{source}",
        "start": row["clip_in"], "end": row["clip_out"],
        "in_view": in_view,
        # `#open:` lands on the file with its preview open, where a bare
        # `#` only scrolls to it.
        "open": ("/browse" + ("?" + "&".join(query) if query else "")
                 + "#open:" + q(f"{folder}/{source}")),
        "splice": (f"/splice/{q(folder)}/{q(str(source))}#{q(str(row['name']))}"
                   if user.is_admin else None),
    }


@app.get("/api/behind/{folder}/{name}")
def api_behind(folder: str, name: str,
               user: Annotated[Principal, Depends(require_user)],
               view: Annotated[ix.Filters, Depends(filters)]) -> JSONResponse:
    """The cells for the files stacked behind one — rendered here, not there.

    Merging two stacks has to offer every photograph in both of them as the one
    to show, and the members are not on the page: that is what stacking them
    did. So they are fetched, and they come back as the same markup the grid is
    already made of. A second copy of a cell written in JavaScript would drift
    from this one, and the first thing to go would be whichever fact was added
    last.
    """
    conn = db()
    key = f"{folder}/{name}"
    rows = [r for r in ix.files(conn, replace(view, within=key, chosen=None),
                                limit=PAGE_LIMIT)
            if key in (r["stacked_under"], r["suggested_under"])]
    return JSONResponse(
        {"cells": "".join(cell(r, replace(view, within=key)) for r in rows)})


@app.get("/api/events")
def api_events(user: Annotated[Principal, Depends(require_user)],
               view: Annotated[ix.Filters, Depends(filters)]) -> JSONResponse:
    return JSONResponse([dict(r) for r in ix.events(db(), view)])


@app.post("/api/decide")
def api_decide(user: Annotated[Principal, Depends(require_user)],
               body: Annotated[DecideBody, Body()]) -> JSONResponse:
    """Write a decision to master, then bring its index row up to date.

    **Sidecar first, index follows** (§4). If the sidecar write fails nothing
    happened; if the index update fails the decision still stands and a
    `pix2 index` catches up — drift is only ever "the index is behind", never
    "the record is wrong". `indexed` in the response says which happened.
    """
    # Both halves, and in this order: what this person may write at all, then
    # whether this file is theirs to write it to. Neither was here while the
    # route was an administrator's — an admin has no scope and every field —
    # and opening it without both would let anybody edit any file by typing
    # its name.
    change = change_from(body)
    writable(user, change)
    allowed(user, body.folder, body.name)

    # The same cascade the page's own route does. There is no view here to say
    # whether a stack is open — this is the scripting surface, and the page
    # writes through `/api/decide/bulk` — so it asks what this person's
    # ordinary view would fold, which is the only reading available.
    conn = ix.open_rw(webroots.DB_PATH) if webroots.DB_PATH.is_file() else None
    try:
        view = ix.Filters()
        named = Target(folder=body.folder, name=body.name)
        targets = only_mine(user, conn, behind(
            conn, view, [named],
            members=isinstance(change.stacked_under, Unset)))
        one_kind(conn, change, targets)
        targets = clip_rules(conn, change, targets)
        was, decision, indexed = decide(body.folder, body.name, change,
                                         conn=conn)
        undo = [history.Before(body.folder, body.name, was,
                               did=_did(change, recorded(change), decision))]
        for target in targets:
            if (target.folder, target.name) == (body.folder, body.name):
                continue
            try:
                before, after, _ = decide(target.folder, target.name, change,
                                           conn=conn)
            except HTTPException:
                # One unwritable take does not cost the decision about the
                # rest, the same way a bulk edit reports a failure and carries
                # on.
                continue
            undo.append(history.Before(target.folder, target.name, before,
                                       did=_did(change, recorded(change),
                                                after)))
    finally:
        if conn is not None:
            conn.close()
    history.record(user.name, summary(change), undo)
    return JSONResponse({
        "folder": body.folder,
        "name": body.name,
        "event": decision.event,
        "date_override": decision.date_override,
        "tags": list(decision.tags),
        "people": list(decision.people),
        "audience": list(decision.audience),
        "has_sidecar": not decision.is_empty(),
        "indexed": indexed,
    })


class PurgeBody(BaseModel):
    """Which files to destroy. Deliberately not a shape `decide` could take:
    purging is not a decision, it is the end of one."""

    files: list[Target]
    batch: str | None = None


@app.post("/api/purge")
def api_purge(user: Annotated[Principal, Depends(require_admin)],
              view: Annotated[ix.Filters, Depends(filters)],
              body: Annotated[PurgeBody, Body()]) -> JSONResponse:
    """Destroy files outright. There is no undo for this one.

    **Every file must already be soft-deleted**, and that is checked here per
    file rather than trusted from the page. The page only offers Purge while
    the deleted filter is on, but this endpoint is reachable without it, and
    this check is the only thing standing between a URL and an original nobody
    ever said should go. A file that is not deleted is reported as failed, not
    skipped quietly — asking to purge a living file is a mistake worth hearing
    about.
    """
    if not body.files:
        return JSONResponse({"purged": 0, "failed": [], "dropped": [],
                             "total": None, "binned": None})
    if len(body.files) > BULK_LIMIT:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{len(body.files)} files in one request — send at most {BULK_LIMIT}")

    purged = 0
    failed: list[dict[str, str]] = []
    gone: list[dict[str, str]] = []
    conn = ix.open_rw(webroots.DB_PATH) if webroots.DB_PATH.is_file() else None
    try:
        for target in body.files:
            try:
                media = master_file(target.folder, target.name)
            except HTTPException as e:
                failed.append({"folder": target.folder, "name": target.name,
                               "error": str(e.detail)})
                continue
            current = decisions.read(media)
            if current is None or not current.deleted:
                failed.append({"folder": target.folder, "name": target.name,
                               "error": "not deleted — delete it first"})
                continue
            # A source's clips go with it: they are its bytes plus a range,
            # and every one of them is binned already — a source cannot be
            # binned while any lives.
            cut = ([str(c["name"]) for c in
                    ix.clips_of(conn, target.folder, target.name)]
                   if conn is not None else [])
            with write_lock:
                removed = destroy_mod.destroy(media, conn=conn,
                                              folder=target.folder,
                                              name=target.name)
                for clip in cut:
                    destroy_mod.destroy(media.parent / clip, conn=conn,
                                        folder=target.folder, name=clip)
            if removed.nothing():
                failed.append({"folder": target.folder, "name": target.name,
                               "error": "nothing could be removed"})
                continue
            purged += 1
            gone.append({"folder": target.folder, "name": target.name})
        total = ix.count(conn, view) if conn is not None else None
        binned = (ix.count(conn, ix.Filters(deleted="only"))
                  if conn is not None else None)
    finally:
        if conn is not None:
            conn.close()

    if purged:
        # No `Before` rows, so History shows it as something that happened and
        # offers no revert. A revert that silently did nothing is worse.
        # No `Before` rows — there is nothing to put back — but it still did
        # something to a number of files, so it carries its own count.
        history.record(user.name, "purged {n}", [], count=purged,
                       batch=body.batch)
    return JSONResponse({"purged": purged, "failed": failed,
                         "dropped": gone, "total": total, "binned": binned})


@app.post("/api/decide/bulk")
def api_decide_bulk(user: Annotated[Principal, Depends(require_user)],
                    view: Annotated[ix.Filters, Depends(filters)],
                    body: Annotated[DecideBulkBody, Body()]) -> JSONResponse:
    """Apply one decision to many files, reporting per-file failures.

    Partial success is the normal outcome to design for, not an error case: a
    few hundred sidecar writes over SMB will occasionally lose one, and the
    right answer is to say which rather than to fail the batch and leave the
    curator unsure what landed. Every write is independent — there is no
    transaction to roll back, because per-file sidecars are the whole point.

    The current filters ride along as query parameters, and the response says
    which of the written files **no longer match** them. Dating a file while
    filtered to undated should make it leave the grid, and the browser cannot
    decide that for itself: a partial override merges with the capture date
    server-side, so only the index knows the resulting year.
    """
    if not body.files:
        return JSONResponse({"written": 0, "indexed": 0, "failed": [],
                             "dropped": [], "total": None, "binned": None})
    if len(body.files) > BULK_LIMIT:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{len(body.files)} files in one request — send at most {BULK_LIMIT}")

    change = change_from(body)
    writable(user, change)
    did = recorded(change)
    written = 0
    indexed = 0
    failed: list[dict[str, str]] = []
    binned: int | None = None
    # One index connection for the whole batch. Opening a SQLite file over SMB
    # per row dominated the cost — measured at 96ms/file against the NAS, most
    # of it the open rather than the write.
    conn = ix.open_rw(webroots.DB_PATH) if webroots.DB_PATH.is_file() else None
    # **After the expansion, not before it.** `_behind` adds files the request
    # never named — the rest of a stack — so checking what was sent would let
    # a household member reach the others through it.
    targets = only_mine(user, conn, behind(
        conn, view, body.files,
        members=isinstance(change.stacked_under, Unset)))
    one_kind(conn, change, targets)
    targets = clip_rules(conn, change, targets)
    done: list[tuple[str, str]] = []
    undo: list[history.Before] = []
    dropped: list[dict[str, str]] = []
    total: int | None = None
    binned: int | None = None
    # Every row was its own commit, and a commit is an fsync to an index that
    # lives on the share — 8.7ms a row against 0.2ms when a batch shares one,
    # measured against the NAS. On 866 files that alone was most of the wait.
    #
    # The sidecars are written outside it and stay written whatever happens
    # here. If this transaction never commits the index is *behind*, which is
    # the one direction drift is allowed to go and what `pix2 index` is for —
    # the opposite bargain, a committed row for a sidecar that failed, is the
    # one the whole design refuses.
    rows = conn if conn is not None else nullcontext()
    # The meta records the refreshes are about to want, fetched together.
    # Each is 5KB of JSON and an 11ms round trip, and they are read-only and
    # independent — so the wait is latency, and latency is what overlapping
    # them removes.
    records = records_for(targets)
    try:
        with rows:
            for target in targets:
                try:
                    was, now, was_indexed = decide(
                        target.folder, target.name, change, conn=conn,
                        record=records.get((target.folder, target.name)),
                        commit=False)
                except HTTPException as e:
                    failed.append({"folder": target.folder, "name": target.name,
                                   "error": str(e.detail)})
                    continue
                written += 1
                indexed += 1 if was_indexed else 0
                done.append((target.folder, target.name))
                undo.append(history.Before(target.folder, target.name, was,
                                           did=_did(change, did, now)))
            # The file everything is being stacked onto stops being stacked
            # itself. Promoting one photograph out of a stack is exactly this —
            # the others come to defer to it, and it has to stop deferring to the
            # one it is replacing, or the stack is a ring nothing can show.
            if (conn is not None and done
                    and not isinstance(change.stacked_under, Unset)
                    and change.stacked_under):
                promoted = promote(conn, change.stacked_under)
                undo.extend(promoted)
                done.extend((b.folder, b.name) for b in promoted)
            # Before asking what left the view, because bringing a stack's
            # members up changes the answer for them too.
            if (conn is not None and done
                    and not isinstance(change.stacked_under, Unset)):
                brought = cascade(conn, done, change.stacked_under)
                undo.extend(brought)
                done.extend((b.folder, b.name) for b in brought)
            if conn is not None and done:
                stays = ix.matching(conn, view, done)
                dropped = [{"folder": f, "name": n}
                           for f, n in done if (f, n) not in stays]
                total = ix.count(conn, view)
                # The header's standing count. It is rendered with the page, so
                # without this it stays at whatever it said when the page loaded —
                # which is wrong the instant anything is deleted or restored.
                binned = ix.count(conn, ix.Filters(deleted="only"))
    finally:
        if conn is not None:
            conn.close()

    # One log line per request, holding what each file said before. The
    # previous values in full rather than a diff: a diff has to be read
    # against whatever the file says *now*, and now may already have moved —
    # which is the whole reason somebody is reverting.
    if undo:
        history.record(user.name, summary(change), undo, batch=body.batch)
    return JSONResponse({"written": written, "indexed": indexed,
                         "failed": failed, "dropped": dropped,
                         "total": total, "binned": binned})
