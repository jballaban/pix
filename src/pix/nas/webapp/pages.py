"""The pages: the landing page of folders, the grid, one stack opened, and the
page that says the index and the app are out of step. Each is a body handed
to the shell.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Annotated, cast, TypedDict

from fastapi import Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from pix import datestr
from pix.nas import accounts, decisions, history, index as ix
from pix.nas.assets import asset
from pix.nas.webapp.address import filters
from pix.nas.webapp.app import app, db, Principal, require_user, store
from pix.nas.webapp.folders import shelves
from pix.nas.webapp.grid import (
    actions,
    browse_url,
    cell,
    sections,
    stack_url,
    totals,
    view_dict,
    viewacts,
)
from pix.nas.webapp.marks import mark
from pix.nas.webapp.shell import (
    act,
    display_menu,
    display_rows,
    LOGIN_CSS,
    page as _page,
    WORKING,
    zoom,
)
from pix.nas.webapp.text import age, h, js, q
from pix.nas.webapp.vocab import (
    ARCHIVED_LABEL,
    audience_names,
    chips,
    EXTRA,
    FIRST_PAGE,
    FIXED,
    FIXED_GROUPS,
    GRID_GROUPS,
    group_names,
    groupings,
    HOME_GROUPING,
    ONE_FIELD,
    PAGE_LIMIT,
    TIERS,
)


@app.exception_handler(ix.StaleIndex)
async def shapes_disagree(request: Request, exc: Exception) -> Response:
    """Say that the two halves disagree, rather than failing in the middle.

    The desktop builds the index and the container reads it, and they are
    updated by two different acts on two different machines. So they *will* go
    out of step — most often while the app is being worked on, when a rebuild
    from the desktop lands under a container still running last week's code.

    Without this the disagreement surfaced wherever a query happened to touch a
    column that had moved: a 500 and a traceback in a log nobody is watching,
    which reads as *the app is broken* rather than as *these two need to be the
    same age*. A page that names both shapes and says which side to move is the
    difference between a five-second fix and an afternoon.

    **503, not 500**, because nothing here is wrong — the app is answering, and
    what it is answering is that it cannot read what it was given.
    """
    stale = cast("ix.StaleIndex", exc)
    if stale.app_is_behind:
        what = ("The archive has moved on without this app. Its index was "
                "written by a newer pix than the one serving this page.")
        fix = ("Update the app: copy <code>src</code> onto the share and "
               "restart the container, or import a newer image if the "
               "dependencies have changed too.")
    else:
        what = ("The index was built by an older pix than the one serving this "
                "page, so it does not have the shape this build reads.")
        fix = ("Rebuild it from the desktop: <code>pix2 index</code>. Nothing "
               "is lost — the index is a projection, and every decision it "
               "holds lives in master.")
    return _page("pix2 — out of step", f"""<div class="gate">
