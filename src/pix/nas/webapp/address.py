"""The view as the address states it: every filter read off the query string
into an index.Filters, with the parts that are an administrator's dropped
for anybody else.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import Depends, HTTPException, Query

from pix.nas import decisions, history, index as ix
from pix.nas.webapp.app import db, Principal, require_user
from pix.nas.webapp.text import split
from pix.nas.webapp.vocab import groupings, OLD_KINDS, pick


def stacks_asked(stacks: list[str] | None, user: Principal
                  ) -> tuple[ix.Pick, bool]:
    """Which stacks to show, and whether the address still says `firm`.

    The values became three checkboxes — `stacked`, `suggested`, `single` —
    when the filters took several values; the old words are read as the
    boxes they meant, so a link from before still opens the same view.
    `firm` was never a set of files: it is *suggestions shown apart*, which is
    `apart` now, and is handed back as that.

    Everybody's, as it was: a household member can accept and refuse a
    suggestion now, which is what took the ground out from under keeping
    them from looking at one.
    """
    del user
    old = {"only": ("stacked", "suggested"), "guesses": ("suggested",)}
    got: list[str] = []
    apart = False
    for v in stacks or []:
        if v == "firm":
            apart = True
        got.extend(old.get(v, (v,) if v in ("stacked", "suggested", "single")
                           else ()))
    return pick(got), apart


def both_sides(deleted: list[str] | None, op_id: str | None,
                user: Principal) -> str | None:
    """Which side of the deletion line to show — and *both*, following a link
    from the log.

    An operation is a set of files, and *deleted 300 files* is the one you most
    want to look at. Leaving the ordinary default in place answered that link
    with an empty grid, because every file it named had just been deleted.

    Only for an administrator, and that is the whole of the access story here:
    the parameter is dropped for everybody else, so a curator following the
    same link sees the living part of that set and no more. The operation
    filter narrows a view; nothing about it widens one. The `viewer` scope
    rides on the same query and is not negotiable either.
    """
    if not user.is_admin:
        return None
    # Two boxes: the binned, and the living. The address may still say the
    # old `only` and `with`, which are the same two answers.
    sides: set[str] = set()
    for v in deleted or []:
        sides |= {"only": {"gone"}, "with": {"gone", "live"},
                  "gone": {"gone"}, "live": {"live"}}.get(v, set())
    if sides == {"gone"}:
        return "only"
    if sides == {"gone", "live"}:
        return "with"
    return "with" if op_id and not sides else None


def from_operation(op_id: str | None,
                    stale: str | None) -> tuple[tuple[str, str], ...] | None:
    """The files one operation touched, or the ones it can no longer put back.

    *Show me what that did* is the question the log cannot answer on its own: it
    can say what happened, but looking at the photographs afterwards means
    getting them into the grid, where everything else already works. So the
    operation becomes a filter, and from there the curator has the whole tool.

    `stale` narrows it to the files a revert cannot put back — the ones edited
    since, which are no longer what the operation left them. Those are the
    interesting ones: a revert reports a number, and that number is the work
    still to look at.

    **Minus whatever a revert has already restored.** *No longer in effect*
    stops telling the two apart the moment one runs, because a file that was
    put back is not in effect either — that is what putting it back means. Ask
    after reverting and every file would look like one it had skipped.

    Worked out from the **index** rather than by reading the sidecars. The
    truth is in master, but three hundred reads over SMB to answer a link would
    take seconds, and the index is a projection of exactly the five fields a
    decision holds.
    """
    if not op_id:
        return None
    op = history.get(op_id)
    if op is None:
        return ()
    touched = tuple((f.folder, f.name) for f in op.files)
    if not stale or not touched:
        return touched

    try:
        conn = db()
    except HTTPException:
        return touched
    try:
        now = {(r["folder"], r["name"]): as_decision(r)
               for r in ix.files(conn, ix.Filters(chosen=touched,
                                                 deleted="with"),
                                 limit=len(touched))}
    except sqlite3.Error:
        return touched
    finally:
        conn.close()
    restored = history.put_back(op_id)
    return tuple(
        (f.folder, f.name) for f in op.files
        if (f.folder, f.name) not in restored
        and not history.in_effect(f.did, now.get((f.folder, f.name))))


def as_decision(row: sqlite3.Row) -> decisions.Decision:
    """An index row read back as the decision it is a projection of."""
    return decisions.Decision(
        event=row["event"] or None,
        date_override=row["date_override"] or None,
        tags=tuple(split(row["tags"])),
        audience=tuple(split(row["audience"])),
        deleted=bool(row["deleted"]))


def filters(
    user: Annotated[Principal, Depends(require_user)],
    event: Annotated[list[str] | None, Query()] = None,
    date: Annotated[list[str] | None, Query()] = None,
    tag: Annotated[list[str] | None, Query()] = None,
    person: Annotated[list[str] | None, Query()] = None,
    audience: Annotated[list[str] | None, Query()] = None,
    kind: Annotated[list[str] | None, Query()] = None,
    band: Annotated[list[str] | None, Query()] = None,
    camera: Annotated[list[str] | None, Query()] = None,
    source: Annotated[list[str] | None, Query()] = None,
    deleted: Annotated[list[str] | None, Query()] = None,
    op: Annotated[str | None, Query()] = None,
    stale: Annotated[str | None, Query()] = None,
    within: Annotated[str | None, Query()] = None,
    apart: Annotated[str | None, Query()] = None,
    cuts: Annotated[str | None, Query()] = None,
    stacks: Annotated[list[str] | None, Query()] = None,
    group: Annotated[str, Query()] = "day",
) -> ix.Filters:
    """The current view, read off the query string.

    In the URL rather than in the page's memory, so a view is a link: shareable,
    bookmarkable, and survivable across the reload that a bulk edit sometimes
    wants. It is also what makes the browser's back button mean "the filter I
    had before", which is the only undo a filter needs.

    `viewer` is **not** among them. It comes from the credentials and rides on
    every query, so a non-admin cannot widen their own view by editing the
    address bar — the one thing a URL-shaped filter model must not allow.

    `date` is a prefix — `2026`, `2026-08`, `2026-08-30` — and anything else is
    dropped rather than refused, the same way a grouping typo is. Its width
    reaches SQL as a `substr` length, so it has to be a number this code chose
    and never one a request did; `ix.date_prefix` is where that is decided.

    `group` is read here as well as by the page, for one reason: grouping by
    stack opens every stack in the view, and what a listing holds is decided in
    one place. Handing the grouping to the grid alone would have left the count
    in the header, the *did this leave the view* check and the grid itself with
    three different ideas of what was on screen.

    `stacks` is dropped for a non-admin for the same reason `deleted` is — it
    hides photographs behind a guess, and only somebody who can accept or
    refuse that guess should be able to turn it on.

    `deleted` is in the URL like any other filter, but it is **dropped for a
    non-admin** rather than merely hidden from their bar. Hiding the chip
    stops it being offered; this is what stops it being asked for. Anything
    but the two known words is dropped too, so a typo reads as the default
    rather than as some third thing.
    """
    picked, legacy_apart = stacks_asked(stacks, user)
    return ix.Filters(event=pick(event),
                      date=pick([d for d in (ix.date_prefix(v)
                                              for v in date or []) if d]),
                      tag=pick(tag), person=pick(person),
                      audience=pick(audience),
                      chosen=from_operation(op, stale),
                      within=within, cuts=cuts or None,
                      stacks=picked,
                      apart=apart == "1" or legacy_apart,
                      unfold="stack" in groupings(group),
                      kind=pick([k for v in kind or []
                                  for k in OLD_KINDS.get(v, (v,))]),
                      band=pick(band),
                      camera=pick(camera), source=pick(source),
                      viewer=user.scope,
                      deleted=both_sides(deleted, op, user))
