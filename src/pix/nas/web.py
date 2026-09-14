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
from dataclasses import dataclass, field, replace
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

from pix import __version__ as _PIX_VERSION
from pix import datestr
from pix.nas import accounts
from pix.nas import auth
from pix.nas import decisions
from pix.nas import destroy as destroy_mod
from pix.nas import history
from pix.nas import index as ix
from pix.nas.const import (
    INDEX_DB, LARGE_DIR, MASTER_DIR, META_DIR, PREVIEW_DIR, RENDER_DIR,
    THUMB_DIR,
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
        --line:#272b33; --accent:#6aa3ff; --keep:#56c16a; --top:#e3b341; --gone:#e06c5a;
        --panel:#1b1e24;
        /* The bars are a surface, not part of the page. They were the same
           colour as it, separated by a single line — which put the tick that
           selects *everything* a few pixels from the one that selects the
           first group, on the same background, looking like the same kind of
           control. Reaching for the group and selecting the library is a
           mistake the colour was inviting. */
        --chrome:#1e232b;
        /* One control's outer height: a 21px line (14px at 1.5), 4px of
           padding each side, 1px of border each side. Named because two rules
           have to agree on it — see `.row`. */
        --ctl:31px; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5
       system-ui,-apple-system,Segoe UI,sans-serif; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
.dim { color:var(--dim); }
main { padding:16px 20px 40px; }

/* The bar never leaves: filters are the address of what you are looking at,
   and losing them 2,000 thumbnails down is losing your place. */
.topbar { position:sticky; top:0; z-index:5; background:var(--chrome);
          border-bottom:1px solid #0008; padding:9px 20px;
          /* It scrolls over the grid, so it reads as a layer above it rather
             than as the first thing in it. */
          box-shadow:0 8px 16px -12px #000c; }
.row { display:flex; gap:9px; align-items:center; flex-wrap:wrap;
       min-height:var(--ctl); }
/* The top row never wraps as a whole. The filters are the one part of it that
   grows without limit, so they are the one part allowed to take two lines —
   and they take them *inside their own box*, or the account and the way out
   get carried down with them, and the way out is what you reach for when
   something has gone wrong. */
.topbar > .row:first-child { flex-wrap:nowrap; align-items:flex-start; }
.topbar > .row:first-child > .brand,
.topbar > .row:first-child > .right {
       min-height:var(--ctl); display:flex; align-items:center; flex:none; }
/* Takes the slack and gives it back: `min-width:0` is what lets a flex item
   shrink below its own content, which is what makes the chips wrap rather
   than push everything past the end of the row. */
.topbar > .row:first-child > .chips { flex:1 1 auto; min-width:0; }
/* The action row empties and fills as the selection changes and it sits above
   the grid, so its height must not depend on what is in it — otherwise every
   thumbnail on the page moves the moment you tick one. `--ctl` is what a text
   button actually measures, so the reserved height and the filled height are
   the same number.
   Deliberately not `min-height` on the buttons themselves to say it twice —
   `.tick`, `.grppick` and `.pick` are buttons with a fixed 16 or 20 pixels,
   and a min-height outranks their `height`, which would have made an oval of
   every select circle in the grid. */
/* `box-sizing: border-box` is set on everything, so a min-height covers the
   element's own padding and border as well as its content. A stacked row has
   8px of padding and a 1px rule above it, which the first row does not — so
   reserving a bare control's height here reserved 22px of room for a 31px
   button, and the row jumped nine pixels the moment one appeared. It has to
   ask for the control *plus* its own chrome. */
.row + .row { margin-top:8px; border-top:1px solid var(--line); padding-top:8px;
              min-height:calc(var(--ctl) + 8px + 1px); }
.brand { font-weight:600; letter-spacing:.02em; color:var(--fg); }
.count { font-variant-numeric:tabular-nums; color:var(--dim);
         white-space:nowrap; }
.spacer { flex:1; }
/* The chips had no container rule at all, so they sat against one another with
   nothing between them and read as one control. Same gap as the row they are
   in, so filters and actions line up. */
.chips { display:flex; gap:9px; align-items:center; flex-wrap:wrap; }
/* The filters you are not using are a list of everything the app can ask,
   which is not a thing to read past on the way to the ones you are. The same
   `+` the grouping heading uses, for the same gesture: one more of these. */
.addchip { padding:3px 9px; font-weight:600; color:var(--dim); }
.addchip:hover { color:var(--fg); border-color:var(--dim); }
/* Counts and messages along the bottom, so the header is only controls:
   every row of chrome up there is a row of photographs pushed off. */
.footbar { position:fixed; left:0; right:0; bottom:0; z-index:4;
           background:var(--chrome); border-top:1px solid #0008;
           padding:6px 20px; display:flex; gap:14px; align-items:baseline;
           flex-wrap:wrap; font-size:12px; }
.footbar:empty { display:none; }
.footbar .note { margin:0; margin-left:auto; }
.ver { color:var(--dim); font-variant-numeric:tabular-nums;
       white-space:nowrap; }
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
/* Named rather than shouted: it sits with the others because it is one of the
   things you do, and a soft delete is undoable. */
button.danger:hover:not(:disabled) { border-color:#c2604f; color:#ffd9d2; }
.chip.on { border-color:var(--accent); background:#20293a; }
/* Arrived at from the log rather than picked from a list, so it is not a
   dropdown — but it says what it is and can be dismissed, because a filter you
   cannot see is a library that looks smaller than it is. */
.from-op { cursor:default; max-width:46ch; overflow:hidden;
           text-overflow:ellipsis; white-space:nowrap; }
.from-op .x { margin-left:7px; color:var(--dim); }
.from-op .x:hover { color:var(--fg); text-decoration:none; }
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
/* Three sizes, and the third is where the thumbnail runs out. Cells stretch
   past their minimum to fill the row, so 230px already renders around 263 on a
   wide screen — from a 400px derived thumbnail, which is spent at that point.
   Bigger than that has to come from the preview tier, which is four times the
   edge and eleven times the bytes; that is the trade the third size makes and
   the reason it is a choice rather than the default. */
.grid { display:grid; gap:6px;
        grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); }
.grid[data-size="medium"] {
        grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); }
.grid[data-size="large"] {
        grid-template-columns:repeat(auto-fill,minmax(380px,1fr)); }
/* A folder: one section of the grid, drawn as what it amounts to. The
   photograph says which pile it is; the caption says what it is and how much
   of it there is. Those were laid over the picture and legible over a dark
   sky and gone over a bright one — and what this page is *for* is the
   reading, not the picture. */
.tile { display:flex; flex-direction:column; background:var(--panel);
        border:1px solid var(--line); border-radius:3px; overflow:hidden;
        text-decoration:none; color:var(--fg); }
.tile .shot { display:block; aspect-ratio:4/3; background:#0d0f12; }
.tile .shot img { width:100%; height:100%; object-fit:cover; display:block; }
.tile .cap { padding:7px 9px 8px; }
.tile .name { display:block; font-size:13px; font-weight:600; line-height:1.3;
              overflow-wrap:anywhere; }
.tile .name .sep { color:var(--dim); font-style:normal; font-weight:400;
                   margin:0 4px; }
.tile .n { display:block; font-size:11px; color:var(--dim); margin-top:3px; }
/* Read as *what is left here*, so it is the colour of unfinished work rather
   than of a count. */
.tile .left { font-style:normal; color:var(--top); }
.tile .left::before { content:"·"; color:var(--dim); margin:0 5px; }
.tile:hover { border-color:var(--accent); }
/* A section with no address. It is still a real pile of files, so it is still
   shown — it just cannot be opened on its own. */
.tile.dead { cursor:default; opacity:.7; }
.tile.dead:hover { border-color:var(--line); }
/* A thumbnail with a letter in it. Three words took three buttons' worth of
   bar for something nobody reads twice — the shape says what it is about and
   the letter says where it is, which is all a size control has to say. */
#sizepick { width:30px; height:24px; padding:0; font-size:11px;
            font-weight:700; letter-spacing:.02em;
            display:inline-flex; align-items:center; justify-content:center;
            color:var(--dim); }
#sizepick:hover { color:var(--fg); }
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
/* The same control as a section heading's, for the same job one level up:
   none, some, all. It replaces a *Select all* and a *Deselect* that were two
   buttons for one question, and were in the top bar rather than beside the
   count they were about. */
.tick[hidden], .count[hidden] { display:none; }
.tick { margin:0; padding:0; width:16px; height:16px; flex:none;
        border-radius:50%; background:transparent;
        border:1.5px solid var(--dim); }
#actions[data-state="all"] .tick { background:var(--accent);
  border-color:var(--accent); }
#actions[data-state="some"] .tick { background:var(--top);
  border-color:var(--top); }
#actions .grp { display:flex; gap:9px; align-items:center; }
/* The same trap `#menu` fell into, and the second time it has been paid for:
   an id selector beats the user agent's `[hidden] { display:none }`, so the
   rule above won and `hidden = true` set a flag that hid nothing. Every action
   stayed on screen with nothing selected. Any rule that gives an element a
   `display` has to say what `hidden` means for it too. */
#actions .grp[hidden] { display:none; }
/* The same trap once more: these are buttons, and `button` has no `display`
   of its own here — but the flex container gives them one, so say what hidden
   means for them too. */
#actions button[hidden] { display:none; }
/* And once more for the grid, which hides everything but the files being
   stacked while a top is chosen. `.cell` sets no `display`, so the user agent
   would cover this — but four rules in this file have needed saying and
   relying on the absence of one is how the fifth gets written. */
.cell[hidden], h3.group[hidden] { display:none; }
#actions .grp b { font-weight:600; }
.cell { position:relative; aspect-ratio:1; background:#0d0f12; overflow:hidden;
        border-radius:3px; cursor:pointer; }
.cell img { width:100%; height:100%; object-fit:cover; display:block; }
/* The cursor is not drawn. It said *which one the keyboard is on*, and there
   is no grid keyboard any more — so the dashed ring marked a position nothing
   could use, and turned up unasked on whatever a delete happened to land on.
   The cursor itself stays: it is how the viewer knows which file it is showing
   and which way left and right go. It is simply not the curator's business. */
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
.cell:hover .pick { opacity:1; }
.cell.picked .pick { opacity:1; background:var(--accent); border-color:var(--accent); }
/* Deleted, and only ever on screen because an administrator asked to see
   them — so this does not need to be subtle, it needs to be unmistakable
   beside a living file. The picture is drained and dimmed rather than merely
   badged: at a glance down a grid the corner marks are what every other fact
   already uses, and all four corners are taken.
   The cross sits where the select circle does and gets out of its way on
   hover, so the two never argue over the same 20 pixels. */
