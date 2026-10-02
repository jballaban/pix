"""The frame every page is drawn in: the stylesheet and shell scripts, the top
bar, the account and Display menus, the action buttons, and the one function
that turns a body into a whole page.
"""

from __future__ import annotations

import sqlite3

from fastapi import HTTPException
from fastapi.responses import HTMLResponse

from pix import __version__ as _PIX_VERSION
from pix.nas import index as ix
from pix.nas.assets import asset
from pix.nas.webapp.app import db, Principal
from pix.nas.webapp.marks import (
    ACT_MARKS,
    BACK,
    FAVICON,
    FILES_MARK,
    FOLDERS_MARK,
    GEAR,
    logo_mark,
    mark,
)
from pix.nas.webapp.permissions import may
from pix.nas.webapp.text import h, q
from pix.nas.webapp.vocab import HOME_GROUPING, INFO, SIZES


STYLE: str = asset("css/app.css")


def zoom(page: str, query: str) -> str:
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
        kept.append(f"group={q(HOME_GROUPING)}")
    return page + ("?" + "&".join(kept) if kept else "")


def brand(zoom: str) -> str:
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
                f'{logo_mark(26)}</a>')
    folders = zoom.startswith("/browse")
    say = "Show the files" if folders else "Show the folders"
    return (f'<a class="brand" href="{h(zoom)}" title="{say}" '
            f'aria-label="{say}">'
            f'{FOLDERS_MARK if folders else FILES_MARK}</a>')


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
INSTALL_JS: str = asset("js/install.js")


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
BAR_JS: str = asset("js/bar.js")


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
MENU_JS: str = asset("js/menu.js")


#: The Display choices, applied to `<html>` **before the page paints**, so a
#: fact turned off is never drawn and then taken away. Only the attributes:
#: the stylesheet does the hiding, and the page script moves what is shown
#: into its lane. Anything not one of the known values is left off, which
#: leaves the default the stylesheet already assumes.
INFO_BOOT: str = """<script>try{var s=JSON.parse(localStorage.getItem('pix2.info')
||'{}'),ok={off:1,top:1,bot:1,on:1};for(var k in s)if(ok[s[k]]&&/^[a-z]+$/.test(k))
document.documentElement.setAttribute('data-info-'+k,s[k]);}catch(e){}</script>"""


def page(title: str, body: str, *, tools: str = "", rows: str = "",
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
<link rel="icon" href="{FAVICON}"><style>{STYLE}</style>{INFO_BOOT}</head><body>
<div class="topbar">
<div class="row"><button class="back" id="back" aria-label="Back"
 title="Back">{BACK}</button>{brand(zoom)}{tools}
<span class="spacer"></span><div class="right">{bar}{whoami(user, right, info)}</div></div>{rows}
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
<script>{BAR_JS}</script>
<script>{MENU_JS}</script>
<script>{INSTALL_JS}</script>
</body></html>""")


def whoami(user: Principal | None, extra: str = "",
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
    waiting = binned() if user.is_admin else 0
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
        f'<span class="name">{h(user.name)}</span>'
        f'<i class="caret">&#9662;</i>'
        f'<span class="gearbtn" aria-hidden="true">{GEAR}</span>'
        f'<i class="dot"></i></summary>'
        f'<div class="memenu">'
        f'<span class="whoami"><b>{h(user.name)}</b>'
        f'<span class="role">'
        f'{"Administrator" if user.is_admin else "Household"}</span></span>'
        f'{view}{admin}'
        f'<span class="info">{info}'
        f'<span class="line ver">v{_PIX_VERSION}</span></span>'
        f'<form method="post" action="/logout">'
        f'<button>Sign out</button></form></div></details>')


def infoset(key: str, label: str, default: str) -> str:
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


def display_rows(user: Principal) -> str:
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
            f'</span>{sizeset()}</span>'
            '<span class="rowhead">Info</span>'
            + "".join(infoset(*i) for i in INFO
                      if i[0] != "access" or user.is_admin)
            + apart)


def display_menu(user: Principal) -> str:
    """The Display menu, for the bar.

    The size control used to stand there on its own. It is still the first
    row, because it is still the one you reach for most — but it is one of
    six ways of saying how you want to look, and a bar with six segmented
    controls in it is a bar about itself. Narrow, the same rows are a group of
    the account menu instead, the way the size control already was.
    """
    return ('<details class="me disp"><summary><span class="name">Display'
            '</span><i class="caret">&#9662;</i></summary>'
            f'<div class="memenu">{display_rows(user)}</div></details>')


def sizeset() -> str:
    """The three sizes as one control. No `id`: there are two of these."""
    opts = "".join(
        f'<button type="button" class="sizeopt" data-size="{key}" '
        f'aria-pressed="false" title="{say}">{letter}</button>'
        for key, letter, say in SIZES)
    return (f'<span class="sizeset" role="group" '
            f'aria-label="Thumbnail size">{opts}</span>')


def binned() -> int:
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


#: The takeover, which any page that writes has to carry.
#:
#: A write is a run of requests over SMB and takes seconds; without this the
#: screen simply sits there. It lived in the grid's markup alone, which was
#: true for exactly as long as the grid was the only page that wrote — and
#: when the landing page learned to, every `if(working)` guard in the script
#: quietly did nothing and a folder edit ran with no sign of it at all.
WORKING: str = """<div id="working">
  <div class="what" id="workwhat"></div>
  <div class="bar"><i id="workbar"></i></div>
  <div class="tally" id="worktally"></div>
  <button id="worksave" class="primary" hidden>Save</button>
  <button id="workstop">Stop</button>
</div>"""


def act(act: str, word: str, cls: str = "", *,
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
            f'aria-label="{name}">{mark(ACT_MARKS.get(act, ""))}'
            f'<span class="word">{word}</span></button>')


LOGIN_CSS: str = asset("css/login.css")


ACCOUNTS_CSS = """
.acct td input, .acct td select { background:#14161a; color:var(--fg);
    border:1px solid var(--line); border-radius:4px; padding:4px 7px;
    font:inherit; }
.acct form { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.acct button { margin:0; }
"""
