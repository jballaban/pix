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
import sqlite3
import threading
import time
from urllib.parse import parse_qs, quote
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path
from typing import Annotated, Any, Sequence, cast

from fastapi import (
    Body, Depends, FastAPI, HTTPException, Query, Request, status,
)
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from pix import datestr
from pix.nas import accounts
from pix.nas import auth
from pix.nas import decisions
from pix.nas import history
from pix.nas import index as ix
from pix.nas.const import (
    INDEX_DB, MASTER_DIR, META_DIR, PREVIEW_DIR, RENDER_DIR, THUMB_DIR,
)
from pix.nas.decisions import Decision, Unset

#: Re-exported so the CLI and tests have one name for it.
DB_PATH: Path = INDEX_DB

app: FastAPI = FastAPI(title="pix2", docs_url=None, redoc_url=None)


@app.exception_handler(status.HTTP_401_UNAUTHORIZED)
async def _unauthenticated(  # pyright: ignore[reportUnusedFunction]
        request: Request,
                           exc: Exception) -> Response:
    """Send a browser to the form; tell a script the truth.

    Deliberately **no** `WWW-Authenticate` header: it would make the browser
    pop its own credential box and start caching, which is the behaviour the
    cookie exists to replace.
    """
    if "text/html" in request.headers.get("accept", ""):
        nxt = quote(str(request.url.path or "/"), safe="")
        return RedirectResponse(f"/login?next={nxt}", status_code=303)
    return JSONResponse({"detail": "sign in"}, status_code=401)
_security = HTTPBasic(auto_error=False)

#: Serializes decision writes. Spec §8 makes last-write-wins the conflict policy
#: and leans on exactly this to keep it a *policy* question: two people tiering
#: the same photo pick a winner, they never interleave into a corrupt sidecar.
_write_lock: threading.Lock = threading.Lock()


# --- auth --------------------------------------------------------------------

def store() -> accounts.Store:
    """The account store, read per request.

    Re-read rather than cached because it is small and changes rarely, and a
    stale cache here means a removed account still works — the one kind of
    staleness an access system cannot have.
    """
    return accounts.load()


@dataclass(frozen=True)
class Principal:
    """Who is asking, and what that entitles them to.

    `scope` is derived here, once, from the credentials — never from anything
    the request can influence.
    """

    name: str
    is_admin: bool

    #: Every name this person's access can be granted to — themselves, and the
    #: roles they hold. A share names one or the other and the check cannot
    #: tell them apart, which is what keeps roles from being a second mechanism.
    grants: frozenset[str] = frozenset()

    @property
    def scope(self) -> frozenset[str] | None:
        """What to restrict queries to, or None for an admin (no restriction)."""
        return None if self.is_admin else (self.grants | {self.name})


def _principal(book: accounts.Store, name: str) -> Principal:
    return Principal(name, is_admin=(name == accounts.ADMIN),
                     grants=book.grants(name))


def signed_in(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_security)],
) -> Principal | None:
    """Who this request is, or None.

    Two ways in. The **cookie** is what a browser uses, because HTTP Basic
    cannot log out — browsers cache the credentials and offer no way to clear
    them, which makes *switch to admin and back* impossible. **Basic** is still
    accepted for scripting, but never challenged for: with no
    `WWW-Authenticate` header a browser will not start caching one, so the
    cookie stays the only thing it holds.
    """
    book = store()
    name = accounts.identify(book, request.cookies.get(accounts.COOKIE))
    if name and (name == accounts.ADMIN or name in book.users):
        return _principal(book, name)
    if credentials and accounts.check(book, credentials.username,
                                      credentials.password):
        return _principal(book, accounts.canonical(credentials.username))
    return None


def require_user(
    user: Annotated[Principal | None, Depends(signed_in)],
) -> Principal:
    """Refuse anyone who is not signed in."""
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in")
    return user


def require_admin(user: Annotated[Principal, Depends(require_user)]) -> Principal:
    """Only an administrator may change who can see what."""
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "only an administrator can change decisions")
    return user


# --- data --------------------------------------------------------------------

def db() -> sqlite3.Connection:
    """A connection to the index, or a clear error if it has not been built."""
    if not DB_PATH.is_file():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"index not built — run `pix2 index` (expected at {DB_PATH})")
    return ix.open_ro(DB_PATH)


# --- pages -------------------------------------------------------------------

_STYLE = """
:root { color-scheme: dark; --bg:#14161a; --fg:#e7e9ee; --dim:#8b93a3;
        --line:#272b33; --accent:#6aa3ff; --keep:#56c16a; --top:#e3b341;
        --panel:#1b1e24; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5
       system-ui,-apple-system,Segoe UI,sans-serif; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
.dim { color:var(--dim); }
main { padding:16px 20px 40px; }

/* The bar never leaves: filters are the address of what you are looking at,
   and losing them 2,000 thumbnails down is losing your place. */
.topbar { position:sticky; top:0; z-index:5; background:var(--bg);
          border-bottom:1px solid var(--line); padding:9px 20px; }
.row { display:flex; gap:9px; align-items:center; flex-wrap:wrap;
       min-height:30px; }
.row + .row { margin-top:8px; border-top:1px solid var(--line); padding-top:8px; }
.brand { font-weight:600; letter-spacing:.02em; color:var(--fg); }
.count { font-variant-numeric:tabular-nums; color:var(--dim);
         white-space:nowrap; }
.spacer { flex:1; }
/* Counts and messages along the bottom, so the header is only controls:
   every row of chrome up there is a row of photographs pushed off. */
.footbar { position:fixed; left:0; right:0; bottom:0; z-index:4;
           background:var(--bg); border-top:1px solid var(--line);
           padding:6px 20px; display:flex; gap:14px; align-items:baseline;
           flex-wrap:wrap; font-size:12px; }
.footbar:empty { display:none; }
.footbar .note { margin:0; margin-left:auto; }
.note.loud { background:#5a1d16; color:#ffd9d2; padding:2px 8px;
             border-radius:3px; font-weight:600; }
main { padding-bottom:48px; }
.hint { color:var(--dim); font-size:12px; }
.hint b { color:var(--fg); font-weight:600; }

button, .chip { background:#222833; color:var(--fg); border:1px solid var(--line);
        border-radius:4px; padding:4px 10px; font:inherit; cursor:pointer; }
button:hover:not(:disabled), .chip:hover { border-color:var(--accent); }
button:disabled { opacity:.4; cursor:default; }
button.primary { background:var(--accent); color:#0d0f12; border-color:var(--accent);
                 font-weight:600; }
.chip.on { border-color:var(--accent); background:#20293a; }
.chip .val { color:var(--accent); margin-left:5px; }
.chip .x { color:var(--dim); margin-left:6px; }
.chip .x:hover { color:var(--fg); }
.sep { width:1px; height:20px; background:var(--line); }

/* menu */
#menu { position:absolute; z-index:20; width:300px; max-height:60vh;
        background:var(--panel); border:1px solid var(--line); border-radius:6px;
        box-shadow:0 10px 30px #0009; display:flex; flex-direction:column; }
/* An id selector beats the user agent's `[hidden] { display:none }`, so the
   rule above quietly won and `hidden = true` set a flag that hid nothing —
   the menu could be opened and never dismissed. */
#menu[hidden] { display:none; }
#menu input { background:#14161a; color:var(--fg); border:0;
              border-bottom:1px solid var(--line); padding:9px 11px; font:inherit;
              border-radius:6px 6px 0 0; outline:none; width:100%; }
#menulist { overflow-y:auto; padding:4px 0; }
.opt { display:flex; gap:8px; padding:5px 11px; cursor:pointer;
       align-items:baseline; }
.opt:hover, .opt.cur { background:#2a3340; }
.opt .n { margin-left:auto; color:var(--dim); font-variant-numeric:tabular-nums;
          font-size:12px; }
/* Three states, the way a file tree shows them: every one, some, none. A
   selection is not one thing, and a two-state box would have to lie. */
.opt .box { width:13px; flex:none; text-align:center; color:var(--keep); }
.opt[data-state="some"] .box { color:var(--top); }
.opt.new { color:var(--keep); }
.band { padding:7px 11px 3px; color:var(--dim); font-size:11px;
        text-transform:uppercase; letter-spacing:.07em; }
.band + .band { display:none; }
#menu .form { padding:10px 11px; display:flex; gap:6px; flex-wrap:wrap;
              align-items:center; }
#menu .form input { width:64px; border:1px solid var(--line); border-radius:4px;
                    padding:4px 6px; }
#menu .form label { color:var(--dim); font-size:12px; }

/* grid */
.grid { display:grid; gap:6px;
        grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); }
/* A heading spans every column, so one flow holds headings and thumbnails —
   which keeps arrow-key movement walking straight through the sections
   rather than having to know they are there. */
h3.group { grid-column:1/-1; margin:18px 0 2px; font-size:13px;
           font-weight:600; display:flex; gap:8px; align-items:center;
           border-bottom:1px solid var(--line); padding-bottom:5px; }
h3.group:first-child { margin-top:0; }
h3.group > span.dim { font-weight:400;
                      font-variant-numeric:tabular-nums; }
/* A path, so the last crumb — the one that actually changed — is the one
   that reads loudest. */
.crumbs { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.crumb { display:inline-flex; align-items:center; }
.crumbs .sep { color:var(--dim); font-weight:400; }
.crumb:not(:last-child) .grpname { color:var(--dim); font-weight:400; }
.rmgrp { background:none; border:0; margin:0; padding:0 4px; color:var(--dim);
         font:inherit; cursor:pointer; opacity:0; transition:opacity .1s; }
.crumb:hover .rmgrp, .rmgrp:focus { opacity:1; }
.rmgrp:hover { color:#ffb4a2; }
/* The name is the control: click it to regroup, `+` to group within it. */
.grpname { background:none; border:0; padding:0; margin:0; color:inherit;
           font:inherit; cursor:pointer; }
.grpname:hover { color:var(--accent); text-decoration:underline; }
/* Beside the name it belongs to, not marooned at the end of the row, where it
   went unnoticed. Dim rather than hidden: a control you cannot see until you
   hover the right thing is a control you never learn is there, and a page of
   faint plus signs is quiet enough. */
.addgrp { margin:0; padding:0 7px; line-height:1.3; font-size:14px;
          opacity:.3; transition:opacity .1s; }
h3.group:hover .addgrp, .addgrp:focus { opacity:1; }
.addgrp:hover { border-color:var(--accent); color:var(--accent); }
/* Same three states as the menus: the whole section, part of it, none. */
.grppick { margin:0; padding:0; width:16px; height:16px; flex:none;
           border-radius:50%; background:transparent;
           border:1.5px solid var(--dim); }
h3.group[data-state="all"] .grppick { background:var(--accent);
  border-color:var(--accent); }
h3.group[data-state="some"] .grppick { background:var(--top);
  border-color:var(--top); }
.cell { position:relative; aspect-ratio:1; background:#0d0f12; overflow:hidden;
        border-radius:3px; cursor:pointer; }
.cell img { width:100%; height:100%; object-fit:cover; display:block; }
/* The cursor is normally also ticked, so it only needs to say *which one the
   keyboard is on* — a lighter ring inside the selection's. */
.cell.cur { outline:2px dashed var(--accent); outline-offset:-5px;
            z-index:1; }
.cell.picked { outline:3px solid var(--accent); outline-offset:-3px;
               z-index:1; }
.cell.picked img { opacity:.75; }
.badge { position:absolute; right:4px; bottom:4px; background:#000a;
         padding:1px 5px; border-radius:3px; font-size:11px; }
/* Out of the way until wanted: 2,000 circles over 2,000 photographs is a page
   about its own controls. Hover reveals it, and a made choice keeps it. */
.pick { position:absolute; left:5px; top:5px; width:20px; height:20px; padding:0;
        border-radius:50%; background:#000a; border:1.5px solid #fff9;
        opacity:0; transition:opacity .08s; z-index:2; }
.cell:hover .pick, .cell.cur .pick { opacity:1; }
.cell.picked .pick { opacity:1; background:var(--accent); border-color:var(--accent); }
/* Touch has no hover, so there the circle is the only way to select at all. */
@media (hover: none) { .pick { opacity:.55; } }
.cell.picked .pick::after { content:"\\2713"; color:#0d0f12; font-weight:700;
                            font-size:13px; line-height:17px; }
/* Shared is the decided state, so it is what reads as finished; a file with no
   audience is the work still to do and looks untouched. An inset ring, so it
   can coexist with the selection outline — the two answer different questions
   and a cull needs both at once. */
.cell[data-audience]:not([data-audience=""]) {
  box-shadow: inset 0 0 0 3px var(--keep); }
/* Access bottom-left, tags top-right, duration bottom-right — three corners,
   nothing overlapping. Each value is its own chip: a thumbnail is 150px and
   three role names are not, so one run of text just gets cut off mid-word
   with no way to find out what it said. */
.who, .tags { position:absolute; display:flex; gap:3px; overflow:hidden;
              max-width:64%; }
.who  { left:5px; bottom:4px; }
.tags { right:4px; top:4px; justify-content:flex-end; max-width:72%; }
.who i, .tags i { font-style:normal; max-width:80px; overflow:hidden;
                  white-space:nowrap; text-overflow:ellipsis;
                  padding:1px 5px; border-radius:3px; background:#000b;
                  font-size:10px; font-weight:600; }
.who i  { color:var(--keep); }
/* Nobody can see this yet — quiet, because early on that is most of the
   library, and unmistakable once it is not. */
.unshared { position:absolute; left:6px; bottom:6px; width:8px; height:8px;
            border-radius:50%; background:var(--top);
            box-shadow:0 0 0 2px #000a; }
.tags i { color:#fff; }

table { border-collapse:collapse; width:100%; max-width:900px; }
th,td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); }
th { color:var(--dim); font-weight:500; font-size:12px;
     text-transform:uppercase; letter-spacing:.06em; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
td.done { color:var(--keep); }
h2.year { font-size:15px; margin:26px 0 8px; display:flex; gap:12px;
          align-items:baseline; flex-wrap:wrap; }
h2.year:first-of-type { margin-top:12px; }
h2.year span { font-size:13px; font-weight:400; }
.note { color:#ffb4a2; margin:8px 0 0; }
.gate .note, main > .note { margin:8px 0; }
.warn { color:#e3b341; }
.who-link { margin-left:10px; display:inline-flex; align-items:center; }
.who-link button { padding:3px 9px; margin:0; }
.empty { color:var(--dim); padding:40px 0; }

#viewer { position:fixed; inset:0; background:#000e; display:none; z-index:30; }
#viewer.on { display:flex; }
.stage { flex:1; min-width:0; display:flex; flex-direction:column;
         align-items:center; justify-content:center; padding:12px; }
.stage img, .stage video { max-width:100%; max-height:86vh;
                           object-fit:contain; display:none; }
.stage img.on, .stage video.on { display:block; }
.stage .meta { padding:10px; color:var(--dim); font-size:12px;
               text-align:center; }
/* A reserved column, not an overlay: metadata you have to summon and that
   then covers the photograph is metadata nobody consults while looking. */
#rail { width:330px; flex:none; background:var(--panel); overflow-y:auto;
        border-left:1px solid var(--line); padding:14px 16px 30px;
        font-size:13px; }
#viewer.norail #rail { display:none; }
#railtoggle { position:absolute; top:10px; right:12px; z-index:2;
              margin:0; opacity:.75; }
#viewclose { position:absolute; top:10px; left:12px; z-index:2; margin:0;
             opacity:.75; font-size:17px; line-height:1; padding:2px 10px; }
#railtoggle:hover, #viewclose:hover { opacity:1; }
.rail-h { color:var(--dim); font-size:11px; text-transform:uppercase;
          letter-spacing:.07em; margin:16px 0 5px; }
.rail-h:first-child { margin-top:0; }
.kv { display:grid; grid-template-columns:104px 1fr; gap:2px 10px;
      align-items:baseline; }
.kv dt { color:var(--dim); }
.kv dd { margin:0; overflow-wrap:anywhere; }
.kv dd.was { color:var(--dim); text-decoration:line-through; }
.kv dd .set { color:var(--top); }
.pill { display:inline-block; background:#2a3340; border-radius:3px;
        padding:0 6px; margin:0 4px 4px 0; font-size:12px; }
#rail details { margin-top:14px; }
#rail summary { cursor:pointer; color:var(--dim); }
#rail details .kv { grid-template-columns:1fr; gap:0; margin-top:8px; }
#rail details dt { margin-top:6px; font-size:11px; }
"""


