"""The app — browse the archive (spec/nas-app.md §8).

Runs in Container Manager on the NAS, bind-mounting `/volume1/pix2`. It reads
the index and serves the **derived** tiers; it never decodes anything, because
`process` already made everything it displays. That is what keeps it viable on a
no-AVX Atom, and it is why the image needs neither Pillow nor ffmpeg.

Deliberately no frontend build step. A keyboard-driven grid needs a page and some
JavaScript, not a toolchain — and a build pipeline is the part most likely to rot
between the day it works and the day you next touch it.

**The one thing it writes is decisions.** A curation write puts an `.xmp` beside
the master file and then updates that file's index row — sidecar first, index
follows (§4). It never rewrites master bytes, and it never rebuilds the index:
a rebuild reads ~62k records, which would block the single worker for minutes at
the request of anyone holding the URL.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import zipfile
import threading
import time
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, nullcontext
from urllib.parse import parse_qs, quote
from dataclasses import dataclass, field, replace
from itertools import groupby
from pathlib import Path
from typing import Annotated, Any, Iterator, Sequence, cast

from fastapi import (
    Body, Depends, FastAPI, HTTPException, Query, Request, status,
)
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response,
    StreamingResponse,
)
from pydantic import BaseModel

from pix import __version__ as _PIX_VERSION
from pix import datestr
from pix.nas import accounts
from pix.nas import auth
from pix.nas import clips
from pix.nas import cut
from pix.nas import decisions
from pix.nas import paths
from pix.nas.assets import asset
from pix.nas import destroy as destroy_mod
from pix.nas import history
from pix.nas import index as ix
from pix.nas import webroots
from pix.nas.webapp.permissions import (
    HOUSEHOLD_FIELDS as _HOUSEHOLD_FIELDS,
    may,
)
from pix.nas.webapp.vocab import (
    BULK_LIMIT,
    FIRST_PAGE,
    HOME_GROUPING,
    META_READERS,
    NEXT_PAGE,
    PAGE_LIMIT,
    ZIP_LIMIT,
    ARCHIVED_LABEL as _ARCHIVED_LABEL,
    DRILL as _DRILL,
    EXTRA as _EXTRA,
    FIXED as _FIXED,
    FIXED_GROUPS as _FIXED_GROUPS,
    GRID_GROUPS as _GRID_GROUPS,
    INFO as _INFO,
    KIND_HALVES as _KIND_HALVES,
    KIND_WORDS as _KIND_WORDS,
    OLD_KINDS as _OLD_KINDS,
    ONE_FIELD as _ONE_FIELD,
    SIZES as _SIZES,
    SPREAD_FILTER as _SPREAD_FILTER,
    SPREAD_LABEL as _SPREAD_LABEL,
    TIERS as _TIERS,
    audience_names as _audience_names,
    chips as _chips,
    group_names as _group_names,
    groupings as _groupings,
    mime as _mime,
    pick as _pick,
)
from pix.nas.webapp.marks import (
    ACT_MARKS as _ACT_MARKS,
    BACK as _BACK,
    FAVICON as _FAVICON,
    FILES_MARK as _FILES_MARK,
    FOLDERS_MARK as _FOLDERS_MARK,
    GEAR as _GEAR,
    ICONS as _ICONS,
    logo_mark as _logo_mark,
    mark as _mark,
)
from pix.nas.webapp.app import (
    Principal,
    usual as _usual,
    write_lock as _write_lock,
    app,
    db,
    require_admin,
    require_user,
    signed_in,
    store,
)
from pix.nas.webapp.text import (
    age as _age,
    dur as _dur,
    h as _h,
    js as _js,
    q as _q,
    split as _split,
    under as _under,
    when as _when,
)
from pix.nas.decisions import Decision, Unset


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncGenerator[None]:
    """Pick up cuts a restart interrupted, then serve."""
    threading.Thread(target=_resume_cuts, name="pix-resume-cuts",
                     daemon=True).start()
    yield


# Attached here rather than when the app is made: what it starts belongs to
# the clips, which are assembled after the app they serve.
app.router.lifespan_context = _lifespan


# --- auth --------------------------------------------------------------------


# --- data --------------------------------------------------------------------


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
<p class="dim">{_h(what)}</p>
<p class="dim">{fix}</p>
<p class="dim" style="margin-top:14px">{_h(stale.say())}</p>
</div>""" + f"<style>{_LOGIN_CSS}</style>", status_code=503)


# --- pages -------------------------------------------------------------------

_STYLE: str = asset("css/app.css")


def _zoom(page: str, query: str) -> str:
    """The other view of this library: the same filters, its own grouping.

    **Filters cross; the grouping does not.** Opening a folder already works
    this way — the folder's values are added to the filters and the grid cuts
    them its own way, by day — and the corner going back has to match it: it
    carried the grid's *by day* up to the folders, and a reading of the
    library that had been built up by hand was lost on every round trip. A
    grouping is how a page is read, and each page reads best its own way.

    `within` is left behind too. A folder view of one stack is the stack, so
    there is nothing coarser to say about it, and leaving a stack is its own
    gesture on the bar.

    The folders page is named its default grouping outright, because an
    address with nothing in it is sent to this year — and an unfiltered grid
    zoomed out is the whole library, not this year of it.
    """
    kept = [part for part in query.split("&")
            if part and not part.startswith(("group=", "within="))]
    if page == "/":
        kept.append(f"group={_q(HOME_GROUPING)}")
    return page + ("?" + "&".join(kept) if kept else "")


def _brand(zoom: str) -> str:
    """The corner: on the two library pages a control, everywhere else the logo.

    `zoom` is where the *other* view of this same library is — the identical
    query against the other page — and its being a `/browse` address is what
    says the page showing it is the folders one. One parameter rather than a
    mode beside it, because two would be two things that could disagree about
    which way round the corner is.

    It shows **what you are looking at** and says what it will do, which is the
    opposite way round from the size control beside the account. That is not an
    inconsistency: size has no shape on screen to read, and this does — the
    corner is a picture of the grid under it, and a picture that disagreed with
    the page would be worse than no picture.
    """
    if not zoom:
        return (f'<a class="brand" href="/" title="pix2" aria-label="pix2">'
                f'{_logo_mark(26)}</a>')
    folders = zoom.startswith("/browse")
    say = "Show the files" if folders else "Show the folders"
    return (f'<a class="brand" href="{_h(zoom)}" title="{say}" '
            f'aria-label="{say}">'
            f'{_FOLDERS_MARK if folders else _FILES_MARK}</a>')


#: Offering the home screen, once (spec/nas-app.md §8).
#:
#: **Only where it can be taken up, and only until it is answered.** A prompt
#: that returns every visit is worse than no prompt: it teaches people to
#: dismiss the bar without reading it, and the next thing that appears there is
#: dismissed too. So there is one answer, it is remembered, and there is no
#: *not now* — a *not now* that comes back tomorrow is the pestering this is
#: trying not to do.
#:
#: Android is offered a real button, because Chrome hands the page the install
#: prompt and one tap is a better offer than a sentence about a menu. iOS has no
#: such API, so it gets the sentence — and the sentence has to name the Share
#: menu, because *Add to Home Screen* lives nowhere else and is not findable by
#: guessing.
#:
#: It lives in the shell rather than in the grid's script, so it works on
#: `/history` and `/accounts`, which carry no page script at all.
_INSTALL_JS: str = asset("js/install.js")


#: How tall the bar is, said in a number the stylesheet can use.
#:
#: The section headings stick *under* it, and it is not a constant: the
#: filters take a second line when there are enough of them, the selection row
#: is there on the grid and not on the landing page, and an installed app adds
#: the strip behind the clock. A heading pinned to a guess sits either over
#: the bar or a gap below it, and the guess is wrong at the moment the page is
#: busiest.
#:
#: A `ResizeObserver` rather than a scroll or resize handler: what matters is
#: the bar changing height, which happens when the filters wrap — something
#: neither of those events reports. It fires once on `observe`, so the value
#: is right before the first paint the reader sees.
_BAR_JS: str = asset("js/bar.js")


#: The three things a `<details>` does not do on its own, on every page.
#:
#: The menu works without this — that is the whole reason it is a `<details>`
#: — and none of what is here is the difference between opening it and not.
#: What is here is the difference between a panel and a menu: a menu goes away
#: when you have finished with it, and the ways people finish with one are
#: Escape, a click somewhere else, and tabbing past the end of it. A panel
#: that stays open over the grid until you press the one control that opened
#: it is a panel you dismiss by navigating, which is the opposite of what it
#: is for.
#:
#: In the shell rather than in the browse script, because it has to work on
#: `/history`, `/accounts` and the login screen too — the same reason the
#: menu carries no page script of its own.
_MENU_JS: str = asset("js/menu.js")


#: The Display choices, applied to `<html>` **before the page paints**, so a
#: fact turned off is never drawn and then taken away. Only the attributes:
#: the stylesheet does the hiding, and the page script moves what is shown
#: into its lane. Anything not one of the known values is left off, which
#: leaves the default the stylesheet already assumes.
_INFO_BOOT: str = """<script>try{var s=JSON.parse(localStorage.getItem('pix2.info')
||'{}'),ok={off:1,top:1,bot:1,on:1};for(var k in s)if(ok[s[k]]&&/^[a-z]+$/.test(k))
document.documentElement.setAttribute('data-info-'+k,s[k]);}catch(e){}</script>"""