<h2>Out of step</h2>
<p class="dim">{h(what)}</p>
<p class="dim">{fix}</p>
<p class="dim" style="margin-top:14px">{h(stale.say())}</p>
</div>""" + f"<style>{LOGIN_CSS}</style>", status_code=503)


@app.get("/", response_class=HTMLResponse)
def home(request: Request,
         user: Annotated[Principal, Depends(require_user)],
         view: Annotated[ix.Filters, Depends(filters)],
         group: Annotated[str, Query()] = HOME_GROUPING) -> Response:
    """The same library as `/browse`, one level up: folders instead of files.

    It was a fixed table of years and their events, which answered two
    questions well and every other one not at all — *which cameras is this
    library from*, *what is still undecided in July*, *which days of the trip
    have the most photographs*. Those are the same questions the grid already
    answers about files, and the controls that ask them already exist.

    So this is the grid at a coarser zoom. The same filters narrow it, the same
    grouping cuts it, and each section is drawn as one folder rather than as a
    heading with its contents beneath. Clicking a folder is the same view with
    that section's values added to the filters — which is why a grouping has to
    be expressible as a filter to be drilled into, and why the two that are not
    say so rather than offering a door into somewhere else.
    """
    if not request.url.query:
        return RedirectResponse(
            f"/?date={time.localtime().tm_year}&group={q(HOME_GROUPING)}",
            status_code=303)
    conn = db()
    groups = groupings(group)
    rows = ix.sections(conn, view, groups=groups, limit=PAGE_LIMIT)
    # How big each of these is when the levels above it are not cutting it up.
    # An event running from February into March is two cards, and without this
    # each of them is a folder saying 312 files with nothing to say it is part
    # of eleven hundred. Same query, one level: what a thing is on its own.
    whole = ({r["grp0"]: (int(r["n"]), r["first_seen"], r["last_seen"])
              for r in ix.sections(conn, view, groups=groups[-1:],
                                   limit=PAGE_LIMIT)}
             if len(groups) > 1 else {})
    s = ix.summary(conn, view)
    # What each section carries, one query per kind. The same `GROUP BY` the
    # sections use, so the buckets line up with the cards exactly — a count
    # drawn from a different question would be a percentage of something else.
    # Audience only for an administrator: a household member sees nothing that
    # is not already shared with them, so the answer is always *all of it*.
    spread = {kind: ix.spread(conn, view, groups=groups, column=kind)
              for kind in (("audience", "people", "tags") if user.is_admin
                           else ("people", "tags"))}

    open_note = ("" if not (user.is_admin
                            and accounts.admin_password_is_initial(store()))
                 else
                 '<span class="warn">&middot; admin still has its shipped '
                 'password</span>')
    # Only an admin sees undecided files at all, so only an admin has a backlog
    # to report. For anyone else the count is trivially complete, and saying so
    # would be noise pretending to be progress.
    backlog = (f'{s["unreviewed"] or 0:,} undecided &middot; '
               if user.is_admin else "")
    head = (f'{s["files"] or 0:,} files &middot; {backlog}'
            f'{s["undated"] or 0:,} undated '
            f'&middot; indexed {age(ix.built_at(conn))} {open_note}')

    body = ('<p class="empty">Nothing matches these filters.</p>' if not rows
            else '<div class="grid folders" id="grid">'
                 + shelves(rows, groups, view, user, whole, spread)
                 + "</div>")
    # The same filters against the other page: the corner is a zoom control, and
    # a zoom that dropped the filters would be a different library rather than
    # the same one seen closer. The grouping is left behind (`_zoom`).
    return _page("pix2",
                 # The shared menu, which every filter and the grouping open
                 # into. Left out, the script threw looking for it the moment
                 # it loaded, and a page whose chips never drew and whose
                 # heading never wired looks exactly like a page whose
                 # controls were never built.
                 f'{body}<div id="menu" hidden></div>{WORKING}',
                 tools='<div class="chips" id="chips"></div>',
                 rows=actions(user, folders=True),
                 # Under the account rather than over the folders or along the
                 # bottom. It is a standing description of the library — how
                 # much there is, how much is undated, when it was last
                 # indexed — and none of it changes while you read, so
                 # anywhere permanent is a row of folders spent on something
                 # that has not moved since yesterday.
                 info=head,
                 script=view_script(user, view, groups, page="/"),
                 zoom=zoom("/browse", request.url.query),
                 user=user)


@app.get("/event/{event}", response_class=HTMLResponse)
def event_grid(event: str) -> RedirectResponse:
    """Kept so older links still land somewhere — an event is just a filter now."""
    return RedirectResponse(f"/browse?event={q(event)}", status_code=307)


def safe_back(back: str) -> str:
    """Where to return to, or the front door.

    Only a path on this app. `back` arrives in a query string, which is a
    place anybody can type anything, and a bare `href` built from one is how a
    link on a page people trust sends them somewhere else entirely.
    """
    if back.startswith("/") and not back.startswith("//") and ":" not in back:
        return back
    return "/"


def stack_actions(user: Principal, decided: bool) -> str:
    """The bar on a stack's page: four things, and no grid.

    The grid's bar was eleven controls, and inside a stack most of them
    answered nothing. *Event*, *Tags*, *People*, *Date* and *Access* are
    statements about a photograph, and the eight in here are one photograph —
    so they belong outside, on the one that represents it, where they reach
    all of them at once. *Stack* offered to stack what is already a stack, and
    on two of them it offered to stack a subset, which is not a thing a stack
    can be. *Make top* is on the photographs now, where the question is.

    What is left is what there is to say in here. **Not a stack** — of one of
    them, or of the whole group by saying it of the one that speaks. **Take
    out**, only where somebody decided: undoing a decision is what it is for,
    and a guess has none to undo. And the two that are about the file rather
    than the stack — a bad take is exactly what you notice with all eight side
    by side, and wanting one of them is exactly why you opened it.
    """
    return f"""<div class="row" id="actions">
  <button id="selall" class="tick" title="Select all" aria-label="Select all"></button>
  <span class="count" id="selcount" style="margin:0"></span>
  <span class="grp" data-side="live" hidden>
    {act("nostack", "Not a stack", user=user)}
    {act("unstack", "Take out", user=user) if decided else ""}
    {act("download", "Download", user=user)}
    <span class="sep"></span>
    {act("delete", "Delete", "danger", user=user)}
  </span>
  <span class="grp" data-side="gone" hidden>
    {act("restore", "Restore", user=user)}
    {act("purge", "Purge&hellip;", "danger", user=user)}
  </span>
  <span class="grp" data-side="choose" hidden>
    <b>Click the one to show</b>
    <button id="choosecancel">Cancel</button>
  </span>