def _page(title: str, body: str, *, tools: str = "", rows: str = "",
          footer: str = "", script: str = "",
          user: Principal | None = None) -> HTMLResponse:
    """One shell.

    `tools` sits beside the brand on the first row, `rows` are whole extra rows
    below it, and `footer` is the strip along the bottom. Counts and messages
    live down there so the header is only controls — every row of chrome at the
    top is a row of photographs pushed off the screen.

    `script` goes **last**, after the footer. A page script that runs from
    inside `<main>` cannot see anything below it: moving the count and the
    message line into the footer left both as `null`, and the first thing every
    write did was set a message — so nothing was ever sent, silently.
    """
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{_STYLE}</style></head><body>
<div class="topbar">
<div class="row"><a class="brand" href="/">pix2</a>{tools}
<span class="spacer"></span>{_whoami(user)}</div>{rows}
</div><main>{body}</main>
<footer class="footbar">{footer}</footer>
{script}</body></html>""")


def _whoami(user: Principal | None) -> str:
    """Who you are signed in as, and the way out.

    Always visible because this app is used as two different people — the
    owner curating, and the admin granting access — and acting as the wrong
    one is invisible until something is shared with the wrong household.
    """
    if user is None:
        return '<a class="who-link" href="/login">Sign in</a>'
    manage = ('<a class="who-link" href="/history">History</a>'
              '<a class="who-link" href="/accounts">Accounts</a>'
              if user.is_admin else "")
    return (f'<span class="who-link dim">{_h(user.name)}</span>{manage}'
            '<form method="post" action="/logout" class="who-link">'
            '<button>Sign out</button></form>')


def filters(
    user: Annotated[Principal, Depends(require_user)],
    event: Annotated[str | None, Query()] = None,
    year: Annotated[str | None, Query()] = None,
    tag: Annotated[str | None, Query()] = None,
    audience: Annotated[str | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
    band: Annotated[str | None, Query()] = None,
) -> ix.Filters:
    """The current view, read off the query string.

    In the URL rather than in the page's memory, so a view is a link: shareable,
    bookmarkable, and survivable across the reload that a bulk edit sometimes
    wants. It is also what makes the browser's back button mean "the filter I
    had before", which is the only undo a filter needs.

    `viewer` is **not** among them. It comes from the credentials and rides on
    every query, so a non-admin cannot widen their own view by editing the
    address bar — the one thing a URL-shaped filter model must not allow.
    """
    return ix.Filters(event=event, year=year, tag=tag, audience=audience,
                      kind=kind, band=band, viewer=user.scope)


@app.get("/", response_class=HTMLResponse)
def home(user: Annotated[Principal, Depends(require_user)],
         view: Annotated[ix.Filters, Depends(filters)]) -> HTMLResponse:
    """The library by year, then by event — the two ways anyone looks for a photo.

    Every row is a filter: clicking a year opens that year, clicking an event
    opens that year *and* event. So the landing page is a shortcut into `/browse`
    rather than a separate way of seeing things.
    """
    conn = db()
    s = ix.summary(conn, view)
    rows = ix.events(conn, view)

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

    if not rows:
        return _page("pix2", '<p class="empty">Nothing indexed yet.</p>',
                     user=user)

    years: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        years.setdefault(str(row["year"]), []).append(row)

    sections = "".join(_year_section(year, group)
                       for year, group in years.items())
    return _page("pix2", f"""<p class="dim">{head}</p>
<p><a href="/browse">Browse everything &rarr;</a></p>{sections}""", user=user)


def _year_section(year: str, group: list[sqlite3.Row]) -> str:
    """One year, with its events and how much of it is done.

    The progress reading is **events**, not files: pass 2 finishes an event at a
    time, so "14 of 22 events" is what tells you the year is being worked
    through. A file count never lands (§8).
    """
    files_n = sum(int(r["n"]) for r in group)
    done = sum(1 for r in group if not r["unreviewed"])
    rows = "".join(
        f'<tr><td><a href="/browse?year={_q(year)}&amp;event={_q(r["event"])}">'
        f'{_h(r["event"])}</a></td>'
        f'<td class="num">{r["n"]:,}</td>'
        + ('<td class="num done">done</td>' if not r["unreviewed"] else
           f'<td class="num dim">{r["unreviewed"]:,}</td>')
        + f'<td class="dim">{_h(str(r["first_seen"] or "")[:10])}</td>'
        f'<td class="dim">{_h(str(r["last_seen"] or "")[:10])}</td></tr>'
        for r in group
    )
    return f"""<h2 class="year"><a href="/browse?year={_q(year)}">{_h(year)}</a>