def _page(title: str, body: str, *, tools: str = "", rows: str = "",
          right: str = "", bar: str = "", info: str = "", script: str = "",
          zoom: str = "", status_code: int = 200,
          user: Principal | None = None) -> HTMLResponse:
    """One shell.

    `tools` sits beside the brand on the first row, `right` goes **inside** the
    account menu at the far end of it, `bar` stands beside that menu rather
    than in it, and `rows` are whole extra rows below.

    `right` used to stand in the bar. It is one control — the thumbnail size
    — used once in a while, and on a phone it was one of three such controls
    holding a row open in front of the filters. Inside the menu it costs a tap
    and no room at all.

    `bar` is the same control again, for the width where the room is not
    scarce: a view control you change while you are looking at the thing it
    changes is worth a glance rather than a trip into a menu, and a desktop
    bar has ninety pixels to spare that a phone does not. Which of the two is
    on screen is a media query, and both are always rendered — turning a
    phone over changes the answer while the page is open.

    The two ends of that row are two different kinds of thing. On the left, what
    you are looking at — the filters, which are the address. On the right, how
    you are looking at it and who as, which no link carries. Counts and messages
    live down there so the header is only controls — every row of chrome at the
    top is a row of photographs pushed off the screen.

    **There is no strip along the bottom.** It held a version, a count and an
    index age — three things that do not change while you read them and that
    nobody is waiting for — and it was a fixed band of screen spent on saying
    so. They are `info` now, under the account menu, where you go when you
    want to know rather than all the time.

    The **version** is on every page twice, for the same reason the CLI
    prints it on every run: the script is inlined into this HTML, so an open
    tab keeps the build it was served with, and a stale tab and a broken build
    look identical from the outside.

    Once for a person, under the account; and once in a `<meta>` for anything
    checking a deploy landed. The two are not redundant — moving the visible
    one into that menu took it off every signed-out page, which is the login
    screen, which is the page a deploy check can reach without a session. The
    tag is in the `<head>` where no rearranging of the body can lose it.

    What was down there and had to stay is the **message line**, which is how
    all forty-odd writes say whether they happened. It is not a strip any
    more; it is a note that is not on screen until there is something to say.

    `script` goes **last**. A page script that runs from inside `<main>`
    cannot see anything below it: moving the count and the message line out of
    it once left both as `null`, and the first thing every write did was set a
    message — so nothing was ever sent, silently.
    """
    # **Never reuse a page.** It carries its own script inlined, so a cached
    # page is a cached *build* — and nothing here says how old one is: no
    # `Cache-Control`, no `ETag`, no `Last-Modified`, which leaves a browser
    # free to decide for itself. Safari in an installed app decides yes, and
    # then a deploy lands, the container restarts, and the phone goes on
    # showing last week's app with no way to tell.
    #
    # The service worker was supposed to be the answer and is not: it fetches
    # navigations rather than serving them from its own cache, but `fetch`
    # goes through the HTTP cache like anything else, so it was handing back
    # the very copy it thought it was avoiding.
    return HTMLResponse(status_code=status_code,
                        headers={"Cache-Control": "no-store"},
                        content=f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#14161a">
<meta name="pix-version" content="{_PIX_VERSION}">
<meta name="color-scheme" content="dark">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="pix">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<title>{title}</title>
<link rel="icon" href="{_FAVICON}"><style>{_STYLE}</style>{_INFO_BOOT}</head><body>
<div class="topbar">
<div class="row"><button class="back" id="back" aria-label="Back"
 title="Back">{_BACK}</button>{_brand(zoom)}{tools}
<span class="spacer"></span><div class="right">{bar}{_whoami(user, right, info)}</div></div>{rows}
</div><main>{body}</main>
<span class="note" id="note" hidden></span>
{script}
<script>if('serviceWorker' in navigator)window.addEventListener('load',function(){{
  navigator.serviceWorker.register('/sw.js').catch(function(){{}});}});</script>
<script>(function(){{
  var back = document.getElementById('back');
  if (!back) return;
  // A fresh launch has nowhere to go back to, and a button that does nothing
  // teaches you to stop believing the rest of them.
  if (history.length <= 1) back.disabled = true;
  back.onclick = function () {{ history.back(); }};
}})();
// **Back shows the page as it is now, not as it was left.** Browsers keep
// the page behind you whole and put it back untouched — the grid you left
// before cutting clips came back without them until reloaded. `no-store`
// does not stop that everywhere (Safari, and Chrome on some pages, keep
// such pages anyway), so a page put back that way asks again instead.
window.addEventListener('pageshow', function (e) {{
  if (e.persisted) location.reload();
}});</script>
<script>{_BAR_JS}</script>
<script>{_MENU_JS}</script>
<script>{_INSTALL_JS}</script>
</body></html>""")


def _whoami(user: Principal | None, extra: str = "",
            info: str = "") -> str:
    """Who you are, what is waiting, and everything you do rarely — one
    control.

    They were three: a bell, a thumbnail size, and the account. Each is small
    and each is used once in a while, and on a phone the three of them plus
    the filters pushed the top bar to four rows and nearly half the screen.
    Three rarely-used controls standing permanently in front of the thing the
    bar is actually for is the same mistake the filters made before they
    learned to fold away.

    **It is a `<details>`, and it opens on a click.** It used to open on
    hover, which is a thing a menu is walked into rather than opened: on a
    touchscreen the first tap is the hover and the second lands on whatever
    the panel has just put under the finger, and to a screen reader a
    `tabindex` on a span is not a control at all and has nothing to say about
    being open. A `<summary>` is a button with an expanded state, for free and
    with no script — which is the property that mattered, because `/history`
    and `/accounts` carry no page script and a Sign out that worked only
    where the grid was loaded would be missing from the page you are most
    likely to be stuck on. `_MENU_JS` adds Escape and dismissal on top; both
    are improvements on a menu that already works without them.

    **The name stays visible where there is room for it.** This app is used as
    two different people — the owner curating and the admin granting access
    — and acting as the wrong one is invisible until something is shared with
    the wrong household. On a phone the name gives way to a gear; it is
    clipped rather than dropped, so it is still the control's accessible name,
    and it is the first line of the menu at every width. The line says the
    *kind* of account as well, which nothing anywhere else does.

    **The dot rides on the trigger** either way. What is waiting has to be
    visible without opening anything, or it is not a notification.

    The rows are grouped, with a rule between the kinds rather than between
    each pair: how you are looking, what you administer, what this page holds,
    and the way out. Eight undifferentiated lines is a list, not a menu.
    """
    if user is None:
        return (f'<span class="ver">v{_PIX_VERSION}</span>'
                f'<a class="who-link" href="/login">Sign in</a>')
    waiting = _binned() if user.is_admin else 0
    # What is waiting, as a row of the menu rather than a bell of its own —
    # and beside the two pages only an admin has, because they are one kind of
    # thing: what you look after rather than what you are looking at.
    admin = (f'<span class="group">{bin_link_html(waiting)}'
             f'<span class="quiet">Nothing waiting</span>'
             f'<a href="/history">History</a>'
             f'<a href="/accounts">Accounts</a></span>'
             if user.is_admin else "")
    view = f'<span class="group viewrow">{extra}</span>' if extra else ""
    return (
        f'<details class="me" id="me" data-any="{"1" if waiting else ""}">'
        f'<summary>'
        f'<span class="name">{_h(user.name)}</span>'
        f'<i class="caret">&#9662;</i>'
        f'<span class="gearbtn" aria-hidden="true">{_GEAR}</span>'
        f'<i class="dot"></i></summary>'
        f'<div class="memenu">'
        f'<span class="whoami"><b>{_h(user.name)}</b>'
        f'<span class="role">'
        f'{"Administrator" if user.is_admin else "Household"}</span></span>'
        f'{view}{admin}'
        f'<span class="info">{info}'
        f'<span class="line ver">v{_PIX_VERSION}</span></span>'
        f'<form method="post" action="/logout">'
        f'<button>Sign out</button></form></div></details>')


def _infoset(key: str, label: str, default: str) -> str:
    """One fact's switch: Off / Top / Bottom, or Off / On for a fixed one."""
    choices = (("off", "Off"), ("on", "On")) if default == "on" else (
        ("off", "Off"), ("top", "Top"), ("bot", "Bottom"))
    opts = "".join(
        f'<button type="button" class="showopt" data-info="{key}" '
        f'data-at="{val}" aria-pressed="{"true" if val == default else "false"}"'
        f'>{word}</button>' for val, word in choices)
    return (f'<span class="sizerow"><span class="rowlab">{label}</span>'
            f'<span class="sizeset" role="group" aria-label="{label}">'
            f'{opts}</span></span>')


def _display_rows(user: Principal) -> str:
    """Everything about how the grid is drawn, as rows: size, then the facts.

    Access only for an administrator, the same as on a folder card: everybody
    else sees only what was shared with them, so who else can see it is not a
    fact their grid has any use for.
    """
    apart = ('<span class="rowhead">Stacks</span>'
             '<span class="sizerow"><span class="rowlab">Suggestions</span>'
             '<span class="sizeset" role="group" aria-label="Suggestions">'
             '<button type="button" class="showopt apartopt" data-at="fold" '
             'aria-pressed="true" title="Folded behind the one each would '
             'show">Fold</button>'
             '<button type="button" class="showopt apartopt" data-at="apart" '
             'aria-pressed="false" title="Each photograph on its own">'
             'Apart</button></span></span>')
    return ('<span class="sizerow"><span class="rowlab">Thumbnail size'
            f'</span>{_sizeset()}</span>'
            '<span class="rowhead">Info</span>'
            + "".join(_infoset(*i) for i in _INFO
                      if i[0] != "access" or user.is_admin)
            + apart)


def _display_menu(user: Principal) -> str:
    """The Display menu, for the bar.

    The size control used to stand there on its own. It is still the first
    row, because it is still the one you reach for most — but it is one of
    six ways of saying how you want to look, and a bar with six segmented
    controls in it is a bar about itself. Narrow, the same rows are a group of
    the account menu instead, the way the size control already was.
    """
    return ('<details class="me disp"><summary><span class="name">Display'
            '</span><i class="caret">&#9662;</i></summary>'
            f'<div class="memenu">{_display_rows(user)}</div></details>')


def _sizeset() -> str:
    """The three sizes as one control. No `id`: there are two of these."""
    opts = "".join(
        f'<button type="button" class="sizeopt" data-size="{key}" '
        f'aria-pressed="false" title="{say}">{letter}</button>'
        for key, letter, say in _SIZES)
    return (f'<span class="sizeset" role="group" '
            f'aria-label="Thumbnail size">{opts}</span>')


def _stacks_asked(stacks: list[str] | None, user: Principal
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
    return _pick(got), apart


def _both_sides(deleted: list[str] | None, op_id: str | None,
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


def _from_operation(op_id: str | None,
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
        now = {(r["folder"], r["name"]): _as_decision(r)
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


def _as_decision(row: sqlite3.Row) -> decisions.Decision:
    """An index row read back as the decision it is a projection of."""
    return decisions.Decision(
        event=row["event"] or None,
        date_override=row["date_override"] or None,
        tags=tuple(_split(row["tags"])),
        audience=tuple(_split(row["audience"])),
        deleted=bool(row["deleted"]))


def _binned() -> int:
    """How many files are in the bin — *8 deleted*, and the way to deal with
    them.

    Deleting is meant to be cheap, which means files accumulate in a state
    nobody is looking at. A link called "Deleted" says nothing about whether
    there is anything to do; a number says there are eight, and says it on
    every page until they are gone.

    Zero if the index cannot answer. A header that fails to render because a
    count could not be read would take the whole page with it.
    """
    try:
        conn = db()
    except HTTPException:
        return 0
    try:
        return ix.count(conn, ix.Filters(deleted="only"))
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def bin_link_html(n: int) -> str:
    """The bin count as the header shows it — and as the page rewrites it."""
    return (f'<a class="bin-link" id="bincount" '
            f'href="/browse?deleted=only"{"" if n else " hidden"}>'
            f'{n:,} deleted</a>')


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
    picked, legacy_apart = _stacks_asked(stacks, user)
    return ix.Filters(event=_pick(event),
                      date=_pick([d for d in (ix.date_prefix(v)
                                              for v in date or []) if d]),
                      tag=_pick(tag), person=_pick(person),
                      audience=_pick(audience),
                      chosen=_from_operation(op, stale),
                      within=within, cuts=cuts or None,
                      stacks=picked,
                      apart=apart == "1" or legacy_apart,
                      unfold="stack" in _groupings(group),
                      kind=_pick([k for v in kind or []
                                  for k in _OLD_KINDS.get(v, (v,))]),
                      band=_pick(band),
                      camera=_pick(camera), source=_pick(source),
                      viewer=user.scope,
                      deleted=_both_sides(deleted, op, user))


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
            f"/?date={time.localtime().tm_year}&group={_q(HOME_GROUPING)}",
            status_code=303)
    conn = db()
    groups = _groupings(group)
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
            f'&middot; indexed {_age(ix.built_at(conn))} {open_note}')

    body = ('<p class="empty">Nothing matches these filters.</p>' if not rows
            else '<div class="grid folders" id="grid">'
                 + _shelves(rows, groups, view, user, whole, spread)
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
                 f'{body}<div id="menu" hidden></div>{_WORKING}',
                 tools='<div class="chips" id="chips"></div>',
                 rows=_actions(user, folders=True),
                 # Under the account rather than over the folders or along the
                 # bottom. It is a standing description of the library — how
                 # much there is, how much is undated, when it was last
                 # indexed — and none of it changes while you read, so
                 # anywhere permanent is a row of folders spent on something
                 # that has not moved since yesterday.
                 info=head,
                 script=_view_script(user, view, groups, page="/"),
                 zoom=_zoom("/browse", request.url.query),
                 user=user)


def _shelves(rows: list[sqlite3.Row], groups: list[str], view: ix.Filters,
             user: Principal,
             whole: dict[object, tuple[int, object, object]] | None = None,
             spread: dict[str, dict[tuple[object, ...],
                                    list[tuple[str, int]]]]
             | None = None) -> str:
    """The folders, under a heading for each level above them.

    `year › event` is a row of events under each year, not a flat list of
    cards each repeating which year it is in. The grid already reads that way
    and this is the same library, so it is the same shape: the outer levels
    are headings, and only the innermost is a folder.

    The heading is the grouping control here as it is there, and it carries
    one crumb per level — values for the levels above, and for the innermost
    the *name* of the cut, because that level has no single value: it is what
    the cards are. So `2025 › By event` says where you are and what you are
    looking at, and either half can be changed by clicking it.
    """
    inner = groups[-1] if groups else ""
    outer = groups[:-1]
    if not groups:
        return _section(
            _heading([], 0, len(rows), pick=False, cut=True),
            "".join(_folder(r, groups, view, user, spread=spread)
                    for r in rows))
    out: list[str] = []
    for keys, run in groupby(rows, key=lambda r: tuple(
            r[f"grp{i}"] for i in range(len(outer)))):
        shelf = list(run)
        labels = [_group_label(k, g, outer[:i], shelf[0])
                  for i, (k, g) in enumerate(zip(keys, outer))]
        labels.append(dict(_GRID_GROUPS).get(inner, inner))
        out.append(_section(
            _heading(labels, len(groups), len(shelf), pick=False, cut=True),
            "".join(_folder(r, groups, view, user, whole or {}, spread)
                    for r in shelf)))
    return "".join(out)


def _spread_chips(kind: str, values: list[tuple[str, int]], n: int,
                  lead: tuple[str, int] | None = None) -> str:
    """What a section says about itself, one kind of value at a time.

    **The name and nothing else.** A share was printed beside each one while
    *undecided* was still a count in a sentence underneath, and the number was
    there to keep that reading alive. Once what is left became a chip of its
    own, `bob 5%` stopped answering anything anybody asks of a folder —
    *who is in here* and *what is left* are the questions, and neither of them
    is a percentage. It cost horizontal room on the one screen with none to
    spare, which is how it was noticed.

    The counts stay in the tooltip, where they cost nothing.

    **All of them, and the card grows.** The first version named three and
    finished with `+4`, which is the one thing a summary must not do: it says
    there is something else in there and refuses to say what, so the card
    stops being an answer and becomes a reason to open the folder — which
    is the errand it exists to save. A taller card is cheaper than that, and
    the ones with most in them are the ones worth reading.

    **One run, not three.** Each kind used to be its own flex box, so each
    started a fresh line however few chips were in it — three rows to say
    *family, Mom, beach*. They carry their own colour now, which is what lets
    them share one wrapping line and still be told apart: the order groups
    them by kind and the hue says which kind without a break to mark it.
    """
    if not n or not (values or lead):
        return ""
    # What is *not* decided, wearing the same clothes as what is. It was a
    # sentence of its own under the bar — *1 undecided* — which made the
    # one negative fact on the card the only one shaped differently from all
    # the positive ones. And *all decided* said nothing at all: a folder with
    # nothing left says it by having no such chip, the way a folder with no
    # tags says that by having no tags.
    col = _SPREAD_FILTER.get(kind, "")

    def chip(value: str, count: int, cls: str = "", filter_on: str = "") -> str:
        # Opening the folder *and* narrowing it to this, which is the one
        # thing the card could say and the page could not then do. A chip
        # reading `undecided` is the part of an event still to work through,
        # and clicking it is how you get to exactly those.
        where = (f' data-col="{col}" data-val="{_h(filter_on or value)}"'
                 if col else "")
        # **What kind, and how many.** It used to repeat the value, which is
        # the word already under the pointer, and then explain the click,
        # which the cursor and the hover already offer. What it could not say
        # without being asked is which of the four kinds this is — the
        # colour says that, once you know the colours.
        what = "No access yet" if cls == "none" else _SPREAD_LABEL.get(kind, kind)
        files = f"{count:,} file" + ("" if count == 1 else "s")
        return (f'<i class="{cls or kind}"{where} '
                f'title="{what} — {files}">' + _h(value) + "</i>")

    # `undecided` is not a value anything carries, it is the absence of one —
    # and the audience filter has a word for that, the same one its own chip
    # uses.
    first = ("" if lead is None or not lead[1] else
             chip(lead[0], lead[1], "none", ix.UNREVIEWED))
    return first + "".join(chip(v, c) for v, c in values)


def _spread_row(chips: str) -> str:
    """All of a card's chips, in one run that wraps where it runs out."""
    return f'<span class="spread">{chips}</span>' if chips else ""


def _folder(row: sqlite3.Row, groups: list[str], view: ix.Filters,
            user: Principal,
            whole: dict[object, tuple[int, object, object]] | None = None,
            spread: dict[str, dict[tuple[object, ...], list[tuple[str, int]]]]
            | None = None) -> str:
    """One section of the grid, drawn as what is worth knowing about it.

    Not a photograph. A cover was whichever file happened to be first, which
    told you what one picture in there looks like and nothing about the
    section — and a wall of unrelated pictures is harder to read than a wall
    of text, not easier. What you want before opening a folder is what is in
    it: how much, when, and how much of it you have not dealt with.
    """
    # Only what this card *is*. Which year it falls in is the heading above
    # it, and a card repeating its own shelf's name is a card saying one thing
    # and looking like it says two.
    last = len(groups) - 1
    name = (_group_label(row[f"grp{last}"], groups[last], groups[:last], row)
            if groups else "Everything")
    href = _drill(row, groups, view)
    n = int(row["n"])
    # An event that runs from February into March is a card under each, and a
    # folder saying *312 files* with nothing to say it is part of eleven
    # hundred is a folder describing the grouping rather than the library.
    # The count says both, which is also the shortest way to say it is split.
    outer = (whole or {}).get(row[f"grp{last}"]) if groups else None
    entire = outer[0] if outer else n
    videos = int(row["videos"] or 0)
    left = int(row["unreviewed"] or 0) if user.is_admin else 0
    # Not under a heading that already says it: grouped by day, the name *is*
    # the date, and printing it twice is a card that looks like it is telling
    # you two things.
    dated = not (groups and groups[-1] == "day")
    when = _span(row["first_seen"], row["last_seen"]) if dated else ""
    # **And the dates say it too.** The count already said *312 of 1,100*, so
    # a card that was a fortnight of a three-week event said so about its
    # files and not about its days — two facts about the same split, one of
    # them told and one of them not. Same shape as the count, so the two read
    # as one sentence about one thing.
    across = _span(outer[1], outer[2]) if (outer and dated) else ""
    if across and across != when:
        when = f'{when} <i>of</i> {across}'
        split_when = " split"
    else:
        split_when = ""
    inner = (
        f'<b class="name">{_h(name)}</b>'
        # `when` may already carry its own markup, so it is not escaped
        # again here; every value inside it came through `_span`, which builds
        # from parsed dates and never from anything a person typed.
        + (f'<span class="when{split_when}">{when}</span>' if when else
           '<span class="when dim">no dates</span>' if dated else "")
        + ('<span class="n split" title="Split by the grouping above it — '
           f'{entire:,} files in all">{n:,} <i>of</i> {entire:,} files'
           if entire > n else
           f'<span class="n">{n:,} file{"" if n == 1 else "s"}')
        + (f'<i class="kinds">{videos:,} video{"" if videos == 1 else "s"}</i>'
           if videos else "")
        + "</span>"
        # Two readings in one bar, because they nest: the whole track is the
        # group this card is part of, the filled part is how much of it is
        # here, and inside that sits how much of *this* has been decided. A
        # card holding all of an event is a full bar; one holding a fortnight
        # of it is a quarter of one, and the green grows inside either as the
        # work gets done.
        # What is *in* it, not just how much of it is done. A thumbnail says
        # which tags and which audience it carries; this is the same sentence
        # for a folder, with the share that carries each — and what is
        # undecided is the first of them rather than a line of its own.
        + _spread_row(
            "".join(
                _spread_chips(kind, (spread or {}).get(kind, {}).get(
                    tuple(row[f"grp{i}"] for i in range(len(groups))), []), n,
                    lead=("undecided", left) if kind == "audience" else None)
                for kind in (("audience", "people", "tags") if user.is_admin
                             else ("people", "tags")))))
    if href is None:
        # Nothing to link to, rather than a link somewhere else. *No day* is
        # every file whose date stops at the month, and there is no filter that
        # says so — offering the month itself would open a folder holding files
        # this one does not.
        return (f'<div class="tile dead" title="There is no filter for this '
                f'one, so it cannot be opened on its own">{inner}</div>')
    # The same circle the thumbnails carry, for the same gesture one zoom
    # out: a folder is a set of files, and a decision about it is a decision
    # about them. Only on a folder that can be opened — one with no address
    # has no set to name either, so there is nothing to select.
    return (f'<a class="tile" href="{_h(href)}">'
            f'<button class="pick" aria-label="Select this folder"></button>'
            f'{inner}</a>')


def _span(first: object, last: object) -> str:
    """When a section happened, in as few words as it takes to say it.

    Written the way a person would: one day is a day, a fortnight in one month
    drops the month from the first end, and a year that appears twice is
    printed once.
    """
    start = datestr.parse_pix(str(first)) if first else None
    end = datestr.parse_pix(str(last)) if last else None
    if start is None or end is None:
        return ""
    if start.date() == end.date():
        return f"{start.day} {start:%B %Y}"
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day} – {end.day} {end:%B %Y}"
    if start.year == end.year:
        return f"{start.day} {start:%b} – {end.day} {end:%b %Y}"
    return f"{start.day} {start:%b %Y} – {end.day} {end:%b %Y}"


def _imprecise(row: sqlite3.Row, name: str) -> str | None:
    """The date filter holding exactly a section with no `name` key, or None.

    Read off how precisely the section's files are dated: all undated is
    `undated`; all dated to one year, or one month, and no finer is
    `2025-*` or `2025-08-*`. A mix of the two has no filter.
    """
    if "pmin" not in row.keys():
        # A section without the reading: only a year nobody knows can still
        # be answered, because that is undated by definition.
        return ix.UNDATED if name == "year" else None
    low, high = row["pmin"], row["pmax"]
    if low is None or low != high:
        return None
    width = int(low)
    if width == datestr.NOTHING:
        return ix.UNDATED
    first, last = str(row["elo"] or ""), str(row["ehi"] or "")
    if width not in (datestr.YEAR, datestr.MONTH) or not first \
            or first[:width] != last[:width]:
        return None
    return f"{first[:width]}-*"


def _drill(row: sqlite3.Row, groups: list[str],
           view: ix.Filters) -> str | None:
    """Where one folder leads: this view, plus what the folder is.

    `None` where the section cannot be said as a filter. *No day* and *no
    month* mean *dated less precisely than that*, and they open as the one
    date filter that says so (`_imprecise`) — or, where their files are dated
    to different widths, not at all. Sending those to the year would open a
    folder with more in it than the one that was clicked, which is worse than
    a folder that does not open.
    """
    patch: dict[str, str | list[str] | None] = {}
    for i, name in enumerate(groups):
        key = row[f"grp{i}"]
        column = _DRILL.get(name)
        if column is None:
            return None
        if key is None:
            # No year, no month or no day: the files are dated less
            # precisely than this level cuts. That is one filter when they
            # are all dated alike — `undated`, or `2025-*` for the year and
            # no month — and otherwise none, because a folder that opened
            # onto more than it counted would be worse than one that does
            # not open.
            exact = _imprecise(row, name)
            if exact is None:
                return None
            patch["date"] = exact
            continue
        patch[column] = str(key)
        # A type folder is every box its kind is made of.
        if name == "kind":
            patch[column] = list(_KIND_HALVES.get(str(key), (str(key),)))
        # A folder of an event *itself*, made beside one folder per part of
        # it: the files directly in the event and no others. Asking for the
        # event plainly would open the whole trip, which is more than the
        # folder counted — the one place a folder's link and a folder's count
        # could disagree about what is in it.
        if (name == "subevent" and key != ix.NO_EVENT
                and decisions.EVENT_SEP not in str(key)):
            patch[column] = f"{key}{decisions.EVENT_SEP}{ix.NO_EVENT}"
    return _browse_url(view, patch)


def _stack_url(key: str, back: str = "") -> str:
    """One stack's page, and the way out of it.

    `back` is the address it was opened from, carried rather than guessed.
    The browser's own history would do it on the way out and does not on the
    way in: a stack reached from a bookmark, a shared link or the operation
    log has nothing behind it, and *leave this stack* still has to mean
    something. It is also what makes the way out survive a write — going back
    to a page that has just stopped being true is how somebody ends up
    reloading by hand.
    """
    folder, _, name = key.partition("/")
    out = f"/stack/{_q(folder)}/{_q(name)}"
    return out + (f"?back={_q(back)}" if back else "")


def _browse_url(view: ix.Filters,
                patch: dict[str, str | list[str] | None]) -> str:
    """The grid, at this view plus `patch`. The page's own `url()` in Python."""
    query = {**_view_dict(view), **patch}
    # Joined with a bare `&`: this is a URL, and the one place it becomes
    # markup escapes it. Building it pre-escaped produced `&amp;amp;` and a
    # link that carried its second filter as part of the first one's value.
    pairs = [(k, x) for k, v in query.items() if v
             for x in (v if isinstance(v, list) else [v])]
    return "/browse" + (
        "?" + "&".join(f"{k}={_q(str(x))}" for k, x in pairs) if pairs else "")


def _totals(conn: sqlite3.Connection, view: ix.Filters,
            groups: list[str]) -> dict[tuple[object, ...], int]:
    """Every section's whole count, by its key — for headings that say how
    many are in the section, not how many of them have arrived."""
    if not groups:
        return {}
    n = len(groups)
    return {tuple(r[f"grp{i}"] for i in range(n)): int(r["n"])
            for r in ix.sections(conn, view, groups=groups, limit=100_000)}


@app.get("/event/{event}", response_class=HTMLResponse)
def event_grid(event: str) -> RedirectResponse:
    """Kept so older links still land somewhere — an event is just a filter now."""
    return RedirectResponse(f"/browse?event={_q(event)}", status_code=307)


def _safe_back(back: str) -> str:
    """Where to return to, or the front door.

    Only a path on this app. `back` arrives in a query string, which is a
    place anybody can type anything, and a bare `href` built from one is how a
    link on a page people trust sends them somewhere else entirely.
    """
    if back.startswith("/") and not back.startswith("//") and ":" not in back:
        return back
    return "/"


def _stack_actions(user: Principal, decided: bool) -> str:
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
    {_act("nostack", "Not a stack", user=user)}
    {_act("unstack", "Take out", user=user) if decided else ""}
    {_act("download", "Download", user=user)}
    <span class="sep"></span>
    {_act("delete", "Delete", "danger", user=user)}
  </span>
  <span class="grp" data-side="gone" hidden>
    {_act("restore", "Restore", user=user)}
    {_act("purge", "Purge&hellip;", "danger", user=user)}
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
    where = _safe_back(back)
    when = _stack_when(lead)
    return _page("pix2 stack", f"""<div class="grid" id="grid">
<div class="cells">{"".join(_cell(r, view) for r in rows)}</div></div>
<div id="viewer">
  <div class="stage"><img id="vimg">
  <video id="vvid" controls playsinline></video>
  <div class="meta" id="vmeta"></div></div>
  <button id="viewclose" title="Close (Esc)">&times;</button>
  <button id="railtoggle" title="Details (I)">Details</button>
  {_viewacts(user, stack=True)}
  <aside id="rail"></aside>
</div>
<div id="menu" hidden></div>
{_WORKING}""",
        # What this is, in words, where the filters would have been. A page
        # with no address on it is a page you cannot tell from the one before.
        tools=(f'<span class="whatis">'
               f'<b>{len(rows)}</b> of one photograph'
               + (f'<i class="asked">the app\'s guess</i>' if guess else "")
               + (f'<span class="dim">{_h(when)}</span>' if when else "")
               + f'</span><a class="leave" href="{_h(where)}">'
                 f'Leave this stack</a>'),
        right=_display_rows(user),
        bar=_display_menu(user),
        rows=_stack_actions(user, decided),
        script=_view_script(user, view, [], stack=key, back=where),
        info=f'<span class="line">{len(rows)} files</span>',
        user=user)


def _stack_when(row: sqlite3.Row) -> str:
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
            _stack_url(within, _browse_url(
                view, {"group": ",".join(_groupings(group)) or "none"})),
            status_code=307)
    conn = db()
    groups = _groupings(group)
    rows = ix.files(conn, view, groups=groups, limit=FIRST_PAGE)
    total = ix.count(conn, view)

    cells = _sections(rows, groups, view, _totals(conn, view, groups), total)
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
  {_viewacts(user)}
  <aside id="rail"></aside>
</div>
<div id="menu" hidden></div>
{_WORKING}""",
        tools=('<div class="chips" id="chips"></div>'
               + _from_link(op, stale, len(rows))
               + _cuts_link(view, total)),
        # Away from the filters, at the end of the row with the account. It is
        # a view control rather than a filter — nothing it does changes which
        # photographs are here — and standing among the chips it read as one
        # more thing narrowing the library.
        #
        # Twice, and the width picks: a labelled row inside the menu, where
        # there is no room for it in the bar, and the bare control in the bar
        # where there is. See `_sizeset`.
        right=_display_rows(user),
        bar=_display_menu(user),
        rows=_actions(user),
        # Up a zoom: the same query, minus the stack. A folder view of one
        # stack is the stack, so the only thing the coarser view can say about
        # `within` is nothing — and leaving a stack is its own gesture, on the
        # bar, rather than a side effect of changing how you are reading.
        zoom=_zoom("/", request.url.query),
        script=_view_script(user, view, groups),
        # No instructions. A standing sentence about clicking and holding is
        # read once, on the first visit, and then occupies a fixed strip at the
        # bottom of every screen for as long as the app exists — which on a
        # phone was three lines of it. The gestures are the ordinary ones; the
        # footer is for what this page is *now*, which is the count and
        # whatever the last write had to say.
        info=(f'<span class="line" id="count">{shown}</span>'
              f'<span class="line">indexed {_age(ix.built_at(conn))}</span>'),
        user=user)


def _view_script(user: Principal, view: ix.Filters, groups: list[str], *,
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
    return (
        f"<script>const VIEW={_js(_view_dict(view))},"
        f"CHIPS={_js(_chips(user))},FIXED={_js(_FIXED)},"
        f"FIXED_GROUPS={_js(_FIXED_GROUPS)},"
        f"EXTRA={_js(_EXTRA)},ADMIN={_js(user.is_admin)},"
        f"USERS={_js(_audience_names())},GROUPS={_js(_group_names())},"
        f"MARKS={_js({c: _mark(c) for c, _ in _chips(user)})},"
        # The word the audience filter uses for *nobody yet*. Sent rather than
        # spelled again here: the page draws a chip that sets it, and two
        # copies of a sentinel are two chances to disagree about what it is.
        f"UNREVIEWED={_js(ix.UNREVIEWED)},"
        f"ARCHIVED={_js(decisions.ARCHIVED)},ARCHIVED_LABEL={_js(_ARCHIVED_LABEL)},"
        f"EVENT_SEP={_js(decisions.EVENT_SEP)},"
        f"NO_EVENT={_js(ix.NO_EVENT)},"
        f"USUAL={_js(store().usual)},PAGE={_js(page)},"
        # Which stack's page this is, and the way out of it. Empty everywhere
        # else, which is how the script knows it is not on one — the grid asks
        # the same question of a shelf of stacks and answers it in place.
        f"STACK={_js(stack)},BACK={_js(back)},"
        f"TIERS={_js(_TIERS)},"
        f"GRID_GROUPS={_js(_GRID_GROUPS)},ONE_FIELD={_js(_ONE_FIELD)},"
        f"GROUPING={_js(groups)};</script>"
        f"<script>{_BROWSE_JS}</script>")


def _from_link(op_id: str | None, stale: str | None, shown: int) -> str:
    """Why this grid is showing a particular set of files, and the way out.

    Not a chip: a chip is a value picked from a list of values, and there is no
    list of operations to pick from — you arrive here from the log. But it has
    to say what it is and be dismissable for the same reason the chips do,
    because a filter you cannot see is a library that looks smaller than it is.
    """
    if not op_id:
        return ""
    op = history.get(op_id)
    what = (_h(op.summary) if op is not None else "an edit that is no longer "
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
def _cuts_link(view: ix.Filters, shown: int) -> str:
    """What the grid is showing when an original's pill opened it, and the
    way back out — said like an operation, for the same reason: there is no
    list of originals to pick one from."""
    if not view.cuts:
        return ""
    name = view.cuts.partition("/")[2]
    return (f'<span class="chip on from-op">cut from <b>{_h(name)}</b>'
            f'<span class="val">{shown:,}</span>'
            f'<a class="x" href="{_h(_browse_url(view, {}))}" '
            f'title="Back to the library">&times;</a></span>')


def _cuts_badge(row: sqlite3.Row) -> str:
    """*5 clips · 1 still* on a video that has them, opening just those.

    Beside the stack badge, because it is the same kind of fact: there are
    more of these, somewhere else, and this is the way to them. Only what
    this viewer would see when it opens (`ix._cuts_cols`).
    """
    keys = row.keys()
    clips_n = int(row["cut_clips"] or 0) if "cut_clips" in keys else 0
    stills_n = int(row["cut_stills"] or 0) if "cut_stills" in keys else 0
    if not clips_n and not stills_n:
        return ""
    words = [f"{n} {word}{'' if n == 1 else 's'}"
             for n, word in ((clips_n, "clip"), (stills_n, "still")) if n]
    key = f'{row["folder"]}/{row["name"]}'
    href = f"/browse?cuts={_q(key)}&group=none"
    return (f'<a class="cuts" href="{_h(href)}" '
            f'title="The clips and stills cut from this video">'
            f'{_h(" · ".join(words))}</a>')


def _writable(user: Principal, change: _Change) -> None:
    """Refuse a decision that touches a field this person may not write.

    **Checked here rather than trusted from the page.** The bar renders only
    the actions `may` allows, but a bar is a suggestion and this is the rule:
    a household member posting `audience` directly gets a 403 whatever their
    browser was showing them.
    """
    if user.is_admin:
        return
    sent = set(_recorded(change))
    # Half-name writes are not in the log's vocabulary — it records the name
    # each file ended up with, not the half that was asked for — so they have
    # to be named here or they would reach the archive unchecked. A field this
    # cannot see is a field nobody is refusing.
    if not isinstance(change.event_head, decisions.Unset):
        sent.add("event")
    if not isinstance(change.event_leaf, decisions.Unset):
        sent.add("event")
    # `add_tags` and the rest name the field they edit; one prefix strip puts
    # them back with it rather than needing their own list.
    touched = {f.removeprefix("add_").removeprefix("remove_") for f in sent}
    refused = sorted(touched - _HOUSEHOLD_FIELDS)
    if refused:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"not yours to change: {', '.join(refused)}")


#: The takeover, which any page that writes has to carry.
#:
#: A write is a run of requests over SMB and takes seconds; without this the
#: screen simply sits there. It lived in the grid's markup alone, which was
#: true for exactly as long as the grid was the only page that wrote — and
#: when the landing page learned to, every `if(working)` guard in the script
#: quietly did nothing and a folder edit ran with no sign of it at all.
_WORKING: str = """<div id="working">
  <div class="what" id="workwhat"></div>
  <div class="bar"><i id="workbar"></i></div>
  <div class="tally" id="worktally"></div>
  <button id="worksave" class="primary" hidden>Save</button>
  <button id="workstop">Stop</button>
</div>"""


def _actions(user: Principal, *, folders: bool = False) -> str:
    """The edit bar — as much of it as this person gets.

    **The family curates** (§8): tagging and naming are the whole point of the
    app, and the whole bar being an administrator's meant a household member
    logged in, saw a circle on every thumbnail, selected things, and watched
    nothing happen. Which of the two readings that is — *no permission* or
    *broken* — was not on screen anywhere.

    What they do not get is `access` and `purge`, and the reason is the same
    for both: they are the two that somebody else noticing cannot undo. Access
    is the only control that can show a photograph to a person who should not
    see it, and purge ends the file. Delete is theirs, because it is soft, and
    restore is not, because `/history` is not — see `HOUSEHOLD`.

    Not merely hidden: the endpoints refuse a field this person may not write,
    whatever their browser was showing them. This is so the page does not
    offer a control that would fail, which reads as brokenness rather than as
    policy — and so that hiding the control is never what is holding the line.

    **Two sets, shown by what is selected rather than by what is filtered.**
    A deleted file cannot be deleted again and a living one cannot be
    restored, so offering either would be offering a button that does nothing.
    Which of them is on screen follows the selection, not the `deleted` chip:
    with *Including deleted* on, a selection can hold both kinds, and then
    both sets are offered and each acts only on the files it means.

    The row itself is always here. It carries the count and the tick, so it
    has something to say with nothing selected — and a row that came and went
    would move the whole grid under the pointer on the first click.

    **One rule separates them, not several.** What a file *is* — its event, its
    tags, its date, who may see it, and which of them speaks for the rest —
    runs together in the order those questions get asked. What happens *to* it
    is the cluster after the bar. Bars between every pair said there were four
    groups when there are two.

    The stack actions join the first cluster rather than earning a bar of their
    own, and not only for tidiness: they are usually hidden, and a separator is
    not, so a bar around them would hang there beside nothing.

    **A folder bar is four of these and no more.** One zoom out, the selection
    is sets rather than photographs, and the question each control asks has to
    survive that. *Event*, *Tags*, *Access* and *Download* are statements about
    a set and read the same either way — naming a run of days as one event
    is the gesture the whole backlog turns on (§8). The four stack actions
    are not: every one of them needs *a photograph* — which of these takes
    speaks for the others, whether they are one moment — and across an
    event there is no such question to ask.
    """
    if folders:
        return f"""<div class="row" id="actions">
  <button id="selall" class="tick" title="Select all"
          aria-label="Select all"></button>
  <span class="count" id="selcount" style="margin:0"></span>
  <span class="grp" data-side="live" hidden>
    {_act("event", "Event&hellip;", user=user)}
    {_act("tags", "Tags&hellip;", user=user)}
    {_act("access", "Access&hellip;", user=user)}
    {_act("download", "Download", user=user)}
  </span>
</div>"""
    return f"""<div class="row" id="actions">
  <button id="selall" class="tick" title="Select all" aria-label="Select all"></button>
  <span class="count" id="selcount" style="margin:0"></span>
  <span class="grp" data-side="live" hidden>
    {_act("event", "Event&hellip;", user=user)}
    {_act("tags", "Tags&hellip;", user=user)}
    {_act("people", "People&hellip;", user=user)}
    {_act("date", "Date&hellip;", user=user)}
    {_act("access", "Access&hellip;", user=user)}
    {_act("stack", "Stack", user=user)}
    {_act("top", "Make top", user=user)}
    {_act("unstack", "Unstack", user=user)}
    {_act("nostack", "Not a stack", user=user)}
    {_act("splice", "Splice", user=user)}
    {_act("download", "Download", user=user)}
    <span class="sep"></span>
    {_act("delete", "Delete", "danger", user=user)}
  </span>
  <span class="grp" data-side="gone" hidden>
    {_act("restore", "Restore", user=user)}
    {_act("purge", "Purge&hellip;", "danger", user=user)}
  </span>
  <span class="grp" data-side="choose" hidden>
    <b>Click the one to show</b>
    <button id="choosecancel">Cancel</button>
  </span>
</div>"""


def _chips_html(cls: str, values: list[str]) -> str:
    """One chip per value, each truncated with the whole thing as a tooltip.

    Individually rather than as a run of text, because a thumbnail is 150px
    and three role names are not: a single line just gets cut off mid-word
    with no way to find out what it said. The container carries the full list
    too, for when there are more chips than fit.
    """
    if not values:
        return ""
    chips = "".join(f'<i title="{_h(v)}">{_h(v)}</i>' for v in values)
    return f'<span class="{cls}" title="{_h(", ".join(values))}">{chips}</span>'


def _sections(rows: list[sqlite3.Row], groups: list[str],
              view: ix.Filters | None = None,
              totals: dict[tuple[object, ...], int] | None = None,
              total: int | None = None) -> str:
    """The cells, with one heading wherever the section changes.

    **One heading, not one per level.** Nested headings meant an indent for
    every level and a row of chrome for each, and the deeper ones said less and
    less. A section's identity is the whole path — *2026 › July › Sports Day* —
    so the heading says that, once, and the levels in it are the controls.

    Headings are grid items spanning every column, so one flow holds headings
    and thumbnails; arrow-key movement walks straight through rather than having
    to know the sections are there.

    There is always at least one heading, even ungrouped: the heading *is* the
    control, so a grid without one would offer no way to start.
    """
    said = "subevent" in groups
    if not groups:
        return _section(_heading([], 0, total if total is not None
                                 else len(rows)),
                        "".join(_cell(r, view, groups=groups) for r in rows),
                        key=())

    out: list[str] = []
    for keys, run in groupby(rows, key=lambda r: tuple(
            r[f"grp{i}"] for i in range(len(groups)))):
        batch = list(run)
        labels = [_group_label(k, g, groups[:i], batch[0])
                  for i, (k, g) in enumerate(zip(keys, groups))]
        # The whole section's count, not the part of it on this page: the
        # grid arrives a page at a time, and a heading saying 240 over a day
        # of 1,100 photographs would be describing the page, not the day.
        out.append(_section(
            _heading(labels, len(groups),
                     (totals or {}).get(keys, len(batch))),
            "".join(_cell(r, view, said=said, groups=groups)
                    for r in batch), key=keys))
    return "".join(out)


def _section(heading: str, items: str,
             key: tuple[object, ...] = ()) -> str:
    """One heading and everything under it, in a box of their own.

    The heading used to be a grid item spanning every column, and the cells
    its siblings — one flow, which is tidier markup and is why it was
    written that way. It cannot stick, though: a grid item is confined to its
    own grid area for the purpose of `position: sticky`, and a heading's grid
    area is the single row it sits in, so it has nowhere to travel.

    Given a box it has the section to travel through, and the next heading
    arrives and pushes it off — which is what *stays at the top while you are
    in it* means, done by the browser rather than by a scroll handler.

    Nothing else about the page moved. The columns still line up across
    sections, because every `.cells` resolves the same `auto-fill` over the
    same width; a heading already started a new row, because it spanned them
    all; and the cells are still in document order, so arrow-key movement
    walks straight through the sections without knowing they are there.
    """
    # The section's key, so a page arriving later can tell whether it
    # carries on the section already at the bottom of the grid or starts the
    # next one — the same day must not get a second heading.
    return (f'<section class="sect" data-key="{_h(_js(list(key)))}">'
            f'{heading}<div class="cells">{items}</div></section>')


def _heading(labels: list[str], levels: int, count: int, *,
             pick: bool = True, cut: bool = False) -> str:
    """One section heading, which is also how grouping is changed.

    Each crumb is two controls: the name changes that level, the `Ã—` drops it.
    Removal lives here rather than inside the menu because *take this away* is
    a thing you should be able to see, not something to go and find.
    """
    if not labels:
        crumbs = ('<span class="crumb" data-level="0">'
                  '<button class="grpname">Ungrouped</button></span>')
    else:
        crumbs = '<span class="crumbsep">&rsaquo;</span>'.join(
            f'<span class="crumb" data-level="{i}">'
            f'<button class="grpname">{_h(label)}</button>'
            f'<button class="rmgrp" title="Remove this grouping">&times;</button>'
            f'</span>'
            for i, label in enumerate(labels))
    # On a shelf the last crumb names the *cut* — "By event" — and it is the
    # same two words over every shelf on the page. What says where you are is
    # the value in front of it, so the emphasis runs the other way round.
    shelf = " shelf" if cut else ""
    add = ("" if levels >= 3 else
           '<button class="addgrp" title="Add a grouping inside this one">'
           "+</button>")
    return (f'<h3 class="group{shelf}">'
            + ('<button class="grppick" title="Select this group" '
               'aria-label="Select this group"></button>'
               if pick else "")
            + f'<span class="crumbs">{crumbs}</span>{add}'
              f'<span class="dim">{count:,}</span>'
              f'</h3>')


def _group_label(key: object, group: str, outer: Sequence[str] = (),
                 first: sqlite3.Row | None = None) -> str:
    """A heading a person reads, not a sort key.

    `outer` is the coarser levels already shown to the left, so a crumb does
    not repeat what the path has said: under *2025*, the month is **January**
    rather than *January 2025*, and under that the day is **Saturday 4**.

    `first` is the section's first file, for the one grouping whose key is not
    something anybody would want to read. A stack is identified by the
    photograph that speaks for it, and a generated name carries the date, the
    camera and the extension — so the heading was 40 characters of machinery
    where the useful part is *when*.
    """
    if key is None or key == "":
        # Named for what is missing rather than for the date as a whole: a file
        # dated to its month lands here when the grid is cut by day, and it is
        # not undated â€” it has no *day*. Calling that "No date" would deny what
        # is actually known about it.
        return {"day": "No day", "month": "No month", "year": "No date",
                "stack": "Not in a stack"}.get(group, "None")
    text = str(key)
    if group == "stack":
        # The moment, because that is what a stack is: one photograph taken
        # several times. The name of the file that speaks for it is machinery.
        moment = (datestr.parse_pix(str(first["effective_date"]))
                  if first is not None and first["precision"] >= datestr.FULL
                  else None)
        if moment is None:
            # No usable clock, so the only honest label left is the file that
            # speaks for the section. The folder is the path the heading
            # already sits in, and repeating it would push the one part that
            # differs off the end of the line.
            return text.rpartition("/")[2]
        if "day" in outer:
            return f"{moment:%H:%M}"
        return (f"{moment:%A} {moment.day} {moment:%B %Y}, "
                f"{moment:%H:%M}")
    if group == "day":
        moment = datestr.parse_pix(text + "-00:00:00")
        if not moment:
            return text
        # Composed rather than one strftime: `%-d` drops the leading zero on
        # Linux and is simply invalid on Windows, and this runs on both.
        if "month" in outer:
            return f"{moment:%A} {moment.day}"
        if "year" in outer:
            return f"{moment:%A} {moment.day} {moment:%B}"
        return f"{moment:%A} {moment.day} {moment:%B %Y}"
    if group == "month":
        moment = datestr.parse_pix(text + "-01-00:00:00")
        if not moment:
            return text
        return moment.strftime("%B" if "year" in outer else "%B %Y")
    return text


def _access_html(shared: list[str]) -> str:
    """What a thumbnail says about who can see it.

    Three states, and only two of them are visible:

    - **nobody** â€” a small mark, because that is the work still to do;
    - **the usual audience, exactly** â€” nothing at all. If nine files in ten
      say `family`, printing `family` on nine thumbnails in ten is noise
      that tells you nothing you did not already assume;
    - **anything else** â€” named, because that is the exception and the whole
      reason to look.

    The usual audience is a setting rather than a hard-coded name: which one
    is usual is a fact about a household, not about the software.
    """
    if not shared:
        return ('<span class="unshared" title="Nobody has access yet">'
                '</span>')
    usual = _usual()
    unusual = [a for a in shared if a != usual]
    return _chips_html("who", unusual)


def _people_html(people: list[str], view: ix.Filters) -> str:
    """Who is **in** the photograph, on the thumbnail.

    It was the one fact a cell carried and never showed. The name was in
    `data-people` for the script, on the folder card, in the viewer rail, in
    its own filter and its own bulk action — everywhere except the thing you
    are actually looking at while you decide.

    Never collapsed into access, and drawn on its own line above it. *Pictures
    of Mum* and *pictures Mum may see* are opposite questions that take the
    same kind of word, and on a 150px tile the colour should not have to carry
    the whole difference.

    **The name the view is already filtered to is left off.** Filtered to
    `person:Ana` every thumbnail on screen says Ana, and a chip that is true
    of everything is furniture — the same rule `_access_html` applies to the
    usual audience and `_part_html` to an event the heading already names.
    """
    return _chips_html("folk", [p for p in people if p != view.person])


def _part_html(row: sqlite3.Row, view: ix.Filters, said: bool) -> str:
    """Which part of its event this file is, on the thumbnail.

    Not where the view has already said it: grouped by event and sub-event
    every heading names one, and filtered to a whole name every thumbnail
    under it carries the same one. A chip that is true of everything on
    screen is furniture.

    Grouping by *event* is not that — it names the trip, and which part of
    the trip is exactly what still differs from cell to cell.
    """
    whole = row["event"]
    leaf = decisions.split_event(whole)[1]
    if said or not leaf or view.event == whole:
        return ""
    return (f'<span class="part" title="Sub-event &mdash; {_h(str(whole))}">'
            f'{_h(leaf)}</span>')


def _cell(row: sqlite3.Row, view: ix.Filters | None = None, *,
          said: bool = False, groups: Sequence[str] = ()) -> str:
    mark = _stack_badge(row, view or ix.Filters(), groups)
    tags = _split(row["tags"])
    shared = _split(row["audience"])
    # Newline-joined, matching what the client splits on. A stray control byte
    # had crept in here from a shell heredoc, so multiple tags arrived at the
    # page as one unsplittable blob.
    nl = chr(10)
    return (
        f'<div class="cell{" gone" if row["deleted"] else ""}'
        f'{" marked" if mark else ""}" '
        f'data-folder="{_h(row["folder"])}" '
        f'data-name="{_h(row["name"])}" data-kind="{_h(row["kind"])}" '
        f'data-audience="{_h(nl.join(shared))}" '
        f'data-event="{_h(row["event"] or "")}" '
        f'data-tags="{_h(nl.join(tags))}" '
        f'data-people="{_h(nl.join(_split(row["people"])))}" '
        f'data-date="{_h(str(row["effective_date"] or "no date"))}" '
        f'data-deleted="{"1" if row["deleted"] else ""}" '
        f'data-under="{_h(row["stacked_under"] or "")}" '
        # What it is only *proposed* to be behind, which is a different fact
        # and was missing: opened, a guessed stack listed its members through
        # this column and the page could not see it, so nothing in there was
        # in a stack as far as the bar was concerned and there was no way to
        # say which of them should be the one that shows.
        #
        # Only while the view folds guesses, the same rule the count follows:
        # with them off these are ordinary photographs sitting in the grid on
        # their own, and nothing is behind anything.
        f'data-proposed-under="'
        f'{_h(row["suggested_under"] or "") if (view or ix.Filters()).folds_guesses else ""}" '
        f'data-ar="{_squareness(row)}" '
        f'data-clip="{_clip_attr(row)}" '
        f'data-splice="{_h(_splices(row))}" '
        f'data-clips="{row["clips"] if "clips" in row.keys() else 0}" '
        f'data-copy="{"1" if _has_render(row) else ""}" '
        f'data-behind="{row["behind"] or 0}" '
        f'data-proposed="{_guessed(row, view or ix.Filters())}">'
        f'<img loading="lazy" src="/thumb/{_q(row["folder"])}/{_q(row["name"])}">'
        # Two lanes, each a run of optional facts that wraps from its own
        # edge inward, and the right-hand end of each held by what is always
        # there. Drawn in the default arrangement (`_INFO`); the page script
        # moves each fact to the lane the viewer chose (`placeInfo`).
        + _lane("top", _chips_html("tags", tags),
                _cuts_badge(row) + _clip_badge(row, view or ix.Filters())
                + mark
                + '<button class="pick" aria-label="select"></button>')
        + _lane("bot",
                _people_html(_split(row["people"]), view or ix.Filters())
                + _access_html(shared)
                + _part_html(row, view or ix.Filters(), said),
                f'<span class="badge">{_dur(row["duration"])}</span>'
                if row["kind"] == "video" else "")
        + "</div>"
    )


def _lane(where: str, flow: str, fixed: str) -> str:
    """One edge of a thumbnail: the facts that flow, then the ones that stay."""
    return (f'<div class="ov {where}"><span class="lane">{flow}</span>'
            f'<span class="fix">{fixed}</span></div>')


def _clip_attr(row: sqlite3.Row) -> str:
    """A clip's range as the page reads it — `start,end` in seconds — or
    nothing for a file that is not a clip."""
    if "clip_of" not in row.keys() or row["clip_of"] is None:
        return ""
    # With a file of its own it plays as itself, from its first frame — a
    # range applied to that would seek past what it is.
    if row["size"] is not None:
        return ""
    return f'{float(row["clip_in"] or 0):g},{float(row["clip_out"] or 0):g}'


def _splices(row: sqlite3.Row) -> str:
    """The video *Splice* on this cell opens — its own name, or for a clip
    its source's — or nothing where there is nothing to cut."""
    of = row["clip_of"] if "clip_of" in row.keys() else None
    if of is not None:
        return str(of)
    name = str(row["name"])
    return name if clips.can_splice(name, str(row["kind"])) is None else ""


def _clip_badge(row: sqlite3.Row, view: ix.Filters) -> str:
    """Says that this was cut from a video, and which kind of cut — to
    someone who may see that video, and to nobody else (`ix._source_seen`).
    To anyone else a clip is simply a video."""
    if "clip_of" not in row.keys() or row["clip_of"] is None:
        return ""
    if view.viewer is not None and not (
            "source_seen" in row.keys() and row["source_seen"]):
        return ""
    still = row["clip_in"] == row["clip_out"]
    word = "Still" if still else "Clip"
    return (f'<span class="clip-mark" title="{word} from '
            f'{_h(row["clip_of"])}">{word}</span>')


def _stack_badge(row: sqlite3.Row, view: ix.Filters,
                 groups: Sequence[str] = ()) -> str:
    """What a thumbnail says about the stack it is part of.

    Two different marks for two different questions. Where the others are out
    of sight, on the file that speaks for them: **how many**, as a link,
    because opening a stack is a view of the library like any other. Only when
    it has anything behind it — a count of one is a photograph, not a stack.

    Where the stack is open — one of them by address, or all of them by
    grouping — on the file that is doing the speaking: **which one**. Everything
    in there looks alike, which is the whole reason they were stacked, so
    without this there is nothing to tell you which one the grid outside will
    show. Not the count again: a depth badge beside the very files it counts
    reads as a stack within a stack, and it would link to where you already
    are.

    A guess counts only where the view is folding guesses. With that off the
    others are on screen as themselves, and a badge saying two photographs are
    here would be the app describing a stack it has not been allowed to make.
    """
    key = f'{row["folder"]}/{row["name"]}'
    # How the library is being read, which the view does not carry: it is not
    # a filter, it is the shape of the page, and a stack opened out of a grid
    # cut by event and day should come back to one.
    grouped = ",".join(groups) or "none"
    behind = row["behind"] or 0
    guessed = _guessed(row, view)
    if view.within or view.unfold:
        speaks = (key == view.within if view.within else
                  not row["stacked_under"] and not row["suggested_under"])
        return ('<span class="top-mark" title="This is the one shown '
                'outside the stack">Top</span>'
                if speaks and behind + guessed else "")
    n = behind + guessed
    if not n:
        return ""
    # Drawn differently when any of it is the app's own guess, because the two
    # numbers answer different questions. A decided count says *somebody put
    # these together*; a guessed one says *these look alike, and nobody has
    # said yet* — and a curator deciding what to trust needs to see which is
    # which without opening it.
    # **A page, not a filter.** This was `/browse?within=…` — the grid with one
    # more thing narrowing it — and the takes of one photograph are not a view
    # of the library. Everything the grid brings with it was wrong in there:
    # eleven filters over eight frames of one moment, a grouping control for a
    # section that is the whole page, and a *Stack* button offering to stack
    # what is already a stack. See `stack_page`.
    #
    # It carries where it was opened from, because the way out has to be the
    # way in reversed: the grid it came from, filters and grouping and all.
    return (f'<a class="stack{" guessed" if guessed else ""}" '
            f'href="{_h(_stack_url(key, _browse_url(view, {"group": grouped})))}" '
            f'title="{n + 1} photographs '
            f'{"that look alike — nobody has said yet" if guessed else ""}'
            f'{"" if guessed else "stacked here"}">'
            f'{n + 1}</a>')


def _has_render(row: sqlite3.Row) -> bool:
    """Whether a playable copy of this file exists and differs from it.

    Only for the clips a browser will not play as they are. For every
    photograph here, and the third of the clips that were already H.264, the
    original *is* the playable file — so *original or copy* is not a question
    to ask about them, and the page only asks it where there is an answer.
    """
    if row["kind"] != "video":
        return False
    try:
        return paths.render_path(
            webroots.MASTER_DIR / str(row["folder"]) / str(row["name"]),
            webroots.RENDER_DIR).is_file()
    except OSError:
        return False


def _squareness(row: sqlite3.Row) -> str:
    """The short edge over the long one, or `1` where nothing is known.

    The grid shows squares, so a thumbnail is cropped to its short edge — but
    the tiers are capped on the **long** one. A 16:9 frame in the 1000px tier
    is 1000x563, and the square taken out of it is 563 across: a bit over half
    the number the tier is named for. That is why a video thumbnail went soft
    a size before a photograph did, and why the page cannot pick a tier from
    the cap alone.
    """
    w, h = row["width"] or 0, row["height"] or 0
    if not w or not h:
        return "1"
    return f"{min(w, h) / max(w, h):.3f}"


def _guessed(row: sqlite3.Row, view: ix.Filters) -> int:
    """How many files this one speaks for **in this view**.

    Nothing, unless the view is folding the app's guesses. A guess changes
    nothing about the library until somebody turns it on — so with it off the
    others are on screen as themselves, and this file has nobody behind it.

    Read by the badge *and* by the cell the page is built from, because the
    two disagreeing is the whole of a bug worth not having again: the badge
    showed nothing and the grid treated the file as a stack, so *Stack* opened
    it and more photographs came back than had been selected.
    """
    return _count(row, "proposed") if view.folds_guesses else 0


def _count(row: sqlite3.Row, column: str) -> int:
    """A counted column, where the query that produced the row had one."""
    try:
        return int(row[column] or 0)
    except IndexError:
        return 0


def _view_dict(view: ix.Filters) -> dict[str, str | list[str] | None]:
    """The view as the address spells it — and as the page script reads it.

    Several values are a list; one is a string, as it always was. `deleted`
    is turned back into the boxes the bar offers rather than the two modes
    the index works in, so the page and the address say the same thing.
    """
    out: dict[str, str | list[str] | None] = {}
    for name in ix.Filters.NAMES:
        got = ix.picks(cast("ix.Pick", getattr(view, name)))
        out[name] = (None if not got else got[0] if len(got) == 1
                     else list(got))
    sides: dict[str, str | list[str]] = {"only": "gone",
                                         "with": ["gone", "live"]}
    out["deleted"] = sides.get(str(view.deleted))
    out["apart"] = "1" if view.apart else None
    return out


def _act(act: str, word: str, cls: str = "", *,
         user: Principal | None = None, attr: str = "data-act") -> str:
    """One button in the edit bar: its drawing, and its name beside it.

    **The name is an element of its own, and it is carried three times.** On a
    wide screen it is read; on a phone the stylesheet hides it and the drawing
    stands alone, because eleven words and eleven drawings is two rows of bar
    on a screen that has none to give. So the word also goes into `title`, for
    a pointer, and into `aria-label`, for everything that does not have one —
    a control whose only name is hidden by a media query has no name at all.

    The span is not decoration either: the takeover names an action by reading
    it back off its own button rather than keeping a second vocabulary for the
    same four words, and `textContent` on a button with a drawing in it would
    take the drawing with it.
    """
    # An action this person does not get is not drawn disabled — it is not
    # there. A greyed-out Purge on a household member's bar is a standing
    # advertisement for a power they will never have.
    if user is not None and not may(user, act):
        return ""
    kind = f' class="{cls}"' if cls else ""
    # The ellipsis says *this one asks something next*, which is a fact about
    # the button and not part of what it is called.
    name = word.replace("&hellip;", "").strip()
    return (f'<button {attr}="{act}"{kind} title="{name}" '
            f'aria-label="{name}">{_mark(_ACT_MARKS.get(act, ""))}'
            f'<span class="word">{word}</span></button>')


#: The grid's script, in the order its parts are read. One top-level scope,
#: so the order is load-bearing: a part uses what the parts before it define
#: (`cells`, `say`, `targets` …) — which is why this is a list and not a glob.
_BROWSE_PARTS: tuple[str, ...] = (
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
_BROWSE_JS: str = "".join(asset(f"js/browse/{p}") for p in _BROWSE_PARTS)


# --- media -------------------------------------------------------------------

@app.get("/thumb/{folder}/{name}")
def thumb(folder: str, name: str,
          user: Annotated[Principal, Depends(require_user)]) -> FileResponse:
    _allowed(user, folder, name)
    return _serve(webroots.THUMB_DIR, folder, name, user)


@app.get("/strip/{folder}/{name}")
def strip(folder: str, name: str,
          user: Annotated[Principal, Depends(require_user)]) -> FileResponse:
    """A video's filmstrip, for the splice page's timeline."""
    _allowed(user, folder, name)
    return _serve(webroots.STRIP_DIR, folder, name, user)


@app.get("/large/{folder}/{name}")
def large(folder: str, name: str,
          user: Annotated[Principal, Depends(require_user)]) -> FileResponse:
    """The grid's largest setting. Between the two others because a cell that
    stretches to 460px wants 920 device pixels, which `thumb` has not got and
    `preview` has four times too many of."""
    _allowed(user, folder, name)
    return _serve(webroots.LARGE_DIR, folder, name, user)


@app.get("/preview/{folder}/{name}")
def preview(folder: str, name: str,
            user: Annotated[Principal, Depends(require_user)]) -> FileResponse:
    _allowed(user, folder, name)
    return _serve(webroots.PREVIEW_DIR, folder, name, user)


def _allowed(user: Principal, folder: str, name: str) -> None:
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
    _allowed(user, folder, name)
    # A clip plays as its source, clamped by the page to its range
    # (spec/clips.md §7). Only a curator gets here: `_allowed` has already
    # refused a viewer, who would otherwise be handed all of the source.
    source = clips.source_of(name)
    if source is not None and not (webroots.MASTER_DIR / folder / name).is_file():
        own = _clip_file(folder, name, playable=True)
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


def _serve(root: Path, folder: str, name: str,
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
        if not _may_see(user, folder, stand_in):
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


def _may_see(user: Principal | None, folder: str, name: str) -> bool:
    """Whether this person may see one file — an administrator may, and a
    caller with nobody in particular in mind is one."""
    if user is None or user.scope is None:
        return True
    conn = db()
    try:
        return ix.sees(conn, user.scope, folder, name)
    finally:
        conn.close()


def _to_send(folder: str, name: str, original: bool) -> tuple[Path, str]:
    """The file to hand over and what to call it.

    Two things can be meant by *the file*. The **original** is what came off
    the camera and is what master holds; the playable copy is the H.264
    rendition the app made of it, which exists only where the original is
    something a browser will not play. For everything else — every photograph
    here, and the third of the clips that were already H.264 — they are the
    same file, and offering a choice between them would be offering a choice
    that is not there.
    """
    media = _master_file(folder, name)
    if not media.is_file():
        # A clip: *original* is its lossless cut, and the playable copy is its
        # render where it has one (spec/clips.md §7). Never its source —
        # downloading a clip and receiving the whole video is the wrong file.
        own = _clip_file(folder, name, playable=not original)
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
    _allowed(user, folder, name)
    target, called = _to_send(folder, name, bool(original))
    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such file")
    return FileResponse(target, filename=called, media_type=_mime(called))


def _one_kind(conn: sqlite3.Connection | None, change: _Change,
              targets: Sequence[Target]) -> None:
    """Refuse a stack that would mix photographs with video — or hold video
    at all.

    **Video does not stack yet** (spec/clips.md §4). Whether one clip can
    speak for another is a question with no answer so far, and a stack is a
    fold: nothing should be taken out of the grid on a rule nobody has made.
    Stacks of video made before this are left alone and can still be taken
    apart — that write names no top, and returns before any of this.

    A stack says *these are the same shot, and this one speaks for the rest*.
    A photograph and a clip are not the same shot whatever else they share —
    same second, same camera, same name — and neither can stand in for the
    other, so folding one behind the other hides a thing nothing on screen
    represents.

    Here as well as in the page, because this is the scripting surface: a rule
    only the page holds is one the next client does not.

    Checked after the expansion, like the permission check above it and for
    the same reason: `_behind` brings in the rest of a stack, and those come
    along whether or not the request named them.

    One query rather than one per file, with the file being deferred to
    counted among them — which makes *is more than one kind of thing in play*
    a single question.
    """
    top = change.stacked_under
    if conn is None or isinstance(top, Unset) or not top:
        return
    folder, _, name = str(top).rpartition("/")
    keys = [f"{t.folder}{chr(10)}{t.name}" for t in targets]
    keys.append(f"{folder}{chr(10)}{name}")
    rows = conn.execute(
        "SELECT files.kind AS kind, COUNT(*) AS n FROM files "
        "JOIN json_each(:keys) "
        "  ON json_each.value = files.folder || char(10) || files.name "
        "GROUP BY files.kind", {"keys": json.dumps(keys)}).fetchall()
    if len(rows) < 2:
        if rows and rows[0]["kind"] == "video":
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "video cannot be stacked yet — whether one clip can speak "
                "for another has not been decided")
        return
    counts = " and ".join(
        f'{r["n"]} {_KIND_WORDS.get(str(r["kind"]), str(r["kind"]))}'
        for r in sorted(rows, key=lambda r: -int(r["n"])))
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        f"a stack is one shot, and this one would be {counts} — "
        f"none of them can speak for the rest")


def _clip_rules(conn: sqlite3.Connection | None, change: _Change,
                targets: Sequence[Target]) -> list[Target]:
    """What binning and restoring mean where clips are involved
    (spec/clips.md §5).

    **A source cannot be binned while it has clips.** A clip is the source's
    bytes plus a range, and cannot outlive it — and *I only want the clips*
    is exactly why somebody would bin the source, so the one gesture would
    destroy what it was meant to keep. The answer is to hide it. Clips binned
    in the same request count as binned, so taking a whole video and its
    clips out at once is still one gesture.

    **Restoring a clip restores its source**, because it cannot exist without
    it — and is refused if it would come back over footage another clip has
    taken since.
    """
    out = list(targets)
    if conn is None or isinstance(change.deleted, Unset):
        return out
    named = {(t.folder, t.name) for t in targets}
    if change.deleted:
        for t in targets:
            if clips.source_of(t.name) is not None:
                continue
            live = [c for c in ix.clips_of(conn, t.folder, t.name)
                    if not c["deleted"] and (t.folder, c["name"]) not in named]
            if live:
                n = len(live)
                them = "it" if n == 1 else "them"
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"{t.name} has {n} clip{'s' if n != 1 else ''} cut from "
                    f"it — archive it instead, or bin {them} first")
        return out
    for t in targets:
        source = clips.source_of(t.name)
        row = ix.one(conn, t.folder, t.name) if source else None
        if source is None or row is None or row["clip_of"] is None:
            continue
        siblings = [(float(c["clip_in"]), float(c["clip_out"]))
                    for c in ix.clips_of(conn, t.folder, source)
                    if not c["deleted"] and c["name"] != t.name]
        try:
            clips.check(float(row["clip_in"]), float(row["clip_out"]),
                        siblings=siblings, duration=None)
        except clips.ClipError as e:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"{t.name} cannot come back: {e}") from e
        parent = ix.one(conn, t.folder, source)
        if (parent is not None and parent["deleted"]
                and (t.folder, source) not in named):
            out.append(Target(folder=t.folder, name=source))
            named.add((t.folder, source))
    return out