</div>"""


@app.get("/stack/{folder}/{name}", response_class=HTMLResponse)
def stack_page(folder: str, name: str,
               user: Annotated[Principal, Depends(require_user)],
               back: Annotated[str, Query()] = "") -> HTMLResponse:
    """The takes of one photograph — a page, not a filtered grid.

    It was `/browse?within=…`, which is the library with one more filter on
    it, and the takes of one photograph are not a view of the library.
    Everything the grid brought with it answered nothing in here: eleven
    filters over eight frames of one moment, a grouping control for a section
    that is the whole page, a *Stack* button offering to stack what is already
    a stack. Each of those had to be reasoned about separately every time
    something changed, and every stack bug this app has had was one of them
    leaking.

    One question, asked once: **are these one photograph, and which of them
    shows.** It is asked on the photographs themselves — every take carries
    the answer — and the bar holds only what is left to say.

    Guesses and decisions are the same page. A suggestion is the app's answer
    to the same question, offered rather than recorded, and reading it needs
    the same eight thumbnails side by side; the only difference is which words
    the page uses about it and whether *Take out* means anything yet.
    """
    conn = db()
    key = f"{folder}/{name}"
    view = ix.Filters(within=key, viewer=user.scope)
    rows = ix.files(conn, view, limit=PAGE_LIMIT)
    # Not a stack, or not one this person may see — the same answer either
    # way, and deliberately: which of the two it is would say whether a
    # photograph they cannot see exists.
    if len(rows) < 2:
        return _page("pix2", '<p class="empty">There is no stack here.</p>',
                     user=user, status_code=404)

    lead = rows[0]
    decided = any(r["stacked_under"] for r in rows)
    guess = not decided
    where = safe_back(back)
    when = stack_when(lead)
    return _page("pix2 stack", f"""<div class="grid" id="grid">
<div class="cells">{"".join(cell(r, view) for r in rows)}</div></div>
<div id="viewer">
  <div class="stage"><img id="vimg">
  <video id="vvid" controls playsinline></video>
  <div class="meta" id="vmeta"></div></div>
  <button id="viewclose" title="Close (Esc)">&times;</button>
  <button id="railtoggle" title="Details (I)">Details</button>
  {viewacts(user, stack=True)}
  <aside id="rail"></aside>
</div>
<div id="menu" hidden></div>
{WORKING}""",
        # What this is, in words, where the filters would have been. A page
        # with no address on it is a page you cannot tell from the one before.
        tools=(f'<span class="whatis">'
               f'<b>{len(rows)}</b> of one photograph'
               + (f'<i class="asked">the app\'s guess</i>' if guess else "")
               + (f'<span class="dim">{h(when)}</span>' if when else "")
               + f'</span><a class="leave" href="{h(where)}">'
                 f'Leave this stack</a>'),
        right=display_rows(user),
        bar=display_menu(user),
        rows=stack_actions(user, decided),
        script=view_script(user, view, [], stack=key, back=where),
        info=f'<span class="line">{len(rows)} files</span>',
        user=user)


def stack_when(row: sqlite3.Row) -> str:
    """When the photograph was taken, for the one line that says which it is."""
    moment = (datestr.parse_pix(str(row["effective_date"]))
              if row["effective_date"] else None)
    if moment is None:
        return ""
    return f"{moment:%A} {moment.day} {moment:%B %Y}, {moment:%H:%M}"


@app.get("/browse", response_class=HTMLResponse)
def browse(request: Request,
           user: Annotated[Principal, Depends(require_user)],
           view: Annotated[ix.Filters, Depends(filters)],
           group: Annotated[str, Query()] = "day",
           op: Annotated[str | None, Query()] = None,
           stale: Annotated[str | None, Query()] = None,
           within: Annotated[str, Query()] = "") -> Response:
    """The one grid, filtered — select files, then say something about them.

    Selecting an event on the landing page is just this page with `?event=`, so
    there is one surface to learn rather than a browser and a separate editor.
    """
    # A stack has a page of its own. This was its address for as long as it
    # was a filter, and links to it are in bookmarks and in the operation log
    # — so it still answers, by saying where the stack lives now. One place to
    # see a stack rather than two that have to agree.
    #
    # Read straight off the query rather than out of the view, because the
    # view no longer carries one: `within` is out of `Filters.NAMES`, so no
    # chip asks it and nothing here can be narrowed by it. This is a
    # forwarding address and nothing else.
    if within:
        return RedirectResponse(
            stack_url(within, browse_url(
                view, {"group": ",".join(groupings(group)) or "none"})),
            status_code=307)
    conn = db()
    groups = groupings(group)
    rows = ix.files(conn, view, groups=groups, limit=FIRST_PAGE)
    total = ix.count(conn, view)

    cells = sections(rows, groups, view, totals(conn, view, groups), total)
    shown = f"{total:,} files"
    # How many there are in all, and how many came with the page: the rest
    # arrive as the grid is scrolled towards them (`/api/page`).
    body = (f'<div class="grid" id="grid" data-total="{total}" '
            f'data-served="{len(rows)}">{cells}</div>'
            '<div id="more" aria-hidden="true"></div>' if rows else
            '<p class="empty">Nothing matches these filters.</p>')
    return _page("pix2 browse", f"""{body}