<span class="dim">{files_n:,} files &middot; {done} of {len(group)}
events reviewed</span></h2>
<table><thead><tr><th>Event</th><th class="num">Files</th>
<th class="num">Undecided</th><th>First</th><th>Last</th></tr></thead>
<tbody>{rows}</tbody></table>"""


#: How many files one grid renders. Enough to hold the largest seeded event
#: (1,766) in a single page, because paging through a cull loses your place.
PAGE_LIMIT: int = 2000


@app.get("/event/{event}", response_class=HTMLResponse)
def event_grid(event: str) -> RedirectResponse:
    """Kept so older links still land somewhere — an event is just a filter now."""
    return RedirectResponse(f"/browse?event={_q(event)}", status_code=307)


@app.get("/browse", response_class=HTMLResponse)
def browse(user: Annotated[Principal, Depends(require_user)],
           view: Annotated[ix.Filters, Depends(filters)],
           group: Annotated[str, Query()] = "day") -> HTMLResponse:
    """The one grid, filtered — select files, then say something about them.

    Selecting an event on the landing page is just this page with `?event=`, so
    there is one surface to learn rather than a browser and a separate editor.
    """
    conn = db()
    groups = _groupings(group)
    rows = ix.files(conn, view, groups=groups, limit=PAGE_LIMIT)
    total = ix.count(conn, view)

    cells = _sections(rows, groups)
    shown = (f"{total:,} files" if total <= PAGE_LIMIT else
             f"{len(rows):,} of {total:,} files")
    body = (f'<div class="grid" id="grid">{cells}</div>'
            if rows else '<p class="empty">Nothing matches these filters.</p>')
    return _page("pix2 browse", f"""{body}
<div id="viewer">
  <div class="stage"><img id="vimg">
  <video id="vvid" controls playsinline></video>
  <div class="meta" id="vmeta"></div></div>
  <button id="viewclose" title="Close (Esc)">&times;</button>
  <button id="railtoggle" title="Details (I)">Details</button>
  <aside id="rail"></aside>
</div>
<div id="menu" hidden></div>""",
        tools=('<div class="chips" id="chips"></div>'
               '<button id="selall">Select all</button>'
               '<button id="selnone">Deselect</button>'),
        rows=_actions(user),
        script=(
            f"<script>const VIEW={_js(_view_dict(view))},"
            f"CHIPS={_js(_chips(user))},FIXED={_js(_FIXED)},"
            f"EXTRA={_js(_EXTRA)},ADMIN={_js(user.is_admin)},"
            f"USERS={_js(_audience_names())},GROUPS={_js(_group_names())},"
            f"USUAL={_js(store().usual)},"
            f"GRID_GROUPS={_js(_GRID_GROUPS)},GROUPING={_js(groups)};</script>"
            f"<script>{_BROWSE_JS}</script>"),
        footer=f"""<span class="count" id="count">{shown}</span>
<span class="hint"><b>click</b> a circle to select &middot;
<b>shift</b> for a range &middot; <b>ctrl</b> to add &middot;
<b>arrows</b> move and select &middot;
<b>S</b> repeat last access &middot; <b>Enter</b> view &middot;
<b>I</b> details</span>
<span class="note" id="note" hidden></span>""",
        user=user)


def _actions(user: Principal) -> str:
    """The edit bar — admin only.

    Not merely hidden: the endpoints refuse a non-admin outright. This is so
    the page does not offer a control that would fail, which reads as
    brokenness rather than as policy.
    """
    if not user.is_admin:
        return ""
    return """<div class="row" id="actions" hidden>
  <span class="count" id="selcount" style="margin:0"></span>
  <button data-act="access">Access&hellip;</button>
  <button data-act="tags">Tags&hellip;</button>
  <span class="sep"></span>
  <button data-act="event">Event&hellip;</button>
  <button data-act="date">Date&hellip;</button>
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


def _groupings(raw: str) -> list[str]:
    """The grouping levels, outermost first.

    A list rather than one key, so a day inside an event is expressible.
    Unknown and repeated names are dropped rather than refused: this comes
    out of a URL, which people type and edit by hand.
    """
    if raw.strip() == "none":
        return []
    out: list[str] = []
    for name in raw.split(","):
        name = name.strip()
        if name in ix.GROUPINGS and name != "none" and name not in out:
            out.append(name)
    # Nothing recognisable is a typo, not a request to stop grouping — `none`
    # says that, and says it on purpose.
    return out[:3] or ["day"]


def _sections(rows: list[sqlite3.Row], groups: list[str]) -> str:
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
    if not groups:
        return _heading([], 0, len(rows)) + "".join(_cell(r) for r in rows)

    out: list[str] = []
    for keys, run in groupby(rows, key=lambda r: tuple(
            r[f"grp{i}"] for i in range(len(groups)))):
        batch = list(run)
        labels = [_group_label(k, g, groups[:i])
                  for i, (k, g) in enumerate(zip(keys, groups))]
        out.append(_heading(labels, len(groups), len(batch)))
        out.extend(_cell(r) for r in batch)
    return "".join(out)


def _heading(labels: list[str], levels: int, count: int) -> str:
    """One section heading, which is also how grouping is changed.

    Each crumb is two controls: the name changes that level, the `×` drops it.
    Removal lives here rather than inside the menu because *take this away* is
    a thing you should be able to see, not something to go and find.
    """
    if not labels:
        crumbs = ('<span class="crumb" data-level="0">'
                  '<button class="grpname">Ungrouped</button></span>')
    else:
        crumbs = '<span class="sep">&rsaquo;</span>'.join(
            f'<span class="crumb" data-level="{i}">'
            f'<button class="grpname">{_h(label)}</button>'
            f'<button class="rmgrp" title="Remove this grouping">&times;</button>'
            f'</span>'
            for i, label in enumerate(labels))
    add = ("" if levels >= 3 else
           '<button class="addgrp" title="Add a grouping inside this one">'
           "+</button>")
    return (f'<h3 class="group">'
            f'<button class="grppick" title="Select this group"></button>'
            f'<span class="crumbs">{crumbs}</span>{add}'
            f'<span class="dim">{count:,}</span>'
            f'</h3>')


def _group_label(key: object, group: str, outer: Sequence[str] = ()) -> str:
    """A heading a person reads, not a sort key.

    `outer` is the coarser levels already shown to the left, so a crumb does
    not repeat what the path has said: under *2025*, the month is **January**
    rather than *January 2025*, and under that the day is **Saturday 4**.
    """
    if key is None or key == "":
        return "No date" if group in ("day", "month", "year") else "None"
    text = str(key)
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

    - **nobody** — a small mark, because that is the work still to do;
    - **the usual audience, exactly** — nothing at all. If nine files in ten
      say `family`, printing `family` on nine thumbnails in ten is noise
      that tells you nothing you did not already assume;
    - **anything else** — named, because that is the exception and the whole
      reason to look.

    The usual audience is a setting rather than a hard-coded name: which one
    is usual is a fact about a household, not about the software.
    """
    if not shared:
        return ('<span class="unshared" title="Nobody has access yet">'
                '</span>')
    usual = store().usual
    unusual = [a for a in shared if a != usual]
    return _chips_html("who", unusual)


def _cell(row: sqlite3.Row) -> str:
    tags = _split(row["tags"])
    shared = _split(row["audience"])
    # Newline-joined, matching what the client splits on. A stray control byte
    # had crept in here from a shell heredoc, so multiple tags arrived at the
    # page as one unsplittable blob.
    nl = chr(10)
    return (
        f'<div class="cell" data-folder="{_h(row["folder"])}" '
        f'data-name="{_h(row["name"])}" data-kind="{_h(row["kind"])}" '
        f'data-audience="{_h(nl.join(shared))}" '
        f'data-event="{_h(row["event"] or "")}" '
        f'data-tags="{_h(nl.join(tags))}" '
        f'data-date="{_h(str(row["effective_date"] or "no date"))}">'
        f'<img loading="lazy" src="/thumb/{_q(row["folder"])}/{_q(row["name"])}">'
        f'<button class="pick" aria-label="select"></button>'
        + (f'<span class="badge">{_dur(row["duration"])}</span>'
           if row["kind"] == "video" else "")
        + _access_html(shared) + _chips_html("tags", tags)
        + "</div>"
    )


def _view_dict(view: ix.Filters) -> dict[str, str | None]:
    return {name: getattr(view, name) for name in ix.Filters.NAMES}


def _chips(user: Principal) -> tuple[tuple[str, str], ...]:
    """The filters this person gets.

    Access is an administrator's control. Everyone else sees only what has
    been shared with them, so filtering by who else can see it offers a
    choice between their whole world and nothing.
    """
    return tuple((col, label) for col, label in _CHIPS
                 if col != "audience" or user.is_admin)


def _group_names() -> list[str]:
    """The groups, so the access menu can put them before the individuals."""
    book = store()
    return sorted(set(book.groups) - {accounts.ADMIN})


def _audience_names() -> list[str]:
    """Who access can be given to: **groups first, then people.**

    In that order because a group is almost always the right answer — sharing
    with `family` keeps working as the family changes, where naming four
    people does not. Both are offered, because sometimes one person really is
    the audience.

    Listed at all because sharing has to be possible on the very first file,
    before any decision exists to draw a suggestion from. The administrator is
    never here — it sees everything already, so granting it access is a no-op
    dressed as a decision.
    """
    book = store()
    groups = sorted(set(book.groups) - {accounts.ADMIN})
    people = sorted(set(book.users) - {accounts.ADMIN} - set(groups))
    return [*groups, *people]


#: Labels for the filter chips and the fixed vocabularies. Kept server-side so
#: the tier and band words are defined once, next to the columns they describe.
_CHIPS: tuple[tuple[str, str], ...] = (
    ("event", "Event"), ("year", "Year"), ("tag", "Tag"),
    ("audience", "Access"), ("kind", "Type"), ("band", "Size"),
)

#: Complete vocabularies — these columns cannot hold anything else.
_FIXED: dict[str, tuple[tuple[str, str], ...]] = {
    "kind": (("image", "Photos"), ("video", "Video"), ("other", "Other")),
    "band": (("small", "Small / short"), ("medium", "Medium"),
             ("large", "Large / long")),
}

#: How the grid can be cut up, and what to call each choice.
_GRID_GROUPS: tuple[tuple[str, str], ...] = (
    ("day", "By day"), ("month", "By month"), ("year", "By year"),
    ("event", "By event"), ("camera", "By camera"), ("kind", "By type"),
    ("none", "Ungrouped"),
)

#: Offered *in addition* to whatever already exists. Audience names are free
#: text, but "nobody yet" is a state rather than a name, and it is the single
#: most useful thing to filter on — it is the pile of work.
_EXTRA: dict[str, tuple[tuple[str, str], ...]] = {
    "audience": ((ix.UNREVIEWED, "Nobody — not shared yet"),),
}

_BROWSE_JS = """
const grid=document.getElementById('grid');
const menu=document.getElementById('menu');
const chips=document.getElementById('chips');
const actions=document.getElementById('actions');
// Absent entirely for a non-admin: the page must not reach for controls
// the server would refuse anyway.
const selcount=document.getElementById('selcount');
const countEl=document.getElementById('count');
const note=document.getElementById('note');
const viewer=document.getElementById('viewer');
const vimg=document.getElementById('vimg'), vvid=document.getElementById('vvid');
const vmeta=document.getElementById('vmeta');
const stage=document.querySelector('.stage');
// Bounded so each request stays short: the server accepts 500, but a chunk that
// takes ten seconds gives no progress reading and holds the single worker.
const CHUNK=100;

