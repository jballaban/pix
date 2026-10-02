"""History: the operations log as a page, and the revert that puts an
operation's files back.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from pix.nas import clips, decisions, history, index as ix, webroots
from pix.nas.webapp.app import app, Principal, require_admin, write_lock
from pix.nas.webapp.clipping import recut
from pix.nas.webapp.session import read_form
from pix.nas.webapp.shell import page
from pix.nas.webapp.text import h, q, under, when
from pix.nas.webapp.writes import is_clip_path


def byline_of(curators: tuple[str, ...], who: str) -> str:
    """Whose work to look at, as a row of names.

    **Links, and the name is in the URL.** Every other filter in this app
    lives there, which is what makes a view something you can send to
    somebody, bookmark, and back out of; and /history carries no page script
    at all, so a control that needed one would be a filter that worked
    everywhere except the page it is on.

    Nothing at all where one person has done everything. A control offering a
    single choice is not a choice — it is the answer, written twice.
    """
    if len(curators) < 2:
        return ""
    def one(name: str, label: str) -> str:
        here = ' aria-current="page"' if name == who else ""
        where = f"/history?who={q(name)}" if name else "/history"
        return f'<a href="{where}"{here}>{h(label)}</a>'
    return ('<p class="byline"><span class="dim">Changed by</span>'
            + one("", "Everyone")
            + "".join(one(name, name) for name in curators) + "</p>")


@app.get("/history", response_class=HTMLResponse)
def history_page(user: Annotated[Principal, Depends(require_admin)],
                 msg: Annotated[str, Query()] = "",
                 look: Annotated[str, Query()] = "",
                 who: Annotated[str, Query()] = "") -> HTMLResponse:
    """What has been changed, newest first, each with a way back.

    A bulk edit can touch several hundred files from one click, and *I just
    gave the children access to three hundred photographs* has no other cure:
    the decisions are individually correct in three hundred sidecars, and
    nothing else remembers they used to say something else.

    **`who` narrows it to one person's work.** Several people curate here —
    that is the reason this is a list of named operations rather than an undo
    stack — and *what did I do this afternoon* is the question a safety net is
    reached for with. Two hundred rows of everybody's work is where the answer
    is, not what it is.

    A name that nobody in the log has done anything under is not an error: the
    filter comes out of a URL, which people type, edit and keep. It shows an
    empty list and the way back to everyone, which is what it is.
    """
    log = history.read(who=who or None)
    ops = list(log.ops)

    # Filtered to one person, the Who column is the same name two hundred
    # times, and the row of names above already says which. A column true of
    # everything on screen is furniture, the same rule the thumbnails follow
    # about the audience they all share.
    said = bool(who)
    rows = "".join(
        f'<tr><td class="dim">{h(when(op.when))}</td>'
        + (f'<td><a href="/browse?op={q(op.id)}" '
           f'title="Look at these files">{h(op.summary)}</a></td>'
           if op.files else
           # A purge names no files: they are gone, the index rows with them.
           # Offering a link to them would lead to an empty grid that reads as
           # broken rather than as *there is nothing left to look at*.
           f'<td>{h(op.summary)}</td>')
        + ("" if said else
           # The name is the way to ask for only their work: the gesture is on
           # the thing it is about, rather than only on a control above the
           # table that has to be found first.
           f'<td class="dim"><a href="/history?who={q(op.who)}" '
           f'title="Only what {h(op.who)} changed">{h(op.who)}</a></td>')
        + ('<td class="dim">undone</td>' if op.id in log.undone else
           '<td class="dim">a revert</td>' if op.reverts else
           f'<td><form method="post" action="/history/revert">'
           f'<input type="hidden" name="id" value="{h(op.id)}">'
           # So a revert comes back to the list you were reading rather than
           # to everybody's.
           + (f'<input type="hidden" name="who" value="{h(who)}">'
              if who else "")
           + f'<button>Revert</button></form></td>')
        + "</tr>"
        for op in ops
    )
    # A revert that left files alone says how many. The number is the work
    # still to look at, so it comes with the way to go and look at it.
    seeing = (f' <a href="/browse?op={q(look)}&amp;stale=1">'
              f'see the ones it left &rarr;</a>' if look else "")
    note = f'<p class="note">{h(msg)}{seeing}</p>' if msg else ""
    byline = byline_of(log.curators, who)
    if not ops:
        empty = (f'Nothing by {h(who)}.' if who else "Nothing changed yet.")
        return page("History", f'{note}{byline}<p class="empty">{empty}</p>',
                     user=user)
    return page("History", f"""{note}{byline}