.cell.gone img { opacity:.32; filter:grayscale(1); }
.cell.gone::after { content:"¹5"; position:absolute; left:5px; top:2px;
                    color:var(--gone); font-size:17px; font-weight:700;
                    line-height:20px; text-shadow:0 1px 3px #000d;
                    pointer-events:none; transition:opacity .08s; }
.cell.gone:hover::after { opacity:0; }
.bin-link { color:var(--gone); font-weight:600; }
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
/* The stack count, top-right beside the tags: it is a fact about the file
   like they are, and a stack is almost never also heavily tagged. Reads as a
   depth — a card with cards behind it. */
.stack { position:absolute; right:4px; top:4px; z-index:3;
         background:#000b; color:var(--fg); font-size:11px; font-weight:600;
         padding:1px 6px; border-radius:3px;
         box-shadow:2px -2px 0 -1px #000b, 4px -4px 0 -2px #000b; }
.stack:hover { background:var(--accent); color:#0d0f12; text-decoration:none; }
/* Nobody has confirmed this one. The same badge in the colour the app uses for
   *this is the one that speaks* rather than the one it uses for a decision —
   so a shelf of guesses reads as a shelf of guesses at a glance. */
.stack.guessed { border-color:var(--top); color:var(--top); }
.stack.guessed:hover { background:var(--top); color:#0d0f12; }
/* Hidden while its files are on screen being chosen between. An anchor has no
   display of its own, so the user agent would cover this — but five rules in
   this file have needed saying, which is enough to stop calling it luck. */
.stack[hidden] { display:none; }
/* On the photograph, and only while one is being chosen. Out of the way until
   the pointer is over it, like the select circle — a control that is always
   there on every thumbnail is a page about its own controls. */
.choose { position:absolute; left:50%; bottom:8px; transform:translateX(-50%);
          z-index:4; opacity:0; transition:opacity .08s; white-space:nowrap;
          background:var(--accent); color:#0d0f12; border-color:var(--accent);
          font-weight:600; }
.cell:hover .choose, .choose:focus { opacity:1; }
/* Nothing to select while a top is being chosen, so nothing offers to. */
.grid[data-choosing] .pick { display:none; }
/* Inside an opened stack, the one that speaks. Everything in there looks
   alike — that is why they were stacked — so without this there is nothing to
   say which one the grid outside will show. */
.top-mark { position:absolute; right:4px; top:4px; z-index:3;
            background:var(--accent); color:#0d0f12; font-size:10px;
            font-weight:700; letter-spacing:.05em; text-transform:uppercase;
            padding:2px 6px; border-radius:3px; }
/* Both of them stand where the tags do, and were simply covering them. */
.cell.marked .tags { padding-right:40px; }
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
/* Another `display` that would outrank the user agent's `[hidden]`. The bin
   count is hidden at zero and shown the moment something is deleted, without
   a reload, so it has to be hideable. */
.who-link[hidden] { display:none; }
.who-link button { padding:3px 9px; margin:0; }
.empty { color:var(--dim); padding:40px 0; }

/* A write of hundreds of sidecars over SMB is seconds, and the only thing
   that said so was a 12px note in the corner that appeared *after* the first
   hundred had already been written. So the screen stops answering instead.
   Above the viewer, because S starts a write from inside it. */
#working { position:fixed; inset:0; z-index:40; background:#0d0f12ee;
           display:none; flex-direction:column; align-items:center;
           justify-content:center; gap:15px; }
#working.on { display:flex; }
#working .what { font-size:17px; }
#working .what b { color:var(--accent); font-weight:600; }
#working .bar { width:min(420px,70vw); height:6px; border-radius:3px;
                background:#222833; overflow:hidden; }
#working .bar i { display:block; height:100%; background:var(--accent);
                  width:0; transition:width .12s linear; }
#working .tally { color:var(--dim); font-size:13px;
                  font-variant-numeric:tabular-nums; }
#working button { margin-top:4px; }

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
          right: str = "", footer: str = "", script: str = "",
          user: Principal | None = None) -> HTMLResponse:
    """One shell.

    `tools` sits beside the brand on the first row, `right` is pushed to the far
    end of it beside who you are, `rows` are whole extra rows below it, and
    `footer` is the strip along the bottom.

    The two ends of that row are two different kinds of thing. On the left, what
    you are looking at — the filters, which are the address. On the right, how
    you are looking at it and who as, which no link carries. Counts and messages
    live down there so the header is only controls — every row of chrome at the
    top is a row of photographs pushed off the screen.

    The **version** is in the footer of every page, for the same reason the CLI
    prints it on every run: so that what is on screen and what is in the tree
    can be compared. The page script is inlined into this HTML, so an open tab
    keeps the script it was served with — a stale tab and a broken build look
    identical from the outside, and this is what tells them apart.

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
<span class="spacer"></span><span class="right">{right}{_whoami(user)}</span></div>{rows}
</div><main>{body}</main>
<footer class="footbar"><span class="ver">v{_PIX_VERSION}</span>{footer}</footer>
{script}</body></html>""")


def _whoami(user: Principal | None) -> str:
    """Who you are signed in as, and the way out.

    Always visible because this app is used as two different people — the
    owner curating, and the admin granting access — and acting as the wrong
    one is invisible until something is shared with the wrong household.
    """
    if user is None:
        return '<a class="who-link" href="/login">Sign in</a>'
    manage = (f'{_bin_link()}'
              '<a class="who-link" href="/history">History</a>'
              '<a class="who-link" href="/accounts">Accounts</a>'
              if user.is_admin else "")
    return (f'<span class="who-link dim">{_h(user.name)}</span>{manage}'
            '<form method="post" action="/logout" class="who-link">'
            '<button>Sign out</button></form>')