def _records_for(targets: Sequence[Target]
                 ) -> dict[tuple[str, str], dict[str, Any]]:
    """The probed facts for a whole batch, fetched together.

    Read-only and independent, so they overlap safely; one that fails is
    simply absent, and the refresh that wanted it falls back to fetching its
    own.
    """
    if len(targets) < 2:
        return {}
    def one(t: Target) -> tuple[str, str, dict[str, Any] | None]:
        return t.folder, t.name, ix.record_of(webroots.META_DIR, t.folder, t.name)

    out: dict[tuple[str, str], dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=META_READERS) as pool:
        for folder, name, record in pool.map(one, targets):
            if record is not None:
                out[(folder, name)] = record
    return out


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
    form = await _form(request)
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
        _allowed(user, folder, name)
        target, called = _to_send(folder, name, original)
        if target.is_file():
            # Foldered inside the zip, because two master folders can hold the
            # same name and a flat zip would quietly keep one of them.
            picked.append((f"{folder}/{called}", target))
    if not picked:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "nothing to send")

    stamp = time.strftime("%Y-%m-%d")
    return StreamingResponse(
        _zipped(picked), media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="pix-{stamp}.zip"'})


class _Sink:
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


def _zipped(picked: Sequence[tuple[str, Path]]) -> Iterator[bytes]:
    """The zip, a chunk at a time."""
    sink = _Sink()
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


# --- api ---------------------------------------------------------------------

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
    groups = _groupings(group)
    rows = ix.files(conn, view, groups=groups, limit=limit, offset=offset)
    total = ix.count(conn, view)
    return JSONResponse({
        "html": _sections(rows, groups, view, _totals(conn, view, groups),
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
    if column in _FIXED:
        return JSONResponse(_fixed_counts(conn, column, view))
    near = (near_from, near_to) if near_from and near_to else None
    out = [{"value": s.value, "n": s.n, "scope": s.scope}
           for s in ix.suggest(conn, column, view, near=near)]
    if column == "audience":
        # The two that are states rather than names, which no row of
        # `file_audience` carries — so they are counted, not listed.
        have = {o["value"] for o in out}
        for value in (ix.UNREVIEWED, decisions.ARCHIVED):
            if value not in have:
                out.extend(o for o in _fixed_counts(conn, column, view,
                                                    (value,)) if o["n"])
    return JSONResponse(out)


def _fixed_counts(conn: sqlite3.Connection, column: str, view: ix.Filters,
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
    for value in values or [v for v, _ in _FIXED[column]]:
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
_FACTS: tuple[tuple[str, str], ...] = (
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
_INHERITED: tuple[tuple[str, str], ...] = (
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
    _allowed(user, folder, name)
    row = ix.one(db(), folder, name)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not indexed")

    media = webroots.MASTER_DIR / folder / name
    decision = decisions.read(media) if _under(webroots.MASTER_DIR, media) else None
    record = ix.record_for(folder, name, meta_dir=webroots.META_DIR) or {}
    raw: object = record.get("exif")
    exif: dict[str, Any] = (
        cast("dict[str, Any]", raw) if isinstance(raw, dict) else {})

    facts = [{"label": label, "value": ix.tag(exif, key)}
             for label, key in _FACTS if ix.tag(exif, key)]
    inherited = {field: ix.tag(exif, key) for field, key in _INHERITED}

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
        "tags": _split(row["tags"]),
        "people": _split(row["people"]),
        "audience": _split(row["audience"]),
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
        "clip": _clip_from(user, row, view),
        "clips": _clips_list(user, row, view),
    })


def _clips_list(user: Principal, row: sqlite3.Row,
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
            "open": ("/browse" + (f"?date={_q(day)}" if len(day) == 10 else "")
                     + "#open:" + _q(f"{folder}/{c['name']}")),
        })
    return out


def _clip_from(user: Principal, row: sqlite3.Row,
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
    if not _may_see(user, folder, str(source)):
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
    query = [f"date={_q(day)}"] if len(day) == 10 else []
    # A hidden source is out of the administrator's own grid too, so the way
    # to it asks for hidden files by name.
    if parent is not None and decisions.ARCHIVED in _split(parent["audience"]):
        query.append(f"audience={_q(decisions.ARCHIVED)}")
    return {
        "source": source,
        "key": f"{folder}/{source}",
        "start": row["clip_in"], "end": row["clip_out"],
        "in_view": in_view,
        # `#open:` lands on the file with its preview open, where a bare
        # `#` only scrolls to it.
        "open": ("/browse" + ("?" + "&".join(query) if query else "")
                 + "#open:" + _q(f"{folder}/{source}")),
        "splice": (f"/splice/{_q(folder)}/{_q(str(source))}#{_q(str(row['name']))}"
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
        {"cells": "".join(_cell(r, replace(view, within=key)) for r in rows)})


@app.get("/api/events")
def api_events(user: Annotated[Principal, Depends(require_user)],
               view: Annotated[ix.Filters, Depends(filters)]) -> JSONResponse:
    return JSONResponse([dict(r) for r in ix.events(db(), view)])


class DecideBody(BaseModel):
    """A curation decision about one master file.

    Every field is optional *and* nullable, and the two mean different things:
    omitting `event` leaves it alone, sending `null` clears it. Without that
    distinction a one-field UI gesture — tier this photo — would silently erase
    whatever else had been decided about it, so the wire format has to carry it.
    """

    folder: str
    name: str
    event: str | None = None
    #: Half a name each: set the event and keep whatever sub-event each file
    #: has, or set the sub-event and keep each file's event. One request, a
    #: different answer per file, which is why neither can be worked out by
    #: the caller.
    event_head: str | None = None
    event_leaf: str | None = None
    date_override: str | None = None
    tags: list[str] | None = None
    add_tags: list[str] = []
    remove_tags: list[str] = []
    people: list[str] | None = None
    add_people: list[str] = []
    remove_people: list[str] = []
    audience: list[str] | None = None
    add_audience: list[str] = []
    remove_audience: list[str] = []
    deleted: bool | None = None
    stacked_under: str | None = None
    no_stack: bool | None = None


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
    change = _change(body)
    _writable(user, change)
    _allowed(user, body.folder, body.name)

    # The same cascade the page's own route does. There is no view here to say
    # whether a stack is open — this is the scripting surface, and the page
    # writes through `/api/decide/bulk` — so it asks what this person's
    # ordinary view would fold, which is the only reading available.
    conn = ix.open_rw(webroots.DB_PATH) if webroots.DB_PATH.is_file() else None
    try:
        view = ix.Filters()
        named = Target(folder=body.folder, name=body.name)
        targets = _mine(user, conn, _behind(
            conn, view, [named],
            members=isinstance(change.stacked_under, Unset)))
        _one_kind(conn, change, targets)
        targets = _clip_rules(conn, change, targets)
        was, decision, indexed = _decide(body.folder, body.name, change,
                                         conn=conn)
        undo = [history.Before(body.folder, body.name, was,
                               did=_did(change, _recorded(change), decision))]
        for target in targets:
            if (target.folder, target.name) == (body.folder, body.name):
                continue
            try:
                before, after, _ = _decide(target.folder, target.name, change,
                                           conn=conn)
            except HTTPException:
                # One unwritable take does not cost the decision about the
                # rest, the same way a bulk edit reports a failure and carries
                # on.
                continue
            undo.append(history.Before(target.folder, target.name, before,
                                       did=_did(change, _recorded(change),
                                                after)))
    finally:
        if conn is not None:
            conn.close()
    history.record(user.name, _summary(change), undo)
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


class Target(BaseModel):
    """One file in a selection."""

    folder: str
    name: str


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
                media = _master_file(target.folder, target.name)
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
            with _write_lock:
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


# --- clips (spec/clips.md) ----------------------------------------------------

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


def _ms(value: float) -> float:
    """Milliseconds, which is finer than any frame and what the sidecar
    keeps — so a range compared here is the range that will be stored."""
    return round(value, 3)


def _clip_conn() -> sqlite3.Connection:
    if not webroots.DB_PATH.is_file():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "the index has not been built")
    return ix.open_rw(webroots.DB_PATH)


def _clip_row(conn: sqlite3.Connection, folder: str,
              name: str) -> sqlite3.Row:
    row = ix.one(conn, folder, name)
    if row is None or row["clip_of"] is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such clip")
    return row


def _siblings(conn: sqlite3.Connection, folder: str, source: str,
              *, besides: Sequence[str] = ()) -> list[tuple[float, float]]:
    """The ranges a clip must not overlap: its living siblings'."""
    return [(float(c["clip_in"]), float(c["clip_out"]))
            for c in ix.clips_of(conn, folder, source)
            if not c["deleted"] and c["name"] not in besides]


def _check(start: float, end: float, *, siblings: list[tuple[float, float]],
           duration: float | None) -> None:
    try:
        clips.check(start, end, siblings=siblings, duration=duration)
    except clips.ClipError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e


def _content(decision: Decision, *, start: float, end: float) -> _Change:
    """A whole clip decision as a write: content and range, nothing left
    to what the sidecar said before — because before, there was none."""
    return _Change(event=decision.event, date_override=decision.date_override,
                   tags=list(decision.tags), people=list(decision.people),
                   audience=list(decision.audience),
                   clip_in=start, clip_out=end)


def _gone() -> _Change:
    """Every field cleared: what removing a clip writes, so that the log
    holds all of it and reverting brings the whole clip back."""
    return _Change(event=None, date_override=None, tags=[], people=[],
                   audience=[], deleted=False, stacked_under=None,
                   no_stack=False, clip_in=None, clip_out=None)


def _write_clips(conn: sqlite3.Connection,
                 writes: Sequence[tuple[str, str, _Change, bool]]
                 ) -> list[history.Before]:
    """Write each `(folder, name, change, creating)` and say what it did."""
    undo: list[history.Before] = []
    for folder, name, change, creating in writes:
        was, decision, _ = _decide(folder, name, change, conn=conn,
                                   creating=creating)
        undo.append(history.Before(
            folder, name, was, did=_did(change, _recorded(change), decision)))
    return undo


@app.get("/api/clips/{folder}/{source}")
def api_clips(folder: str, source: str,
              user: Annotated[Principal, Depends(require_admin)]
              ) -> JSONResponse:
    """A source's clips and stills, in the order they come in the video."""
    conn = _clip_conn()
    try:
        rows = ix.clips_of(conn, folder, source)
    finally:
        conn.close()
    return JSONResponse([_clip_json(r) for r in rows])


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
    media = _master_file(body.folder, body.source)
    conn = _clip_conn()
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
        siblings = _siblings(conn, body.folder, body.source)
        base = clips.inherited(decisions.read(media), row["event"])
        writes: list[tuple[str, str, _Change, bool]] = []
        for start, end in _snapped(_keys(body.folder, body.source),
                                   [(_ms(a.start), _ms(a.end))
                                    for a in body.clips], siblings):
            _check(start, end, siblings=siblings, duration=row["duration"])
            siblings.append((start, end))
            clip_id = clips.new_id(taken)
            taken.add(clip_id)
            writes.append((body.folder, clips.name_of(body.source, clip_id),
                           _content(base, start=start, end=end), True))
        undo = _write_clips(conn, writes)
    finally:
        conn.close()
    n = len(undo)
    history.record(user.name, f"cut {n} clip{'s' if n != 1 else ''} from "
                   f"{body.source}", undo)
    for b in undo:
        _recut(body.folder, b.name)
    return JSONResponse({"made": [b.name for b in undo]})


def _snapped(keys: tuple[float, ...] | None,
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
        out.append((_snap_start(keys, start, end, lo), end))
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
    start, end = _ms(body.start), _ms(body.end)
    conn = _clip_conn()
    try:
        row = _clip_row(conn, body.folder, body.name)
        if (start == end) != (row["clip_in"] == row["clip_out"]):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "a still stays a still and a clip stays a clip")
        source = ix.one(conn, body.folder, str(row["clip_of"]))
        siblings = _siblings(conn, body.folder, str(row["clip_of"]),
                             besides=[body.name])
        if start != float(row["clip_in"]):
            lo = max([e for s0, e in siblings
                      if s0 != e and e <= start + 0.0005] + [0.0])
            start = _snap_start(_keys(body.folder, str(row["clip_of"])),
                                start, end, lo)
        _check(start, end, siblings=siblings,
               duration=source["duration"] if source else None)
        undo = _write_clips(conn, [(body.folder, body.name,
                                    _Change(clip_in=start, clip_out=end),
                                    False)])
    finally:
        conn.close()
    history.record(user.name, f"moved the ends of {body.name}", undo)
    _recut(body.folder, body.name)
    return JSONResponse({"name": body.name, "start": start, "end": end})


@app.post("/api/clips/split")
def api_clips_split(user: Annotated[Principal, Depends(require_admin)],
                    body: Annotated[ClipSplitBody, Body()]) -> JSONResponse:
    """Cut a clip in two at `at`.

    The first half keeps the id and everything decided about it; the second
    is a new clip that starts with a **copy** of those decisions, because
    both halves were that clip and both start out true to it.
    """
    at = _ms(body.at)
    conn = _clip_conn()
    try:
        row = _clip_row(conn, body.folder, body.name)
        start, end = float(row["clip_in"]), float(row["clip_out"])
        if row["deleted"]:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "that clip is binned")
        if not start < at < end:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{at:g}s is not inside the clip ({start:g}s–{end:g}s)")
        source = str(row["clip_of"])
        keys = _keys(body.folder, source)
        if keys:
            # The second half starts here, so here has to be a keyframe.
            snapped = cut.snap(at, keys, lo=start + 0.001, hi=end)
            if snapped is None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "there is no keyframe inside this clip to split it on")
            at = snapped
        was = decisions.read(_master_file(body.folder, body.name)) or Decision()
        taken = {clips.id_of(str(c["name"]))
                 for c in ix.clips_of(conn, body.folder, source)}
        second = clips.name_of(source, clips.new_id(taken))
        undo = _write_clips(conn, [
            (body.folder, body.name, _Change(clip_out=at), False),
            (body.folder, second, _content(was, start=at, end=end), True)])
    finally:
        conn.close()
    history.record(user.name, f"split {body.name}", undo)
    _recut(body.folder, body.name)
    _recut(body.folder, second)
    return JSONResponse({"first": body.name, "second": second, "at": at})


@app.post("/api/clips/merge")
def api_clips_merge(user: Annotated[Principal, Depends(require_admin)],
                    body: Annotated[ClipMergeBody, Body()]) -> JSONResponse:
    """Take away the split between two clips that meet.

    The earlier one survives, with both clips' decisions merged by the
    duplicate ladder (`clips.merged`); the later one is removed — logged in
    full, so reverting the merge brings it back as it was.
    """
    conn = _clip_conn()
    try:
        a = _clip_row(conn, body.folder, body.first)
        b = _clip_row(conn, body.folder, body.second)
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
        first = _master_file(body.folder, str(a["name"]))
        second = _master_file(body.folder, str(b["name"]))
        content = clips.merged(
            decisions.read(first) or Decision(),
            decisions.sidecar_path(first).stat().st_mtime,
            decisions.read(second) or Decision(),
            decisions.sidecar_path(second).stat().st_mtime)
        undo = _write_clips(conn, [
            (body.folder, str(a["name"]),
             _content(content, start=float(a["clip_in"]),
                      end=float(b["clip_out"])), False),
            (body.folder, str(b["name"]), _gone(), False)])
    finally:
        conn.close()
    history.record(user.name, f"joined {a['name']} and {b['name']}", undo)
    _recut(body.folder, str(a["name"]))
    _recut(body.folder, str(b["name"]))
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
    at = _ms(body.at)
    conn = _clip_conn()
    try:
        a = _clip_row(conn, body.folder, body.first)
        b = _clip_row(conn, body.folder, body.second)
        if float(a["clip_in"]) > float(b["clip_in"]):
            a, b = b, a
        if (a["clip_of"] != b["clip_of"]
                or abs(float(a["clip_out"]) - float(b["clip_in"])) > 0.001):
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "those clips do not share a marker")
        lo, hi = float(a["clip_in"]), float(b["clip_out"])
        keys = _keys(body.folder, str(a["clip_of"]))
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
        order = [(str(b["name"]), _Change(clip_in=at, clip_out=float(b["clip_out"]))),
                 (str(a["name"]), _Change(clip_in=lo, clip_out=at))]
        if at < float(a["clip_out"]):
            order.reverse()
        undo = _write_clips(conn, [(body.folder, n, c, False)
                                   for n, c in order])
    finally:
        conn.close()
    history.record(user.name, f"moved the cut between {a['name']} and "
                   f"{b['name']}", undo)
    _recut(body.folder, str(a["name"]))
    _recut(body.folder, str(b["name"]))
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
    media = _master_file(body.folder, body.source)
    conn = _clip_conn()
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
        keys = _keys(body.folder, body.source)
        ordered = sorted(body.clips, key=lambda c: (c.start, c.end))
        placed: list[tuple[float, float]] = []
        final: dict[str, tuple[float, float]] = {}
        for c in ordered:
            start, end = _ms(c.start), _ms(c.end)
            if start != end:
                lo = max([e for s0, e in placed
                          if s0 != e and e <= start + 0.0005] + [0.0])
                start = _snap_start(keys, start, end, lo)
            _check(start, end, siblings=placed, duration=row["duration"])
            placed.append((start, end))
            final[c.id] = (start, end)

        def said(name: str) -> tuple[Decision, float]:
            path = _master_file(body.folder, name)
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
        writes: list[tuple[str, str, _Change, bool]] = []
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
                               _content(merged, start=start, end=end), False))
            else:
                content[c.id] = said(c.id)[0]
                was = live[c.id]
                if (float(was["clip_in"]), float(was["clip_out"])) != (start, end):
                    writes.append((body.folder, c.id,
                                   _Change(clip_in=start, clip_out=end), False))
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
                               _content(start_with, start=start, end=end),
                               True))
                pending.remove(c)
                progressed = True
            if not progressed:
                raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                    "a split part copies a clip that is not "
                                    "in the draft")
        for other in sorted(absorbed):
            writes.append((body.folder, other, _gone(), False))
        for other in sorted(gone):
            writes.append((body.folder, other, _Change(deleted=True), False))
        undo = _write_clips(conn, writes)
    finally:
        conn.close()
    if undo:
        history.record(user.name, f"edited the clips of {body.source}", undo)
    for b in undo:
        if b.name not in gone:
            _recut(body.folder, b.name)
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
    media = _master_file(body.folder, body.source)
    conn = _clip_conn()
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
            with _write_lock:
                tmp = own.with_name(own.name + ".tmp")
                shutil.copyfile(file, tmp)
                os.replace(tmp, own)
                was = decisions.read(clip) or Decision()
                decisions.write(own, Decision(
                    event=was.event, date_override=was.date_override,
                    tags=was.tags, people=was.people, audience=was.audience))
                _stand_in(body.folder, own, c, record, body.source)
                destroy_mod.destroy(clip, conn=conn, folder=body.folder,
                                    name=name)
            ix.refresh(conn, body.folder, own.name, meta_dir=webroots.META_DIR,
                       master_dir=webroots.MASTER_DIR)
            freed.append(own.name)
        binned = _Change(deleted=True)
        was, decision, _ = _decide(body.folder, body.source, binned,
                                   conn=conn)
        undo = [history.Before(body.folder, body.source, was,
                               did=_did(binned, _recorded(binned), decision))]
    finally:
        conn.close()
    del media
    n = len(freed)
    history.record(user.name, f"binned {body.source}, keeping its {n} "
                   f"clip{'s' if n != 1 else ''} as files of their own", undo)
    return JSONResponse({"freed": freed})