<p class="dim">Reverting puts those files back to exactly what they said
before — not an undo stack, because several people curate here and the last
thing done is not always yours. A revert is itself recorded, so it can be
reverted in turn.</p>
<table class="acct"><thead><tr><th>When</th><th>What</th>
{"" if said else "<th>Who</th>"}
<th></th></tr></thead><tbody>{rows}</tbody></table>""", user=user)


@app.post("/history/revert")
async def history_revert(
    request: Request,
    user: Annotated[Principal, Depends(require_admin)],
) -> Response:
    """Undo what one operation did, wherever that is still what the file says.

    **The inverse of the change, not the whole previous value.** Writing every
    recorded field back was wrong twice over: set a date on a file and then an
    event, revert the date, and the event went with it — it was never this
    operation's to undo. The objection that used to justify the wholesale
    write — that the inverse of *added family* is only *remove family* if
    nothing else touched the file since — is answered by checking that nothing
    else did, per file, rather than by undoing more than was asked.

    Files that have moved on are left alone and counted. At three hundred files
    some always will have, and refusing the whole operation for one of them
    would make revert useless exactly when it is needed most.
    """
    form = await read_form(request)
    op_id = form.get("id", "")
    # Whose work was being read. A revert that dropped the filter would answer
    # *and now here is everybody again*, which is not what pressing a button
    # in a narrowed list asks for.
    back = f"&who={q(form.get('who', ''))}" if form.get("who") else ""
    op = history.get(op_id)
    if op is None:
        return RedirectResponse(f"/history?msg=no+such+operation{back}",
                                status_code=303)

    if not op.revertable():
        return RedirectResponse(
            "/history?msg=" + quote("that was recorded before reverting knew "
                                    "what an operation had done, so it cannot "
                                    "be put back") + back,
            status_code=303)

    restored = 0
    failed = 0
    moved = 0
    undo: list[history.Before] = []
    conn = ix.open_rw(webroots.DB_PATH) if webroots.DB_PATH.is_file() else None
    try:
        for item in op.files:
            media = webroots.MASTER_DIR / item.folder / item.name
            # A clip has no file, and may have no sidecar either — reverting
            # the merge that removed it is how it comes back.
            if not under(webroots.MASTER_DIR, media) or not (
                    media.is_file() or is_clip_path(media, creating=True)):
                failed += 1
                continue
            with write_lock:
                try:
                    was = decisions.read(media)
                    if not history.in_effect(item.did, was):
                        moved += 1
                        continue
                    putting_back = history.undo(item.did, item.decision)
                    decisions.change(media, **putting_back)
                except (decisions.DecisionError, OSError):
                    failed += 1
                    continue
                # What the revert did to *this* file, so putting the revert
                # back is the same gesture again rather than a special case.
                undo.append(history.Before(item.folder, item.name, was,
                                           did=dict(putting_back)))
                if conn is not None:
                    try:
                        ix.refresh(conn, item.folder, item.name,
                                   meta_dir=webroots.META_DIR, master_dir=webroots.MASTER_DIR)
                    except sqlite3.Error:
                        pass
            if clips.source_of(item.name) is not None and (
                    "clip_in" in putting_back or "clip_out" in putting_back):
                recut(item.folder, item.name)
            restored += 1
    finally:
        if conn is not None:
            conn.close()

    if undo:
        # Recorded with what it did, like any other operation, so putting a
        # revert back is the same gesture again rather than a special case.
        history.record(user.name, f"reverted: {op.summary}", undo,
                       reverts=op.id)
    noun = "file" if restored == 1 else "files"
    said = f"{restored:,} {noun} put back"
    if moved:
        said += f", {moved:,} changed since and left alone"
    if failed:
        said += f", {failed:,} could not be"
    # A count is not much use on its own. The ones it left alone are the work
    # still to look at, so the message carries a way to go and look at them.
    tail = f"&look={q(op.id)}" if moved else ""
    return RedirectResponse(f"/history?msg={quote(said)}{tail}{back}",
                            status_code=303)