let cells=[...document.querySelectorAll('.cell')];
let cur=-1, anchor=-1, busy=false;
// Keyed by element rather than index: cells leave the grid when a change
// pushes them out of the filters, and indices would then quietly re-point
// a selection at whatever slid into the gap.
const picked=new Set();

// --- filter chips ------------------------------------------------------------
function url(patch){
  const q=new URLSearchParams();
  for(const [k,v] of Object.entries({...VIEW,...patch})) if(v!==null&&v!=='') q.set(k,v);
  // Keep the grouping across a filter change: it is how you are reading the
  // library, not what you are reading.
  q.set('group',GROUPING.join(',')||'none');
  return '/browse'+(q.toString()?'?'+q:'');
}
function drawChips(){
  chips.innerHTML='';
  for(const [col,label] of CHIPS){
    const v=VIEW[col];
    const b=document.createElement('button');
    b.className='chip'+(v?' on':'');
    b.innerHTML=label+(v?`<span class="val">${esc(labelFor(col,v))}</span>`
                        +'<span class="x">&times;</span>':'');
    b.onclick=e=>{
      e.stopPropagation();
      if(e.target.classList.contains('x')){location.href=url({[col]:null});return;}
      openMenu(b,{column:col,mode:'filter'});
    };
    chips.appendChild(b);
  }
}
function labelFor(col,v){
  const fixed=FIXED[col];
  if(!fixed) return v;
  const hit=fixed.find(f=>f[0]===v);
  return hit?hit[1]:v;
}
function esc(s){return String(s).replace(/[&<>"]/g,c=>(
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

// --- the shared menu ---------------------------------------------------------
let menuCtx=null;
function closeMenu(){menu.hidden=true;menuCtx=null;}
// Anywhere outside dismisses. The opener stops propagation and toggles,
// so clicking the same label again closes rather than reopening — a menu
// you cannot dismiss with the control that opened it feels stuck.
document.addEventListener('click',e=>{
  if(!menu.hidden&&!menu.contains(e.target)) closeMenu();
});
window.addEventListener('resize',closeMenu);
window.addEventListener('scroll',closeMenu,{passive:true});

menu.addEventListener('click',e=>e.stopPropagation());

async function openMenu(anchorEl,ctx){
  const key=ctx.mode+':'+(ctx.column||'')+':'+(ctx.as||'');
  if(menuCtx&&menuCtx.key===key&&!menu.hidden){closeMenu();return;}
  ctx.key=key; menuCtx=ctx;
  const r=anchorEl.getBoundingClientRect();
  menu.style.left=Math.min(r.left,window.innerWidth-316)+'px';
  menu.style.top=(r.bottom+window.scrollY+4)+'px';
  menu.hidden=false;

  if(ctx.mode==='date'){drawDate();return;}
  const fixed=FIXED[ctx.column];
  menu.innerHTML='<input id="menuq" autocomplete="off">'
                +'<div id="menulist" class="dim" style="padding:10px 11px">'
                +'loading…</div>';
  const q=document.getElementById('menuq');
  q.placeholder = ctx.mode!=='set' ? 'Filter…'
    : ctx.as==='access' ? 'Tick who can see these'
    : ctx.as==='tags' ? 'Tick a tag, or type a new one'
    : 'Type a new name, or pick one below';
  q.oninput=()=>render(q.value);
  q.onkeydown=e=>{
    if(e.key==='Enter'&&ctx.mode==='set'&&ctx.column!=='audience'
       &&q.value.trim()){
      choose(q.value.trim()); e.preventDefault();
    }
    if(e.key==='Escape'){closeMenu();}
    e.stopPropagation();
  };
  setTimeout(()=>q.focus(),0);

  let opts=[];
  if(fixed && ctx.mode!=='set'){
    opts=fixed.map(f=>({value:f[0],label:f[1],n:null,scope:'all'}));
  }else{
    const p=new URLSearchParams();
    for(const [k,v] of Object.entries(VIEW)) if(v) p.set(k,v);
    p.set('column',ctx.column);
    try{
      const res=await fetch('/api/suggest?'+p);
      opts=(await res.json()).map(o=>({...o,label:o.value}));
    }catch(e){opts=[];}
    // Extras are prepended, not substituted: "shared with nobody" is a state
    // rather than a name, and the configured logins have to be offerable
    // before any file carries them.
    const extra=ctx.mode==='filter'?(EXTRA[ctx.column]||[]):[];
    // *Add* offers what is valid to grant; *remove* offers what is actually
    // there. They differ, and the difference matters: a grant left behind by
    // a renamed or deleted account names nobody, and seeding the remove list
    // from the account list would make it unremovable.
    const seed=ctx.column==='audience' ? USERS.map(u=>[u,u]) : [];
    const have=new Set(opts.map(o=>o.value));
    opts=[...extra,...seed].filter(e=>!have.has(e[0]))
      .map(e=>({value:e[0],label:e[1],n:null,scope:'all'}))
      .concat(opts);
  }
  if(menuCtx!==ctx) return;   // a later menu opened while this was loading

  function choose(value){
    closeMenu();
    if(ctx.mode==='filter') location.href=url({[ctx.column]:value});
    else applyToSelection(ctx.as||ctx.column,value,true);
  }

  // A checklist, not a list of commands. `some` clears first and then
  // adds: taking access away is the safer direction, so it is the one that
  // costs a single click.
  // The field each action edits. `event` holds one value where tags and
  // access hold many, but the question the menu asks is the same one —
  // *do these files say this?* — so it is one control either way.
  const FIELD=MULTI[ctx.as]?MULTI[ctx.as][0]:(ctx.as==='event'?'event':null);

  async function toggle(value,row){
    const cs=targets();
    if(!cs.length){say('nothing selected');return;}
    const state=shareState(cs,FIELD,value);
    if(FIELD==='event'){
      // Ticking the event they already have clears it; anything else sets
      // it. One value, so there is nothing to add to.
      await applyToSelection('event',state==='all'?null:value,true);
    }else{
      await applyToSelection(ctx.as,value,state==='none');
    }
    mark(row,shareState(targets(),FIELD,value));
  }
  function mark(row,state){
    row.dataset.state=state;
    const box=row.querySelector('.box');
    if(box) box.textContent=state==='all'?'\u2713':state==='some'?'\u25cf':'';
  }
  function render(text){
    const t=(text||'').toLowerCase();
    const checkable=ctx.mode==='set'&&!!FIELD;
    const hits=opts.filter(o=>o.label.toLowerCase().includes(t));
    const list=document.createElement('div');
    list.id='menulist';
    if(ctx.mode==='filter'&&VIEW[ctx.column]){
      list.appendChild(opt({label:'Any',n:null},()=>choose(null)));
    }
    const typed=(text||'').trim();
    // Tags are invented as you go; access is not. Somebody who can be given
    // access is an account or a role, made under Accounts — offering to
    // create one here would write a grant that reaches nobody.
    // Tags are invented as you go; access is not. Somebody who can be given
    // access is an account or a role, made under Accounts.
    const invent=ctx.mode==='set'&&ctx.column==='tag';
    if(invent&&typed&&!opts.some(o=>o.label===typed)){
      const o=opt({label:'Add “'+typed+'”',n:null},()=>choose(typed));
      o.classList.add('new'); list.appendChild(o);
    }
    if(ctx.mode==='set'&&!checkable){
      list.appendChild(opt({label:'Clear',n:null},()=>choose(null)));
    }
    // What these files already say comes first, ticked, so the menu opens
    // showing the answer instead of asking a question whose answer is on
    // the screen behind it.
    const present=checkable?currentValues(targets(),FIELD):[];
    if(present.length){
      const shown=present.filter(v=>v.toLowerCase().includes(t));
      if(shown.length){
        const h=document.createElement('div');
        h.className='band'; h.textContent='On these files';
        list.appendChild(h);
        shown.forEach(v=>list.appendChild(opt(
          {value:v,label:v,n:null},()=>choose(v),true)));
      }
    }
    const group=(title,band)=>{
      if(!band.length) return;
      const h=document.createElement('div');
      h.className='band'; h.textContent=title; list.appendChild(h);
      band.forEach(o=>list.appendChild(
        opt(o,()=>choose(o.value),checkable)));
    };
    const left=hits.filter(o=>!present.includes(o.value));
    if(ctx.column==='audience'){
      // The sentinel first and on its own: *nobody has this yet* is the
      // pile of work, not a name, and grouping it with the names buried it
      // under a heading that read as though it were a deleted account.
      const special=new Set((EXTRA.audience||[]).map(e=>e[0]));
      left.filter(o=>special.has(o.value))
          .forEach(o=>list.appendChild(opt(o,()=>choose(o.value),false)));
      // Groups before individuals: a group keeps working as the household
      // changes, where naming four people does not — so it is almost always
      // the right answer and belongs where the eye lands first.
      const named=left.filter(o=>!special.has(o.value));
      group('Groups',named.filter(o=>GROUPS.includes(o.value)));
      group('People',named.filter(o=>!GROUPS.includes(o.value)
                                     &&USERS.includes(o.value)));
      group('No longer an account',
            named.filter(o=>!USERS.includes(o.value)));
    }else{
      // Three bands, most relevant first: values already used by what you
      // are looking at, then by anything one filter away, then the rest.
      for(const [scope,title] of [['all','In this view'],['any','Related'],
                                  ['other','Elsewhere']]){
        const band=left.filter(o=>o.scope===scope);
        if(hits.some(o=>o.scope!==scope)||present.length) group(title,band);
        else band.forEach(o=>list.appendChild(
          opt(o,()=>choose(o.value),checkable)));
      }
    }
    if(!hits.length&&!typed){
      list.innerHTML='<div class="band">nothing yet</div>';
    }
    menu.querySelector('#menulist').replaceWith(list);
  }
  function opt(o,fn,checkable){
    const d=document.createElement('div');
    d.className='opt';
    d.innerHTML=(checkable?'<span class="box"></span>':'')
               +`<span>${esc(o.label)}</span>`
               +(o.n!==null&&o.n!==undefined?`<span class="n">${o.n}</span>`:'');
    if(checkable){
      mark(d,shareState(targets(),FIELD,o.value));
      d.onclick=e=>{e.stopPropagation();toggle(o.value,d);};
    }else{
      d.onclick=fn;
    }
    return d;
  }
  render('');
}

function drawDate(){
  // The **effective** date: what the file actually has, after any override.
  // Opening on the current answer is the difference between editing a date
  // and guessing at one.
  const cs=targets();
  const parts=n=>[...new Set(cs.map(c=>{
    const d=c.dataset.date||'';
    return /^\\d{4}-\\d{2}-\\d{2}/.test(d)?d.split('-')[n]:'';
  }))];
  const one=n=>{const v=parts(n); return v.length===1?v[0]:'';};
  const dates=[...new Set(cs.map(c=>(c.dataset.date||'').slice(0,10)))];
  const now=!cs.length ? 'nothing selected'
          : dates.length===1 ? dates[0]
          : `${dates.length} different dates`;

  // Year alone is a complete answer — that is the whole point of a partial
  // date, so month and day stay optional rather than being required to submit.
  menu.innerHTML=`<div class="form">
    <div class="hint" style="width:100%">Now: <b>${esc(now)}</b></div>
    <label>Year <input id="dy" maxlength="4" placeholder="${esc(one(0)||'*')}"
      value="${esc(one(0))}"></label>
    <label>Month <input id="dm" maxlength="2" placeholder="${esc(one(1)||'*')}"
      value="${esc(one(1))}"></label>
    <label>Day <input id="dd" maxlength="2" placeholder="${esc(one(2)||'*')}"
      value="${esc(one(2))}"></label>
    <button class="primary" id="dok">Apply</button>
    <button id="dclr">Clear</button>
    <div class="hint">Empty keeps what the file already says.</div>
  </div>`;
  const pad=(v,n)=>v.trim()?v.trim().padStart(n,'0'):'*';
  menu.querySelector('#dok').onclick=()=>{
    const dy=menu.querySelector('#dy'), dm=menu.querySelector('#dm'),
          dd=menu.querySelector('#dd');
    const y=pad(dy.value,4), m=pad(dm.value,2), d=pad(dd.value,2);
    if(y==='*'&&m==='*'&&d==='*'){closeMenu();return;}
    closeMenu();
    applyToSelection('date_override',`${y}-${m}-${d}-*:*:*`,true);
  };
  menu.querySelector('#dclr').onclick=()=>{
    closeMenu(); applyToSelection('date_override',null,true);
  };
  setTimeout(()=>menu.querySelector('#dy').focus(),0);
}

// --- selection ---------------------------------------------------------------
// Moving the cursor **selects** what it lands on, the way a file manager
// does. The alternative was a cell that looked half-chosen: not ticked, the
// count saying none, and the actions quietly applying to it anyway. Pass
// `keep` to move without disturbing a selection.
function setCur(n,keep){
  if(!cells.length){cur=-1;return;}
  n=Math.max(0,Math.min(cells.length-1,n));
  cells.forEach(c=>c.classList.remove('cur'));
  cur=n; cells[cur].classList.add('cur');
  cells[cur].scrollIntoView({block:'nearest'});
  if(!keep){
    picked.forEach(c=>c.classList.remove('picked'));
    picked.clear();
    togglePick(cur,true);
    touched=false;
    drawSel();
  }
  if(viewer.classList.contains('on')) load(cells[cur]);
}
function togglePick(n,on){
  const c=cells[n]; if(!c) return;
  // A changed selection is a fresh one; the old edits were to other files.
  touched=false;
  if(on===undefined) on=!picked.has(c);
  on?picked.add(c):picked.delete(c);
  c.classList.toggle('picked',on);
}
function range(a,b){
  const [lo,hi]=a<b?[a,b]:[b,a];
  for(let n=lo;n<=hi;n++) togglePick(n,true);
}
function clearPicks(){picked.forEach(c=>c.classList.remove('picked'));
                      picked.clear(); touched=false; drawSel();}
// Whether this selection has been edited yet. The button says *Deselect*
// while nothing has happened and *Done* once something has, because a
// selection that survives its own edit looks like an edit that did not take.
let touched=false;
function drawSel(){
  drawGroupPicks();
  const done=document.getElementById('selnone');
  if(done){
    done.textContent = touched&&picked.size ? 'Done' : 'Deselect';
    done.classList.toggle('primary', touched&&picked.size>0);
  }
  if(!actions) return;
  actions.hidden = picked.size===0;
  if(selcount) selcount.textContent = `${picked.size} selected`;
}
cells.forEach((c,n)=>{
  c.querySelector('.pick').addEventListener('click',e=>{
    e.stopPropagation();
    // The circle is the deliberate gesture: it adds and removes without
    // throwing away what is already ticked.
    if(e.shiftKey&&anchor>=0) range(anchor,n); else {togglePick(n); anchor=n;}
    setCur(n,true); drawSel();
  });
  c.addEventListener('click',e=>{
    if(e.shiftKey&&anchor>=0){range(anchor,n);setCur(n,true);drawSel();return;}
    if(e.ctrlKey||e.metaKey){togglePick(n);anchor=n;setCur(n,true);drawSel();
                             return;}
    setCur(n); anchor=n; openViewer();
  });
});
document.getElementById('selall').onclick=()=>{
  cells.forEach((_,n)=>togglePick(n,true)); drawSel();};
const selnone=document.getElementById('selnone');
if(selnone) selnone.onclick=clearPicks;

// --- viewer ------------------------------------------------------------------
function load(c){
  const f=encodeURIComponent(c.dataset.folder), n=encodeURIComponent(c.dataset.name);
  // Always stop the previous clip: moving on while audio keeps playing from the
  // one before is the kind of thing that makes a viewer feel broken.
  vvid.pause(); vvid.removeAttribute('src'); vvid.load();
  if(c.dataset.kind==='video'){
    vimg.classList.remove('on'); vvid.classList.add('on');
    vvid.src=`/media/${f}/${n}`; vvid.play().catch(()=>{});
  }else{
    vvid.classList.remove('on'); vimg.classList.add('on');
    vimg.src=`/preview/${f}/${n}`;
  }
  vmeta.textContent=`${c.dataset.name} — ${c.dataset.date}`
                   +(c.dataset.tags?' — '+c.dataset.tags.split('\\n').join(', '):'');
  fill(c);
}
const rail=document.getElementById('rail');
const railToggle=document.getElementById('railtoggle');
// Remembered per browser: whether you want the numbers alongside is a
// working style, not a per-photo choice.
let railOn=true;
try{railOn=localStorage.getItem('pix2.rail')!=='0';}catch(e){}
function drawRail(){
  viewer.classList.toggle('norail',!railOn);
  railToggle.textContent=railOn?'Hide details':'Details';
  try{localStorage.setItem('pix2.rail',railOn?'1':'0');}catch(e){}
}
railToggle.onclick=e=>{e.stopPropagation();railOn=!railOn;drawRail();
                       if(railOn&&cells[cur]) fill(cells[cur]);};
document.getElementById('viewclose').onclick=e=>{
  e.stopPropagation(); closeViewer();};
drawRail();

const details=new Map();
async function fill(c){
  if(!railOn) return;
  const key=c.dataset.folder+'\\n'+c.dataset.name;
  rail.innerHTML='<p class="dim">loading…</p>';
  let d=details.get(key);
  if(!d){
    try{
      const r=await fetch(`/api/file/${encodeURIComponent(c.dataset.folder)}`
                         +`/${encodeURIComponent(c.dataset.name)}`);
      if(!r.ok) throw new Error(await r.text());
      d=await r.json(); details.set(key,d);
    }catch(e){rail.innerHTML='<p class="dim">no details</p>';return;}
  }
  // The cursor may have moved on while this was in flight.
  if(cells[cur]!==c) return;
  rail.innerHTML=railHtml(d);
}

function kv(rows){
  const body=rows.filter(r=>r[1]!==null&&r[1]!==undefined&&r[1]!=='')
    .map(r=>`<dt>${esc(r[0])}</dt><dd${r[2]?' class="'+r[2]+'"':''}>`
            +`${r[3]?r[1]:esc(r[1])}</dd>`).join('');
  return body?`<dl class="kv">${body}</dl>`:'';
}
function bytes(n){
  if(!n&&n!==0) return null;
  const u=['B','KB','MB','GB']; let i=0, v=n;
  while(v>=1024&&i<u.length-1){v/=1024;i++;}
  return (i?v.toFixed(1):v)+' '+u[i];
}
function secs(n){
  if(n===null||n===undefined) return null;
  const t=Math.round(n); return `${Math.floor(t/60)}:${String(t%60).padStart(2,'0')}`;
}

// Fact and judgement are shown apart, always. Collapsing them into one
// "date" would hide the only interesting question: is this what the file
// says, or what somebody chose?
function railHtml(d){
  const dec=d.decided||{};
  const inh=d.inherited||{};
  const overridden=v=>`<span class="set">${esc(v)}</span>`;

  const dateRows=[['Effective',d.effective_date||'—']];
  if(d.date_override){
    dateRows.push(['Camera said',d.capture_date||'nothing','was']);
    dateRows.push(['Override',overridden(d.date_override),null,true]);
  }else{
    dateRows.push(['Camera said',d.capture_date||'nothing']);
  }

  const eventRow=dec.event
    ? [['Event',overridden(dec.event),null,true],
       ...(inh.event_auto&&inh.event_auto!==dec.event
           ? [['Inherited',inh.event_auto,'was']] : [])]
    : [['Event',d.event||'—']];

  const tags=(d.tags||[]).map(t=>`<span class="pill">${esc(t)}</span>`).join('');
  const all=Object.entries(d.exif||{})
    .map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');

  return `<div class="rail-h">Decisions</div>`
    + kv([['Status',d.tier||'undecided',d.tier?null:'was'],
          ...eventRow])
    + (tags?`<div style="margin-top:6px">${tags}</div>`
          :'<div class="dim" style="margin-top:4px">no tags</div>')
    + (d.has_sidecar?'':'<div class="dim" style="margin-top:6px">'
        +'no sidecar &mdash; nothing decided yet</div>')
    + `<div class="rail-h">Date</div>` + kv(dateRows)
    + `<div class="rail-h">File</div>`
    + kv([['Name',d.name],['Folder',d.folder],['Size',bytes(d.size)],
          ['Kind',d.kind],['Band',d.band],
          ['Pixels',d.width&&d.height?`${d.width} × ${d.height}`:null],
          ['Length',secs(d.duration)],
          ['Render',d.kind==='video'?(d.has_render?'yes':'no'):null]])
    + (d.facts&&d.facts.length
        ? `<div class="rail-h">Capture</div>`
          + kv(d.facts.map(f=>[f.label,f.value])) : '')
    + (all?`<details><summary>All metadata`
           +` (${Object.keys(d.exif).length})</summary>`
           +`<dl class="kv">${all}</dl></details>`:'');
}

function openViewer(){viewer.classList.add('on');setCur(cur<0?0:cur);}
function closeViewer(){viewer.classList.remove('on');vvid.pause();}
// The stage fills the viewer, so clicking beside the picture lands on it
// rather than on the viewer itself — the old check never matched and there
// was no way back out except the keyboard.
viewer.addEventListener('click',e=>{
  if(e.target===viewer||e.target===stage||e.target===vmeta) closeViewer();
});
rail.addEventListener('click',e=>e.stopPropagation());

// --- writing -----------------------------------------------------------------
// Loud, because the alternative has bitten twice: a write that fails without
// saying so is indistinguishable from one that worked, and the curator only
// finds out much later that nothing was recorded.
function say(text,bad){
  if(!note) return;
  note.textContent=text||'';
  note.hidden=!text;
  note.classList.toggle('loud',!!bad);
}

// A page script that throws takes every handler with it and leaves a grid that
// simply ignores clicks. Saying so beats looking broken.
window.addEventListener('error',e=>say('page error: '+e.message,true));
window.addEventListener('unhandledrejection',
  e=>say('page error: '+(e.reason&&e.reason.message||e.reason),true));

// Exactly what is ticked — no implicit extra. A count that says none while
// an action changes something is the one thing a selection must never do.
function targets(){
  return [...picked];
}

// A file that no longer matches the filters leaves the grid. Keeping it on
// screen would be showing a view that is no longer true, and the next click
// would act on a photograph the filters say is somewhere else.
function drop(gone){
  if(!gone.length) return;
  const keys=new Set(gone.map(g=>g.folder+'\\n'+g.name));
  const at=cells[cur];
  const leaving=cells.filter(
    c=>keys.has(c.dataset.folder+'\\n'+c.dataset.name));
  leaving.forEach(c=>{picked.delete(c); c.remove();});
  const was=cells.indexOf(at);
  cells=cells.filter(c=>!leaving.includes(c));
  resection();
  // Land where the cursor was, not where it would have been pushed to — and
  // land without selecting. Moving the cursor normally *is* a selection, which
  // is right when a person pressed an arrow key and wrong here: nobody asked
  // for this move. The files left because they stopped matching the filters,
  // and ticking whatever slid into the gap would invent a selection out of
  // that — one nobody made, easy to miss, and waiting to be caught up in the
  // next edit.
  cur=-1; anchor=-1;
  if(cells.length)
    setCur(cells.includes(at)?cells.indexOf(at):Math.max(0,was), true);
  if(!cells.length&&grid) grid.innerHTML=
    '<p class="empty">Nothing matches these filters any more.</p>';
  drawSel();
}

// Which multi-valued field each action edits, and whether it adds or removes.
// Which cell attribute each multi-valued action edits, and the two request
// fields that add to it and take from it.
const MULTI={tags:['tags','add_tags','remove_tags'],
             access:['audience','add_audience','remove_audience']};

function valuesOf(c,field){
  return c.dataset[field]?c.dataset[field].split('\\n'):[];
}

// How much of the selection already carries a value: all of it, some of it,
// or none. The three states Explorer's tree uses, for the same reason — a
// selection is not one thing, and pretending otherwise means every bulk
// edit silently overwrites what you could not see.
function shareState(cs,field,value){
  const has=c=>field==='event' ? (c.dataset.event||'')===value
                               : valuesOf(c,field).includes(value);
  const n=cs.filter(has).length;
  return n===0?'none':(n===cs.length?'all':'some');
}

// What the selection already says, so a menu opens showing the answer
// rather than asking a question whose answer is on screen behind it.
function currentValues(cs,field){
  const seen=new Set();
  cs.forEach(c=>{
    if(field==='event'){ if(c.dataset.event) seen.add(c.dataset.event); }
    else valuesOf(c,field).forEach(v=>seen.add(v));
  });
  return [...seen].sort();
}

// Optimistic: the cell changes now and the write follows, because a cull is a
// rhythm and waiting on SMB between gestures destroys it. A failure puts the
// old value back rather than leaving the screen claiming something untrue.
async function applyToSelection(act,value,add){
  const cs=targets();
  if(!cs.length){say('nothing selected');return;}
  const multi=MULTI[act];
  if(multi&&value===null){say('pick a name');return;}
  const body = multi ? {[add?multi[1]:multi[2]]:[value]} : {[act]:value};
  const before=multi?cs.map(c=>c.dataset[multi[0]]||''):null;
  if(multi) cs.forEach(c=>paint(c,multi[0],value,add));
  else if(act==='event') cs.forEach(c=>{c.dataset.event=value||'';});
  const out=await send(cs,body);
  if(out===null&&multi) cs.forEach((c,i)=>{
    c.dataset[multi[0]]=before[i]; repaint(c,multi[0]);
  });
  if(out!==null&&act==='access'&&add) lastShare=value;
  return out;
}

// The grid shows tags and audience, so both have to change the moment the
// gesture lands rather than when the round trip finishes.
function paint(c,field,value,add){
  const set=new Set(c.dataset[field]?c.dataset[field].split('\\n'):[]);
  add?set.add(value):set.delete(value);
  c.dataset[field]=[...set].sort().join('\\n');
  repaint(c,field);
}
function repaint(c,field){
  const cls=field==='tags'?'tags':'who';
  const all=valuesOf(c,field);
  // Access says only what is unusual: the usual audience is silent, and
  // nobody-at-all gets a mark. Same rule the server renders by, so a cell
  // edited here and a cell fetched fresh cannot look different.
  const list=cls==='who'?all.filter(v=>v!==USUAL):all;
  let mark=c.querySelector('.unshared');
  if(cls==='who'){
    if(!all.length&&!mark){
      mark=document.createElement('span');
      mark.className='unshared';
      mark.setAttribute('title','Nobody has access yet');
      c.appendChild(mark);
    }else if(all.length&&mark){ mark.remove(); }
  }
  let el=c.querySelector('.'+cls);
  if(!list.length){if(el) el.remove(); return;}
  if(!el){el=document.createElement('span');el.className=cls;c.appendChild(el);}
  el.setAttribute('title',list.join(', '));
  el.innerHTML=list.map(v=>`<i title="${esc(v)}">${esc(v)}</i>`).join('');
}

async function send(cs,body){
  if(busy){say('still writing…');return null;}
  busy=true; say('');
  let done=0, failed=0, gone=[], total=null;
  for(let s=0;s<cs.length;s+=CHUNK){
    const batch=cs.slice(s,s+CHUNK);
    try{
      // The filters ride along so the server can say which files left the
      // view; it owns the matching rules, and a second copy here would drift.
      const p=new URLSearchParams();
      for(const [k,v] of Object.entries(VIEW)) if(v) p.set(k,v);
      const r=await fetch('/api/decide/bulk?'+p,{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({...body,
          files:batch.map(c=>({folder:c.dataset.folder,name:c.dataset.name}))})});
      if(!r.ok) throw new Error((await r.text()).slice(0,200));
      const out=await r.json();
      failed+=out.failed.length;
      gone=gone.concat(out.dropped||[]);
      if(out.total!==null&&out.total!==undefined) total=out.total;
    }catch(e){
      busy=false;
      say(`stopped after ${done} of ${cs.length}: ${e.message}`,true);
      return null;
    }
    done+=batch.length;
    if(cs.length>CHUNK) say(`writing… ${done} of ${cs.length}`);
  }
  busy=false;
  touched=true; drawSel();
  cs.forEach(c=>details.delete(c.dataset.folder+'\\n'+c.dataset.name));
  if(viewer.classList.contains('on')&&cells[cur]) fill(cells[cur]);
  drop(gone);
  if(total!==null&&countEl) countEl.textContent=`${total.toLocaleString()} files`;
  if(failed) say(`${failed} file(s) could not be written`,true);
  else if(gone.length) say(`${gone.length} file(s) no longer match — removed`);
  else say('');
  return true;
}

// What each action edits, and which column its suggestions come from. The
// two are not the same word: `unshare` writes the audience field and offers
// audience values, and using the action name as the column asked the server
// for a column called `share` — a 400, and an empty list every time.
const ACT_COLUMN={tags:'tag', access:'audience', event:'event'};
(actions?[...actions.querySelectorAll('[data-act]')]:[]).forEach(b=>{
  const act=b.dataset.act;
  b.onclick=e=>{e.stopPropagation();openMenu(b, act==='date'
    ? {mode:'date'}
    : {column:ACT_COLUMN[act]||act, mode:'set', as:act});};
});

// --- keyboard ----------------------------------------------------------------
// Sharing needs a name, so no single key can express it in general. What a cull
// actually repeats is the *same* share over and over, so S repeats the last one
// and only falls back to the menu when there is nothing to repeat.
let lastShare=null;
try{lastShare=localStorage.getItem('pix2.share')||null;}catch(e){}
function repeatShare(){
  if(!ADMIN) return;
  if(cur<0&&!picked.size) setCur(0);
  if(!lastShare){
    const b=actions&&actions.querySelector('[data-act="access"]');
    if(b) b.click();
    return;
  }
  const cs=targets();
  if(!cs.length) return;
  applyToSelection('access',lastShare,true);
  try{localStorage.setItem('pix2.share',lastShare);}catch(e){}
  if(!picked.size&&cur<cells.length-1) setCur(cur+1);
}

document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT') return;
  if(e.key==='i'||e.key==='I'){
    e.preventDefault(); railOn=!railOn; drawRail();
    if(railOn&&cells[cur]) fill(cells[cur]);
    return;
  }
  if(e.key==='Escape'){
    if(viewer.classList.contains('on')) closeViewer();
    else if(!menu.hidden) closeMenu();
    else clearPicks();
    return;
  }
  if(e.key==='Enter'){
    viewer.classList.contains('on')?closeViewer():openViewer(); return;
  }
  if((e.key==='s'||e.key==='S')&&!e.ctrlKey&&!e.metaKey){
    // Advance only when working one at a time: with a selection the gesture is
    // deliberate and moving the cursor underneath it would be noise.
    e.preventDefault(); repeatShare(); return;
  }
  const move={ArrowRight:'next',ArrowLeft:'prev',
              ArrowDown:'down',ArrowUp:'up'}[e.key];
  if(move===undefined) return;
  e.preventDefault();
  const from=cur<0?0:cur;
  const next=move==='next'?from+1:move==='prev'?from-1
            :rowNeighbour(from,move==='down'?1:-1);
  if(e.shiftKey&&cur>=0){
    range(anchor<0?cur:anchor,Math.max(0,Math.min(cells.length-1,next)));
    setCur(next,true); drawSel();
  }else if(e.ctrlKey||e.metaKey){
    setCur(next,true);          // move the cursor, leave the ticks alone
  }else{
    setCur(next); anchor=next;
  }
});

// --- grouping ----------------------------------------------------------
// The heading is the control: the thing you want to regroup is the thing
// you click, and it costs no row at the top — every row of chrome up there
// is a row of photographs pushed off the screen.
function groupUrl(levels){
  const q=new URLSearchParams();
  for(const [k,v] of Object.entries(VIEW)) if(v) q.set(k,v);
  q.set('group',levels.join(',')||'none');
  return '/browse?'+q;
}

function groupMenu(anchorEl,level,insert){
  const levels=[...GROUPING];
  menu.innerHTML='<div id="menulist"></div>';
  const list=menu.querySelector('#menulist');
  const row=(label,fn,cls)=>{
    const d=document.createElement('div');
    d.className='opt'+(cls?' '+cls:'');
    d.innerHTML=`<span>${esc(label)}</span>`;
    d.onclick=e=>{e.stopPropagation();closeMenu();fn();};
    list.appendChild(d);
  };
  const head=document.createElement('div');
  head.className='band';
  head.textContent=insert?'Then group by':'Group by';
  list.appendChild(head);
  for(const [key,label] of GRID_GROUPS){
    if(key==='none') continue;
    if(levels.includes(key)&&levels[level]!==key) continue;
    row(label,()=>{
      const next=[...levels];
      if(insert) next.splice(level+1,0,key); else next[level]=key;
      location.href=groupUrl(next);
    },levels[level]===key&&!insert?'cur':'');
  }
  const r=anchorEl.getBoundingClientRect();
  menu.style.left=Math.min(r.left,window.innerWidth-316)+'px';
  menu.style.top=(r.bottom+window.scrollY+4)+'px';
  menu.hidden=false;
  menuCtx={key:'group:'+level+':'+insert};
}

// Up and down are answered geometrically rather than by adding a column count
// to an index. Group headings are grid items spanning every column, so a
// heading eats a whole row and `index + columns` lands a cell short — which
// read as "down goes down and one to the right". Asking where things actually
// are is immune to that, to ragged final rows, and to the column count
// changing with the window.
function rowNeighbour(from,dir){
  const a=cells[from]&&cells[from].getBoundingClientRect();
  if(!a) return from;
  const ax=a.left+a.width/2, ay=a.top+a.height/2;
  let best=from, score=Infinity;
  for(let i=0;i<cells.length;i++){
    if(i===from) continue;
    const b=cells[i].getBoundingClientRect();
    const dy=(b.top+b.height/2)-ay;
    // Same visual row: not a move up or down.
    if(Math.abs(dy)<a.height/2) continue;
    if(dir>0?dy<0:dy>0) continue;
    // Nearest row first, then nearest column within it.
    const s=Math.abs(dy)*1000+Math.abs((b.left+b.width/2)-ax);
    if(s<score){score=s;best=i;}
  }
  return best;
}

// Every cell under a heading, down to the next one. There is one heading per
// section now, so this is simply "until the next heading".
function sectionCells(h){
  const out=[];
  for(let el=h.nextElementSibling; el; el=el.nextElementSibling){
    if(el.classList.contains('group')) break;
    if(el.classList.contains('cell')) out.push(el);
  }
  return out;
}

// A section is its heading and the cells beneath it, so when files leave the
// view both have to answer for it: the count says what is there now, and a
// heading whose files have all gone is a label for nothing.
//
// Recounted from the DOM rather than decremented by the number dropped. The
// grid is one render with nothing lazily loaded, so the cells present *are*
// the section — a running tally would be a second account of the same thing,
// free to drift from it.
function resection(){
  document.querySelectorAll('.group').forEach(h=>{
    const mine=sectionCells(h);
    if(!mine.length){ h.remove(); return; }
    const n=h.querySelector('.dim');
    if(n) n.textContent=mine.length.toLocaleString();
  });
}

function drawGroupPicks(){
  document.querySelectorAll('.group').forEach(h=>{
    const mine=sectionCells(h);
    const n=mine.filter(c=>picked.has(c)).length;
    h.dataset.state=n===0?'none':(n===mine.length?'all':'some');
  });
}

document.querySelectorAll('.group').forEach(h=>{
  // Each crumb is two controls: the name changes that level, the cross drops
  // it. Removal is on the crumb rather than inside the menu because *take this
  // away* is a thing you should be able to see, not go and find.
  h.querySelectorAll('.crumb').forEach(crumb=>{
    const level=+crumb.dataset.level;
    crumb.querySelector('.grpname').onclick=e=>{
      e.stopPropagation(); groupMenu(crumb,level,false);};
    const rm=crumb.querySelector('.rmgrp');
    if(rm) rm.onclick=e=>{
      e.stopPropagation();
      const next=[...GROUPING]; next.splice(level,1);
      location.href=groupUrl(next);
    };
  });
  const add=h.querySelector('.addgrp');
  if(add) add.onclick=e=>{
    e.stopPropagation(); groupMenu(add,GROUPING.length-1,true);};
  h.querySelector('.grppick').onclick=e=>{
    e.stopPropagation();
    const mine=sectionCells(h);
    const on=mine.some(c=>!picked.has(c));
    mine.forEach(c=>togglePick(cells.indexOf(c),on));
    if(on&&mine.length) setCur(cells.indexOf(mine[0]),true);
    drawSel();
  };
});

drawChips(); drawSel();
"""


# --- media -------------------------------------------------------------------

@app.get("/thumb/{folder}/{name}")
def thumb(folder: str, name: str,
          user: Annotated[Principal, Depends(require_user)]) -> FileResponse:
    _allowed(user, folder, name)
    return _serve(THUMB_DIR, folder, name)


@app.get("/preview/{folder}/{name}")
def preview(folder: str, name: str,
            user: Annotated[Principal, Depends(require_user)]) -> FileResponse:
    _allowed(user, folder, name)
    return _serve(PREVIEW_DIR, folder, name)


def _allowed(user: Principal, folder: str, name: str) -> None:
    """Refuse a file this person has not been shared.

    Checked on **every** byte-serving route, not just on the listings. A grid
    that omits a photograph while `/preview/...` still returns it is not
    access control; it is a tidier index. Anyone can type a URL.

    An admin has no scope and pays nothing for this.
    """
    if user.scope is None:
        return
    conn = db()
    try:
        if not ix.matching(conn, ix.Filters(viewer=user.scope),
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
    # Prefer the render: for an HEVC master it is the only playable copy, and
    # where both exist they are the same footage.
    rendered = (RENDER_DIR / folder / (name + ".mp4")).resolve()
    target = (MASTER_DIR / folder / name).resolve()
    if (MASTER_DIR.resolve() not in target.parents
            or RENDER_DIR.resolve() not in rendered.parents):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad path")
    if rendered.is_file():
        target = rendered
    elif not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return FileResponse(target, headers={"Cache-Control": "private, max-age=3600",
                                         "Accept-Ranges": "bytes"})


def _serve(root: Path, folder: str, name: str) -> FileResponse:
    """Serve a derived image, refusing anything that escapes its tier.

    The path components come from a URL, so they are untrusted: `..` in either
    would otherwise read arbitrary files off the share.
    """
    target = (root / folder / (name + ".jpg")).resolve()
    if root.resolve() not in target.parents:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad path")
    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not derived yet")
    return FileResponse(target, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


# --- api ---------------------------------------------------------------------

@app.get("/api/files")
def api_files(user: Annotated[Principal, Depends(require_user)],
              view: Annotated[ix.Filters, Depends(filters)],
              limit: Annotated[int, Query(le=2000)] = 500,
              offset: Annotated[int, Query(ge=0)] = 0) -> JSONResponse:
    rows = ix.files(db(), view, limit=limit, offset=offset)
    return JSONResponse([dict(r) for r in rows])


@app.get("/api/suggest")
def api_suggest(user: Annotated[Principal, Depends(require_user)],
                view: Annotated[ix.Filters, Depends(filters)],
                column: Annotated[str, Query()]) -> JSONResponse:
    """Existing values for a column, most relevant to the current view first.

    A library ends up with hundreds of events and tags, and an alphabetical
    list of all of them buries the handful that apply to what is on screen.
    Ranking by how much of the current view already uses a value puts the
    likely answer in the first few rows — see `index.suggest`.
    """
    if column not in ("event", "tag", "audience", "year", "kind", "band"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"cannot suggest values for {column!r}")
    return JSONResponse([
        {"value": s.value, "n": s.n, "scope": s.scope}
        for s in ix.suggest(db(), column, view)])


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
             user: Annotated[Principal, Depends(require_user)]) -> JSONResponse:
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

    media = MASTER_DIR / folder / name
    decision = decisions.read(media) if _under(MASTER_DIR, media) else None
    record = ix.record_for(folder, name, meta_dir=META_DIR) or {}
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
        "has_render": (RENDER_DIR / folder / (name + ".mp4")).is_file(),
    })


def _split(value: object) -> list[str]:
    """A `group_concat` column back into a list."""
    return str(value).split(chr(10)) if value else []


def _under(root: Path, target: Path) -> bool:
    """Whether `target` really sits inside `root`, after resolving `..`."""
    return root.resolve() in target.resolve().parents


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
    date_override: str | None = None
    tags: list[str] | None = None
    add_tags: list[str] = []
    remove_tags: list[str] = []
    audience: list[str] | None = None
    add_audience: list[str] = []
    remove_audience: list[str] = []


@app.post("/api/decide")
def api_decide(user: Annotated[Principal, Depends(require_admin)],
               body: Annotated[DecideBody, Body()]) -> JSONResponse:
    """Write a decision to master, then bring its index row up to date.

    **Sidecar first, index follows** (§4). If the sidecar write fails nothing
    happened; if the index update fails the decision still stands and a
    `pix2 index` catches up — drift is only ever "the index is behind", never
    "the record is wrong". `indexed` in the response says which happened.
    """
    was, decision, indexed = _decide(body.folder, body.name, _change(body))
    history.record(user.name, _summary(_change(body), 1),
                   [history.Before(body.folder, body.name, was)])
    return JSONResponse({
        "folder": body.folder,
        "name": body.name,
        "event": decision.event,
        "date_override": decision.date_override,
        "tags": list(decision.tags),
        "audience": list(decision.audience),
        "has_sidecar": not decision.is_empty(),
        "indexed": indexed,
    })


class Target(BaseModel):
    """One file in a selection."""

    folder: str
    name: str


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
    event: str | None = None
    date_override: str | None = None
    tags: list[str] | None = None
    add_tags: list[str] = []
    remove_tags: list[str] = []
    audience: list[str] | None = None
    add_audience: list[str] = []
    remove_audience: list[str] = []


#: Bounds one request rather than the whole gesture. Finishing a 1,766-file
#: event is chunked by the client, which keeps each request short enough not to
#: hold the single worker and gives a progress reading for free.
BULK_LIMIT: int = 500


@app.post("/api/decide/bulk")
def api_decide_bulk(user: Annotated[Principal, Depends(require_admin)],
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
                             "dropped": [], "total": None})
    if len(body.files) > BULK_LIMIT:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{len(body.files)} files in one request — send at most {BULK_LIMIT}")

    change = _change(body)
    written = 0
    indexed = 0
    failed: list[dict[str, str]] = []
    # One index connection for the whole batch. Opening a SQLite file over SMB
    # per row dominated the cost — measured at 96ms/file against the NAS, most
    # of it the open rather than the write.
    conn = ix.open_rw(DB_PATH) if DB_PATH.is_file() else None
    done: list[tuple[str, str]] = []
    undo: list[history.Before] = []
    dropped: list[dict[str, str]] = []
    total: int | None = None
    try:
        for target in body.files:
            try:
                was, _, was_indexed = _decide(target.folder, target.name,
                                              change, conn=conn)
            except HTTPException as e:
                failed.append({"folder": target.folder, "name": target.name,
                               "error": str(e.detail)})
                continue
            written += 1
            indexed += 1 if was_indexed else 0
            done.append((target.folder, target.name))
            undo.append(history.Before(target.folder, target.name, was))
        if conn is not None and done:
            stays = ix.matching(conn, view, done)
            dropped = [{"folder": f, "name": n}
                       for f, n in done if (f, n) not in stays]
            total = ix.count(conn, view)
    finally:
        if conn is not None:
            conn.close()

    # One log line per request, holding what each file said before. The
    # previous values in full rather than a diff: a diff has to be read
    # against whatever the file says *now*, and now may already have moved —
    # which is the whole reason somebody is reverting.
    if undo:
        history.record(user.name, _summary(change, len(undo)), undo)
    return JSONResponse({"written": written, "indexed": indexed,
                         "failed": failed, "dropped": dropped,
                         "total": total})


# --- the write path ----------------------------------------------------------

@dataclass(frozen=True)
class _Change:
    """One decision edit, with "leave it alone" distinct from "clear it"."""

    event: str | None | Unset = decisions.UNSET
    date_override: str | None | Unset = decisions.UNSET
    tags: Sequence[str] | None | Unset = decisions.UNSET
    add_tags: Sequence[str] = field(default_factory=tuple)
    remove_tags: Sequence[str] = field(default_factory=tuple)
    audience: Sequence[str] | None | Unset = decisions.UNSET
    add_audience: Sequence[str] = field(default_factory=tuple)
    remove_audience: Sequence[str] = field(default_factory=tuple)


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
                   date_override=got("date_override"), tags=got("tags"),
                   add_tags=tuple(body.add_tags),
                   remove_tags=tuple(body.remove_tags),
                   audience=got("audience"),
                   add_audience=tuple(body.add_audience),
                   remove_audience=tuple(body.remove_audience))


def _decide(folder: str, name: str, change: _Change,
            *, conn: sqlite3.Connection | None = None
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
    """
    media = _master_file(folder, name)
    with _write_lock:
        try:
            was, decision = decisions.change(
                media, event=change.event,
                date_override=change.date_override, tags=change.tags,
                add_tags=change.add_tags, remove_tags=change.remove_tags,
                audience=change.audience,
                add_audience=change.add_audience,
                remove_audience=change.remove_audience)
        except decisions.DecisionError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
        except OSError as e:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"could not write the sidecar: {e}") from e

        indexed = False
        own = conn is None and DB_PATH.is_file()
        if own:
            conn = ix.open_rw(DB_PATH)
        if conn is not None:
            try:
                # Passed rather than left to the index's own constants, so the
                # path the decision was written to and the path the row is
                # rebuilt from are the same one.
                indexed = ix.refresh(conn, folder, name,
                                     meta_dir=META_DIR, master_dir=MASTER_DIR)
            except sqlite3.Error:
                indexed = False
            finally:
                if own:
                    conn.close()
    return was, decision, indexed