def _both_sides(deleted: str | None, op_id: str | None,
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
    if deleted in ("only", "with"):
        return deleted
    return "with" if op_id else None


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


def _bin_link() -> str:
    """*8 deleted* — a standing count, and the way to go and deal with them.

    Deleting is meant to be cheap, which means files accumulate in a state
    nobody is looking at. A link called "Deleted" says nothing about whether
    there is anything to do; a number says there are eight, and says it on
    every page until they are gone.

    It clears the other filters rather than adding to them. Arriving at
    *8 deleted* and being shown two because last week's event filter was still
    on would be the count lying, which is the one thing it cannot do.

    Silent at zero: an empty bin is not news, and a nag that is always there
    stops being read. Rendered anyway and hidden, rather than left out, because
    deleting something has to light it up without a reload — and an element
    that is not there cannot be updated.
    """
    try:
        conn = db()
    except HTTPException:
        return ""
    try:
        n = ix.count(conn, ix.Filters(deleted="only"))
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
    return bin_link_html(n)


def bin_link_html(n: int) -> str:
    """The bin count as the header shows it — and as the page rewrites it."""
    return (f'<a class="who-link bin-link" id="bincount" '
            f'href="/browse?deleted=only"{"" if n else " hidden"}>'
            f'{n:,} deleted</a>')


def filters(
    user: Annotated[Principal, Depends(require_user)],
    event: Annotated[str | None, Query()] = None,
    date: Annotated[str | None, Query()] = None,
    tag: Annotated[str | None, Query()] = None,
    audience: Annotated[str | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
    band: Annotated[str | None, Query()] = None,
    camera: Annotated[str | None, Query()] = None,
    source: Annotated[str | None, Query()] = None,
    deleted: Annotated[str | None, Query()] = None,
    op: Annotated[str | None, Query()] = None,
    stale: Annotated[str | None, Query()] = None,
    within: Annotated[str | None, Query()] = None,
    stacks: Annotated[str | None, Query()] = None,
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
    return ix.Filters(event=event, date=ix.date_prefix(date), tag=tag,
                      audience=audience, chosen=_from_operation(op, stale),
                      within=within,
                      stacks=(stacks if user.is_admin
                              and stacks in ("with", "only") else None),
                      unfold="stack" in _groupings(group),
                      kind=kind, band=band, camera=camera, source=source,
                      viewer=user.scope,
                      deleted=_both_sides(deleted, op, user))


@app.get("/", response_class=HTMLResponse)
def home(user: Annotated[Principal, Depends(require_user)],
         view: Annotated[ix.Filters, Depends(filters)],
         group: Annotated[str, Query()] = "year") -> HTMLResponse:
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
    conn = db()
    groups = _groupings(group)
    rows = ix.sections(conn, view, groups=groups, limit=PAGE_LIMIT)
    s = ix.summary(conn, view)

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
        body = '<p class="empty">Nothing matches these filters.</p>'
    else:
        labels = [dict(_GRID_GROUPS).get(g, g) for g in groups]
        body = (_heading(labels, len(groups), len(rows), pick=False)
                + '<div class="grid folders" id="grid">'
                + "".join(_folder(r, groups, view, user) for r in rows)
                + "</div>")
    return _page("pix2",
                 # The shared menu, which every filter and the grouping open
                 # into. Left out, the script threw looking for it the moment
                 # it loaded, and a page whose chips never drew and whose
                 # heading never wired looks exactly like a page whose
                 # controls were never built.
                 f'<p class="dim">{head}</p>{body}<div id="menu" hidden></div>',
                 # The way past the folders. Every one of them opens the grid
                 # at that section; this opens it at everything the filters
                 # still allow, which is the one view no folder stands for.
                 tools=('<div class="chips" id="chips"></div>'
                        f'<a class="who-link" href="{_h(_browse_url(view, {}))}">'
                        'All files &rarr;</a>'),
                 # Folders are photographs too, and 300 days of them at
                 # thumbnail size is a wall. The same control, remembered in
                 # the same place, so the two pages agree about how big things
                 # are without being told twice.
                 right='<button id="sizepick" aria-label="Thumbnail size"></button>',
                 footer='<span id="note" class="note"></span>',
                 script=_view_script(user, view, groups, page="/"),
                 user=user)


def _folder(row: sqlite3.Row, groups: list[str], view: ix.Filters,
            user: Principal) -> str:
    """One section of the grid, drawn as the folder it amounts to.

    The cover is the section's first photograph — the one at the top left if
    you opened it — so the folder looks like what is inside it rather than like
    a name somebody chose.
    """
    labels = [_group_label(row[f"grp{i}"], g, groups[:i], row)
              for i, g in enumerate(groups)] or ["Everything"]
    href = _drill(row, groups, view)
    n = int(row["n"])
    left = int(row["unreviewed"] or 0) if user.is_admin else 0
    inner = (
        '<span class="shot"><img loading="lazy" '
        f'src="/thumb/{_q(row["folder"])}/{_q(row["name"])}"></span>'
        # Beneath the photograph rather than over it. What a folder *is* was
        # small text laid on whatever its first picture happened to be, so it
        # was legible over a dark sky and gone over a bright one.
        '<span class="cap"><b class="name">'
        + '<i class="sep">&rsaquo;</i>'.join(_h(x) for x in labels)
        + f'</b><span class="n">{n:,} file{"" if n == 1 else "s"}'
        + (f'<i class="left">{left:,} undecided</i>' if left else "")
        + "</span></span>")
    if href is None:
        # Nothing to link to, rather than a link somewhere else. *No day* is
        # every file whose date stops at the month, and there is no filter that
        # says so — offering the month itself would open a folder holding files
        # this one does not.
        return (f'<div class="tile dead" title="There is no filter for this '
                f'one, so it cannot be opened on its own">{inner}</div>')
    return f'<a class="tile" href="{_h(href)}">{inner}</a>'


#: What each grouping means as a filter, which is what makes a folder openable.
#: `camera` is here because of this page: it could cut the library by camera
#: and then had nowhere to send you.
_DRILL: dict[str, str] = {
    "day": "date", "month": "date", "year": "date", "event": "event",
    "camera": "camera", "source": "source", "kind": "kind",
    "stack": "within",
}


def _drill(row: sqlite3.Row, groups: list[str],
           view: ix.Filters) -> str | None:
    """Where one folder leads: this view, plus what the folder is.

    `None` where the section cannot be said as a filter. Only two can't —
    *no day* and *no month*, which mean *dated less precisely than that*, and
    the date filter answers `undated` or a prefix and nothing in between.
    Sending those to the year would open a folder with more in it than the one
    that was clicked, which is worse than a folder that does not open.
    """
    patch: dict[str, str | None] = {}
    for i, name in enumerate(groups):
        key = row[f"grp{i}"]
        column = _DRILL.get(name)
        if column is None:
            return None
        if key is None:
            # A year nobody knows is genuinely *undated*; a day nobody knows
            # is a file dated to its month, which is a different thing.
            if name != "year":
                return None
            patch["date"] = ix.UNDATED
            continue
        patch[column] = str(key)
    return _browse_url(view, patch)


def _browse_url(view: ix.Filters, patch: dict[str, str | None]) -> str:
    """The grid, at this view plus `patch`. The page's own `url()` in Python."""
    query = {**_view_dict(view), **patch}
    # Joined with a bare `&`: this is a URL, and the one place it becomes
    # markup escapes it. Building it pre-escaped produced `&amp;amp;` and a
    # link that carried its second filter as part of the first one's value.
    return "/browse" + (
        "?" + "&".join(f"{k}={_q(str(v))}" for k, v in query.items() if v)
        if any(query.values()) else "")


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
           group: Annotated[str, Query()] = "day",
           op: Annotated[str | None, Query()] = None,
           stale: Annotated[str | None, Query()] = None) -> HTMLResponse:
    """The one grid, filtered — select files, then say something about them.

    Selecting an event on the landing page is just this page with `?event=`, so
    there is one surface to learn rather than a browser and a separate editor.
    """
    conn = db()
    groups = _groupings(group)
    rows = ix.files(conn, view, groups=groups, limit=PAGE_LIMIT)
    total = ix.count(conn, view)

    cells = _sections(rows, groups, view)
    shown = (f"{total:,} files" if total <= PAGE_LIMIT else
             f"{len(rows):,} of {total:,} files")
    body = (f'<div class="grid" id="grid">{cells}</div>' if rows else
            '<p class="empty">Nothing matches these filters.</p>')
    return _page("pix2 browse", f"""{body}
<div id="viewer">
  <div class="stage"><img id="vimg">
  <video id="vvid" controls playsinline></video>
  <div class="meta" id="vmeta"></div></div>
  <button id="viewclose" title="Close (Esc)">&times;</button>
  <button id="railtoggle" title="Details (I)">Details</button>
  <aside id="rail"></aside>
</div>
<div id="menu" hidden></div>
<div id="working">
  <div class="what" id="workwhat"></div>
  <div class="bar"><i id="workbar"></i></div>
  <div class="tally" id="worktally"></div>
  <button id="workstop">Stop</button>
</div>""",
        tools=('<div class="chips" id="chips"></div>'
               + _from_link(op, stale, len(rows))),
        # Away from the filters, at the end of the row with the account. It is
        # a view control rather than a filter — nothing it does changes which
        # photographs are here — and standing among the chips it read as one
        # more thing narrowing the library.
        #
        # It says what it will do rather than what is true. With two sizes that
        # is the whole of it: no state to read off a label that might mean
        # either.
        right=('<button id="sizepick" aria-label="Thumbnail size"></button>'),
        rows=_actions(user),
        script=_view_script(user, view, groups),
        footer=f"""<span class="count" id="count">{shown}</span>
<span class="hint"><b>click</b> a circle to select &middot;
<b>shift</b> for a range &middot; <b>ctrl</b> to add &middot;
<b>click</b> a photo to open it &middot;
<b>&larr; &rarr;</b> page the viewer</span>
<span class="note" id="note" hidden></span>""",
        user=user)


def _view_script(user: Principal, view: ix.Filters, groups: list[str], *,
                 page: str = "/browse") -> str:
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
        f"EXTRA={_js(_EXTRA)},ADMIN={_js(user.is_admin)},"
        f"USERS={_js(_audience_names())},GROUPS={_js(_group_names())},"
        f"USUAL={_js(store().usual)},PAGE={_js(page)},"
        f"GRID_GROUPS={_js(_GRID_GROUPS)},GROUPING={_js(groups)};</script>"
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


def _actions(user: Principal) -> str:
    """The edit bar — admin only.

    Not merely hidden: the endpoints refuse a non-admin outright. This is so
    the page does not offer a control that would fail, which reads as
    brokenness rather than as policy.

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
    """
    if not user.is_admin:
        return ""
    return """<div class="row" id="actions">
  <button id="selall" class="tick" title="Select all"></button>
  <span class="count" id="selcount" style="margin:0"></span>
  <span class="grp" data-side="live" hidden>
    <button data-act="event">Event&hellip;</button>
    <button data-act="tags">Tags&hellip;</button>
    <button data-act="date">Date&hellip;</button>
    <button data-act="access">Access&hellip;</button>
    <button data-act="stack">Stack</button>
    <button data-act="top">Make top</button>
    <button data-act="unstack">Unstack</button>
    <button data-act="nostack">Not a stack</button>
    <span class="sep"></span>
    <button data-act="delete" class="danger">Delete</button>
  </span>
  <span class="grp" data-side="gone" hidden>
    <button data-act="restore">Restore</button>
    <button data-act="purge" class="danger">Purge&hellip;</button>
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


def _sections(rows: list[sqlite3.Row], groups: list[str],
              view: ix.Filters | None = None) -> str:
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
        return (_heading([], 0, len(rows))
                + "".join(_cell(r, view) for r in rows))

    out: list[str] = []
    for keys, run in groupby(rows, key=lambda r: tuple(
            r[f"grp{i}"] for i in range(len(groups)))):
        batch = list(run)
        labels = [_group_label(k, g, groups[:i], batch[0])
                  for i, (k, g) in enumerate(zip(keys, groups))]
        out.append(_heading(labels, len(groups), len(batch)))
        out.extend(_cell(r, view) for r in batch)
    return "".join(out)


def _heading(labels: list[str], levels: int, count: int, *,
             pick: bool = True) -> str:
    """One section heading, which is also how grouping is changed.

    Each crumb is two controls: the name changes that level, the `Ã—` drops it.
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
            + ('<button class="grppick" title="Select this group"></button>'
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
    usual = store().usual
    unusual = [a for a in shared if a != usual]
    return _chips_html("who", unusual)


def _cell(row: sqlite3.Row, view: ix.Filters | None = None) -> str:
    mark = _stack_badge(row, view or ix.Filters())
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
        f'data-date="{_h(str(row["effective_date"] or "no date"))}" '
        f'data-deleted="{"1" if row["deleted"] else ""}" '
        f'data-under="{_h(row["stacked_under"] or "")}" '
        f'data-behind="{row["behind"] or 0}" '
        f'data-proposed="{_count(row, "proposed")}">'
        f'<img loading="lazy" src="/thumb/{_q(row["folder"])}/{_q(row["name"])}">'
        f'<button class="pick" aria-label="select"></button>'
        + (f'<span class="badge">{_dur(row["duration"])}</span>'
           if row["kind"] == "video" else "")
        + mark
        + _access_html(shared) + _chips_html("tags", tags)
        + "</div>"
    )


def _stack_badge(row: sqlite3.Row, view: ix.Filters) -> str:
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
    behind = row["behind"] or 0
    guessed = _count(row, "proposed") if view.stacks else 0
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
    return (f'<a class="stack{" guessed" if guessed else ""}" '
            f'href="/browse?within={_q(key)}" '
            f'title="{n + 1} photographs '
            f'{"that look alike — nobody has said yet" if guessed else ""}'
            f'{"" if guessed else "stacked here"}">'
            f'{n + 1}</a>')


def _count(row: sqlite3.Row, column: str) -> int:
    """A counted column, where the query that produced the row had one."""
    try:
        return int(row[column] or 0)
    except IndexError:
        return 0


def _view_dict(view: ix.Filters) -> dict[str, str | None]:
    return {name: getattr(view, name) for name in ix.Filters.NAMES}


def _chips(user: Principal) -> tuple[tuple[str, str], ...]:
    """The filters this person gets.

    Access is an administrator's control. Everyone else sees only what has
    been shared with them, so filtering by who else can see it offers a
    choice between their whole world and nothing.
    """
    return tuple((col, label) for col, label in _CHIPS
                 if col not in ("audience", "deleted", "stacks")
                 or user.is_admin)


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
    # The same order as the actions, because they are the same questions:
    # what it is, then what it is for. `kind`, `band` and `deleted` come last
    # as a group of their own — they are facts about the file rather than
    # judgements about it, and nobody reaches for them mid-cull.
    ("event", "Event"), ("tag", "Tag"), ("date", "Date"),
    ("audience", "Access"),
    ("kind", "Type"), ("band", "Size"), ("source", "Source"),
    ("camera", "Camera"),
    ("stacks", "Stacks"), ("deleted", "Deleted"),
)

#: Complete vocabularies — these columns cannot hold anything else.
_FIXED: dict[str, tuple[tuple[str, str], ...]] = {
    "kind": (("image", "Photos"), ("video", "Video"), ("other", "Other")),
    "band": (("small", "Small / short"), ("medium", "Medium"),
             ("large", "Large / long")),
    # Off is the third value and has no entry: clearing the chip is what says
    # *the living*, the same gesture as clearing any other filter.
    "deleted": (("only", "Only deleted"), ("with", "Including deleted")),
    # Off is a named choice here rather than only the cross, because it is
    # not the absence of a question — it is one of three answers to *how much
    # of the app's guessing do you want in this view*, and the one most people
    # want most of the time. Its value is empty, which is how every other
    # filter says off, so choosing it clears the chip like the cross does.
    "stacks": (("with", "Including suggestions"), ("only", "Only suggested"),
               ("", "Exclude suggestions")),
}

#: How the grid can be cut up, and what to call each choice.
_GRID_GROUPS: tuple[tuple[str, str], ...] = (
    ("day", "By day"), ("month", "By month"), ("year", "By year"),
    ("event", "By event"), ("source", "By source"),
    ("camera", "By camera"), ("kind", "By type"),
    ("stack", "By stack"), ("none", "Ungrouped"),
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
const binEl=document.getElementById('bincount');
const sizePick=document.getElementById('sizepick');
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

// --- thumbnail size ----------------------------------------------------------
// A preference about looking, not about which photographs — so it lives in the
// browser rather than the URL, beside the details rail. A view is a link; how
// big you like the thumbnails is not part of where you are.
// Small, medium, large — the names the letters stand for, so the code and the
// control say the same thing.
const SIZES=['small','medium','large'];
const LABEL={small:'S',medium:'M',large:'L'};
const SIZE_NAME={small:'Small thumbnails',medium:'Medium thumbnails',
                 large:'Large thumbnails'};
let thumbSize='small';
try{
  const saved=localStorage.getItem('pix2.thumb');
  // `big` and `huge` are what these were called for an afternoon.
  const known={big:'medium',huge:'large'}[saved]||saved;
  if(SIZES.includes(known)) thumbSize=known;
}catch(e){}

// The biggest size is bigger than the thumbnail tier has pixels for, so it
// reads from `large` — sized for exactly this and nothing else. The media
// routes take the same path after the tier name, so this swaps one segment
// rather than building an address a second time.
function useSource(c){
  const img=c.querySelector('img');
  if(!img) return;
  const want=thumbSize==='large'?'/large/':'/thumb/';
  const other=want==='/thumb/'?'/large/':'/thumb/';
  const have=img.getAttribute('src')||'';
  if(have.startsWith(other)) img.setAttribute('src',want+have.slice(other.length));
}

function drawSize(){
  if(grid) grid.dataset.size=thumbSize;
  if(sizePick){
    sizePick.textContent=LABEL[thumbSize];
    sizePick.title=SIZE_NAME[thumbSize]+' — click for the next size';
  }
  cells.forEach(useSource);
}

if(sizePick) sizePick.onclick=e=>{
  e.stopPropagation();
  // Anchored the way a write is: the row heights are about to change under
  // whatever you were looking at, and the point of a bigger thumbnail is to
  // look harder at the one you had already found.
  const at=cells.find(c=>c.getBoundingClientRect().bottom>0);
  const was=at?at.getBoundingClientRect().top:null;
  thumbSize=SIZES[(SIZES.indexOf(thumbSize)+1)%SIZES.length];
  try{localStorage.setItem('pix2.thumb',thumbSize);}catch(e){}
  drawSize();
  if(at&&was!==null){
    const now=at.getBoundingClientRect().top;
    if(now!==was) window.scrollBy(0,now-was);
  }
};
drawSize();

// --- filter chips ------------------------------------------------------------
function url(patch){
  const q=new URLSearchParams();
  for(const [k,v] of Object.entries({...VIEW,...patch})) if(v!==null&&v!=='') q.set(k,v);
  // Keep the grouping across a filter change: it is how you are reading the
  // library, not what you are reading.
  q.set('group',GROUPING.join(',')||'none');
  // Back to the page you are standing on. Both of these said `/browse`, so
  // every filter and every regrouping worked perfectly and then left the
  // landing page — which looks exactly like a control that does nothing,
  // except that the library you were summarising turns into a wall of files.
  return PAGE+(q.toString()?'?'+q:'');
}
// Only the filters that are doing something, and a `+` for the rest.
//
// Every filter, always, was a row of eleven controls that grew every time the
// app learned to ask something new — ten of them saying nothing, in front of
// the one or two that are the address of what you are looking at. The unused
// ones are a list of questions, and a list of questions belongs in a menu.
function drawChips(){
  chips.innerHTML='';
  for(const [col,label] of CHIPS){
    const v=VIEW[col];
    if(!v) continue;
    const b=document.createElement('button');
    b.className='chip on';
    b.innerHTML=label+`<span class="val">${esc(labelFor(col,v))}</span>`
                     +'<span class="x">&times;</span>';
    b.onclick=e=>{
      e.stopPropagation();
      if(e.target.classList.contains('x')){location.href=url({[col]:null});return;}
      openMenu(b,{column:col,mode:'filter'});
    };
    chips.appendChild(b);
  }
  const spare=CHIPS.filter(([col])=>!VIEW[col]);
  if(spare.length){
    const add=document.createElement('button');
    add.className='chip addchip';
    add.textContent='+';
    add.title='Add a filter';
    add.onclick=e=>{e.stopPropagation();filterMenu(add,spare);};
    chips.appendChild(add);
  }
}

// Which question to ask, and then what to answer — two steps, because the
// value list is the same one the chip itself opens and building a second
// version of it here is how the two would come to disagree.
function filterMenu(anchorEl,spare){
  const key='addfilter';
  if(menuCtx&&menuCtx.key===key&&!menu.hidden){closeMenu();return;}
  menu.innerHTML='<div id="menulist"></div>';
  const list=menu.querySelector('#menulist');
  const head=document.createElement('div');
  head.className='band';
  head.textContent='Filter by';
  list.appendChild(head);
  for(const [col,label] of spare){
    const d=document.createElement('div');
    d.className='opt';
    d.innerHTML=`<span>${esc(label)}</span>`;
    d.onclick=e=>{e.stopPropagation();closeMenu();
                  openMenu(anchorEl,{column:col,mode:'filter'});};
    list.appendChild(d);
  }
  placeMenu(anchorEl);
  menuCtx={key};
}

// Under the control that opened it, and never off the right-hand edge.
function placeMenu(anchorEl){
  const r=anchorEl.getBoundingClientRect();
  menu.style.left=Math.min(r.left,window.innerWidth-316)+'px';
  menu.style.top=(r.bottom+window.scrollY+4)+'px';
  menu.hidden=false;
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
  placeMenu(anchorEl);

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
    // What the selection spans, which only the page knows. Undated files are
    // left out rather than counted as some smallest date — one of those would
    // stretch the range across the whole library and propose everything.
    if(ctx.mode==='set'&&ctx.column==='event'){
      const days=targetsOn('live').map(c=>c.dataset.date)
                                  .filter(d=>d&&d!=='no date').sort();
      if(days.length){p.set('near_from',days[0]);
                      p.set('near_to',days[days.length-1]);}
    }
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
      for(const [scope,title] of [['near','Around these dates'],
                                  ['all','In this view'],['any','Related'],
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
  // Only while the viewer is open, where the cursor is what you are looking at
  // and the grid behind should end up where you left off. With it closed
  // nothing points at the cursor, so scrolling to it is the page moving for
  // reasons of its own — which is what a bulk delete did, landing the cursor
  // on a survivor hundreds of rows away.
  if(viewer.classList.contains('on')) cells[cur].scrollIntoView({block:'nearest'});
  if(!keep){
    picked.forEach(c=>c.classList.remove('picked'));
    picked.clear();
    togglePick(cur,true);
    drawSel();
  }
  if(viewer.classList.contains('on')) load(cells[cur]);
}
function togglePick(n,on){
  const c=cells[n]; if(!c) return;
  if(on===undefined) on=!picked.has(c);
  on?picked.add(c):picked.delete(c);
  c.classList.toggle('picked',on);
}
function range(a,b){
  const [lo,hi]=a<b?[a,b]:[b,a];
  for(let n=lo;n<=hi;n++) togglePick(n,true);
}
function clearPicks(){picked.forEach(c=>c.classList.remove('picked'));
                      picked.clear(); drawSel();}
function show(act,on){
  const b=actions&&actions.querySelector('[data-act="'+act+'"]');
  if(b) b.hidden=!on;
}

function drawSel(){
  drawGroupPicks();
  if(!actions) return;
  // A menu that acts on the selection has nothing left to act on once the
  // selection is empty — which is exactly where a write that pushes every
  // file out of the view leaves it, and it sat there open over a grid it
  // could no longer touch. Filter and grouping menus are about the view
  // rather than the selection, so they are left alone.
  if(!picked.size&&menuCtx&&(menuCtx.mode==='set'||menuCtx.mode==='date'))
    closeMenu();
  // Each set is on screen exactly when the selection holds files it applies
  // to. Not greyed: an action that is absent says *not for these files*,
  // where a greyed one says *not yet* — and with a mixed selection both are
  // present and neither is waiting for anything.
  const live=targetsOn('live'), dead=targetsOn('gone').length;
  // The tick and the count belong to a selection, and while a top is being
  // chosen there is not one.
  const tickEl=document.getElementById('selall');
  if(tickEl) tickEl.hidden=!!choosing;
  if(selcount) selcount.hidden=!!choosing;
  for(const g of actions.querySelectorAll('.grp')){
    const side=g.dataset.side;
    // While a top is being chosen there is one question on screen, so there is
    // one set of controls: the others would be offering to do something else
    // to a selection that is halfway through becoming a stack.
    g.hidden = choosing ? side!=='choose'
             : side==='choose' ? true
             : !(side==='gone'?dead:live.length);
  }
  // The stack actions ask a narrower question than *is anything selected*, so
  // they answer it themselves: two or more to make a stack, one that is in one
  // to promote, anything already stacked to take out.
  show('stack', live.length > 1 || live.some(tops));
  // Only for a file that is behind something. Offered on the one already
  // showing, it could do nothing but say so — a button whose whole answer is
  // that it should not have been there.
  show('top', live.length === 1 && stacked(live[0]));
  // Not for a guess: there is nothing to take apart yet, and undoing
  // something nobody did would be a button whose whole answer is that it
  // should not have been there. Refusing is what a guess answers to.
  show('unstack', live.some(c => stacked(c) || +(c.dataset.behind||0) > 0));
  // Only where there is a guess to refuse. On a stack somebody made it would
  // be offering to un-decide a decision, which is what Unstack is for.
  show('nostack', live.some(guessed));
  // The tick wears the three states of what it would do: nothing selected and
  // it selects everything, anything selected and it clears.
  actions.dataset.state = !picked.size ? 'none'
    : picked.size===cells.length ? 'all' : 'some';
  if(selcount) selcount.textContent = `${picked.size} selected`;
}
// The index is looked up **at click time**, never captured when the handler is
// bound. Cells leave the grid when an edit pushes them out of the filters, and
// `cells` is rebuilt around the gap — so a handler holding the position its
// cell had at load would open whatever has since slid into it. Delete two
// files near the top and every thumbnail below them opened the picture two
// along. `picked` is keyed by element for exactly this reason; the handlers
// were the half that still counted.
function wire(c){
  c.querySelector('.pick').addEventListener('click',e=>{
    e.stopPropagation();
    const n=cells.indexOf(c);
    if(n<0) return;
    // The circle is the deliberate gesture: it adds and removes without
    // throwing away what is already ticked.
    if(e.shiftKey&&anchor>=0) range(anchor,n); else {togglePick(n); anchor=n;}
    setCur(n,true); drawSel();
  });
  c.addEventListener('click',e=>{
    const n=cells.indexOf(c);
    if(n<0) return;
    if(e.shiftKey&&anchor>=0){range(anchor,n);setCur(n,true);drawSel();return;}
    if(e.ctrlKey||e.metaKey){togglePick(n);anchor=n;setCur(n,true);drawSel();
                             return;}
    // A plain click is *show me this one*, and it must not cost a selection.
    // The viewer moves the cursor itself, since whether that also selects
    // depends on what was selected before the click.
    anchor=n; openViewer(n);
  });
}
cells.forEach(wire);
// One control for one question. Empty, it selects everything; otherwise it
// clears — which is what both *Select all* and *Deselect* were for, and it
// sits beside the count it is about rather than up in the filter bar.
const selall=document.getElementById('selall');
if(selall) selall.onclick=e=>{
  e.stopPropagation();
  if(picked.size){clearPicks();return;}
  cells.forEach((_,n)=>togglePick(n,true));
  drawSel();
};

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
if(railToggle) railToggle.onclick=e=>{
  e.stopPropagation();railOn=!railOn;drawRail();
                       if(railOn&&cells[cur]) fill(cells[cur]);};
const viewClose=document.getElementById('viewclose');
if(viewClose) viewClose.onclick=e=>{e.stopPropagation(); closeViewer();};
// Everything above is the viewer, which only a page with photographs on it
// has. The landing page shows folders: it carries none of these elements, so
// the script wires none of them. Guarded one statement at a time rather than
// wrapped in a block, because the functions here are called from the grid and
// a block would put them out of its reach.
if(viewer) drawRail();

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

// Looking is not choosing. Opening the viewer used to reset the selection to
// the single photograph you opened, so one mis-aimed click on a thumbnail
// threw away a selection that had taken hundreds of gestures to build, with
// nothing that could bring it back.
//
// So the viewer leaves a selection alone: it is a bigger look at the cursor,
// not a place that decides anything. With nothing selected it still selects
// what you opened, because otherwise the actions would have nothing to apply
// to and the count would sit at none while you looked straight at the file
// you meant.
//
// Looking changes nothing at all. The viewer used to select what you opened
// when nothing was selected — so that the actions had something to apply to
// while you were in there — and leave the selection alone otherwise. That was
// two behaviours for one gesture and it showed: the first photograph you
// opened got ticked and the next one did not.
//
// It bought nothing any more. The rule existed for `S`, which wrote to the
// photograph on screen, and the viewer has had no controls of its own since
// the keyboard went. So opening and paging are pure looking, and the only way
// to choose a file is still to click its circle.
function openViewer(n){
  viewer.classList.add('on');
  setCur(n===undefined?(cur<0?0:cur):n, true);
}
function closeViewer(){ viewer.classList.remove('on'); vvid.pause(); }
// The stage fills the viewer, so clicking beside the picture lands on it
// rather than on the viewer itself — the old check never matched and there
// was no way back out except the keyboard.
if(viewer) viewer.addEventListener('click',e=>{
  if(e.target===viewer||e.target===stage||e.target===vmeta) closeViewer();
});
if(rail) rail.addEventListener('click',e=>e.stopPropagation());

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

// Which kind of file an action is about. A deleted file cannot be deleted
// again and a living one cannot be restored, so every action has a side and
// acts only on that side of the selection. Select a day that holds both and
// Delete takes the living ones while Purge takes the deleted ones — each does
// what it says to the files it means, rather than refusing the whole gesture
// because the selection was not pure.
const ACT_SIDE={access:'live', tags:'live', event:'live', date:'live',
                delete:'live', stack:'live', top:'live', unstack:'live',
                restore:'gone', purge:'gone'};
function gone(c){ return !!c.dataset.deleted; }
function sideOf(act,value){
  // One flag, two buttons: deleting is something you do to a living file and
  // restoring to a deleted one, so the field name alone cannot say which.
  if(act==='deleted') return value?'live':'gone';
  return ACT_SIDE[act]||'live';
}

// --- stacks ------------------------------------------------------------------
// Eight takes of one photograph, one shown and the rest folded behind it. Each
// of the others records which file it defers to; the top records nothing,
// because being spoken for is the decision and speaking is what is left.
function keyOf(c){ return c.dataset.folder+'/'+c.dataset.name; }
function stacked(c){ return !!c.dataset.under; }
// Anything folded behind this one, however it got there. A guessed stack opens
// like a decided one — the question *which of these do I keep* is the same
// question, and the answer to it is what turns one into the other.
function tops(c){ return +(c.dataset.behind||0)+ +(c.dataset.proposed||0) > 0; }
function guessed(c){ return +(c.dataset.proposed||0) > 0; }

// Stacking asks which one to show, rather than taking the first ticked and
// hoping. The rule was invisible: nothing on screen said that the order you
// happened to click in had decided which photograph spoke for the rest.
//
// So the grid narrows to the files being stacked and waits. No round trip and
// no address of its own — they are already on screen, so *filter to these* is
// hiding the others and *back to where you were* is showing them again, with
// the scroll never having moved.
let choosing=null, fetched=[], opened=[], wasPicked=[], wasScrolled=0;
let choiceBtns=[];

// Merging two stacks has to offer every photograph in both of them as the one
// to show. Choosing between the two that happen to be speaking is choosing
// between two of ten, and the other eight are only hidden because stacking
// them is what hid them.
async function stackSelection(){
  const cs=targetsOn('live');
  if(cs.length<2){say('select the ones to stack');return;}
  choosing=cs; fetched=[]; opened=[]; wasPicked=cs.slice();
  // Where you were, because it is about to be taken from you. Hiding the rest
  // of the grid collapses the page to a few rows, and a browser will not hold
  // a scroll position past the bottom of a document — so it clamps to the top,
  // and putting the cells back afterwards does not put you back with them.
  wasScrolled=window.scrollY;
  const keep=new Set(cs);
  cells.forEach(c=>{c.hidden=!keep.has(c);});
  if(grid) grid.dataset.choosing='1';
  choiceBtns=[];
  cs.forEach(offerChoice);
  document.querySelectorAll('.group').forEach(h=>{h.hidden=true;});
  // Nothing is selected while a top is being chosen. The question is *which
  // one of these*, and leaving the files you arrived with ringed while the
  // ones fetched out of a stack are not says they are two kinds of candidate.
  // They are not: any of them can be the one that shows.
  say(''); clearPicks();
  for(const head of cs.filter(c=>tops(c))) await expand(head);
}

async function expand(head){
  let html='';
  try{
    const r=await fetch('/api/behind/'+encodeURIComponent(head.dataset.folder)
                        +'/'+encodeURIComponent(head.dataset.name));
    if(!r.ok) throw new Error(await r.text());
    html=(await r.json()).cells||'';
  }catch(e){say('could not open that stack: '+e.message,true);return;}
  if(!choosing||!html) return;
  // The depth badge means *there are more of these, somewhere else*. Once they
  // are sitting beside it that is no longer true, and leaving it there says
  // the stack is still closed while its files are on screen being chosen
  // between.
  const badge=head.querySelector('.stack');
  if(badge){badge.hidden=true; opened.push(badge);}
  const holder=document.createElement('div');
  holder.innerHTML=html;
  const added=[...holder.children];
  let after=head;
  for(const c of added){
    after.insertAdjacentElement('afterend',c);
    after=c;
    cells.push(c); choosing.push(c); fetched.push(c); wire(c); useSource(c);
  offerChoice(c);
  }
}

// Clicking a photograph opens it, here as everywhere else. It used to mean
// *this one* while a top was being chosen, and the cost of that was finding
// out by having chosen: you click one to see it properly, and instead the
// question is answered and the grid comes back.
//
// So choosing has a control of its own, on the photograph, appearing when the
// pointer is over it. There is one of them per candidate and one candidate per
// click, which is why it can be a button rather than a mode.
function offerChoice(c){
  const b=document.createElement('button');
  b.className='choose';
  b.textContent='Show this one';
  b.onclick=e=>{e.stopPropagation();chooseTop(c);};
  c.appendChild(b);
  choiceBtns.push(b);
}

// A guess, refused. Remembered against every photograph in it, so the same
// group is not offered again tomorrow, and recorded like any other decision —
// which is the way back when a shelf of them is waved off by mistake.
//
// What it was hiding comes back out onto the page. In a view of nothing but
// guesses there is nothing to come back to: the whole section stops matching
// and leaves, which the server already reports. In the mixed view the files
// are still here and still match, and leaving them off the grid until the next
// reload would be the page quietly holding some of the library back.
async function notAStack(){
  const cs=targetsOn('live').filter(guessed);
  if(!cs.length){say('nothing selected that the app guessed at');return;}
  const fan=VIEW.stacks==='with'
    ? new Map(await Promise.all(cs.map(async c=>[c,await behind(c)])))
    : null;
  const out=await applyToSelection('no_stack',true,undefined,cs);
  if(out&&out.done&&fan) cs.forEach(c=>fanOut(c,fan.get(c)));
}

// Fetched before the refusal is written: afterwards they are nothing's
// members, and the page would have no way left to ask what it had been hiding.
async function behind(head){
  try{
    const r=await fetch('/api/behind/'+encodeURIComponent(head.dataset.folder)
                        +'/'+encodeURIComponent(head.dataset.name));
    if(!r.ok) throw new Error(await r.text());
    return (await r.json()).cells||'';
  }catch(e){say('could not open that stack: '+e.message,true);return '';}
}

function fanOut(head,html){
  if(!html) return;
  const holder=document.createElement('div');
  holder.innerHTML=html;
  const added=[...holder.children];
  let after=head;
  for(const c of added){after.insertAdjacentElement('afterend',c);after=c;}
  // Spliced where they sit rather than appended: `cells` is the reading order
  // the viewer and the arrow keys walk, and a file that is on screen here and
  // last in the order is a preview that opens the wrong photograph.
  const at=cells.indexOf(head);
  cells.splice(at<0?cells.length:at+1,0,...added);
  added.forEach(c=>{wire(c);useSource(c);});
  const badge=head.querySelector('.stack');
  if(badge) badge.remove();
  head.dataset.proposed='0';
  head.classList.remove('marked');
  resection(); drawSel();
}

function endChoosing(restore){
  if(!choosing) return;
  choosing=null;
  choiceBtns.forEach(b=>b.remove());
  choiceBtns=[];
  if(grid) delete grid.dataset.choosing;
  // Whatever was fetched belongs to a stack, and a stack's files do not sit in
  // the grid — that is the whole point of one. They were borrowed to be
  // chosen between.
  fetched.forEach(c=>{picked.delete(c); c.remove();});
  cells=cells.filter(c=>!fetched.includes(c));
  fetched=[];
  // Closed again, so the badge means what it says again.
  opened.forEach(b=>{b.hidden=false;});
  opened=[];
  cells.forEach(c=>{c.hidden=false;});
  document.querySelectorAll('.group').forEach(h=>{h.hidden=false;});
  // The page is tall again, so the position it was holding means something
  // again. Whatever leaves the grid next is anchored from here.
  window.scrollTo(0,wasScrolled);
  // Changing your mind puts back what you had, not an empty grid: the files
  // were selected before this asked anything, and cancelling asked for none
  // of it to have happened.
  if(restore) wasPicked.forEach(c=>{
    const n=cells.indexOf(c);
    if(n>=0) togglePick(n,true);
  });
  wasPicked=[];
  drawSel();
}

async function chooseTop(top){
  // Anything already behind this one is where it should be; writing it again
  // would be an edit that changes nothing and a line in the log saying so.
  const key=keyOf(top);
  const family=(choosing||[]).filter(
    c=>c!==top&&c.dataset.under!==key);
  if(!family.length){endChoosing(true);return;}
  const behind=+(top.dataset.behind||0)+family.length;
  // If the one chosen came out of a stack it stays — it is a file that speaks
  // for others now, which is exactly what the grid shows. Everything else
  // borrowed goes back. The server takes it out of whatever it was behind.
  fetched=fetched.filter(c=>c!==top);
  top.dataset.under='';
  endChoosing();
  const out=await applyToSelection('stacked_under',keyOf(top),undefined,family);
  // The files that went behind it leave the grid on their own — they stopped
  // matching the moment they were stacked — but the one left standing has to
  // start saying how many it now speaks for.
  if(out&&out.done) markStack(top,behind);
  // Same rename, if this was done from inside the stack being merged into.
  if(out&&out.done&&VIEW.within&&VIEW.within!==keyOf(top)){
    location.href=url({within:keyOf(top)});
    return;
  }
  // And the selection is spent. It used to survive, holding the file that had
  // just become a top — so the next things ticked were stacked *with it*, and
  // its own members ended up a level down behind a file that was itself behind
  // something. Nothing on screen said that was about to happen.
  clearPicks();
}

// Built here rather than fetched, the way the access and tag chips are: it is
// one anchor, and a round trip to redraw a badge would be a round trip to
// redraw a badge.
function markStack(c,behind){
  c.dataset.behind=String(behind);
  let badge=c.querySelector('.stack');
  if(!badge){
    badge=document.createElement('a');
    badge.className='stack';
    c.appendChild(badge);
  }
  badge.href='/browse?within='+encodeURIComponent(keyOf(c));
  badge.title=(behind+1)+' photographs stacked here';
  badge.textContent=String(behind+1);
}

// Which stack a file belongs to, named by the file that currently speaks for
// it. Empty for a file in no stack — and that emptiness is the whole point:
// comparing `dataset.under` directly made every unstacked file on screen look
// like a member of the same stack as every other, because they all share the
// empty string. Selecting a stack's top in the ordinary grid and promoting it
// swept the entire visible grid underneath it.
function stackKey(c){ return c.dataset.under || (tops(c) ? keyOf(c) : ''); }

async function makeTop(){
  const cs=targetsOn('live');
  if(cs.length!==1){say('select the one to show');return;}
  const top=cs[0];
  const key=stackKey(top);
  if(!key){say('that one is not in a stack');return;}
  if(key===keyOf(top)){say('that one already shows');return;}
  // Everything else in this stack, the old top included: it stops speaking and
  // starts deferring, which is the same write as any other member. They are on
  // screen because promoting happens inside an opened stack.
  const family=cells.filter(c=>c!==top&&stackKey(c)===key);
  if(!family.length){say('nothing else is in that stack');return;}
  // One write. The other half — taking the new top out of what it was behind —
  // is the server's, because a file everything defers to cannot be left
  // deferring to one of them whoever asks for it.
  const out=await applyToSelection('stacked_under',keyOf(top),undefined,family);
  if(!out||!out.done) return;
  // A stack is named by the file that speaks for it, so promoting one renames
  // it. An open stack's address is that name — stay on it and the page asks
  // for a stack whose files have all just gone somewhere else, which is how
  // this left you looking at one photograph with no way back but the browser.
  if(VIEW.within) location.href=url({within:keyOf(top)});
}

async function unstack(){
  // The file that speaks for a stack counts as being in one. Selecting it and
  // being told to select something in a stack was the tool disagreeing with
  // the screen, which showed a depth badge on the thing it was refusing.
  const cs=targetsOn('live').filter(c=>stacked(c)||tops(c));
  if(!cs.length){say('select files that are in a stack');return;}
  const dissolving=cs.some(c=>tops(c));
  const out=await applyToSelection('stacked_under',null,undefined,cs);
  if(!out) return;
  cs.forEach(c=>{c.dataset.under='';});
  // Taking a whole stack apart puts photographs *back* into the grid, and the
  // grid can only ever lose cells on its own — the ones that come back were
  // never sent to it. This is the one gesture that needs the page again.
  if(dissolving) location.reload();
}
function targetsOn(side){
  return [...picked].filter(c=>side==='gone'?gone(c):!gone(c));
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
  // Work through the files at the top of the screen and they vanish from
  // above you: the grid shortens over your head and everything left slides up,
  // which reads as the page having scrolled down on its own. So a survivor is
  // measured before and after, and the scroll corrected by the difference —
  // which puts the work you had *not* done yet back where you left it.
  //
  // Anchored to an element rather than to a count of rows, because how much
  // height leaves depends on where the gaps fall and how many cells fit a row,
  // neither of which this knows and both of which change with the window.
  const anchorCell=cells.find(
    c=>!leaving.includes(c)&&c.getBoundingClientRect().bottom>0);
  const wasAt=anchorCell?anchorCell.getBoundingClientRect().top:null;
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
  // After every change to the grid, the headings `resection` took away
  // included. Skipped while the viewer is open: it is full-screen, the grid
  // behind it is not what anybody is looking at, and `setCur` has already
  // scrolled to the file on show.
  if(wasAt!==null&&anchorCell&&!viewer.classList.contains('on')){
    const nowAt=anchorCell.getBoundingClientRect().top;
    if(nowAt!==wasAt) window.scrollBy(0,nowAt-wasAt);
  }
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
// `only` names the files to write to when they are not simply *the selection
// on this side*. Stacking writes to everything except the keeper; making a new
// top writes to the rest of its stack and then to itself. Both are one gesture
// over a selection, and neither is the whole of it.
async function applyToSelection(act,value,add,only,batch){
  const cs=only||targetsOn(sideOf(act,value));
  if(!cs.length){say('nothing selected');return;}
  const multi=MULTI[act];
  if(multi&&value===null){say('pick a name');return;}
  const body = multi ? {[add?multi[1]:multi[2]]:[value]} : {[act]:value};
  // Everything each cell said before, so the ones that never got written can
  // be put back. All four fields rather than the one being edited: it costs
  // nothing and means the restore cannot be wrong about which was in play.
  const before=cs.map(c=>({tags:c.dataset.tags||'',
                           audience:c.dataset.audience||'',
                           event:c.dataset.event||'',
                           deleted:c.dataset.deleted||''}));
  if(multi) cs.forEach(c=>paint(c,multi[0],value,add));
  else if(act==='event') cs.forEach(c=>{c.dataset.event=value||'';});
  // Under `Including deleted` a restored file stays on screen, so the cross
  // has to go the moment the decision does. Under `Only deleted` it leaves
  // instead, and `drop` takes the cell with it.
  else if(act==='deleted') cs.forEach(c=>{
    c.dataset.deleted=value?'1':'';
    c.classList.toggle('gone',!!value);
  });
  const out=await send(cs,body,actLabel(act,value,add),batch);
  // Only the tail. A write that stops half way — cancelled, or a share that
  // dropped — has really written the first part, and painting all of it back
  // would leave the screen denying what is on disk. The cells that were
  // written keep what they now say; the rest go back to what they said.
  const wrote=out?out.done:0;
  cs.slice(wrote).forEach((c,i)=>{
    const was=before[wrote+i];
    c.dataset.tags=was.tags; c.dataset.audience=was.audience;
    c.dataset.event=was.event; c.dataset.deleted=was.deleted;
    c.classList.toggle('gone',!!was.deleted);
    repaint(c,'tags'); repaint(c,'audience');
  });
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

// --- the takeover ------------------------------------------------------------
// Hundreds of sidecars over SMB is seconds of writing, and the only thing that
// ever said so was a 12px note in the corner — which appeared *after* the first
// hundred had already been written, and not at all below that. Between the
// click and the first reply the page looked idle and finished.
//
// So the screen stops answering. Nothing underneath is live while it is up,
// because the grid is mid-change: cells are about to leave it and their
// counts are about to be wrong, and a click into that is a decision taken
// against a view that has already stopped being true.
//
// **Painted on a delay, not on the click.** Most writes are one file and come
// back inside the threshold, and a full-screen takeover flashing on every S
// press would wreck exactly the cull rhythm that the optimistic write exists
// to protect. If a single file does stall, the delay expires and the takeover
// is simply the truth.
const working=document.getElementById('working');
const workWhat=document.getElementById('workwhat');
const workBar=document.getElementById('workbar');
const workTally=document.getElementById('worktally');
const workStop=document.getElementById('workstop');
const TAKEOVER_MS=180;
let workTimer=null;
// Asked for, not done yet. A write is a run of requests and this is checked
// between them, never inside one — see `send`.
let stopping=false;

// Enough to tell one gesture from another in the log; it never leaves this
// session and nothing is decided by it.
function newBatch(){
  return Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,8);
}

function workOpen(label,total){
  stopping=false;
  if(workStop) workStop.disabled=false;
  if(workWhat) workWhat.innerHTML=label;
  workProgress(0,total);
  clearTimeout(workTimer);
  workTimer=setTimeout(()=>{if(working) working.classList.add('on');},
                       TAKEOVER_MS);
}
function workProgress(done,total){
  if(workBar) workBar.style.width=(total?Math.round(done/total*100):0)+'%';
  if(workTally) workTally.textContent=
    `${done.toLocaleString()} of ${total.toLocaleString()} `+
    (total===1?'file':'files');
}
// The standing count of what is waiting in the bin. It is rendered with the
// page, so every delete, restore and purge has to say what it is now — a
// number that only refreshes on reload is worse than no number, because it
// looks current.
function drawBin(n){
  if(binEl===null||n===null||n===undefined) return;
  binEl.textContent=`${n.toLocaleString()} deleted`;
  binEl.hidden=!n;
}

function workClose(){
  clearTimeout(workTimer); workTimer=null;
  if(working) working.classList.remove('on');
}

// Stopping is a decision about the rest of the work, not about the request in
// flight. That one has already reached the server and its files are either
// written or not; tearing it up here would lose the log entry saying which,
// and the whole point of stopping is to be able to go to History and put back
// exactly what did land.
function stopWork(){
  if(!busy||stopping) return;
  stopping=true;
  if(workStop) workStop.disabled=true;
  if(workTally) workTally.textContent='finishing the files already sent…';
}
if(workStop) workStop.onclick=e=>{e.stopPropagation();stopWork();};

const chooseCancel=document.getElementById('choosecancel');
if(chooseCancel) chooseCancel.onclick=e=>{e.stopPropagation();
                                          endChoosing(true);};

// The takeover says the word the control you pressed says — read off the
// button itself rather than kept as a second vocabulary for the same four
// actions, which would be free to drift from the one on screen.
function actLabel(act,value,add){
  // `deleted` is the one action whose button is not named after its field:
  // one flag, two controls, and the word for it depends on which way it is
  // going.
  if(act==='deleted') return value?'Delete':'Restore';
  const b=actions&&actions.querySelector('[data-act="'+act+'"]');
  const word=esc(b?b.textContent.replace(/\\u2026|\\.\\.\\./,'').trim():act);
  if(!value) return word+' &mdash; clearing';
  return word+(add===false?' &mdash; removing <b>':' &mdash; <b>')
             +esc(value)+'</b>';
}

async function send(cs,body,label,sharedBatch){
  if(busy){say('still writing…');return null;}
  busy=true; say('');
  workOpen(label||'Writing', cs.length);
  // One id for the whole gesture. The chunking below is about keeping each
  // request short; the log should not learn about it. A caller doing a gesture
  // in more than one write passes its own, so the log does not learn about
  // that either.
  const batch=sharedBatch||newBatch();
  let done=0, failed=0, gone=[], total=null, binned=null, trouble=null;
  for(let s=0;s<cs.length;s+=CHUNK){
    // Checked between requests, never inside one. The chunk in flight is
    // allowed to finish so that the server records what it wrote, which is
    // what History then has to offer back.
    if(stopping) break;
    const chunk=cs.slice(s,s+CHUNK);
    try{
      // The filters ride along so the server can say which files left the
      // view; it owns the matching rules, and a second copy here would drift.
      const p=new URLSearchParams();
      for(const [k,v] of Object.entries(VIEW)) if(v) p.set(k,v);
      const r=await fetch('/api/decide/bulk?'+p,{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({...body, batch,
          files:chunk.map(c=>({folder:c.dataset.folder,name:c.dataset.name}))})});
      if(!r.ok) throw new Error((await r.text()).slice(0,200));
      const out=await r.json();
      failed+=out.failed.length;
      gone=gone.concat(out.dropped||[]);
      if(out.total!==null&&out.total!==undefined) total=out.total;
      if(out.binned!==null&&out.binned!==undefined) binned=out.binned;
    }catch(e){
      trouble=e.message;
      break;
    }
    done+=chunk.length;
    workProgress(done,cs.length);
  }
  const stopped=stopping;
  busy=false; workClose();
  drawSel();
  cs.forEach(c=>details.delete(c.dataset.folder+'\\n'+c.dataset.name));
  if(viewer.classList.contains('on')&&cells[cur]) fill(cells[cur]);
  drop(gone);
  if(total!==null&&countEl) countEl.textContent=`${total.toLocaleString()} files`;
  drawBin(binned);
  if(trouble) say(`stopped after ${done} of ${cs.length}: ${trouble}`,true);
  else if(stopped) say(`stopped — ${done.toLocaleString()} of `
                      +`${cs.length.toLocaleString()} written. `
                      +`History has what landed.`,true);
  else if(failed) say(`${failed} file(s) could not be written`,true);
  else if(gone.length) say(`${gone.length} file(s) no longer match — removed`);
  else say('');
  // How many were actually written, so the caller can put back the ones that
  // were not. It used to report only pass or fail, and a failure half way
  // undid the half that had already been recorded.
  return {done};
}

// What each action edits, and which column its suggestions come from. The
// two are not the same word: `unshare` writes the audience field and offers
// audience values, and using the action name as the column asked the server
// for a column called `share` — a 400, and an empty list every time.
const ACT_COLUMN={tags:'tag', access:'audience', event:'event'};
(actions?[...actions.querySelectorAll('[data-act]')]:[]).forEach(b=>{
  const act=b.dataset.act;
  b.onclick=e=>{
    e.stopPropagation();
    if(act==='delete'){closeMenu();deleteSelection();return;}
    if(act==='stack'){closeMenu();stackSelection();return;}
    if(act==='top'){closeMenu();makeTop();return;}
    if(act==='unstack'){closeMenu();unstack();return;}
    if(act==='nostack'){closeMenu();notAStack();return;}
    if(act==='restore'){closeMenu();applyToSelection('deleted',false);return;}
    if(act==='purge'){closeMenu();purgeSelection();return;}
    openMenu(b, act==='date'
      ? {mode:'date'}
      : {column:ACT_COLUMN[act]||act, mode:'set', as:act});};
});

// The one action with no value to pick, so it asks instead of opening a menu.
// A confirm rather than a ceremony: this is the soft delete, it writes
// `deleted` into the sidecar like any other judgement, and History puts it
// back. Destroying the file itself is somewhere else entirely, and admin only.
//
// The question says none of that. *Deleted* is what the curator meant and what
// they should be told; that it is recoverable, and by whom, is how the app
// keeps its promise rather than a caveat on it. Answering "are you sure?" with
// "well, sort of" invites a yes that was never really given.
function deleteSelection(){
  const cs=targetsOn('live');
  if(!cs.length){say('nothing selected');return;}
  const what=cs.length===1?'this file':`these ${cs.length.toLocaleString()} files`;
  if(!confirm(`Are you sure you want to delete ${what}?`)) return;
  applyToSelection('deleted',true);
}

// The end of a file, so the question names the thing that cannot be taken
// back rather than asking politely. It goes to its own endpoint: purging is
// not a decision about a photograph, it is the end of one, and a shape
// `decide` could accept would make it one field of a routine edit.
async function purgeSelection(){
  const cs=targetsOn('gone');
  if(!cs.length){say('nothing selected');return;}
  const what=cs.length===1?'1 file':`${cs.length.toLocaleString()} files`;
  if(!confirm(`Permanently destroy ${what}? The originals and everything `
             +`made from them are removed. This cannot be undone.`)) return;
  if(busy){say('still writing…');return;}
  busy=true; say(''); workOpen('Purge', cs.length);
  const batch=newBatch();
  let purged=0, failed=0, gone=[], total=null, binned=null;
  try{
    for(let s0=0;s0<cs.length;s0+=CHUNK){
      if(stopping) break;
      const chunk=cs.slice(s0,s0+CHUNK);
      const p=new URLSearchParams();
      for(const [k,v] of Object.entries(VIEW)) if(v) p.set(k,v);
      const r=await fetch('/api/purge?'+p,{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({batch,
          files:chunk.map(c=>({folder:c.dataset.folder,name:c.dataset.name}))})});
      if(!r.ok) throw new Error((await r.text()).slice(0,200));
      const out=await r.json();
      purged+=out.purged; failed+=out.failed.length;
      gone=gone.concat(out.dropped||[]);
      if(out.total!==null&&out.total!==undefined) total=out.total;
      if(out.binned!==null&&out.binned!==undefined) binned=out.binned;
      workProgress(Math.min(s0+chunk.length,cs.length),cs.length);
    }
  }catch(e){
    busy=false; workClose();
    say(`stopped after ${purged} of ${cs.length}: ${e.message}`,true);
    return;
  }
  busy=false; workClose();
  drop(gone);
  if(total!==null&&countEl) countEl.textContent=`${total.toLocaleString()} files`;
  drawBin(binned);
  if(failed) say(`${failed} file(s) could not be purged`,true);
  else say(`${purged.toLocaleString()} file(s) destroyed`);
}

// --- keyboard ----------------------------------------------------------------
// The mouse is the interface. Keyboard navigation of the grid — arrows that
// moved and selected, shift to extend, ctrl to move without selecting, S to
// repeat a share — is gone rather than patched. Every one of those keys had
// to decide what it meant for the selection, each answered slightly
// differently, and between them they kept producing selections nobody had
// made. None of them could do anything the mouse cannot, so none of them was
// worth the ambiguity. Keyboard support is worth designing on purpose later,
// not accreting a key at a time.
//
// What stays is what only a key can say once the viewer is full-screen:
// which way to go, and stop.
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT') return;
  if(e.key==='Escape'){
    // Dismissal rather than navigation. A full-screen viewer with no key out
    // is a trap, even though clicking beside the picture also closes it.
    if(busy){stopWork();return;}
    if(choosing){endChoosing(true);return;}
    if(viewer.classList.contains('on')) closeViewer();
    else if(!menu.hidden) closeMenu();
    return;
  }
  if(!viewer.classList.contains('on')) return;
  const step=e.key==='ArrowRight'?1:e.key==='ArrowLeft'?-1:0;
  if(!step) return;
  e.preventDefault();
  // Paging is looking, not choosing, so whatever is selected stays selected.
  setCur((cur<0?0:cur)+step, true);
  drawSel();
});

// --- grouping ----------------------------------------------------------
// The heading is the control: the thing you want to regroup is the thing
// you click, and it costs no row at the top — every row of chrome up there
// is a row of photographs pushed off the screen.
function groupUrl(levels){
  const q=new URLSearchParams();
  for(const [k,v] of Object.entries(VIEW)) if(v) q.set(k,v);
  q.set('group',levels.join(',')||'none');
  return PAGE+'?'+q;
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
  placeMenu(anchorEl);
  menuCtx={key:'group:'+level+':'+insert};
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
  // The landing page's heading is the same control minus the selecting: it
  // is a page of folders, and there is nothing on it to tick.
  const gp=h.querySelector('.grppick');
  if(gp) gp.onclick=e=>{
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


@app.get("/large/{folder}/{name}")
def large(folder: str, name: str,
          user: Annotated[Principal, Depends(require_user)]) -> FileResponse:
    """The grid's largest setting. Between the two others because a cell that
    stretches to 460px wants 920 device pixels, which `thumb` has not got and
    `preview` has four times too many of."""
    _allowed(user, folder, name)
    return _serve(LARGE_DIR, folder, name)


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
    if column not in ("event", "tag", "audience", "date", "kind", "band",
                      "camera", "source"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"cannot suggest values for {column!r}")
    near = (near_from, near_to) if near_from and near_to else None
    return JSONResponse([
        {"value": s.value, "n": s.n, "scope": s.scope}
        for s in ix.suggest(db(), column, view, near=near)])


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
    date_override: str | None = None
    tags: list[str] | None = None
    add_tags: list[str] = []
    remove_tags: list[str] = []
    audience: list[str] | None = None
    add_audience: list[str] = []
    remove_audience: list[str] = []
    deleted: bool | None = None
    stacked_under: str | None = None
    no_stack: bool | None = None


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
    history.record(user.name, _summary(_change(body)),
                   [history.Before(body.folder, body.name, was,
                                   did=_recorded(_change(body)))])
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
    conn = ix.open_rw(DB_PATH) if DB_PATH.is_file() else None
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
            with _write_lock:
                removed = destroy_mod.destroy(media, conn=conn,
                                              folder=target.folder,
                                              name=target.name)
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
    date_override: str | None = None
    tags: list[str] | None = None
    add_tags: list[str] = []
    remove_tags: list[str] = []
    audience: list[str] | None = None
    add_audience: list[str] = []
    remove_audience: list[str] = []
    deleted: bool | None = None
    stacked_under: str | None = None
    no_stack: bool | None = None


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
                             "dropped": [], "total": None, "binned": None})
    if len(body.files) > BULK_LIMIT:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{len(body.files)} files in one request — send at most {BULK_LIMIT}")

    change = _change(body)
    did = _recorded(change)
    written = 0
    indexed = 0
    failed: list[dict[str, str]] = []
    binned: int | None = None
    # One index connection for the whole batch. Opening a SQLite file over SMB
    # per row dominated the cost — measured at 96ms/file against the NAS, most
    # of it the open rather than the write.
    conn = ix.open_rw(DB_PATH) if DB_PATH.is_file() else None
    targets = _with_guessed(conn, view, body.files)
    done: list[tuple[str, str]] = []
    undo: list[history.Before] = []
    dropped: list[dict[str, str]] = []
    total: int | None = None
    binned: int | None = None
    try:
        for target in targets:
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
            undo.append(history.Before(target.folder, target.name, was,
                                       did=did))
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
    date_override: str | None | Unset = decisions.UNSET
    tags: Sequence[str] | None | Unset = decisions.UNSET
    add_tags: Sequence[str] = field(default_factory=tuple)
    remove_tags: Sequence[str] = field(default_factory=tuple)
    audience: Sequence[str] | None | Unset = decisions.UNSET
    add_audience: Sequence[str] = field(default_factory=tuple)
    remove_audience: Sequence[str] = field(default_factory=tuple)
    deleted: bool | Unset = decisions.UNSET
    stacked_under: str | None | Unset = decisions.UNSET
    no_stack: bool | Unset = decisions.UNSET


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
                   remove_audience=tuple(body.remove_audience),
                   deleted=_flag(got("deleted")),
                   stacked_under=got("stacked_under"),
                   no_stack=_flag(got("no_stack")))


def _recorded(change: _Change) -> dict[str, Any]:
    """What an operation did, in the shape the log keeps it.

    Only the fields it actually sent — `UNSET` means *left alone*, and a log
    that could not tell that apart from *set to nothing* would revert fields
    the operation never touched.
    """
    out: dict[str, Any] = {}
    for name in ("event", "date_override", "tags", "audience", "deleted",
                 "stacked_under", "no_stack"):
        value: Any = getattr(change, name)
        if isinstance(value, Unset):
            continue
        out[name] = ([str(v) for v in cast("Sequence[str]", value)]
                     if isinstance(value, (list, tuple)) else value)
    for name in ("add_tags", "remove_tags", "add_audience", "remove_audience"):
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
                remove_audience=change.remove_audience,
                deleted=change.deleted,
                stacked_under=change.stacked_under,
                no_stack=change.no_stack)
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


def _with_guessed(conn: sqlite3.Connection | None, view: ix.Filters,
                  files: Sequence[Target]) -> list[Target]:
    """The selection, plus whatever a folded view is hiding behind it.

    A guessed stack shows one photograph and hides the rest, and the whole
    point of that is to work as though there is one file — so a decision made
    about what is on screen is a decision about all of them. Exactly the rule a
    real stack follows; the difference is only who did the grouping.

    **Resolved before the first write, not after.** Refusing a guess writes
    `no_stack` to the photograph that speaks for it, and the index answers by
    recomputing that group — which would leave the others grouped behind a new
    leader, still unanswered, ready to be offered again tomorrow. Asked first,
    the refusal reaches all of them.

    Not when the view is opened by grouping: there the members are on screen
    and in the selection already, so following them again would be a second
    write to a file the curator can see they already picked.
    """
    if conn is None or not view.stacks or view.unfold:
        return list(files)
    out = list(files)
    seen = {(t.folder, t.name) for t in files}
    for target in files:
        for folder, name in ix.proposed(conn, f"{target.folder}/{target.name}"):
            if (folder, name) in seen:
                continue
            seen.add((folder, name))
            out.append(Target(folder=folder, name=name))
    return out


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
    if change.add_audience:
        return f"gave {', '.join(change.add_audience)} access to {files}"
    if change.remove_audience:
        return f"took {', '.join(change.remove_audience)} access "\
               f"from {files}"
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
                 msg: Annotated[str, Query()] = "",
                 look: Annotated[str, Query()] = "") -> HTMLResponse:
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
        + (f'<td><a href="/browse?op={_q(op.id)}" '
           f'title="Look at these files">{_h(op.summary)}</a></td>'
           if op.files else
           # A purge names no files: they are gone, the index rows with them.
           # Offering a link to them would lead to an empty grid that reads as
           # broken rather than as *there is nothing left to look at*.
           f'<td>{_h(op.summary)}</td>')
        + f'<td class="dim">{_h(op.who)}</td>'
        + ('<td class="dim">undone</td>' if op.id in already else
           '<td class="dim">a revert</td>' if op.reverts else
           f'<td><form method="post" action="/history/revert">'
           f'<input type="hidden" name="id" value="{_h(op.id)}">'
           f'<button>Revert</button></form></td>')
        + "</tr>"
        for op in ops
    )
    # A revert that left files alone says how many. The number is the work
    # still to look at, so it comes with the way to go and look at it.
    seeing = (f' <a href="/browse?op={_q(look)}&amp;stale=1">'
              f'see the ones it left &rarr;</a>' if look else "")
    note = f'<p class="note">{_h(msg)}{seeing}</p>' if msg else ""
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
    op_id = (await _form(request)).get("id", "")
    op = history.get(op_id)
    if op is None:
        return RedirectResponse("/history?msg=no+such+operation", status_code=303)

    if not op.revertable():
        return RedirectResponse(
            "/history?msg=" + quote("that was recorded before reverting knew "
                                    "what an operation had done, so it cannot "
                                    "be put back"),
            status_code=303)

    restored = 0
    failed = 0
    moved = 0
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
                                   meta_dir=META_DIR, master_dir=MASTER_DIR)
                    except sqlite3.Error:
                        pass
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
    return RedirectResponse(f"/history?msg={quote(said)}{tail}",
                            status_code=303)


def _when(moment: float) -> str:
    """A timestamp as a person reads it."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(moment))