def _stand_in(folder: str, own: Path, clip: sqlite3.Row,
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


# --- the splice page (spec/clips.md §9) ---------------------------------------

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
            f"/splice/{_q(folder)}/{_q(source)}#{_q(name)}",
            status_code=status.HTTP_303_SEE_OTHER)
    media = _master_file(folder, name)
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
        return _page(title, '<p class="empty">Not indexed yet — run '
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
        return _page(title, f'<p class="empty">{_h(why)}</p>', user=user)
    try:
        fps = float(exif.get("QuickTime:VideoFrameRate") or 0) or 30.0
    except (TypeError, ValueError):
        fps = 30.0
    state = {
        "folder": folder, "source": name,
        "duration": row["duration"], "fps": fps,
        "hidden": decisions.ARCHIVED in _split(row["audience"]),
        "hiddenName": decisions.ARCHIVED,
        "clips": [_clip_json(c) for c in cut if not c["deleted"]],
        "strip": _strip_of(media, folder, name),
    }
    src = f"/media/{_q(folder)}/{_q(name)}"
    return _page(title, _splice_html().replace("{src}", src),
                 user=user,
                 script=(f"<style>{_SPLICE_CSS}</style>"
                         f"<script>const SPLICE={_js(state)};</script>"
                         f"<script>{_SPLICE_JS}</script>"))


def _strip_of(media: Path, folder: str, name: str) -> dict[str, Any] | None:
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
            "url": f"/strip/{_q(folder)}/{_q(name)}"}