def _summary(change: _Change, n: int) -> str:
    """What an operation did, in the words a person would use.

    Read months later off a list, so it says the value and the count — "gave
    family access to 312 files" is a thing you can recognise as the mistake
    you are looking for; "bulk edit" is not.
    """
    files = f"{n} file" + ("s" if n != 1 else "")
    if change.add_audience:
        return f"gave {', '.join(change.add_audience)} access to {files}"
    if change.remove_audience:
        return f"took {', '.join(change.remove_audience)} access "\
               f"from {files}"
    if change.add_tags:
        return f"tagged {files} {', '.join(change.add_tags)}"
    if change.remove_tags:
        return f"untagged {', '.join(change.remove_tags)} on {files}"
    if not isinstance(change.event, Unset):
        return (f"set the event on {files} to {change.event}"
                if change.event else f"cleared the event on {files}")
    if not isinstance(change.date_override, Unset):
        return (f"dated {files} {change.date_override}"
                if change.date_override
                else f"cleared the date override on {files}")
    return f"changed {files}"


def _master_file(folder: str, name: str) -> Path:
    """Resolve a master path from untrusted URL/body components.

    Same guard as `_serve`, and needed more here because this one writes: `..`
    in either component would otherwise drop an `.xmp` anywhere on the share.
    A sidecar is refused as a target too — decisions are about media, and
    `a.jpg.xmp.xmp` is nobody's intent.
    """
    target = (MASTER_DIR / folder / name).resolve()
    if MASTER_DIR.resolve() not in target.parents:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad path")
    if name.lower().endswith(".xmp"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "that is a sidecar, not a file")
    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not in master")
    return target


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Liveness for Container Manager — deliberately unauthenticated."""
    return {"ok": True, "index": DB_PATH.is_file()}


# --- helpers -----------------------------------------------------------------

def _h(text: object) -> str:
    """Escape for HTML text and attributes."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _js(value: object) -> str:
    """Embed a value in a <script> block.

    `<` is escaped because an event named with a literal `</script>` would
    otherwise close the block and run whatever followed as markup — and event
    names are typed by whoever is curating.
    """
    return json.dumps(value).replace("<", "\\u003c")