<div id="viewer">
  <div class="stage"><img id="vimg">
  <video id="vvid" controls playsinline></video>
  <div class="meta" id="vmeta"></div></div>
  <button id="viewclose" title="Close (Esc)">&times;</button>
  <button id="railtoggle" title="Details (I)">Details</button>
  {viewacts(user)}
  <aside id="rail"></aside>
</div>
<div id="menu" hidden></div>
{WORKING}""",
        tools=('<div class="chips" id="chips"></div>'
               + from_link(op, stale, len(rows))
               + cuts_link(view, total)),
        # Away from the filters, at the end of the row with the account. It is
        # a view control rather than a filter — nothing it does changes which
        # photographs are here — and standing among the chips it read as one
        # more thing narrowing the library.
        #
        # Twice, and the width picks: a labelled row inside the menu, where
        # there is no room for it in the bar, and the bare control in the bar
        # where there is. See `_sizeset`.
        right=display_rows(user),
        bar=display_menu(user),
        rows=actions(user),
        # Up a zoom: the same query, minus the stack. A folder view of one
        # stack is the stack, so the only thing the coarser view can say about
        # `within` is nothing — and leaving a stack is its own gesture, on the
        # bar, rather than a side effect of changing how you are reading.
        zoom=zoom("/", request.url.query),
        script=view_script(user, view, groups),
        # No instructions. A standing sentence about clicking and holding is
        # read once, on the first visit, and then occupies a fixed strip at the
        # bottom of every screen for as long as the app exists — which on a
        # phone was three lines of it. The gestures are the ordinary ones; the
        # footer is for what this page is *now*, which is the count and
        # whatever the last write had to say.
        info=(f'<span class="line" id="count">{shown}</span>'
              f'<span class="line">indexed {age(ix.built_at(conn))}</span>'),
        user=user)


class PageConfig(TypedDict):
    """Everything the page script is told about this page and this view.

    One object, `window.PIX`, which the script unpacks on its first line
    (`static/js/browse/00-page.js`) — the keys are the names it reads them
    by. Typed, so a key missing or misspelt is a type error here rather than
    an `undefined` three thousand lines into the script.
    """

    VIEW: dict[str, str | list[str] | None]
    CHIPS: tuple[tuple[str, str], ...]
    FIXED: dict[str, tuple[tuple[str, str], ...]]
    FIXED_GROUPS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]]
    EXTRA: dict[str, tuple[tuple[str, str], ...]]
    ADMIN: bool
    USERS: list[str]
    GROUPS: list[str]
    MARKS: dict[str, str]
    UNREVIEWED: str
    ARCHIVED: str
    ARCHIVED_LABEL: str
    EVENT_SEP: str
    NO_EVENT: str
    USUAL: str | None
    PAGE: str
    STACK: str
    BACK: str
    TIERS: tuple[tuple[str, int], ...]
    GRID_GROUPS: tuple[tuple[str, str], ...]
    ONE_FIELD: dict[str, str]
    GROUPING: list[str]

def view_script(user: Principal, view: ix.Filters, groups: list[str], *,
                 page: str = "/browse", stack: str = "",
                 back: str = "") -> str:
    """The page script, and what it needs to know about this view.

    One script for both pages. The landing page and the grid ask the same two
    questions — *which files* and *cut how* — and the controls that ask them
    are the chips and the heading. A second copy of either would be a second
    place for them to drift, and the chips are the part of this app that has
    been rewritten most.

    What the grid has and this does not is a selection, a viewer and a set of
    actions, so the script finds none of those elements and wires none of
    them. That is a page without photographs on it, not a broken one.
    """
    config: PageConfig = {
        "VIEW": view_dict(view),
        "CHIPS": chips(user),
        "FIXED": FIXED,
        "FIXED_GROUPS": FIXED_GROUPS,
        "EXTRA": EXTRA,
        "ADMIN": user.is_admin,
        "USERS": audience_names(),
        "GROUPS": group_names(),
        "MARKS": {c: mark(c) for c, _ in chips(user)},
        # The word the audience filter uses for *nobody yet*. Sent rather than
        # spelled again here: the page draws a chip that sets it, and two
        # copies of a sentinel are two chances to disagree about what it is.
        "UNREVIEWED": ix.UNREVIEWED,
        "ARCHIVED": decisions.ARCHIVED,
        "ARCHIVED_LABEL": ARCHIVED_LABEL,
        "EVENT_SEP": decisions.EVENT_SEP,
        "NO_EVENT": ix.NO_EVENT,
        "USUAL": store().usual,
        "PAGE": page,
        # Which stack's page this is, and the way out of it. Empty everywhere
        # else, which is how the script knows it is not on one — the grid asks
        # the same question of a shelf of stacks and answers it in place.
        "STACK": stack,
        "BACK": back,
        "TIERS": TIERS,
        "GRID_GROUPS": GRID_GROUPS,
        "ONE_FIELD": ONE_FIELD,
        "GROUPING": groups,
    }
    return (f"<script>window.PIX={js(config)};</script>"
            f"<script>{BROWSE_JS}</script>")


def from_link(op_id: str | None, stale: str | None, shown: int) -> str:
    """Why this grid is showing a particular set of files, and the way out.

    Not a chip: a chip is a value picked from a list of values, and there is no
    list of operations to pick from — you arrive here from the log. But it has
    to say what it is and be dismissable for the same reason the chips do,
    because a filter you cannot see is a library that looks smaller than it is.
    """
    if not op_id:
        return ""
    op = history.get(op_id)
    what = (h(op.summary) if op is not None else "an edit that is no longer "
            "in the log")
    lead = "left behind by" if stale else "from"
    return (f'<span class="chip on from-op">{lead} <b>{what}</b>'
            f'<span class="val">{shown:,}</span>'
            f'<a class="x" href="/browse" title="Show everything">&times;</a>'
            f'</span>')


#: Every action, and the decision fields it writes.
#:
#: One table, because the buttons a page renders and the fields an endpoint
#: accepts are the same question and must not be able to disagree. Hiding a
#: control is not access control — anyone can post to `/api/decide` — so this
#: drives the refusal first and the bar second.
def cuts_link(view: ix.Filters, shown: int) -> str:
    """What the grid is showing when an original's pill opened it, and the
    way back out — said like an operation, for the same reason: there is no
    list of originals to pick one from."""
    if not view.cuts:
        return ""
    name = view.cuts.partition("/")[2]
    return (f'<span class="chip on from-op">cut from <b>{h(name)}</b>'
            f'<span class="val">{shown:,}</span>'
            f'<a class="x" href="{h(browse_url(view, {}))}" '
            f'title="Back to the library">&times;</a></span>')


#: The grid's script, in the order its parts are read. One top-level scope,
#: so the order is load-bearing: a part uses what the parts before it define
#: (`cells`, `say`, `targets` …) — which is why this is a list and not a glob.
BROWSE_PARTS: tuple[str, ...] = (
    "00-page.js",
    "01-device.js",
    "02-size.js",
    "03-display.js",
    "04-chips.js",
    "05-menu.js",
    "06-selection.js",
    "07-viewer.js",
    "08-swipe.js",
    "09-writing.js",
    "10-folders.js",
    "11-stacks.js",
    "12-download.js",
    "13-takeover.js",
    "14-keyboard.js",
    "15-grouping.js",
    "16-paging.js",
)


BROWSE_JS: str = "".join(asset(f"js/browse/{p}") for p in BROWSE_PARTS)