def _viewacts(user: Principal, *, stack: bool = False) -> str:
    """What can be done to the one photograph filling the screen.

    Everything the bar does that makes sense of a single file, so that seeing
    something worth acting on is not a reason to leave it: name its event,
    tag it, say who is in it, fix its date, decide who sees it, cut it, save
    it, bin it. The stack actions are not here — each is a question about
    several photographs — except *Show this one* on a stack's own page, which
    is the question that page is for.

    The same buttons as the bar, by the same rules (`_act`, `may`), marked
    `data-vact` rather than `data-act` so the bar's wiring cannot pick them
    up. The page script points the bar's own actions at the file on show
    (`viewAct`), so a menu here is the same menu, with the same suggestions,
    the same confirmation and the same History line.

    Two sets, like the bar's: a binned file is restored or purged, never
    tagged.
    """
    live = "".join(_act(a, w, c, user=user, attr="data-vact") for a, w, c in (
        ("event", "Event&hellip;", ""), ("tags", "Tags&hellip;", ""),
        ("people", "People&hellip;", ""), ("date", "Date&hellip;", ""),
        ("access", "Access&hellip;", "")))
    gone = "".join(_act(a, w, c, user=user, attr="data-vact") for a, w, c in (
        ("restore", "Restore", ""), ("purge", "Purge&hellip;", "danger")))
    bin_ = _act("delete", "Delete", "danger", user=user, attr="data-vact")
    return ('<div id="viewacts">'
            + ('<button id="viewtop" hidden></button>' if stack else "")
            + f'<span class="vgrp" data-side="live">{live}</span>'
            + _viewsplice(user)
            + f'<a id="viewget" class="who-link" download>{_mark("get", 19)}</a>'
            + f'<span class="vgrp" data-side="live">{bin_}</span>'
            + f'<span class="vgrp" data-side="gone" hidden>{gone}</span>'
            + '</div>')