def _q(text: object) -> str:
    return quote(str(text), safe="")


def _age(timestamp: float | None) -> str:
    """How long ago the index was built, in words.

    Nothing watches the share, so an index predating the last `process` run
    simply does not know about the files it made. Showing the age is how that
    gets noticed, rather than experienced as photos mysteriously missing.
    """
    if timestamp is None:
        return "at an unknown time"
    seconds = max(time.time() - timestamp, 0)
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 36:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"


def _dur(seconds: object) -> str:
    try:
        total = int(float(str(seconds)))
    except (TypeError, ValueError):
        return "video"
    return f"{total // 60}:{total % 60:02d}"


# --- signing in ---------------------------------------------------------------

_LOGIN_CSS = """
.gate { max-width:320px; margin:14vh auto; }
.gate h2 { font-size:16px; margin:0 0 14px; }
.gate label { display:block; color:var(--dim); font-size:12px; margin:10px 0 3px; }
.gate input { width:100%; background:#14161a; color:var(--fg);
              border:1px solid var(--line); border-radius:4px; padding:7px 9px;
              font:inherit; }
.gate button { width:100%; margin:16px 0 0; padding:8px; }
"""


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

    existing = book.users.get(who)
    if existing is None and not password:
        return _back(f"{who} needs a password to sign in with")

    hashed = auth.hash_password(password) if password else (
        existing.password if existing else "")
    # Absent and empty are different: the admin form submits no groups field
    # at all and must not clear them, while an empty box on the people form is
    # how you take somebody out of every group.
    kept = (tuple(sorted({accounts.canonical(g)
                          for g in form["groups"].split(",") if g.strip()}))
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
                          if g.strip()})
    accounts.save(book)
    return _back("saved groups")