def _viewsplice(user: Principal) -> str:
    """Splice, from the preview: an administrator's, like the action."""
    if not may(user, "splice"):
        return ""
    return (f'<a id="viewsplice" class="who-link" hidden title="Splice" '
            f'aria-label="Splice">{_mark("splice", 19)}</a>')


def _clip_json(row: sqlite3.Row) -> dict[str, Any]:
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


def _clip_file(folder: str, name: str, *, playable: bool) -> Path | None:
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


def _first_frame(record: dict[str, Any] | None, offset: float) -> str | None:
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


def _cut_one(folder: str, name: str) -> None:
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
                 created=_first_frame(record, decision.clip_in))
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
_CUTS: cut.Queue = cut.Queue(_cut_one)


def _recut(folder: str, name: str) -> None:
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
    _CUTS.schedule(folder, name)


def _resume_cuts() -> None:
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
            _CUTS.schedule(str(row["folder"]), str(row["name"]))


def _keys(folder: str, source: str) -> tuple[float, ...] | None:
    return cut.keyframes(webroots.MASTER_DIR / folder / source)


def _snap_start(keys: tuple[float, ...] | None, start: float, end: float,
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
    media = _master_file(folder, name)
    keys = cut.keyframes(media)
    return JSONResponse({"keys": list(keys) if keys is not None else None})


_SPLICE_HTML: str = asset("html/splice.html")


def _splice_html() -> str:
    """The page, with its drawings put in — `@name@` for each, so the markup
    reads as a layout rather than as a wall of paths."""
    import re as _re

    return _re.sub(r"@(sp_\w+)@", lambda m: _mark(m.group(1), 20),
                   _SPLICE_HTML)


_SPLICE_CSS: str = asset("css/splice.css")


_SPLICE_JS: str = asset("js/splice.js")


class DecideBulkBody(BaseModel):
    """One decision applied across a selection of files.

    The primitive both bulk gestures need. *Finishing an event* writes
    `tier: "none"` to everything left unpromoted (§8) — a few hundred files from
    one click. *Naming a range* writes `event` across every file the curator
    selected, which is what makes event assignment cheap: the range is evaluated
    once, here, and never stored as a rule.

    The selection is **explicit file names, not a query.** The client already has
    them, and sending them means what gets written is what the curator saw — a
    query re-evaluated server-side could pick up a file someone else just moved
    into the event.
    """

    files: list[Target]
    #: Ties the chunks of one gesture together in the log. Chunking is a fact
    #: about the transport, and without this it read as several edits.
    batch: str | None = None
    event: str | None = None
    #: Half a name each: set the event and keep whatever sub-event each file
    #: has, or set the sub-event and keep each file's event. One request, a
    #: different answer per file, which is why neither can be worked out by
    #: the caller.
    event_head: str | None = None
    event_leaf: str | None = None
    date_override: str | None = None
    tags: list[str] | None = None
    add_tags: list[str] = []
    remove_tags: list[str] = []
    people: list[str] | None = None
    add_people: list[str] = []
    remove_people: list[str] = []
    audience: list[str] | None = None
    add_audience: list[str] = []
    remove_audience: list[str] = []
    deleted: bool | None = None
    stacked_under: str | None = None
    no_stack: bool | None = None


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

    change = _change(body)
    _writable(user, change)
    did = _recorded(change)
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
    targets = _mine(user, conn, _behind(
        conn, view, body.files,
        members=isinstance(change.stacked_under, Unset)))
    _one_kind(conn, change, targets)
    targets = _clip_rules(conn, change, targets)
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
    records = _records_for(targets)
    try:
        with rows:
            for target in targets:
                try:
                    was, now, was_indexed = _decide(
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
                promoted = _promote(conn, change.stacked_under)
                undo.extend(promoted)
                done.extend((b.folder, b.name) for b in promoted)
            # Before asking what left the view, because bringing a stack's
            # members up changes the answer for them too.
            if (conn is not None and done
                    and not isinstance(change.stacked_under, Unset)):
                brought = _cascade(conn, done, change.stacked_under)
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
        history.record(user.name, _summary(change), undo, batch=body.batch)
    return JSONResponse({"written": written, "indexed": indexed,
                         "failed": failed, "dropped": dropped,
                         "total": total, "binned": binned})


# --- the write path ----------------------------------------------------------

@dataclass(frozen=True)
class _Change:
    """One decision edit, with "leave it alone" distinct from "clear it"."""

    event: str | None | Unset = decisions.UNSET
    event_head: str | None | Unset = decisions.UNSET
    event_leaf: str | None | Unset = decisions.UNSET
    date_override: str | None | Unset = decisions.UNSET
    tags: Sequence[str] | None | Unset = decisions.UNSET
    add_tags: Sequence[str] = field(default_factory=tuple)
    remove_tags: Sequence[str] = field(default_factory=tuple)
    people: Sequence[str] | None | Unset = decisions.UNSET
    add_people: Sequence[str] = field(default_factory=tuple)
    remove_people: Sequence[str] = field(default_factory=tuple)
    audience: Sequence[str] | None | Unset = decisions.UNSET
    add_audience: Sequence[str] = field(default_factory=tuple)
    remove_audience: Sequence[str] = field(default_factory=tuple)
    deleted: bool | Unset = decisions.UNSET
    stacked_under: str | None | Unset = decisions.UNSET
    no_stack: bool | Unset = decisions.UNSET
    #: A clip's range. Never read from a decide request — a range is edited
    #: through the clip routes, which check it against its siblings — but
    #: carried here so those routes write through the same path as every
    #: other decision, and are logged and reverted the same way.
    clip_in: float | None | Unset = decisions.UNSET
    clip_out: float | None | Unset = decisions.UNSET


def _change(body: DecideBody | DecideBulkBody) -> _Change:
    """Which decision fields the request actually sent.

    Omitted and `null` mean different things, so a field nobody sent becomes
    `UNSET` and is left exactly as it was. Tags additionally distinguish
    *replace* from *add* and *remove*: applying a tag to a selection of 200
    files has to add to what each one already carries.
    """
    sent = body.model_fields_set
    def got(name: str) -> Any:
        return getattr(body, name) if name in sent else decisions.UNSET
    return _Change(event=got("event"),
                   event_head=got("event_head"), event_leaf=got("event_leaf"),
                   date_override=got("date_override"), tags=got("tags"),
                   add_tags=tuple(body.add_tags),
                   remove_tags=tuple(body.remove_tags),
                   people=got("people"),
                   add_people=tuple(body.add_people),
                   remove_people=tuple(body.remove_people),
                   audience=got("audience"),
                   add_audience=tuple(body.add_audience),
                   remove_audience=tuple(body.remove_audience),
                   deleted=_flag(got("deleted")),
                   stacked_under=got("stacked_under"),
                   no_stack=_flag(got("no_stack")))


def _did(change: _Change, base: dict[str, Any],
         decision: Decision) -> dict[str, Any]:
    """What the operation did to *this* file.

    A half-name write resolves differently per file — *Sicily > Taormina*
    and *Sicily > Catania* both become *Family Trip > something* — so the
    log records the name each one ended up with rather than the half that was
    asked for. Reverting compares what it recorded against what the file says
    now, and a batch-wide *event_head* would match neither of them.
    """
    if (isinstance(change.event_head, Unset)
            and isinstance(change.event_leaf, Unset)):
        return base
    return {**base, "event": decision.event}


def _recorded(change: _Change) -> dict[str, Any]:
    """What an operation did, in the shape the log keeps it.

    Only the fields it actually sent — `UNSET` means *left alone*, and a log
    that could not tell that apart from *set to nothing* would revert fields
    the operation never touched.
    """
    out: dict[str, Any] = {}
    for name in ("event", "date_override", "tags", "people", "audience",
                 "deleted",
                 "stacked_under", "no_stack", "clip_in", "clip_out"):
        value: Any = getattr(change, name)
        if isinstance(value, Unset):
            continue
        out[name] = ([str(v) for v in cast("Sequence[str]", value)]
                     if isinstance(value, (list, tuple)) else value)
    for name in ("add_tags", "remove_tags", "add_people", "remove_people",
                 "add_audience", "remove_audience"):
        many = cast("Sequence[str]", getattr(change, name))
        if many:
            out[name] = [str(v) for v in many]
    return out


def _flag(value: Any) -> bool | Unset:
    """A boolean decision, where `null` means *leave it alone*.

    For every other field `null` clears it, because every other field has a
    cleared state distinct from any value it could hold. A flag does not:
    there is no third thing between deleted and not, so rather than invent
    one, sending nothing and sending null say the same thing.

    `UNSET` has to survive untouched, not be coerced — `bool(UNSET)` is `True`,
    which turned every edit of any field into a deletion.
    """
    if isinstance(value, Unset) or value is None:
        return decisions.UNSET
    return bool(value)


def _decide(folder: str, name: str, change: _Change,
            *, conn: sqlite3.Connection | None = None,
            record: dict[str, Any] | None = None,
            commit: bool = True,
            creating: bool = False
            ) -> tuple[Decision | None, Decision, bool]:
    """Write one decision to master, then bring its index row up to date.

    **Sidecar first, index follows** (§4). If the sidecar write fails nothing
    happened; if the index update fails the decision still stands and a
    `pix2 index` catches up — drift is only ever "the index is behind", never
    "the record is wrong".

    The lock is taken per file, not per batch. Finishing a large event would
    otherwise hold it for a minute and stall every other curator's clicks, and
    there is nothing to gain: the files are disjoint, and where they are not,
    last-write-wins is the stated policy anyway (§8).

    `conn` lets a batch reuse one index connection; alone, it opens and closes
    its own.

    **`commit=False` puts the row in the caller's transaction.** Every row of
    a bulk edit was its own commit, and a commit is an fsync — to an index
    that lives on the share, measured at 8.7ms a row against 0.2ms when a
    batch shares one. `record` is the same idea for the meta file the refresh
    would otherwise fetch per row.

    The sidecar write stays per file and outside any of that. It is the
    record; the index is a projection of it, and a projection that rolls back
    is behind, which is the one direction drift is allowed to go.
    """
    media = _master_file(folder, name, creating=creating)
    # The event this file is showing, which for most of the library is in its
    # own tags and not in a sidecar — a half-name write keeps the half it is
    # not replacing, and that half has to be the one on screen.
    #
    # Read only where a half-name write is actually in play: the meta is an
    # SMB round trip, and no other field needs it.
    half = not (isinstance(change.event_head, Unset)
                and isinstance(change.event_leaf, Unset))
    inherited = ix.inherited_event(
        record if record is not None
        else ix.record_of(webroots.META_DIR, folder, name)) if half else None
    with _write_lock:
        try:
            was, decision = decisions.change(
                media, event=change.event,
                event_head=change.event_head, event_leaf=change.event_leaf,
                inherited_event=inherited,
                date_override=change.date_override, tags=change.tags,
                add_tags=change.add_tags, remove_tags=change.remove_tags,
                people=change.people,
                add_people=change.add_people,
                remove_people=change.remove_people,
                audience=change.audience,
                add_audience=change.add_audience,
                remove_audience=change.remove_audience,
                deleted=change.deleted,
                stacked_under=change.stacked_under,
                no_stack=change.no_stack,
                clip_in=change.clip_in, clip_out=change.clip_out)
        except decisions.DecisionError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
        except OSError as e:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"could not write the sidecar: {e}") from e

        # Nothing was written, so there is nothing for the row to catch up
        # with. The index is exactly as right as it was a moment ago, which is
        # the only promise it makes.
        if (decision == was if was is not None else decision.is_empty()):
            return was, decision, True
        indexed = False
        own = conn is None and webroots.DB_PATH.is_file()
        if own:
            conn = ix.open_rw(webroots.DB_PATH)
        if conn is not None:
            try:
                # Passed rather than left to the index's own constants, so the
                # path the decision was written to and the path the row is
                # rebuilt from are the same one.
                # The decision is the one just written, so the refresh
                # does not go back to the share to read it again.
                indexed = ix.refresh(conn, folder, name,
                                     meta_dir=webroots.META_DIR, master_dir=webroots.MASTER_DIR,
                                     decision=decision, record=record,
                                     commit=commit)
            except sqlite3.Error:
                indexed = False
            finally:
                if own:
                    conn.close()
    return was, decision, indexed


def _behind(conn: sqlite3.Connection | None, view: ix.Filters,
            files: Sequence[Target], *, members: bool = True
            ) -> list[Target]:
    """The selection, plus whatever a folded view is hiding behind it.

    A stack shows one photograph and hides the rest, and the whole point of
    that is to work as though there is one file — so a decision made about
    what is on screen is a decision about all of them. Tag the stack and every
    take carries the tag; share it and every take is shared, so that taking it
    apart later leaves what somebody thought they had shared.

    **Both kinds, and this is what it got wrong.** It followed only the app's
    *guesses* and never a stack somebody had actually made, while claiming in
    this very docstring to be following "exactly the rule a real stack
    follows". It was not. The library therefore cascaded the grouping it had
    proposed and not the one that had been confirmed — which is the wrong way
    round, a stack you made being the stronger statement of the two. Sharing a
    stack shared one photograph of it, and unstacking months later produced
    files nobody could see.

    **Resolved before the first write, not after.** Refusing a guess writes
    `no_stack` to the photograph that speaks for it, and the index answers by
    recomputing that group — which would leave the others grouped behind a new
    leader, still unanswered, ready to be offered again tomorrow. Asked first,
    the refusal reaches all of them. The same hazard applies to a real stack:
    the write that moves a member out is the write that changes who its
    members are.

    Not when the view is already opened — inside a stack, or grouped by one.
    There the members are on screen and in the selection already, so following
    them again would be a second write to a file the curator can see they
    picked.

    **`members=False` for a write that moves a stack about**, because that one
    already has a cascade of its own and it runs afterwards: `_cascade` sends
    the members where the file that spoke for them went, and knows not to
    point one at itself. Following them here as well applied the head's new
    `stacked_under` to each of them — so promoting a take put the new top
    behind itself, and the whole stack disappeared from every listing at once.
    A guessed group is still followed, because nothing else follows it.
    """
    if conn is None:
        return list(files)
    # `within` opens one stack and `unfold` opens every stack in the view;
    # either way what is behind is in front of the curator already.
    opened = bool(view.within) or view.unfold
    if opened:
        return list(files)
    out = list(files)
    seen = {(t.folder, t.name) for t in files}
    for target in files:
        key = f"{target.folder}/{target.name}"
        # A confirmed stack always. A guessed one only where a guess is
        # behaving as a stack for this viewer, because where it is not, those
        # files are on screen in their own right and were not picked.
        hidden = list(ix.members(conn, key)) if members else []
        if view.folds_guesses:
            hidden += ix.proposed(conn, key)
        for folder, name in hidden:
            if (folder, name) in seen:
                continue
            seen.add((folder, name))
            out.append(Target(folder=folder, name=name))
    return out


def _mine(user: Principal, conn: sqlite3.Connection | None,
          targets: Sequence[Target]) -> list[Target]:
    """Drop the ones this person may not write to, without saying which.

    **Silently**, which is the deliberate part. A cascade reaches files the
    curator never named — the rest of a stack — and a household member can be
    shown a stack whose other takes were never shared with them. Refusing the
    whole edit would make an ordinary tag fail for a reason they cannot see;
    naming what was skipped would tell them a photograph is there. So their
    decision lands on the files that are theirs and stops at the ones that are
    not, which is the same answer the grid already gives them.

    An admin has no scope and pays nothing for this.
    """
    if user.scope is None or not targets:
        return list(targets)
    look = conn if conn is not None else db()
    try:
        mine = ix.matching(look, ix.Filters(viewer=user.scope, unfold=True),
                           [(t.folder, t.name) for t in targets])
    finally:
        if conn is None:
            look.close()
    return [t for t in targets if (t.folder, t.name) in mine]


def _cascade(conn: sqlite3.Connection | None,
             done: list[tuple[str, str]],
             top: str | None) -> list[history.Before]:
    """A stack's members follow the file that speaks for them.

    One rule, and it answers both directions. Stacked behind something else,
    and they go with it — stacks are flat, and the alternative is not a deeper
    stack but a stranded one, with the members a level down where no listing
    reaches them. Taken out of its stack, and they come out too: *unstack this*
    said of the file that speaks means the stack, not the one photograph, and
    leaving the others deferring to a file that defers to nobody would leave a
    stack nobody asked to keep.

    A file that speaks for nobody has nothing to cascade, which is why taking
    one photograph out of a stack takes only that one.

    Their previous values come back so the whole thing reverts as one gesture.
    Nothing recurses: this runs on every stacking write, so there is never more
    than one level to follow.
    """
    if conn is None:
        return []
    moved: list[history.Before] = []
    # Never the file being stacked *onto*: it is the one that speaks now, and
    # pointing it at itself hides it from every listing at once — a file behind
    # itself is behind something, so nothing shows it, and everything deferring
    # to it goes with it. Promoting one photograph out of a stack did exactly
    # that, and the whole stack vanished.
    written: set[str] = {f"{f}/{n}" for f, n in done}
    if top:
        written.add(top)
    for folder, name in done:
        for m_folder, m_name in ix.members(conn, f"{folder}/{name}"):
            if f"{m_folder}/{m_name}" in written:
                continue
            try:
                was, _, _ = _decide(m_folder, m_name,
                                    _Change(stacked_under=top), conn=conn)
            except HTTPException:
                continue
            moved.append(history.Before(m_folder, m_name, was,
                                        did={"stacked_under": top}))
    return moved


def _promote(conn: sqlite3.Connection, top: str) -> list[history.Before]:
    """Take the file that is about to speak out of whatever it was behind.

    A top is a file nothing is behind. Stacking onto one that is itself stacked
    leaves a ring — it defers to the file now deferring to it — and a ring
    shows nowhere, because every file in it is behind something.
    """
    folder, _, name = top.partition("/")
    if not folder or not name:
        return []
    try:
        media = _master_file(folder, name)
    except HTTPException:
        return []
    current = decisions.read(media)
    if current is None or not current.stacked_under:
        return []
    was, _, _ = _decide(folder, name, _Change(stacked_under=None), conn=conn)
    return [history.Before(folder, name, was, did={"stacked_under": None})]


def _summary(change: _Change) -> str:
    """What an operation did, in the words a person would use.

    Read months later off a list, so it says the value and the count — "gave
    family access to 312 files" is a thing you can recognise as the mistake
    you are looking for; "bulk edit" is not.

    The count is left as `{n}` for the log to fill in. A bulk edit arrives as
    several requests and no single one of them knows the total; only the reader,
    once it has put them back together, does.
    """
    files = "{n}"
    # Archiving is not giving somebody access, though it is written as an
    # audience — and *gave archived access to 3* is not how anybody says it.
    if list(change.add_audience) == [decisions.ARCHIVED]:
        return f"archived {files}"
    if list(change.remove_audience) == [decisions.ARCHIVED]:
        return f"took {files} out of the archive"
    if change.add_audience:
        return f"gave {', '.join(change.add_audience)} access to {files}"
    if change.remove_audience:
        return f"took {', '.join(change.remove_audience)} access "\
               f"from {files}"
    if change.add_people:
        return f"put {', '.join(change.add_people)} in {files}"
    if change.remove_people:
        return f"took {', '.join(change.remove_people)} out of {files}"
    if change.add_tags:
        return f"tagged {files} {', '.join(change.add_tags)}"
    if change.remove_tags:
        return f"untagged {', '.join(change.remove_tags)} on {files}"
    if not isinstance(change.no_stack, Unset):
        return (f"said {files} are not a stack" if change.no_stack
                else f"let {files} be suggested again")
    if not isinstance(change.stacked_under, Unset):
        return (f"stacked {files} under {change.stacked_under.rpartition('/')[2]}"
                if change.stacked_under else f"unstacked {files}")
    if not isinstance(change.deleted, Unset):
        return (f"deleted {files}" if change.deleted
                else f"restored {files}")
    if not isinstance(change.event, Unset):
        return (f"set the event on {files} to {change.event}"
                if change.event else f"cleared the event on {files}")
    if not isinstance(change.date_override, Unset):
        return (f"dated {files} {change.date_override}"
                if change.date_override
                else f"cleared the date override on {files}")
    return f"changed {files}"


def _master_file(folder: str, name: str, *, creating: bool = False) -> Path:
    """Resolve a master path from untrusted URL/body components.

    Same guard as `_serve`, and needed more here because this one writes: `..`
    in either component would otherwise drop an `.xmp` anywhere on the share.
    A sidecar is refused as a target too — decisions are about media, and
    `a.jpg.xmp.xmp` is nobody's intent.

    **A clip is a path with no file at it** (spec/clips.md §2). It is found by
    its sidecar, beside a source that is there — or, while it is being made,
    by the source alone, which is what `creating` allows and nothing else
    does: a decision about a clip that does not exist is not a way to invent
    one.
    """
    target = (webroots.MASTER_DIR / folder / name).resolve()
    if webroots.MASTER_DIR.resolve() not in target.parents:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad path")
    if name.lower().endswith(".xmp"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "that is a sidecar, not a file")
    if not target.is_file() and not _is_clip_path(target, creating=creating):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not in master")
    return target


def _is_clip_path(target: Path, *, creating: bool = False) -> bool:
    """Whether `target` names a clip whose source is in master."""
    source = clips.source_of(target.name)
    if source is None or not (target.parent / source).is_file():
        return False
    return creating or decisions.sidecar_path(target).is_file()


# --- installing it on a phone -------------------------------------------------
#
# What separates an app from a bookmark is three files and a header: a manifest
# saying what to call it and which icon to use, a service worker so the browser
# will offer to install it at all, and icons that survive being cropped to
# whatever shape the launcher likes. None of it is authenticated — a manifest and
# an icon say nothing about the library, and a login wall in front of them would
# only mean the install prompt never appears.


_MANIFEST: dict[str, object] = {
    "id": "/",
    "name": "pix",
    "short_name": "pix",
    "description": "The library, at home.",
    "start_url": "/",
    "scope": "/",
    # Standalone, not fullscreen: the status bar is worth keeping — knowing the
    # time and the battery while you cull for half an hour is not a loss of
    # screen worth arguing about.
    "display": "standalone",
    "orientation": "any",
    # Both the same, and both the page's own background: a launch screen that
    # flashes white before a dark app is the tell that something is a web page.
    "background_color": "#14161a",
    "theme_color": "#14161a",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png",
         "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "any"},
        {"src": "/icon-maskable-512.png", "sizes": "512x512",
         "type": "image/png", "purpose": "maskable"},
    ],
}