def _back(message: str) -> Response:
    return RedirectResponse(f"/accounts?msg={_q(message)}", status_code=303)


# --- history ------------------------------------------------------------------

@app.get("/history", response_class=HTMLResponse)
def history_page(user: Annotated[Principal, Depends(require_admin)],
                 msg: Annotated[str, Query()] = "") -> HTMLResponse:
    """What has been changed, newest first, each with a way back.

    A bulk edit can touch several hundred files from one click, and *I just
    gave the children access to three hundred photographs* has no other cure:
    the decisions are individually correct in three hundred sidecars, and
    nothing else remembers they used to say something else.
    """
    ops = history.recent()
    already = history.undone(ops)

    rows = "".join(
        f'<tr><td class="dim">{_h(_when(op.when))}</td>'
        f'<td>{_h(op.summary)}</td>'
        f'<td class="dim">{_h(op.who)}</td>'
        + ('<td class="dim">undone</td>' if op.id in already else
           '<td class="dim">a revert</td>' if op.reverts else
           f'<td><form method="post" action="/history/revert">'
           f'<input type="hidden" name="id" value="{_h(op.id)}">'
           f'<button>Revert</button></form></td>')
        + "</tr>"
        for op in ops
    )
    note = f'<p class="note">{_h(msg)}</p>' if msg else ""
    if not ops:
        return _page("History", f'{note}<p class="empty">Nothing changed yet.</p>',
                     user=user)
    return _page("History", f"""{note}
<p class="dim">Reverting puts those files back to exactly what they said
before — not an undo stack, because several people curate here and the last
thing done is not always yours. A revert is itself recorded, so it can be
reverted in turn.</p>
<table class="acct"><thead><tr><th>When</th><th>What</th><th>Who</th>
<th></th></tr></thead><tbody>{rows}</tbody></table>""", user=user)


@app.post("/history/revert")
async def history_revert(
    request: Request,
    user: Annotated[Principal, Depends(require_admin)],
) -> Response:
    """Put the files in one operation back to what they said before it.

    Written wholesale from the recorded previous value, not as an inverse of
    the change: the inverse of *added family* is only *remove family* if nothing
    else touched the file since, and something might have.
    """
    op_id = (await _form(request)).get("id", "")
    op = history.get(op_id)
    if op is None:
        return RedirectResponse("/history?msg=no+such+operation", status_code=303)

    restored = 0
    failed = 0
    undo: list[history.Before] = []
    conn = ix.open_rw(DB_PATH) if DB_PATH.is_file() else None
    try:
        for item in op.files:
            media = MASTER_DIR / item.folder / item.name
            if not _under(MASTER_DIR, media) or not media.is_file():
                failed += 1
                continue
            with _write_lock:
                try:
                    was = decisions.read(media)
                    decisions.write(media, item.decision or decisions.Decision())
                except (decisions.DecisionError, OSError):
                    failed += 1
                    continue
                undo.append(history.Before(item.folder, item.name, was))
                if conn is not None:
                    try:
                        ix.refresh(conn, item.folder, item.name,
                                   meta_dir=META_DIR, master_dir=MASTER_DIR)
                    except sqlite3.Error:
                        pass
            restored += 1
    finally:
        if conn is not None:
            conn.close()

    if undo:
        history.record(user.name, f"reverted: {op.summary}", undo,
                       reverts=op.id)
    tail = f", {failed} could not be" if failed else ""
    noun = "file" if restored == 1 else "files"
    return RedirectResponse(
        f"/history?msg={_q(f'{restored} {noun} put back{tail}')}",
        status_code=303)


def _when(moment: float) -> str:
    """A timestamp as a person reads it."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(moment))