#: What the worker keeps. Deliberately not the pages.
#:
#: **The page script is inlined into its HTML**, so a cached page is a cached
#: *build* — and a tab serving last week's script against this week's API is the
#: failure this project has already spent an afternoon on. Navigations therefore
#: go to the network every time, and fall back to one honest offline page rather
#: than to a stale copy of the library.
_SERVICE_WORKER: str = asset("js/sw.js")


#: What the offline page does when it opens (spec/nas-app.md §8).
#:
#: A worker knows only that `fetch` rejected, and it rejects the same way for a
#: device with no network, a name that will not resolve, a certificate the
#: browser refused and a NAS that is switched off. Asserting one of them is how
#: this page came to say *not on the network* to somebody whose phone was on
#: wifi the whole time and whose DNS was the actual fault.
#:
#: So it asks `/healthz` — unauthenticated, tiny, and already reporting whether
#: the index can be read. Three answers come back from one question: it replies
#: and is well, it replies and says the app and the archive are out of step, or
#: it does not reply at all. Only the last is *offline*, and `navigator.onLine`
#: then separates having no network from having one that cannot reach home.
_OFFLINE_JS: str = asset("js/offline.js")


@app.get("/manifest.webmanifest")
def manifest() -> Response:
    """What to call this and which icon to use, for a launcher."""
    return JSONResponse(_MANIFEST, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker() -> Response:
    """Served from the root, which is what gives it the whole app as its scope.

    A worker can only ever control paths below where it was served from, so this
    cannot live under `/static/` without also sending a header to widen it —
    a detail that is invisible until installing silently does nothing.
    """
    return Response(_SERVICE_WORKER, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


@app.get("/offline", response_class=HTMLResponse)
def offline() -> HTMLResponse:
    """The one page the worker keeps, for when a request did not get through.

    **It finds out which kind of not-getting-through, rather than guessing.** A
    service worker cannot tell *no network* from *name will not resolve* from
    *server refused* — `fetch` rejects identically for all three — so the first
    version of this page asserted the most likely one and was wrong in a way
    that cost an afternoon of diagnosis. This one asks `/healthz`, which answers
    all three questions at once by either replying or not.
    """
    return _page("pix", """<div class="gate">
<h2 id="offhead">One moment</h2>
<p class="dim" id="offsay">Finding out what happened.</p>
<button class="primary" id="offgo" hidden>Try again</button>
</div>""" + f"<style>{_LOGIN_CSS}</style>"
                 + f"<script>{_OFFLINE_JS}</script>")


@app.get("/{icon}.png")
def icon(icon: str) -> Response:
    """One of the home-screen icons, by name."""
    path = (_ICONS / f"{icon}.png").resolve()
    if path.parent != _ICONS.resolve() or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such icon")
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Liveness for Container Manager — deliberately unauthenticated.

    `ok` stays true through a shape disagreement: the app is running and
    answering, it simply cannot read the projection it was given. Reporting that
    as *dead* would have Container Manager restart a container that is working
    perfectly, over and over, and the restart would not fix it.
    """
    state: dict[str, Any] = {"ok": True, "index": webroots.DB_PATH.is_file()}
    if webroots.DB_PATH.is_file():
        try:
            ix.open_ro(webroots.DB_PATH).close()
        except ix.StaleIndex as stale:
            state["index"] = False
            state["says"] = stale.say()
        except sqlite3.Error as e:
            state["index"] = False
            state["says"] = f"{type(e).__name__}: {e}"
    return state


# --- helpers -----------------------------------------------------------------


# --- signing in ---------------------------------------------------------------

_LOGIN_CSS: str = asset("css/login.css")


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request,
               user: Annotated[Principal | None, Depends(signed_in)],
               next: Annotated[str, Query()] = "/",
               bad: Annotated[int, Query()] = 0) -> Response:
    """The form. Already signed in? Then there is nothing to ask."""
    if user is not None and not bad:
        return RedirectResponse(_safe_next(next), status_code=303)
    warn = ('<p class="note">That did not match.</p>' if bad else "")
    hint = ("" if not accounts.admin_password_is_initial(store()) else
            '<p class="dim" style="margin-top:14px">First run: sign in as '
            '<b>admin</b> with the password <b>admin</b>, then change it.</p>')
    return _page("Sign in", f"""<div class="gate">
<div class="logo">{_logo_mark(34)}<span class="word">pi<b>x</b></span></div>
<h2>Sign in</h2>{warn}
<form method="post" action="/login">
<input type="hidden" name="next" value="{_h(_safe_next(next))}">
<label>Name</label><input name="name" autofocus autocomplete="username">
<label>Password</label>
<input name="password" type="password" autocomplete="current-password">
<button class="primary">Sign in</button>
</form>{hint}</div>""" + f"<style>{_LOGIN_CSS}</style>")


async def _form(request: Request) -> dict[str, str]:
    """A urlencoded form body, as plain strings.

    Parsed here rather than through FastAPI's `Form()` or Starlette's
    `request.form()`, both of which require `python-multipart` even for a body
    that needs no multipart parsing at all. The app ships without Pillow and
    without ffmpeg on purpose; a dependency for reading two fields off a login
    form does not earn its place either.
    """
    raw = (await request.body()).decode("utf-8", "replace")
    return {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}


@app.post("/login")
async def login(request: Request) -> Response:
    """Check the credentials and hand out a session cookie."""
    form = await _form(request)
    name = accounts.canonical(form.get("name", ""))
    password = form.get("password", "")
    next = form.get("next", "/")
    book = store()
    if not accounts.check(book, name, password):
        # No detail about which half was wrong: it would turn the form into a
        # way to ask whether an account exists.
        return RedirectResponse(
            f"/login?bad=1&next={_q(_safe_next(next))}", status_code=303)

    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(
        accounts.COOKIE, accounts.mint(book, name),
        max_age=accounts.SESSION_DAYS * 86400,
        # HttpOnly so page scripts cannot read it, Lax so following a link into
        # the app still arrives signed in. Not `secure`: this is served over
        # plain HTTP on a LAN, and a cookie marked secure would simply never be
        # sent, which reads as "login silently does nothing".
        httponly=True, samesite="lax", path="/")
    return response


@app.post("/logout")
def logout() -> Response:
    """Forget the session. The reason the cookie exists at all."""
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(accounts.COOKIE, path="/")
    return response


def _safe_next(target: str) -> str:
    """Only ever redirect inside this app.

    An open redirect turns the login form into a way to send somebody to
    somewhere else entirely, wearing this app's address.
    """
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


# --- accounts -----------------------------------------------------------------

_ACCOUNTS_CSS = """
.acct td input, .acct td select { background:#14161a; color:var(--fg);
    border:1px solid var(--line); border-radius:4px; padding:4px 7px;
    font:inherit; }
.acct form { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.acct button { margin:0; }
"""


@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(user: Annotated[Principal, Depends(require_admin)],
                  msg: Annotated[str, Query()] = "") -> HTMLResponse:
    """Who exists, what roles they hold — admin only.

    In the app rather than in environment variables because adding a person is
    a household event, not a deployment: it should not need a shell, a text
    editor and a container restart.
    """
    book = store()
    groups = sorted(set(book.groups))
    rows = "".join(
        f'<tr><td>{_h(a.name)}</td>'
        f'<td class="dim">{_h(", ".join(a.groups)) or "&mdash;"}</td>'
        f'<td><form method="post" action="/accounts/save">'
        f'<input type="hidden" name="name" value="{_h(a.name)}">'
        f'<input name="groups" value="{_h(", ".join(a.groups))}" '
        f'placeholder="groups, comma separated" size="22">'
        f'<input name="password" type="password" placeholder="new password" '
        f'size="14" autocomplete="new-password">'
        f'<button>Save</button></form></td>'
        f'<td><form method="post" action="/accounts/delete" '
        f'onsubmit="return confirm(\'Remove {_h(a.name)}?\')">'
        f'<input type="hidden" name="name" value="{_h(a.name)}">'
        f'<button>Remove</button></form></td></tr>'
        for a in sorted(book.users.values(), key=lambda a: a.name))

    note = f'<p class="note">{_h(msg)}</p>' if msg else ""
    warn = ("" if not accounts.admin_password_is_initial(book) else
            '<p class="note">The admin account still has its shipped password. '
            'Change it below.</p>')
    return _page("Accounts", f"""{note}{warn}
<h2 class="year">People</h2>
<table class="acct"><thead><tr><th>Name</th><th>Groups</th>
<th>Change</th><th></th></tr></thead><tbody>{rows}</tbody></table>

<h2 class="year">Add someone</h2>
<form method="post" action="/accounts/save" class="acct">
<input name="name" placeholder="name" size="14" autocomplete="off">
<input name="groups" placeholder="groups, comma separated" size="22">
<input name="password" type="password" placeholder="password" size="14"
       autocomplete="new-password">
<button class="primary">Add</button></form>

<h2 class="year">The usual audience</h2>
<p class="dim">Most photographs end up shared with the same people. Name
that audience and the grid stops printing it on every thumbnail — what is
left is the exceptions, which is the part worth seeing.</p>
<form method="post" action="/accounts/usual" class="acct">
<input name="usual" value="{_h(book.usual)}" size="20"
       placeholder="family" autocomplete="off">
<button>Save</button></form>

<h2 class="year">Groups</h2>
<p class="dim">A grant names a person or a group and access cannot tell them
apart, so sharing with <b>family</b> reaches everyone in it.
{_h(", ".join(groups)) or "None yet."}</p>
<form method="post" action="/accounts/groups" class="acct">
<input name="groups" value="{_h(", ".join(groups))}" size="40"
       placeholder="family, parents, tv">
<button>Save groups</button></form>

<h2 class="year">The admin account</h2>
<p class="dim">Built in, cannot be removed, sees everything, and is never
something to share with.</p>
<form method="post" action="/accounts/save" class="acct">
<input type="hidden" name="name" value="{accounts.ADMIN}">
<input name="password" type="password" placeholder="new admin password"
       size="20" autocomplete="new-password">
<button>Change</button></form>
<style>{_ACCOUNTS_CSS}</style>""", user=user)


@app.post("/accounts/save")
async def accounts_save(request: Request,
                        user: Annotated[Principal,
                                        Depends(require_admin)]) -> Response:
    """Create or update one account."""
    form = await _form(request)
    name = form.get("name", "")
    password = form.get("password", "")
    book = store()
    who = accounts.canonical(name)
    if not who:
        return _back("a name is required")
    if who == decisions.ARCHIVED:
        return _back(f"{who} is reserved — it is how a file is kept out of "
                     "every view")

    existing = book.users.get(who)
    if existing is None and not password:
        return _back(f"{who} needs a password to sign in with")

    hashed = auth.hash_password(password) if password else (
        existing.password if existing else "")
    # Absent and empty are different: the admin form submits no groups field
    # at all and must not clear them, while an empty box on the people form is
    # how you take somebody out of every group.
    kept = (tuple(sorted({accounts.canonical(g)
                          for g in form["groups"].split(",") if g.strip()}
                         - accounts.RESERVED))
            if "groups" in form else (existing.groups if existing else ()))
    book.users[who] = accounts.Account(who, hashed, kept)
    # A group used here should exist without having to be declared twice.
    book.groups = sorted({*book.groups, *kept})
    accounts.save(book)
    return _back(f"saved {who}")


@app.post("/accounts/delete")
async def accounts_delete(
    request: Request,
    user: Annotated[Principal, Depends(require_admin)],
) -> Response:
    """Remove an account. Files shared with them keep the grant.

    Deliberately: the share is a decision recorded in master, and deleting a
    login is not a statement about the photographs. Recreating the name
    restores the access, and nothing had to be rewritten across the archive.
    """
    name = (await _form(request)).get("name", "")
    name = accounts.canonical(name)
    book = store()
    if name == accounts.ADMIN:
        return _back("the admin account is built in")
    book.users.pop(name, None)
    accounts.save(book)
    return _back(f"removed {name}")


@app.post("/accounts/usual")
async def accounts_usual(
    request: Request,
    user: Annotated[Principal, Depends(require_admin)],
) -> Response:
    """Name the audience the grid should stay quiet about."""
    book = store()
    book.usual = accounts.canonical((await _form(request)).get("usual", ""))
    accounts.save(book)
    return _back(f"the usual audience is {book.usual or 'unset'}")


@app.post("/accounts/groups")
async def accounts_groups(
    request: Request,
    user: Annotated[Principal, Depends(require_admin)],
) -> Response:
    """Set the groups that exist.

    Kept explicitly so a group can exist before anyone is in it — otherwise
    creating `tv` would be impossible until something had already been shared
    with it.
    """
    raw = (await _form(request)).get("groups", "")
    book = store()
    book.groups = sorted({accounts.canonical(g) for g in raw.split(",")
                          if g.strip()} - accounts.RESERVED)
    accounts.save(book)
    return _back("saved groups")


def _back(message: str) -> Response:
    return RedirectResponse(f"/accounts?msg={_q(message)}", status_code=303)


# --- history ------------------------------------------------------------------

def _byline(curators: tuple[str, ...], who: str) -> str:
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
        where = f"/history?who={_q(name)}" if name else "/history"
        return f'<a href="{where}"{here}>{_h(label)}</a>'
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
        f'<tr><td class="dim">{_h(_when(op.when))}</td>'
        + (f'<td><a href="/browse?op={_q(op.id)}" '
           f'title="Look at these files">{_h(op.summary)}</a></td>'
           if op.files else
           # A purge names no files: they are gone, the index rows with them.
           # Offering a link to them would lead to an empty grid that reads as
           # broken rather than as *there is nothing left to look at*.
           f'<td>{_h(op.summary)}</td>')
        + ("" if said else
           # The name is the way to ask for only their work: the gesture is on
           # the thing it is about, rather than only on a control above the
           # table that has to be found first.
           f'<td class="dim"><a href="/history?who={_q(op.who)}" '
           f'title="Only what {_h(op.who)} changed">{_h(op.who)}</a></td>')
        + ('<td class="dim">undone</td>' if op.id in log.undone else
           '<td class="dim">a revert</td>' if op.reverts else
           f'<td><form method="post" action="/history/revert">'
           f'<input type="hidden" name="id" value="{_h(op.id)}">'
           # So a revert comes back to the list you were reading rather than
           # to everybody's.
           + (f'<input type="hidden" name="who" value="{_h(who)}">'
              if who else "")
           + f'<button>Revert</button></form></td>')
        + "</tr>"
        for op in ops
    )
    # A revert that left files alone says how many. The number is the work
    # still to look at, so it comes with the way to go and look at it.
    seeing = (f' <a href="/browse?op={_q(look)}&amp;stale=1">'
              f'see the ones it left &rarr;</a>' if look else "")
    note = f'<p class="note">{_h(msg)}{seeing}</p>' if msg else ""
    byline = _byline(log.curators, who)
    if not ops:
        empty = (f'Nothing by {_h(who)}.' if who else "Nothing changed yet.")
        return _page("History", f'{note}{byline}<p class="empty">{empty}</p>',
                     user=user)
    return _page("History", f"""{note}{byline}
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
    form = await _form(request)
    op_id = form.get("id", "")
    # Whose work was being read. A revert that dropped the filter would answer
    # *and now here is everybody again*, which is not what pressing a button
    # in a narrowed list asks for.
    back = f"&who={_q(form.get('who', ''))}" if form.get("who") else ""
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
            if not _under(webroots.MASTER_DIR, media) or not (
                    media.is_file() or _is_clip_path(media, creating=True)):
                failed += 1
                continue
            with _write_lock:
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
                _recut(item.folder, item.name)
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
    tail = f"&look={_q(op.id)}" if moved else ""
    return RedirectResponse(f"/history?msg={quote(said)}{tail}{back}",
                            status_code=303)


