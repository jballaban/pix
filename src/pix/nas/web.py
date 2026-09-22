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
import zipfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
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
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from pix import __version__ as _PIX_VERSION
from pix import datestr
from pix.nas import accounts
from pix.nas import auth
from pix.nas import decisions
from pix.nas import paths
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

_STYLE = """
/* **A card has to look like a card.** The surfaces were a shade apart and
   measured it: a tile against the page was 1.08:1, and its own border 1.04:1
   against the tile — which is not an edge, it is a rumour of one. Forty of
   them read as a single grey field with text in it, which is the complaint.
   Raised until each plane is told from the one beneath it: the page, the
   surfaces on it, and the lines that end them. No colour changed; the steps
   between them did. */
:root { color-scheme: dark; --bg:#14161a; --fg:#e7e9ee; --dim:#98a1b2;
        --line:#3a4250; --accent:#6aa3ff; --keep:#56c16a; --top:#e3b341; --gone:#e06c5a;
        /* Tags had no colour of their own and wore white, which on a card
           beside three coloured things reads as *the one nobody assigned*.
           Violet because it is the hue left: clear of the blue people wear,
           the green an audience does, and the amber that means unfinished. */
        --tag:#a48bff;
        /* Each of those again as something to sit *on*. A chip was coloured
           text in a near-black lozenge, so the colour was a thin outline of
           itself; filled at a seventh it becomes the chip, and a card reads
           as a few facts in their own colours rather than a grey list. */
        --keep-bed:#56c16a26; --accent-bed:#6aa3ff26;
        --tag-bed:#a48bff26; --top-bed:#e3b34126;
        --panel:#222833;
        /* Accent at a sixth, for saying *this one* behind a word rather than
           through it. */
        --tint:#6aa3ff2b;
        /* The bars are a surface, not part of the page. They were the same
           colour as it, separated by a single line — which put the tick that
           selects *everything* a few pixels from the one that selects the
           first group, on the same background, looking like the same kind of
           control. Reaching for the group and selecting the library is a
           mistake the colour was inviting. */
        --chrome:#262d38;
        /* One control's outer height: a 21px line (14px at 1.5), 4px of
           padding each side, 1px of border each side. Named because two rules
           have to agree on it — see `.row`. */
        --ctl:31px;
        /* The side gutter, in one place, because three bars and the page
           itself have to agree about it — and because in landscape the notch
           lies over the left of the screen, so each of them has to take the
           larger of the gutter and whatever the device says is unusable. */
        --gut:20px; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5
       system-ui,-apple-system,Segoe UI,sans-serif; }
a { color:var(--accent); text-decoration:none; }
/* Never an underline. It lands across the descenders of the very word you are
   reading at the moment you are trying to read it, and this page is lists of
   names — events, days, accounts, cameras — where the letters are the
   content. A tint behind the word says the same thing and leaves them alone.
   The `box-shadow` spread is the padding: it gives the tint room to breathe
   without taking up any, so nothing on the line moves. */
a:hover { background:var(--tint); box-shadow:0 0 0 3px var(--tint);
          border-radius:2px; }
.dim { color:var(--dim); }
main { padding:16px max(var(--gut),env(safe-area-inset-right)) 20px
               max(var(--gut),env(safe-area-inset-left)); }

/* The bar never leaves: filters are the address of what you are looking at,
   and losing them 2,000 thumbnails down is losing your place. */
/* Installed on a phone, the page owns the whole screen — including the strip
   behind the clock and the one the home indicator sits on. `env()` is zero in a
   browser tab, so this costs nothing there and is the difference between an app
   and a web page in a window everywhere else. */
.topbar { position:sticky; top:0; z-index:5;
          /* A plane catches more light at its top edge. Two stops and four
             per cent is not a gradient anybody will name — it is the
             difference between a panel and a rectangle of paint. */
          background:linear-gradient(180deg,#2b3340 0%,var(--chrome) 100%);
          border-bottom:1px solid #0b0e13;
          padding:calc(9px + env(safe-area-inset-top))
                  max(var(--gut),env(safe-area-inset-right)) 9px
                  max(var(--gut),env(safe-area-inset-left));
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
/* Where the browser draws no chrome of its own. Never in a tab, where it
   already draws this and a second one is a second thing to wonder about. */
.back { display:none; padding:4px 7px; margin-right:2px; flex:none; }
html[data-inapp] .back { display:inline-flex; align-items:center; }
@media (display-mode: standalone) { .back { display:inline-flex;
                                            align-items:center; } }
.back:disabled { opacity:.3; }
/* Three frames, one in front, with a picture in it — the app in one glyph:
   photographs, and one of them standing for the others. */
.brand { display:inline-flex; align-items:center; color:var(--fg); }
.brand:hover { background:none; box-shadow:none; }
.brand svg { display:block; }
.brand:hover .front { stroke:var(--accent); }
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
.chips .addchip { display:none; }
.addchip { padding:3px 9px; font-weight:600; color:var(--dim); }
.addchip:hover { color:var(--fg); border-color:var(--dim); }
/* The install offer, above the footer rather than over the photographs. It is
   an aside, not a decision to make, so it has the weight of one: panel colours,
   one line, and a way to end it permanently. */
.install { position:fixed; left:12px; right:12px; z-index:6;
           bottom:calc(12px + env(safe-area-inset-bottom));
           display:flex; gap:10px; align-items:center; font-size:13px;
           background:var(--panel); border:1px solid var(--line);
           border-radius:8px; padding:9px 11px;
           box-shadow:0 12px 30px -14px #000; }
.install .say { flex:1 1 auto; }
.install button { font:inherit; }
.install .no { background:none; border:none; color:var(--dim);
               cursor:pointer; padding:4px 6px; }
.install .no:hover { color:var(--fg); }
/* Not a strip that is usually empty — a note that is not there until there
   is something to say. It is how every write reports whether it happened, so
   it has to be unmissable when it speaks and take nothing when it does not. */
.note { position:fixed; z-index:22;
        left:50%; transform:translateX(-50%);
        bottom:calc(14px + env(safe-area-inset-bottom));
        max-width:min(560px, calc(100vw - 24px));
        background:var(--panel); color:var(--fg);
        border:1px solid var(--line); border-radius:6px;
        padding:9px 14px; font-size:13px;
        box-shadow:0 14px 34px -12px #000e; }
.note[hidden] { display:none; }
/* Signed out there is no menu to put it in, and the login screen is the one
   page a deploy can be checked on without a session. */
.ver { color:var(--dim); font-size:11px; margin-right:10px;
       font-variant-numeric:tabular-nums; }
/* What the library is, under the account. Read when wanted, never standing
   in front of the photographs. */
.memenu .info { display:block; border-top:1px solid var(--line);
                margin-top:4px; padding-top:5px; }
.memenu .info .line { display:block; padding:3px 10px; color:var(--dim);
                      font-size:11px; white-space:nowrap; }
.memenu .info .ver { font-variant-numeric:tabular-nums; }

.note.loud { background:#5a1d16; color:#ffd9d2; border-color:#7c2f24;
             font-weight:600; }

/* Still used inside the date menu, which explains what an empty box will
   do — a sentence about the control you are looking at, which is not the
   same thing as a standing list of gestures along the bottom of the app. */
.hint { color:var(--dim); font-size:12px; }
.hint b { color:var(--fg); font-weight:600; }

button, .chip { background:linear-gradient(180deg,#313a49,#28303c);
        color:var(--fg); border:1px solid var(--line);
        border-radius:5px; padding:4px 10px; font:inherit; cursor:pointer;
        box-shadow:inset 0 1px 0 #ffffff0f; }
/* A chip is a drawing, a value and a cross in a row, so it lays them out
   rather than relying on them being inline. `gap` is what stops the glyph
   sitting against the number it belongs to. */
.chip { display:inline-flex; align-items:center; gap:5px; }
.chip svg, .opt .mark svg { display:block; flex:none; }
/* A filter that is not doing anything: its question, and nothing else. Dim,
   because the ones that *are* doing something are what the bar is for and
   these must not compete with them — a row where everything is lit is a row
   with nothing highlighted. */
.chip.off { color:var(--dim); padding:4px 7px; }
.chip.off:hover { color:var(--fg); }
/* The list is the only place that shows a glyph and its name together, which
   makes it where the glyphs are learnt. */
.opt .mark { color:var(--dim); flex:none; display:block; }
/* Buttons that are a drawing and a word — the drawing needs the same air from
   the word that the chips give theirs. */
#actions button svg { display:block; flex:none; }
/* The tick is deliberately outside this: it is a circle with nothing in
   it, and a gap round nothing is still a wider circle. */
#actions .grp button { display:inline-flex; align-items:center; gap:6px; }
button:hover:not(:disabled), .chip:hover { border-color:var(--accent); }
button:disabled { opacity:.4; cursor:default; }
button.primary { background:linear-gradient(180deg,#83b2ff,#5b97fb);
                 color:#0b1220; border-color:#5b97fb; font-weight:600;
                 box-shadow:inset 0 1px 0 #ffffff40,
                            0 2px 10px -4px #6aa3ff8c; }
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
/* `dvh` after `vh`, never instead of it: an engine that does not know the
   unit keeps the old value rather than losing the rule. `vh` is the viewport
   as though the browser's own bars were not there, so the bottom of a long
   list sat underneath them. */
#menu { position:absolute; z-index:20; width:300px; max-height:60vh;
        max-height:60dvh;
        background:var(--panel); border:1px solid var(--line); border-radius:6px;
        box-shadow:0 10px 30px #0009; display:flex; flex-direction:column; }
/* An id selector beats the user agent's `[hidden] { display:none }`, so the
   rule above quietly won and `hidden = true` set a flag that hid nothing —
   the menu could be opened and never dismissed. */
#menu[hidden] { display:none; }
#menu input { background:#14161a; color:var(--fg); border:0;
              border-bottom:1px solid var(--line); padding:9px 11px; font:inherit;
              border-radius:6px 6px 0 0; outline:none; width:100%; }
/* `contain`, so dragging past the end of the list scrolls nothing. Without it
   the page behind takes over, and the page closing on a scroll is what closes
   the menu — a list you cannot reach the bottom of without dismissing it. */
#menulist { overflow-y:auto; padding:4px 0; overscroll-behavior:contain; }
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
/* A folder: one section of the library, and what is worth knowing about it
   before you open it. No photograph — a cover was whichever file happened to
   be first, which said what one picture in there looks like and nothing about
   the section, and a wall of unrelated pictures is harder to read than a wall
   of text rather than easier. */
.grid.folders { grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); }
.tile { display:flex; flex-direction:column; gap:3px; padding:11px 13px 12px;
        background:var(--panel); border:1px solid var(--line);
        border-radius:5px; text-decoration:none; color:var(--fg);
        /* An edge and a shadow say *this is a thing on top of that* twice,
           which is what a card needs to say when there are forty of them. */
        box-shadow:inset 0 1px 0 #ffffff0d,
                   0 1px 2px #0006, 0 6px 14px -10px #000a; }
.tile .name { font-size:15px; font-weight:600; line-height:1.25;
              letter-spacing:-.005em; overflow-wrap:anywhere; }
.tile .name .sep { color:var(--dim); font-style:normal; font-weight:400;
                   margin:0 5px; }
.tile .when { font-size:11px; color:var(--dim); }
/* The same *of* the count uses, for the same reason: this card is a slice of
   something longer, and saying so about the files and not about the days told
   half of one fact. */
.tile .when.split i { font-style:normal; opacity:.72; margin:0 2px; }
.tile .n { font-size:12px; margin-top:3px;
           font-variant-numeric:tabular-nums; }
.tile .kinds { font-style:normal; color:var(--dim); }
/* A folder that is one slice of something larger. The two numbers say it
   without a word — this many here, that many in all — so it needs no colour
   of its own. On a real library most cards are slices the moment you group by
   month and event, and a page where most cards are highlighted is a page with
   no highlight on it. */
.tile .split i { font-style:normal; color:var(--dim); }
.tile .kinds::before { content:" · "; }
/* What the folder holds, in the same chips a thumbnail wears — so a card
   and a photograph say the same kind of thing about themselves. */
.spread { display:flex; flex-wrap:wrap; gap:3px; margin-top:6px; }
.spread i { font-style:normal; font-size:10px; font-weight:600;
            padding:2px 7px; border-radius:999px;
            max-width:100%; overflow:hidden; white-space:nowrap;
            text-overflow:ellipsis; }

/* Each one opens the folder cut down to itself, so it reads as something to
   press rather than as a label that happens to be there. */
.spread i[data-col] { cursor:pointer; }
.spread i[data-col]:hover { background:#000; outline:1px solid currentColor;
                            outline-offset:-1px; }
.spread.audience i { color:#8fe0a0; background:var(--keep-bed); }
.spread.people i { color:#a6c8ff; background:var(--accent-bed); }
.spread.tags i { color:#cbb8ff; background:var(--tag-bed); }
/* What is left to do, in the colour this app has always used for *this wants
   you* — the same amber as a guessed stack and a half-ticked box. It is the
   one chip on a card that is a job rather than a fact, and green filed it in
   with the audience it is counted from.

   **After the three above, and it has to be.** `.spread i.none` and
   `.spread.audience i` weigh exactly the same, so the later one wins and this
   was drawn green for as long as it sat higher up the file. Anything added
   below this that colours a chip takes it back. */
.spread.audience i.none { color:#f2d38a; background:var(--top-bed); }
.tile:hover { border-color:var(--accent); background:#272e3b;
             box-shadow:0 1px 2px #0006, 0 10px 22px -12px #000c; }
/* A folder can be selected, so it carries the same circle a thumbnail does
   and reads the same when it is chosen. Top right rather than top left: a
   card leads with its name, and a control over the first word of it is a
   control in the way of the thing you are reading. */
.tile { position:relative; }
.tile .pick { left:auto; right:9px; top:11px; }
.tile:hover .pick, .tile.picked .pick { opacity:1; }
.tile .name { padding-right:28px; }
.tile.picked { border-color:var(--accent); background:#20273a; }
/* A section with no address. It is still a real pile of files, so it is still
   shown — it just cannot be opened on its own. */
.tile.dead { cursor:default; opacity:.7; }
.tile.dead:hover { border-color:var(--line); background:var(--panel); }
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
           padding-bottom:5px;
           /* Fading out rather than ruled across: a line that stops where the
              words do says *this heading* where a full-width rule says
              *another table*. */
           border-bottom:1px solid transparent;
           border-image:linear-gradient(90deg,var(--accent),var(--line) 38%,
                                        transparent) 1; }
h3.group:first-child { margin-top:0; }
h3.group > span.dim { font-weight:400;
                      font-variant-numeric:tabular-nums; }
/* A path, so the last crumb — the one that actually changed — is the one
   that reads loudest. */
.crumbs { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.crumb { display:inline-flex; align-items:center; }
.crumbs .sep { color:var(--dim); font-weight:400; }
.crumb:not(:last-child) .grpname { color:var(--dim); font-weight:400; }
/* The other way round on a shelf, where the last crumb is the name of the cut
   rather than a value: *By event* is the same two words over every shelf on
   the page, and *2025* is the one that says which shelf this is. */
h3.group.shelf .crumb:not(:last-child) .grpname { color:var(--fg);
                                                  font-weight:600; }
h3.group.shelf .crumb:last-child .grpname { color:var(--dim);
                                            font-weight:400; }
/* **A heading is words, not a toolbar.** Three of these are buttons, and a
   button here got a border, a gradient and a hairline of light along its top
   when those were given to buttons generally — so the heading grew a line
   above its own text and a box around its plus sign. They wear nothing until
   they are pointed at; `box-shadow:none` is the part that is easy to forget,
   because clearing the background and the border looks like it was enough. */
.rmgrp { background:none; border:0; box-shadow:none; margin:0; padding:0 4px;
         color:var(--dim);
         font:inherit; cursor:pointer; opacity:0; transition:opacity .1s; }
.crumb:hover .rmgrp, .rmgrp:focus { opacity:1; }
.rmgrp:hover { color:#ffb4a2; }
/* The name is the control: click it to regroup, `+` to group within it. */
.grpname { background:none; border:0; box-shadow:none; padding:0; margin:0;
           color:inherit; font:inherit; cursor:pointer; }
.grpname:hover { color:var(--accent); background:var(--tint);
                 box-shadow:0 0 0 3px var(--tint); border-radius:2px; }
/* Beside the name it belongs to, not marooned at the end of the row, where it
   went unnoticed. Dim rather than hidden: a control you cannot see until you
   hover the right thing is a control you never learn is there, and a page of
   faint plus signs is quiet enough. */
.addgrp { margin:0; padding:0 7px; line-height:1.3; font-size:14px;
          background:none; border-color:transparent; box-shadow:none;
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
.cell.picked .pick::after,
.tile.picked .pick::after { content:"\\2713"; color:#0d0f12; font-weight:700;
                            font-size:13px; line-height:17px; }
.tile.picked .pick { background:var(--accent); border-color:var(--accent); }
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
.stack:hover { background:var(--accent); color:#0d0f12; box-shadow:none; }
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
/* The same violet a card gives a tag, kept legible over a photograph by
   staying text on a dark lozenge rather than a tint. One colour for one kind
   of thing, whichever surface it is written on. */
.tags i { color:#cbb8ff; }

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
/* Everything about you, and everything you do rarely, under one control.
   Opens on hover *and* on focus, with no script, because /history and
   /accounts carry none — and a Sign out that only worked where the grid was
   loaded would be missing from the page you are most likely to be stuck on. */
.me { position:relative; display:inline-flex; align-items:center;
      outline:none; }
.me .name { cursor:default; color:var(--fg); gap:5px; }
/* The name where there is room, the gear where there is not. Both are in the
   markup and the width picks, because the one that is hidden must not be a
   second copy of anything — two `#sizepick`s is one id and two controls. */
.me .gearbtn { display:none; cursor:default; align-items:center; }
.memenu .whoami { display:none; }
/* What is waiting, on the control rather than beside it. A notification that
   needs opening to be seen is not one. */
.me .dot { display:none; position:absolute; right:-2px; top:0;
           width:7px; height:7px; border-radius:50%;
           background:var(--gone); border:1.5px solid var(--chrome); }
.me[data-any="1"] .dot { display:block; }
/* One or the other: something to report, or the fact that there is nothing. */
.me[data-any="1"] .memenu .quiet { display:none; }
.memenu .quiet { color:var(--dim); padding:6px 10px; white-space:nowrap; }
.memenu .bin-link { display:block; padding:6px 10px; border-radius:3px; }
.memenu .bin-link:hover { background:#242a33; box-shadow:none; }
/* The size control reads as a row of the menu now, not a lozenge in the bar:
   it has a whole line to say what it is on, so it says it. */
.memenu .sizerow { display:block; width:100%; text-align:left;
                   background:none; border:0; color:var(--fg);
                   padding:6px 10px; border-radius:3px; cursor:pointer; }
.memenu .sizerow:hover { background:#242a33; }
.me .caret { font-style:normal; font-size:9px; color:var(--dim);
             transition:transform .12s; }
.me:hover .caret, .me:focus-within .caret { transform:rotate(180deg); }
.memenu { position:absolute; right:0; top:100%; margin-top:6px; z-index:21;
          min-width:150px; display:none; flex-direction:column;
          background:var(--panel); border:1px solid var(--line);
          border-radius:4px; padding:4px; box-shadow:0 10px 24px -8px #000d; }
.me:hover .memenu, .me:focus-within .memenu { display:flex; }
/* A hover menu that starts below its own trigger has a gap to cross, and the
   pointer leaves through it. This is that gap, made part of the control. */
.me::after { content:""; position:absolute; right:0; top:100%;
             width:100%; height:8px; }
.memenu a, .memenu button { display:block; width:100%; text-align:left;
          padding:6px 10px; border:0; background:none; color:var(--fg);
          font:inherit; border-radius:3px; text-decoration:none;
          cursor:pointer; }
.memenu a:hover, .memenu button:hover { background:#242a33;
                                        box-shadow:none; }
.memenu form { margin:0; }
/* The way out is the one thing in here that is not navigation. */
.memenu button { color:var(--gone); }
/* Another `display` that would outrank the user agent's `[hidden]`. The bin
   count is hidden at zero and shown the moment something is deleted, without
   a reload, so it has to be hideable. */
.who-link[hidden] { display:none; }
.who-link button { padding:3px 9px; margin:0; }
.empty { color:var(--dim); padding:48px 0; font-size:15px;
         text-align:center; }

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

/* Nothing behind it moves. A full-screen overlay over a scrollable page
   rubber-bands the grid underneath every time you drag on the photograph,
   and on a phone that is most touches. `touch-action` rather than locking
   the body: the viewer deliberately scrolls the grid behind it as you page,
   so that closing it leaves you where you were looking, and a fixed body
   would break that.

   Pinch goes with it. It only ever zoomed the *page* — the photograph is as
   big as it is — which left a fixed overlay panned half off the screen with
   the way out somewhere past the edge. */
#viewer { position:fixed; inset:0; background:#000e; display:none; z-index:30;
          touch-action:none; overscroll-behavior:none; }
#viewer.on { display:flex; }
.stage { flex:1; min-width:0; display:flex; flex-direction:column;
         align-items:center; justify-content:center; padding:12px; }
.stage img, .stage video { max-width:100%; max-height:86vh;
                           max-height:86dvh;
                           object-fit:contain; display:none; }
.stage img.on, .stage video.on { display:block; }
.stage .meta { padding:10px; color:var(--dim); font-size:12px;
               text-align:center; }
/* A reserved column, not an overlay: metadata you have to summon and that
   then covers the photograph is metadata nobody consults while looking. */
#rail { touch-action:pan-y;
        width:330px; flex:none; background:var(--panel); overflow-y:auto;
        border-left:1px solid var(--line); padding:14px 16px 30px;
        font-size:13px; }
#viewer.norail #rail { display:none; }
/* The viewer covers the whole screen, the status bar and the island
   included, so its own controls are the one place in the app that has to
   say so itself — everything else sits inside a bar that already has. */
#railtoggle { position:absolute; top:calc(10px + env(safe-area-inset-top));
              right:calc(12px + env(safe-area-inset-right)); z-index:2;
              margin:0; opacity:.75; }
#viewclose { position:absolute; top:calc(10px + env(safe-area-inset-top));
             left:calc(12px + env(safe-area-inset-left)); z-index:2; margin:0;
             opacity:.75; font-size:17px; line-height:1; padding:2px 10px; }
/* Beside Details, because it is the same kind of thing: something you reach
   for about the photograph you are looking at, rather than a way out of it. */
#viewget { position:absolute; top:calc(10px + env(safe-area-inset-top));
           right:calc(112px + env(safe-area-inset-right)); z-index:2; margin:0;
           opacity:.75; border:1px solid var(--line); border-radius:3px;
           padding:3px 10px; font-size:13px; background:var(--chrome);
           color:var(--fg); }
#railtoggle:hover, #viewclose:hover, #viewget:hover { opacity:1; }
#viewget:hover { border-color:var(--accent); background:var(--chrome);
                 box-shadow:none; }
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

/* --- a narrow screen ------------------------------------------------------
   About width, not about fingers: a phone in landscape and a window dragged
   narrow want the same layout, and a touchscreen laptop with a desk's worth
   of glass does not. What the finger changes is in the block after this one.

   Twenty pixels of gutter either side is a tenth of a phone, and the three
   thumbnail sizes were 2, 1 and 1 columns — two of the three settings the
   same, which is a control that does nothing two-thirds of the time. */
@media (max-width: 720px) {
  :root { --gut:10px; }
  main { padding-top:12px; padding-bottom:16px; }
  .grid { grid-template-columns:repeat(auto-fill,minmax(108px,1fr)); }
  .grid[data-size="medium"] {
          grid-template-columns:repeat(auto-fill,minmax(165px,1fr)); }
  .grid[data-size="large"] { grid-template-columns:1fr; }
  /* Above the footer rather than across it. */
  .install { bottom:calc(46px + env(safe-area-inset-bottom)); }

  /* The bar is for what you are looking at. Everything you reach for once in
     a while goes behind the gear, and the name goes to the first line of the
     menu it opens — one tap rather than none, which is what the room costs.
     The three of them plus the filters had the bar at four rows and nearly
     half the screen. */
  .me .name { display:none; }
  .me .gearbtn { display:inline-flex; }
  .memenu .whoami { display:block; color:var(--dim); font-size:11px;
                    text-transform:uppercase; letter-spacing:.07em;
                    padding:7px 10px 3px; }
  /* The corner stays. It was moved to the bottom when the bar was four rows
     deep; the bar is the filters and a gear now, and a picture of the grid
     under it earns the thirty pixels. */

  /* The unused filters are ten glyphs, which fit across a desktop bar and
     do not fit across a phone. Here the same list stays behind the `+`.
     Both are always rendered, because which one applies can change while the
     page is open by turning the phone over. */
  .chips .spare { display:none; }
  .chips .addchip { display:inline-flex; }

  /* Eleven drawings and eleven words is two rows of bar on a screen with none
     to give, and the drawings are the half that survives being small. The
     word stays in the markup — the takeover reads it back, and it is the
     button's `aria-label` besides. */
  #actions .grp button .word { display:none; }
  #actions .grp button { min-width:44px; justify-content:center; }
  /* Said in words because it is a state rather than a control: the bar is
     waiting for you to point at a photograph, and there is no drawing for
     *now do that*. */
  #actions .grp b { font-size:12px; }

  /* The details rail was a 330px column, which on a 393px phone leaves sixty
     pixels of photograph — the photograph being what the viewer is for. There
     is not room for both, so it stops trying: details is a *tab*, and the
     control that opened the column now switches between the two. */
  #viewer { flex-direction:column; }
  .stage { flex:1 1 auto; min-height:0; }
  .stage img, .stage video { max-height:100%; }
  #viewer:not(.norail) .stage { display:none; }
  #rail { width:auto; flex:1 1 auto; max-height:none; border-left:0;
          /* Clear of the row of controls that floats over the top of it. */
          padding-top:calc(52px + env(safe-area-inset-top));
          padding-bottom:calc(30px + env(safe-area-inset-bottom)); }
  /* Three controls, two corners. `right:112px` was measured against the width
     of the word *Details*, which is not a number to rest a layout on once
     everything in the bar is taller and wider. */
  #viewget { right:auto; left:calc(68px + env(safe-area-inset-left)); }
}

/* Anything the page keeps out of sight until the pointer is over it is
   unreachable where there is no pointer. Separate from the sizing block
   below, because these are two different questions and a device can answer
   them differently — a touchscreen laptop has a pointer and a phone plugged
   into a trackpad has a coarse one. */
@media (hover: none) {
  /* Touch has no hover, so here the circle is the only way to select at all. */
  .pick { opacity:.55; }
  /* The only way to say which file a stack shows. It is on screen solely
     while one is being chosen, so there is nothing for it to clutter. */
  .choose { opacity:1; }
  /* Removing a grouping level, and adding one. Dim rather than hidden, the
     way the select circle already is here. */
  .rmgrp { opacity:.55; }
  .addgrp { opacity:1; }
  /* Nothing hides it on a tap, so it moves off the circle instead of
     waiting to be got out of the way. */
  .cell.gone::after { left:auto; right:6px; }
  .cell.gone:hover::after { opacity:1; }

  /* A tap leaves `:hover` stuck on whatever was tapped until something else
     is, so every link keeps a blue box and every folder card stays lit — the
     page slowly fills with marks saying *you were here*, which is not a thing
     it was ever trying to say. Said here rather than by wrapping forty rules
     in `(hover: hover)`: this is the exception, and it should read like one.
     `:active` below puts back the press feedback these were doing. */
  a:hover { background:none; box-shadow:none; }
  .tile:hover { border-color:var(--line); background:var(--panel); }
  .grpname:hover { color:inherit; background:none; box-shadow:none; }
  button:hover:not(:disabled), .chip:hover { border-color:var(--line); }
  .stack:hover { background:#000b; color:var(--fg);
                 box-shadow:2px -2px 0 -1px #000b, 4px -4px 0 -2px #000b; }
}

/* --- a finger ---------------------------------------------------------------
   Apple asks for 44 points square and the page was drawn to 16, 20 and 31.
   Nothing here changes what anything *looks* like where it can be helped:
   the select circles keep their twenty pixels and grow an invisible slug
   around them, because a page of 44px circles over the photographs would be
   a page about its own controls. */
@media (pointer: coarse) {
  /* One number, and the rest follows. `--ctl` exists so that the height the
     action row reserves and the height a button actually measures cannot
     drift (see the note where it is declared) — so this is the whole of the
     change to the bar, including `.row + .row`'s reserved height and the
     brand and account at the ends of the top row. */
  :root { --ctl:44px; }

  /* Every button except the three that are circles. Those are excluded for
     the reason set out above `.row + .row`: a `min-height` outranks their
     `height` and would make an oval of every select circle in the grid.
     `.chip` is in by name because two of them — the one saying which
     operation you arrived from, and the one saying which stack you are in —
     are spans rather than buttons, and a 31px chip in a 44px row is the
     misalignment `--ctl` exists to prevent. */
  /* **Size only. It must not touch `display`.** Three `:not()`s weigh three
     classes, so this outranks almost anything that tries to hide one of these
     later — and the filters a phone folds away are buttons. Giving them a
     `display` here put every unused filter back on the bar, four rows of
     them, while `.chips .spare { display:none }` sat two blocks above being
     outweighed. A button centres its own text; nothing here needed to say so.
     */
  button:not(.tick):not(.grppick):not(.pick), .chip {
    min-height:44px; padding:8px 12px; }
  /* Rows in a dropdown are full width, so they stay blocks and simply get
     taller. Carrying the same three `:not()`s as the rule above, and not for
     tidiness: each of those counts as a class, so `.memenu button` is the
     lighter selector and loses — and Sign out shrink-wraps to its own text
     under History and Accounts, which do not, because they are links and that
     rule never touched them. Equal weight and later in the sheet is what
     makes this the one that counts. */
  .memenu a, .memenu .quiet, .memenu .bin-link, .memenu .whoami,
  .memenu button:not(.tick):not(.grppick):not(.pick) {
    min-height:44px; display:flex; align-items:center; width:100%; }
  .opt { min-height:44px; align-items:center; }
  #sizepick { width:44px; height:44px; }

  /* The circles. An invisible slug either side, so twenty pixels on screen is
     forty-four to a thumb. `.pick` is already positioned and already uses
     `::after` for its tick; the other two are neither, so they are told to
     be. */
  .tick, .grppick { position:relative; }
  .pick::before, .tick::before, .grppick::before {
    content:""; position:absolute; inset:-12px; }
  /* And now they must not poach their neighbours. A slug reaches twelve
     pixels past a circle that had eight of clearance, so the gap has to grow
     by more than the overhang — otherwise a tap on the left edge of a group's
     name selects the group instead of regrouping by it. */
  h3.group { gap:14px; }
  #actions .grp { gap:14px; }
  /* The cross that clears a filter, which was ten pixels of glyph inside a
     button that does something else. Stretched to the chip's own height
     rather than padded, so the chip does not grow to hold it. */
  /* Padding for the hit area and a matching negative margin so the chip
     does not grow to hold it. Not `align-self: stretch`, which would work
     only for as long as every chip is a flex container — and one of them is
     a span that is only one because the rule above made it one. */
  .chip .x, .from-op .x { padding:12px 10px; margin:-12px -10px -12px 0; }
  /* `text-overflow` needs a block box, and the chips are flex ones here. The
     name of the operation is the part that can run long, so it is the part
     that has to do the eliding. */
  .from-op { overflow:hidden; }
  .from-op b { overflow:hidden; text-overflow:ellipsis; min-width:0; }

  /* Tapping twice quickly on two circles side by side is a double-tap, and a
     double-tap zooms. */
  .cell, button, .opt, .chip { touch-action:manipulation; }

  /* iOS paints a grey box over anything tapped, which on a grid of
     photographs reads as a rendering fault. Removing it without putting
     something back leaves touch with no press feedback at all, so both
     happen here or neither should. */
  button, .chip, .opt, .cell, a { -webkit-tap-highlight-color:transparent; }
  button:active:not(:disabled), .chip:active { background:#2f3745; }
  .opt:active { background:#38424f; }
  .cell:active img { opacity:.75; }

  /* Long-pressing a thumbnail raises the system's own sheet, whose
     *Save to Photos* saves the four-hundred-pixel thumbnail — and in the
     viewer, the sixteen-hundred-pixel preview. Either way somebody walks off
     believing they have the photograph. Save is how you get the photograph.
     Scoped to coarse, because on a desk selecting a heading to copy it is
     ordinary. */
  .cell, .cell img, .stage img, .pick, .tick, .grppick, h3.group {
    -webkit-touch-callout:none; -webkit-user-select:none; user-select:none;
    -webkit-user-drag:none; }

  /* Sixteen pixels, or the page zooms in on focus and does not zoom back —
     which leaves the library at 130% with no way to say so. It is the exact
     threshold, so this is the smallest these can be. */
  #menu input, #menu .form input { font-size:16px; }
}
"""


def _folder_path(x: float, y: float, w: float = 16, h: float = 12.4,
                 r: float = 2.4) -> str:
    """One folder outline, drawn exactly where a card of the same size sits.

    Same box as the cards in `_FILES_MARK`, so the two marks are the same
    three shapes at the same three offsets and only their *kind* differs —
    which is the whole point of a toggle you read at a glance.
    """
    # `:g` throughout: plain arithmetic on these puts 20.400000000000002 into
    # the markup, which renders identically and reads like a mistake.
    return (f"M{x + r:g} {y:g}h5.2l1.4 1.8H{x + w - r:g}"
            f"a{r:g} {r:g} 0 0 1 {r:g} {r:g}V{y + h - r:g}"
            f"a{r:g} {r:g} 0 0 1 {-r:g} {r:g}H{x + r:g}"
            f"a{r:g} {r:g} 0 0 1 {-r:g} {-r:g}V{y + r:g}"
            f"a{r:g} {r:g} 0 0 1 {r:g} {-r:g}z")


#: The library as files: three photographs, the front one showing.
_FILES_MARK = (
    '<svg viewBox="0 0 28 28" width="26" height="26" aria-hidden="true">'
    '<rect x="9" y="3.6" width="16" height="12.4" rx="2.4" fill="none" '
    'stroke="var(--dim)" stroke-width="1.4" opacity=".45"/>'
    '<rect x="6" y="7" width="16" height="12.4" rx="2.4" fill="none" '
    'stroke="var(--dim)" stroke-width="1.4" opacity=".75"/>'
    '<rect class="front" x="3" y="10.4" width="16" height="12.4" rx="2.4" '
    'fill="var(--panel)" stroke="var(--fg)" stroke-width="1.5"/>'
    '<circle cx="7.6" cy="14.4" r="1.5" fill="var(--top)"/>'
    '<path d="M4.4 20.6 L8.6 16.6 L11.4 19.2 L13.6 17.2 L17.6 20.8" '
    'fill="none" stroke="var(--accent)" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>')

#: The same library as folders: the same three shapes, with tabs.
_FOLDERS_MARK = (
    '<svg viewBox="0 0 28 28" width="26" height="26" aria-hidden="true">'
    f'<path d="{_folder_path(9, 3.6)}" fill="none" stroke="var(--dim)" '
    'stroke-width="1.4" opacity=".45"/>'
    f'<path d="{_folder_path(6, 7)}" fill="none" stroke="var(--dim)" '
    'stroke-width="1.4" opacity=".75"/>'
    f'<path class="front" d="{_folder_path(3, 10.4)}" fill="var(--panel)" '
    'stroke="var(--fg)" stroke-width="1.5" stroke-linejoin="round"/>'
    '<path d="M6.4 17.6H13.2" stroke="var(--accent)" stroke-width="1.5" '
    'stroke-linecap="round"/>'
    '<path d="M6.4 20.2H10.6" stroke="var(--dim)" stroke-width="1.5" '
    'stroke-linecap="round"/></svg>')


def _logo_mark(size: int) -> str:
    """The logo: one photograph, on its own.

    Deliberately *not* the three-shape marks above. Those became a control the
    moment the corner started toggling between files and folders, and a control
    that changes under you cannot also be what the app is called. This is what
    stays still — the favicon and the sign-in page — and one card reads at 16
    pixels where a stack of three is mush.
    """
    return (f'<svg viewBox="0 0 28 28" width="{size}" height="{size}" '
            'aria-hidden="true">'
            '<rect x="4" y="6" width="20" height="16" rx="3" '
            'fill="var(--panel)" stroke="var(--fg)" stroke-width="1.6"/>'
            '<circle cx="9.2" cy="11" r="1.8" fill="var(--top)"/>'
            '<path d="M5.6 20.4 L11 14.6 L14.4 18.2 L17.2 15.4 L22.4 20.8" '
            'fill="none" stroke="var(--accent)" stroke-width="1.6" '
            'stroke-linecap="round" stroke-linejoin="round"/></svg>')


#: The same mark with the colours written out, because a favicon is a document
#: of its own and never sees this page's variables. On a tile, because that is
#: what a browser puts in a tab strip and a bookmark bar.
_FAVICON = "data:image/svg+xml," + quote(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28 28">'
    '<rect width="28" height="28" rx="6" fill="#14161a"/>'
    '<rect x="4" y="6" width="20" height="16" rx="3" fill="#1b1e24" '
    'stroke="#e7e9ee" stroke-width="1.6"/>'
    '<circle cx="9.2" cy="11" r="1.8" fill="#e3b341"/>'
    '<path d="M5.6 20.4 L11 14.6 L14.4 18.2 L17.2 15.4 L22.4 20.8" '
    'fill="none" stroke="#6aa3ff" stroke-width="1.6" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>', safe="")


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
_INSTALL_JS: str = """
(function () {
  var KEY = 'pix2.install';
  function answered() {
    try { return localStorage.getItem(KEY) === 'no'; } catch (e) { return false; }
  }
  function remember() {
    try { localStorage.setItem(KEY, 'no'); } catch (e) {}
  }

  // Already installed: the offer would be absurd inside the thing it offers.
  // `display-mode` is the standard reading; `navigator.standalone` is how iOS
  // has always said it and still does.
  var inApp = (window.matchMedia
               && window.matchMedia('(display-mode: standalone)').matches)
              || navigator.standalone === true;
  if (inApp || answered()) return;

  var ua = navigator.userAgent || '';
  // iPadOS reports itself as a Mac, and has done for years; touch points are
  // what tell a tablet from a desktop that happens to have a trackpad.
  var ios = /iPhone|iPad|iPod/.test(ua)
            || (/Macintosh/.test(ua) && (navigator.maxTouchPoints || 0) > 1);
  var android = /Android/.test(ua);
  if (!ios && !android) return;

  var strip = null, prompter = null;

  function close() {
    remember();
    if (strip && strip.remove) strip.remove();
    strip = null;
  }

  function show() {
    if (strip) return;
    strip = document.createElement('div');
    strip.className = 'install';
    strip.id = 'install';

    var say = document.createElement('span');
    say.className = 'say';
    say.textContent = ios
      ? 'Add pix to your home screen: tap Share, then Add to Home Screen.'
      : (prompter ? 'Add pix to your home screen.'
                  : 'Add pix to your home screen from your browser menu.');
    strip.appendChild(say);

    if (prompter) {
      var go = document.createElement('button');
      go.className = 'primary go';
      go.textContent = 'Install';
      go.onclick = function () {
        // Whatever they answer, they have answered: a prompt dismissed at the
        // system level must not bring this bar back on the next page.
        var asked = prompter;
        prompter = null;
        close();
        if (asked && asked.prompt) asked.prompt();
      };
      strip.appendChild(go);
    }

    var no = document.createElement('button');
    no.className = 'no';
    no.textContent = 'No thanks';
    no.onclick = close;
    strip.appendChild(no);

    document.body.appendChild(strip);
  }

  // Chrome offers the prompt to the page instead of showing its own; taking it
  // is what turns the sentence into a button. It may never arrive — Firefox
  // and Samsung Internet do not send it — which is why the bar does not wait
  // for it.
  window.addEventListener('beforeinstallprompt', function (e) {
    if (e.preventDefault) e.preventDefault();
    prompter = e;
    if (strip && strip.remove) { strip.remove(); strip = null; }
    show();
  });
  window.addEventListener('appinstalled', close);
  show();
})();
"""


def _page(title: str, body: str, *, tools: str = "", rows: str = "",
          right: str = "", info: str = "", script: str = "",
          zoom: str = "", status_code: int = 200,
          user: Principal | None = None) -> HTMLResponse:
    """One shell.

    `tools` sits beside the brand on the first row, `right` goes **inside** the
    account menu at the far end of it, `rows` are whole extra rows below it,
    and `footer` is the strip along the bottom.

    `right` used to stand in the bar. It is one control — the thumbnail size
    — used once in a while, and on a phone it was one of three such controls
    holding a row open in front of the filters. Inside the menu it costs a tap
    and no room at all.

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
<link rel="icon" href="{_FAVICON}"><style>{_STYLE}</style></head><body>
<div class="topbar">
<div class="row"><button class="back" id="back" aria-label="Back"
 title="Back">{_BACK}</button>{_brand(zoom)}{tools}
<span class="spacer"></span><span class="right">{_whoami(user, right, info)}</span></div>{rows}
</div><main>{body}</main>
<span class="note" id="note" hidden></span>
{script}
<script>if('serviceWorker' in navigator)window.addEventListener('load',function(){{
  navigator.serviceWorker.register('/sw.js').catch(function(){{}});}});</script>
<script>(function(){{
  // Installed, the app is the whole window: no address bar, no back. The
  // media query is the standard reading and `navigator.standalone` is how iOS
  // has always said it — the same pair the install offer asks.
  var app = (window.matchMedia
             && window.matchMedia('(display-mode: standalone)').matches)
            || navigator.standalone === true;
  if (app) document.documentElement.setAttribute('data-inapp', '');
  var back = document.getElementById('back');
  if (!back) return;
  // A fresh launch has nowhere to go back to, and a button that does nothing
  // teaches you to stop believing the rest of them.
  if (history.length <= 1) back.disabled = true;
  back.onclick = function () {{ history.back(); }};
}})();</script>
<script>{_INSTALL_JS}</script>
</body></html>""")


#: Back, for where the browser's own is not on screen.
#:
#: **Only there.** Installed on a phone the app owns the whole window and
#: there is no chrome around it at all — every filter, every grouping and
#: every folder opened is a navigation, so the history is right there and
#: nothing could reach it. In a tab the browser already draws this button, and
#: a second one beside it is a second thing to wonder about.
_BACK: str = (
    '<svg viewBox="0 0 24 24" width="19" height="19" aria-hidden="true" '
    'fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M15 5.5 8.5 12l6.5 6.5"/></svg>')


#: The gear, for when there is no room to spell any of it out.
_GEAR: str = (
    '<svg viewBox="0 0 24 24" width="19" height="19" aria-hidden="true" '
    'fill="none" stroke="currentColor" stroke-width="1.7" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="3.1"/>'
    '<path d="M19.1 14.6a1.5 1.5 0 0 0 .3 1.7l.1.1a1.8 1.8 0 1 1-2.6 2.6l-.1-.1'
    'a1.5 1.5 0 0 0-1.7-.3 1.5 1.5 0 0 0-.9 1.4v.2a1.8 1.8 0 1 1-3.6 0v-.1'
    'a1.5 1.5 0 0 0-1-1.4 1.5 1.5 0 0 0-1.7.3l-.1.1a1.8 1.8 0 1 1-2.6-2.6l.1-.1'
    'a1.5 1.5 0 0 0 .3-1.7 1.5 1.5 0 0 0-1.4-.9h-.2a1.8 1.8 0 1 1 0-3.6h.1'
    'a1.5 1.5 0 0 0 1.4-1 1.5 1.5 0 0 0-.3-1.7l-.1-.1a1.8 1.8 0 1 1 2.6-2.6'
    'l.1.1a1.5 1.5 0 0 0 1.7.3h.1a1.5 1.5 0 0 0 .9-1.4v-.2a1.8 1.8 0 1 1 3.6 0'
    'v.1a1.5 1.5 0 0 0 .9 1.4 1.5 1.5 0 0 0 1.7-.3l.1-.1a1.8 1.8 0 1 1 2.6 2.6'
    'l-.1.1a1.5 1.5 0 0 0-.3 1.7v.1a1.5 1.5 0 0 0 1.4.9h.2a1.8 1.8 0 1 1 0 3.6'
    'h-.1a1.5 1.5 0 0 0-1.4.9z"/></svg>')


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

    **The name stays visible where there is room for it.** This app is used as
    two different people — the owner curating and the admin granting access
    — and acting as the wrong one is invisible until something is shared with
    the wrong household. On a phone the name gives way to a gear and moves to
    the first line inside the menu, which is one tap rather than none; that is
    the cost of the room, and it is paid where the room is scarce.

    **The dot rides on the trigger** either way. What is waiting has to be
    visible without opening anything, or it is not a notification.

    **No script.** `/history` and `/accounts` carry no page script at all, so
    this opens on hover and on keyboard focus with CSS alone. A settings menu
    that worked only where the grid was loaded would be a sign-out button
    missing from the page you are most likely to be stuck on.
    """
    if user is None:
        return (f'<span class="ver">v{_PIX_VERSION}</span>'
                f'<a class="who-link" href="/login">Sign in</a>')
    waiting = _binned() if user.is_admin else 0
    manage = ('<a href="/history">History</a>'
              '<a href="/accounts">Accounts</a>' if user.is_admin else "")
    # What is waiting, as a row of the menu rather than a bell of its own.
    activity = (bin_link_html(waiting) if user.is_admin else "")
    quiet = ('<span class="quiet">Nothing waiting</span>'
             if user.is_admin else "")
    return (
        f'<span class="me" id="me" tabindex="0" data-any="{"1" if waiting else ""}">'
        f'<span class="who-link name">{_h(user.name)}'
        f'<i class="caret">&#9662;</i></span>'
        f'<span class="who-link gearbtn" aria-label="Settings" '
        f'title="Settings">{_GEAR}</span>'
        f'<i class="dot"></i>'
        f'<span class="memenu">'
        f'<span class="whoami">{_h(user.name)}</span>'
        f'{extra}{activity}{quiet}{manage}'
        f'<span class="info">{info}'
        f'<span class="line ver">v{_PIX_VERSION}</span></span>'
        f'<form method="post" action="/logout">'
        f'<button>Sign out</button></form></span></span>')


def _stacks(stacks: str | None, user: Principal) -> str | None:
    """What this person's view does with stacks.

    **Folding is the off position, for everybody.** A suggestion is the app
    saying *these eight look like one photograph*, and reading them as one is
    what makes a thousand of them reviewable at all — so it is what the
    library does until somebody says otherwise, and `firm` is the way to say
    otherwise.

    It was not always. A household member was pinned to `firm` on the grounds
    that folding hides photographs on the app's own evidence and somebody who
    could not accept or refuse a guess should not have it done to them. They
    can accept and refuse now, which took the ground out from under it —
    and what was left was a filter whose cleared state and whose *No
    suggestions* value did the same thing, so it read as stuck rather than as
    careful. A control with an off position that is also one of its values is
    a control that appears broken, and was.

    Anything unrecognised reads as the default rather than as some fifth
    thing.
    """
    return stacks if stacks in ("only", "guesses", "firm") else None


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
    event: Annotated[str | None, Query()] = None,
    date: Annotated[str | None, Query()] = None,
    tag: Annotated[str | None, Query()] = None,
    person: Annotated[str | None, Query()] = None,
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
                      person=person,
                      audience=audience, chosen=_from_operation(op, stale),
                      within=within,
                      stacks=_stacks(stacks, user),
                      unfold="stack" in _groupings(group),
                      kind=kind, band=band, camera=camera, source=source,
                      viewer=user.scope,
                      deleted=_both_sides(deleted, op, user))


#: What the front door opens on: this year, by month and then by event.
#: Applied as a **redirect from a bare `/`** rather than as a default inside
#: the page, so that everything after it is in the URL where the rest of the
#: view already lives. A default applied invisibly could not be cleared —
#: taking the year off would put the year straight back on.
HOME_GROUPING: str = "month,event"


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
    # The same query against the other page: the corner is a zoom control, and
    # a zoom that dropped the filters would be a different library rather than
    # the same one seen closer.
    q = request.url.query
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
                 zoom="/browse" + (f"?{q}" if q else ""),
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
        return (_heading([], 0, len(rows), pick=False, cut=True)
                + "".join(_folder(r, groups, view, user, spread=spread)
                          for r in rows))
    out: list[str] = []
    for keys, run in groupby(rows, key=lambda r: tuple(
            r[f"grp{i}"] for i in range(len(outer)))):
        shelf = list(run)
        labels = [_group_label(k, g, outer[:i], shelf[0])
                  for i, (k, g) in enumerate(zip(keys, outer))]
        labels.append(dict(_GRID_GROUPS).get(inner, inner))
        out.append(_heading(labels, len(groups), len(shelf), pick=False,
                            cut=True))
        out.extend(_folder(r, groups, view, user, whole or {}, spread)
                   for r in shelf)
    return "".join(out)


#: Which filter each kind of chip narrows by, so a chip on a card can open
#: the folder already cut down to itself.
_SPREAD_FILTER: dict[str, str] = {
    "audience": "audience", "people": "person", "tags": "tag",
}


def _spread_html(kind: str, values: list[tuple[str, int]], n: int,
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
        # reading `undecided 50%` is the half of an event still to work
        # through, and clicking it is how you get to exactly those.
        where = (f' data-col="{col}" data-val="{_h(filter_on or value)}"'
                 if col else "")
        say = ("" if not col else
               f" — click for the {_h(value)} ones in here")
        return (f'<i class="{cls}"{where} '
                f'title="{_h(value)} — {count:,} of {n:,}{say}">'
                + _h(value) + "</i>")

    # `undecided` is not a value anything carries, it is the absence of one —
    # and the audience filter has a word for that, the same one its own chip
    # uses.
    first = ("" if lead is None or not lead[1] else
             chip(lead[0], lead[1], "none", ix.UNREVIEWED))
    chips = first + "".join(chip(v, c) for v, c in values)
    return f'<span class="spread {kind}">{chips}</span>'


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
        + "".join(
            _spread_html(kind, (spread or {}).get(kind, {}).get(
                tuple(row[f"grp{i}"] for i in range(len(groups))), []), n,
                lead=("undecided", left) if kind == "audience" else None)
            for kind in (("audience", "people", "tags") if user.is_admin
                         else ("people", "tags"))))
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


#: The derived tiers a thumbnail can be drawn from, smallest first, with what
#: each one is capped at. Sent to the page so that *which tier is big enough*
#: is arithmetic there rather than a second copy of these numbers — they are
#: `derive`'s to choose and have already changed once.
_TIERS: tuple[tuple[str, int], ...] = (
    ("/thumb/", paths.THUMB_PX),
    ("/large/", paths.LARGE_PX),
    ("/preview/", paths.PREVIEW_PX),
)

#: How many files one grid renders. Enough to hold the largest seeded event
#: (1,766) in a single page, because paging through a cull loses your place.
PAGE_LIMIT: int = 2000


@app.get("/event/{event}", response_class=HTMLResponse)
def event_grid(event: str) -> RedirectResponse:
    """Kept so older links still land somewhere — an event is just a filter now."""
    return RedirectResponse(f"/browse?event={_q(event)}", status_code=307)


@app.get("/browse", response_class=HTMLResponse)
def browse(request: Request,
           user: Annotated[Principal, Depends(require_user)],
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
  <a id="viewget" class="who-link" download>{_mark("get", 19)}</a>
  <aside id="rail"></aside>
</div>
<div id="menu" hidden></div>
{_WORKING}""",
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
        right=('<button id="sizepick" class="sizerow" '
               'aria-label="Thumbnail size"></button>'),
        rows=_actions(user),
        # Up a zoom: the same query, minus the stack. A folder view of one
        # stack is the stack, so the only thing the coarser view can say about
        # `within` is nothing — and leaving a stack is its own gesture, on the
        # bar, rather than a side effect of changing how you are reading.
        zoom="/" + (lambda q: f"?{q}" if q else "")(
            "&".join(x for x in request.url.query.split("&")
                     if x and not x.startswith("within="))),
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
        f"MARKS={_js({c: _mark(c) for c, _ in _chips(user)})},"
        # The word the audience filter uses for *nobody yet*. Sent rather than
        # spelled again here: the page draws a chip that sets it, and two
        # copies of a sentinel are two chances to disagree about what it is.
        f"UNREVIEWED={_js(ix.UNREVIEWED)},"
        f"USUAL={_js(store().usual)},PAGE={_js(page)},"
        f"TIERS={_js(_TIERS)},"
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


#: Every action, and the decision fields it writes.
#:
#: One table, because the buttons a page renders and the fields an endpoint
#: accepts are the same question and must not be able to disagree. Hiding a
#: control is not access control — anyone can post to `/api/decide` — so this
#: drives the refusal first and the bar second.
_ACT_WRITES: dict[str, tuple[str, ...]] = {
    "event": ("event",),
    "tags": ("tags",),
    "people": ("people",),
    "date": ("date_override",),
    "access": ("audience",),
    "stack": ("stacked_under",),
    "top": ("stacked_under",),
    "unstack": ("stacked_under",),
    "nostack": ("no_stack", "stacked_under"),
    "delete": ("deleted",),
    "restore": ("deleted",),
    # Takes a copy away and decides nothing, so it writes no field — and is
    # listed anyway, because a table of actions with one missing is a table
    # nobody can read as complete.
    "download": (),
    "purge": (),
}

#: What a household member gets.
#:
#: The family curates (§8) — *tagging and ranking are the whole point of the
#: app* — and the whole edit bar being an administrator's is the opposite of
#: that. What they do not get is the two that cannot be taken back by somebody
#: else noticing: **access**, which is the only control that can show a
#: photograph to a person who should not see it, and **purge**, which ends the
#: file. **Restore** is absent because `/history` is, and the deleted are not
#: in anybody else's view to find: deleting is theirs, undeleting is not.
HOUSEHOLD: frozenset[str] = frozenset(_ACT_WRITES) - {"access", "purge",
                                                      "restore"}

#: The decision fields a household member may write, derived rather than
#: listed — a second list is a second thing to forget.
_HOUSEHOLD_FIELDS: frozenset[str] = frozenset(
    f for act in HOUSEHOLD for f in _ACT_WRITES[act])


def may(user: Principal, act: str) -> bool:
    """Whether this person gets this action."""
    return user.is_admin or act in HOUSEHOLD


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
        crumbs = '<span class="sep">&rsaquo;</span>'.join(
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
        f'data-people="{_h(nl.join(_split(row["people"])))}" '
        f'data-date="{_h(str(row["effective_date"] or "no date"))}" '
        f'data-deleted="{"1" if row["deleted"] else ""}" '
        f'data-under="{_h(row["stacked_under"] or "")}" '
        f'data-ar="{_squareness(row)}" '
        f'data-copy="{"1" if _has_render(row) else ""}" '
        f'data-behind="{row["behind"] or 0}" '
        f'data-proposed="{_guessed(row, view or ix.Filters())}">'
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
    return (f'<a class="stack{" guessed" if guessed else ""}" '
            f'href="/browse?within={_q(key)}" '
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
            MASTER_DIR / str(row["folder"]) / str(row["name"]),
            RENDER_DIR).is_file()
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


def _view_dict(view: ix.Filters) -> dict[str, str | None]:
    return {name: getattr(view, name) for name in ix.Filters.NAMES}


def _chips(user: Principal) -> tuple[tuple[str, str], ...]:
    """The filters this person gets.

    Access is an administrator's control. Everyone else sees only what has
    been shared with them, so filtering by who else can see it offers a
    choice between their whole world and nothing.

    The bin is an administrator's too, and for a household member it is not
    merely hidden but empty by definition: the deleted are in nobody else's
    view to be found, which is what makes deleting safe to hand over and
    restoring not.

    **Stacks are everyone's**, because the stack actions are. A thousand
    suggestions is shared work, and *Not a stack* cannot be reached without
    the filter that makes a suggestion fold into one — see `_stacks`, which
    still leaves the default the safe way round for a household member.
    """
    return tuple((col, label) for col, label in _CHIPS
                 if col not in ("audience", "deleted")
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
    ("event", "Event"), ("tag", "Tag"), ("person", "People"),
    ("date", "Date"), ("audience", "Access"),
    ("kind", "Type"), ("band", "Size"), ("source", "Source"),
    ("camera", "Camera"),
    ("stacks", "Stacks"), ("deleted", "Deleted"),
)

#: One drawing per filter, so the bar can say which question a chip asks
#: without spending a word on it.
#:
#: **Drawn here rather than taken from a set.** An icon font is a second
#: typeface to load for ten glyphs, and none of the general-purpose sets has a
#: mark for *stacks of near-identical photographs* or for *which import this
#: came off* — so the two that matter most in this app would have been the two
#: approximated. These are the same twenty-four unit grid, the same 1.7 stroke
#: and the same round ends as the bell in the bar, which is what makes them
#: read as one family rather than as clip art.
#:
#: Each is chosen against its neighbours as much as for itself: the set has to
#: be told apart at seventeen pixels, so no two share a silhouette.
_MARKS: dict[str, str] = {
    # An occasion — planted somewhere and named. Not a calendar: that is the
    # date, and an event here is *which occasion*, not when.
    "event": '<path d="M6 21V3.6"/>'
             '<path d="M6 4.4h10.8l-2.6 3.6 2.6 3.6H6"/>',
    # The one shape nothing else uses, eyelet and all.
    "tag": '<path d="M3.6 11.9V5.3a1.7 1.7 0 0 1 1.7-1.7h6.6a1.7 1.7 0 0 1 '
           '1.2.5l7.1 7.1a1.7 1.7 0 0 1 0 2.4l-6.6 6.6a1.7 1.7 0 0 1-2.4 '
           '0l-7.1-7.1a1.7 1.7 0 0 1-.5-1.2z"/>'
           '<circle cx="8.1" cy="8.1" r="1.4"/>',
    "date": '<rect x="3.4" y="5" width="17.2" height="15.6" rx="2.2"/>'
            '<path d="M3.4 10h17.2"/><path d="M8 3.2v3.5"/>'
            '<path d="M16 3.2v3.5"/>',
    # Who is **in** the photograph. A head and shoulders, because that is
    # what a person is, and inside a frame because the question is who is in
    # *this* — which is also where a detected face will one day be drawn.
    "person": '<rect x="3.3" y="3.3" width="17.4" height="17.4" rx="3"/>'
              '<circle cx="12" cy="10" r="2.9"/>'
              '<path d="M6.9 19.4a5.6 5.6 0 0 1 10.2 0"/>',
    # Who may **see** it, which is the opposite question and had the person
    # shape until People needed it more. An eye: the thing this decides is
    # whether somebody can look, and nothing else in the set is round.
    "audience": '<path d="M2.2 12s3.6-6.4 9.8-6.4S21.8 12 21.8 12s-3.6 6.4-9.8 '
                '6.4S2.2 12 2.2 12z"/><circle cx="12" cy="12" r="2.9"/>',
    # Photographs, video, and whatever else — so, kinds of thing. A play
    # triangle would have named one of the three values rather than the
    # question.
    "kind": '<rect x="3.4" y="3.4" width="9.4" height="9.4" rx="1.8"/>'
            '<circle cx="15.8" cy="15.8" r="4.8"/>',
    # Its three values are small, medium and large, and this is that sentence
    # with no words in it.
    "band": '<path d="M5 19.8v-3.4"/><path d="M12 19.8v-7.6"/>'
            '<path d="M19 19.8v-11.6"/>',
    # Which import it came off. A card rather than a phone, because the camera
    # filter next to it is already a device and two devices side by side is
    # two silhouettes to tell apart at seventeen pixels.
    "source": '<path d="M6 3.5h8.6L19 7.9V20a1.6 1.6 0 0 1-1.6 1.6H6A1.6 1.6 '
              '0 0 1 4.4 20V5.1A1.6 1.6 0 0 1 6 3.5z"/>'
              '<path d="M8.4 3.6v3.2"/><path d="M11.4 3.6v3.2"/>'
              '<path d="M14.4 4.2v2.6"/>',
    "camera": '<path d="M3.5 8.6a1.8 1.8 0 0 1 1.8-1.8h2.5l1.5-2.3h5.4l1.5 '
              '2.3h2.5a1.8 1.8 0 0 1 1.8 1.8v9a1.8 1.8 0 0 1-1.8 '
              '1.8H5.3a1.8 1.8 0 0 1-1.8-1.8z"/>'
              '<circle cx="12" cy="13" r="3.5"/>',
    # A card with cards behind it — the same depth the stack badge on a
    # thumbnail is drawn with, so the filter and the thing it filters on look
    # like the same idea.
    "stacks": '<rect x="3.4" y="9.2" width="12.6" height="11.4" rx="2"/>'
              '<path d="M6.9 6.4h9.5a2 2 0 0 1 2 2v9.2"/>'
              '<path d="M10.4 3.6h8.2a2 2 0 0 1 2 2v8.4"/>',
    # The bin, because that is what every other part of the app calls it.
    "deleted": '<path d="M4 6.4h16"/>'
               '<path d="M6.6 6.4l.9 12a1.8 1.8 0 0 0 1.8 1.7h5.4a1.8 1.8 0 0 '
               '0 1.8-1.7l.9-12"/>'
               '<path d="M9.6 6.4V4.7a1.3 1.3 0 0 1 1.3-1.3h2.2a1.3 1.3 0 0 1 '
               '1.3 1.3v1.7"/>',
    # --- and the things you *do*, which are not filters ----------------
    # The edit bar asks the same questions in the same order as the filter bar
    # — that is deliberate, and it is why most of these reuse a drawing from
    # above rather than get one of their own. These five are the actions with
    # no question above them to borrow from.

    # Taking a copy away is the same gesture on every platform and has the
    # same mark everywhere: into something, downwards.
    "get": '<path d="M12 3.6v11"/><path d="M7.8 10.4 12 14.6l4.2-4.2"/>'
           '<path d="M4.4 16.2v2.4a1.8 1.8 0 0 0 1.8 1.8h11.6a1.8 1.8 0 0 0 '
           '1.8-1.8v-2.4"/>',
    # Which one of a stack speaks for the rest: raise this to the top, drawn
    # as an arrow meeting a ceiling it cannot go past.
    "top": '<path d="M4.6 3.9h14.8"/><path d="M12 20.4V8.4"/>'
           '<path d="M7.2 13.2 12 8.4l4.8 4.8"/>',
    # Cards side by side and not touching — the same two shapes the stack mark
    # overlaps, which is the whole of what the action does to them.
    "unstack": '<rect x="2.9" y="7.5" width="8.2" height="10.6" rx="1.8"/>'
               '<rect x="12.9" y="7.5" width="8.2" height="10.6" rx="1.8"/>',
    # Refusing the app's guess, so: a stack, struck through. Two cards rather
    # than the filter's three, because a slash across three is mush at
    # seventeen pixels.
    "nostack": '<rect x="3.2" y="9" width="11.6" height="11.6" rx="2"/>'
               '<path d="M7 6.2h9a2 2 0 0 1 2 2v9"/>'
               '<path d="M3.6 20.6 20.6 3.6"/>',
    # Back out of the bin. A circle turned the other way is *undo* everywhere.
    "restore": '<path d="M3.5 12a8.5 8.5 0 1 0 2.5-6"/>'
               '<path d="M3.4 4.3v5.4h5.4"/>',
    # The end of the file rather than a decision about it. The bin it shares
    # with Delete, and the cross that says this one is not coming back.
    "purge": '<path d="M4 6.4h16"/>'
             '<path d="M6.6 6.4l.9 12a1.8 1.8 0 0 0 1.8 1.7h5.4a1.8 1.8 0 0 0 '
             '1.8-1.7l.9-12"/>'
             '<path d="M9.6 6.4V4.7a1.3 1.3 0 0 1 1.3-1.3h2.2a1.3 1.3 0 0 1 '
             '1.3 1.3v1.7"/>'
             '<path d="M10.4 11.5 13.6 15.3"/><path d="M13.6 11.5 10.4 15.3"/>',
}

#: Which drawing each action wears.
#:
#: Most of them point back into the filter marks above, and that is the point:
#: *Event* the filter and *Event* the action are the same question asked twice,
#: once about what you are looking at and once about what it should become.
#: Two bars that read the same way — a control that changes its face between
#: them is a control you have to learn twice.
_ACT_MARKS: dict[str, str] = {
    "event": "event", "tags": "tag", "people": "person", "date": "date",
    "access": "audience",
    "stack": "stacks", "top": "top", "unstack": "unstack",
    "nostack": "nostack", "download": "get", "delete": "deleted",
    "restore": "restore", "purge": "purge",
}


def _act(act: str, word: str, cls: str = "", *,
         user: Principal | None = None) -> str:
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
    return (f'<button data-act="{act}"{kind} title="{name}" '
            f'aria-label="{name}">{_mark(_ACT_MARKS.get(act, ""))}'
            f'<span class="word">{word}</span></button>')


def _mark(name: str, size: int = 17) -> str:
    """One glyph, as the bar draws it.

    Same attributes as the bell beside it: `currentColor`, so a mark takes the
    colour of whatever state its chip is in and nothing has to be drawn twice.
    """
    body = _MARKS.get(name)
    if not body:
        return ""
    return (f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" '
            'aria-hidden="true" fill="none" stroke="currentColor" '
            'stroke-width="1.7" stroke-linecap="round" '
            f'stroke-linejoin="round">{body}</svg>')


#: Complete vocabularies — these columns cannot hold anything else.
_FIXED: dict[str, tuple[tuple[str, str], ...]] = {
    "kind": (("image", "Photos"), ("video", "Video"), ("other", "Other")),
    "band": (("small", "Small / short"), ("medium", "Medium"),
             ("large", "Large / long")),
    # Off is the third value and has no entry: clearing the chip is what says
    # *the living*, the same gesture as clearing any other filter.
    "deleted": (("only", "Only deleted"), ("with", "Including deleted")),
    # No entry for the ordinary view, the same as every other chip: *not
    # filtering on this* is what the cross says, and a value that only clears
    # the filter is a second way to say it — which is one more thing to read
    # in the list of the ones that do something. It was named while off meant
    # something of its own; folding is the default now, so it does not.
    "stacks": (("only", "Only stacks"), ("guesses", "Only suggested"),
               ("firm", "No suggestions")),
}

#: How the grid can be cut up, and what to call each choice.
_GRID_GROUPS: tuple[tuple[str, str], ...] = (
    # Day, month and year lead because grouping by time is how the library is
    # mostly read and `day` is the default — an ordering by use, which is
    # allowed to win. Everything after them follows the filter bar, because
    # there it is the same set of questions and there is no reason for it to
    # be a second arrangement to learn: `kind` sat after `camera` here and
    # before `source` there, for no reason anybody chose.
    ("day", "By day"), ("month", "By month"), ("year", "By year"),
    ("event", "By event"), ("kind", "By type"), ("source", "By source"),
    ("camera", "By camera"),
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

// --- what kind of thing is this being used on --------------------------------
// Asked once, of the browser rather than of its name. A user-agent string says
// what somebody wants you to believe; `pointer: coarse` says a finger is what
// will be aiming at these twenty pixels, which is the only thing any of this
// turns on.
//
// `typeof` because the page script is run under a DOM stub in the tests, where
// `matchMedia` does not exist — and a bare reference would throw on load and
// take every handler on the page with it, which is the exact failure the stub
// was written to catch.
function media(q){
  try{ return typeof matchMedia==='function'&&matchMedia(q).matches; }
  catch(e){ return false; }
}
const COARSE=media('(pointer: coarse)');
// Whether the file can be handed to the system rather than downloaded. On iOS
// this is the only route into Photos at all: a download — in Safari, and
// assuming it happens at all in an installed app — lands in Files and nowhere
// else. Feature-tested, so a desktop browser without it simply never takes
// this path.
const CAN_SHARE=(typeof navigator!=='undefined'
                 &&typeof navigator.canShare==='function'
                 &&typeof navigator.share==='function');

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
// How wide a cell is drawn, in the pixels the screen actually has. Measured
// once rather than per cell: every cell in the grid is the same width, and
// asking two thousand of them costs a layout each.
// Capped at two on a phone, and it is not a compromise about sharpness.
// A modern phone reports three, so a 174px cell asked for 520 real pixels,
// which no thumbnail has — and every cell in the grid came from `large` at a
// thousand pixels, or from `preview` at sixteen hundred at the other two
// sizes. The biggest derivative in the library, ten times the bytes it can
// show, over wifi or a VPN, on the most memory-constrained thing in the house.
// Two is past the point anyone can see on a cell this size and it is what the
// thumbnail tier was built to cover.
function cellPixels(){
  const c=cells.find(x=>!x.hidden)||cells[0];
  const w=c?c.getBoundingClientRect().width:0;
  const dpr=window.devicePixelRatio||1;
  return Math.round((w||150)*(media('(max-width: 720px)')?Math.min(dpr,2):dpr));
}

// The smallest tier that can fill it. Not a fixed tier per size: the same
// grid on a retina screen needs twice the pixels for the same inch of glass,
// and reading `large` there was asking a 563-pixel square to cover 830 — soft
// in exactly the way a photograph never is in the viewer.
// With a fifth to spare, not to the pixel. A source that only just covers the
// cell is being shown at very nearly 1:1, which on a video frame — already
// soft, already compressed once — looks nothing like the same cell filled
// from a photograph with half again as many pixels to give away. The margin
// is what makes the two look alike.
const SPARE=1.2;

function sourceFor(c,px){
  const ar=+(c.dataset.ar||1)||1;
  for(const [dir,cap] of TIERS) if(cap*ar>=px*SPARE) return dir;
  return TIERS[TIERS.length-1][0];
}

function useSource(c,px){
  const img=c.querySelector('img');
  if(!img) return;
  const have=img.getAttribute('src')||'';
  const at=have.indexOf('/',1);
  if(at<0) return;
  const want=sourceFor(c,px===undefined?cellPixels():px);
  if(!have.startsWith(want)) img.setAttribute('src',want+have.slice(at+1));
}

function drawSize(){
  if(grid) grid.dataset.size=thumbSize;
  if(sizePick){
    sizePick.textContent=LABEL[thumbSize];
    sizePick.title=SIZE_NAME[thumbSize]+' — click for the next size';
  }
  // After the grid has been told its new size, or every cell is measured at
  // the width it is about to stop being.
  const px=cellPixels();
  cells.forEach(c=>useSource(c,px));
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
// The glyph a filter is drawn with, and its name where it has no glyph. The
// fallback is not decoration: a filter added to `_CHIPS` without a drawing
// would otherwise be a button with nothing in it, which is invisible — so an
// undrawn filter falls back to being a word, the way all of them used to be.
const MARK=(typeof MARKS!=='undefined')?MARKS:{};
function markOf(col,label){return MARK[col]||esc(label);}

function drawChips(){
  chips.innerHTML='';
  // **One pass, in one order, whatever is set.** Every filter keeps the same
  // place in the bar whether it is doing something or not.
  //
  // They used to be drawn in two passes — the ones in use, then the rest — so
  // setting a filter made its glyph jump from ninth place to first, and
  // clearing it threw the glyph back again. Nothing on the bar could be
  // reached from memory: the camera was wherever the camera happened to be
  // that second, and the position you reached for belonged to whatever was
  // last switched on. Grouping the active ones at the left reads well in a
  // screenshot and is unusable under a thumb.
  //
  // They are still told apart at a glance — one is lit and carries a value,
  // the other is a dim glyph — which is the job colour is for. Position is
  // for finding things.
  const spare=CHIPS.filter(([col])=>!VIEW[col]);
  for(const [col,label] of CHIPS){
    const v=VIEW[col];
    const b=document.createElement('button');
    const up=v?wider(col,v):null;
    // The name is the drawing now. It stays in `title` for a pointer and in
    // `aria-label` for everything else — a glyph with no name anywhere is a
    // control only the person who drew it can read.
    b.title=label;
    if(v){
      b.className='chip on';
      b.setAttribute('aria-label',label+': '+labelFor(col,v));
      b.innerHTML=markOf(col,label)
                 +`<span class="val">${esc(labelFor(col,v))}</span>`
                 +`<span class="x" title="${up?'Up to '+esc(up):'Clear'}">`
                 +'&times;</span>';
    }else{
      // `spare` as well as `off`, because a narrow screen shows only the
      // filters that are doing something and puts the rest behind the `+`:
      // ten glyphs fit across a desktop bar and do not fit across a phone,
      // and which it is can change while the page is open by turning the
      // phone over. Hiding them leaves the rest exactly where they were.
      b.className='chip off spare';
      b.setAttribute('aria-label',label);
      b.innerHTML=markOf(col,label);
    }
    b.onclick=e=>{
      e.stopPropagation();
      if(v&&e.target.closest('.x')){location.href=url({[col]:up});return;}
      openMenu(b,{column:col,mode:'filter'});
    };
    chips.appendChild(b);
  }
  if(spare.length){
    const add=document.createElement('button');
    add.className='chip addchip';
    add.textContent='+';
    add.title='Add a filter';
    add.onclick=e=>{e.stopPropagation();filterMenu(add,spare);};
    chips.appendChild(add);
  }
  // An opened stack, said the way an operation is: not a chip, because a chip
  // is a value picked from a list and there is no list of stacks to pick from
  // — you arrive inside one by opening it. But it has to say where you are
  // and be dismissable for the same reason the chips are, because a filter
  // you cannot see is a library that looks smaller than it is. Until this,
  // the only way out of a stack was the browser's own back button.
  if(VIEW.within){
    const back=url({within:null});
    const s=document.createElement('span');
    s.className='chip on from-op';
    s.textContent='in a stack';
    const n=document.createElement('span');
    n.className='val';
    n.textContent=String(cells.length);
    s.appendChild(n);
    const out=document.createElement('a');
    out.className='x';
    out.href=back;
    out.title='Leave this stack';
    out.textContent='×';
    out.onclick=e=>{
      e.stopPropagation();
      // Back out the way you came in, when that is how you got here: the same
      // address reached afresh is the same photographs at the top of the
      // page, and the top of the page is not where you were standing. A path
      // match rather than a parsed URL because a referrer from somewhere else
      // costs nothing here — the link below is where it lands instead.
      if(document.referrer&&document.referrer.endsWith(back)){
        e.preventDefault(); history.back();
      }
    };
    s.appendChild(out);
    chips.appendChild(s);
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
    // Glyph beside name, which is where the glyphs are learnt: this list is
    // the only place in the app that says both at once.
    d.innerHTML=`<i class="mark">${MARK[col]||''}</i><span>${esc(label)}</span>`;
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
  const room=window.innerWidth;
  // Below 316px the old arithmetic went negative and hung the menu off the
  // left edge of the screen to discover it. On anything this narrow a 300px
  // menu is not standing beside something anyway, so it spans instead.
  if(room<=720){
    menu.style.left='8px';
    menu.style.width=(room-16)+'px';
  }else{
    menu.style.left=Math.max(8,Math.min(r.left,room-316))+'px';
    menu.style.width='';
  }
  menu.style.top=(r.bottom+window.scrollY+4)+'px';
  menu.hidden=false;
}

// One step wider, or nothing where there is no such step.
//
// A date is a prefix — `2026`, `2026-09`, `2026-09-15` — so it is the one
// filter that is a hierarchy rather than a value, and closing it should mean
// *out of September*, not *out of dates altogether*. Going all the way is then
// two more clicks, where the old behaviour had no way back to the year but
// retyping it.
function wider(col,v){
  if(col!=='date') return null;
  const at=String(v).lastIndexOf('-');
  return at<0?null:String(v).slice(0,at);
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
// Closing on a scroll means *you have moved on*. On a phone it meant the
// keyboard: focusing a field makes the browser scroll the document to bring
// it into view, so the menu shut the instant it became usable — every Event,
// Tags, Date and Access, on the device this library is mostly read on, opened
// and then disappeared. Nothing in the app was broken and none of it worked.
//
// So a scroll with the cursor still inside the menu is the browser moving the
// page, not the reader. Nothing else changes: the menu is positioned in the
// document and scrolls with it either way.
window.addEventListener('scroll',()=>{
  const at=(typeof document!=='undefined')&&document.activeElement;
  if(at&&!menu.hidden&&menu.contains(at)) return;
  closeMenu();
},{passive:true});

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
  // Not on a phone. There the keyboard is half the screen, and it would come
  // up over the list before anyone has decided whether they want to type —
  // when what is usually wanted is the third name down, already on screen.
  // Tapping the field is how you ask for it.
  if(!COARSE) setTimeout(()=>q.focus(),0);

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

  async function choose(value){
    closeMenu();
    if(ctx.mode==='filter'){location.href=url({[ctx.column]:value});return;}
    const cs=await acting();
    if(!cs) return;
    if(!cs.length){workClose();say('nothing selected');return;}
    await applyToSelection(ctx.as||ctx.column,value,true,cs);
    if(FOLDERS) redrawFolders();
  }

  // A checklist, not a list of commands — and a half-ticked box completes,
  // it does not clear.
  //
  // It used to clear, on the argument that taking access away is the safer
  // direction and should be the one that costs a single click. Two things
  // were wrong with that. Ticking `family` on a folder that is ninety per
  // cent `family` is not an ambiguous gesture: it says *all of it*, and
  // answering with *none of it* is the opposite of what was asked, on the
  // ninety per cent that were already right. And `event` beside it always
  // completed — one field going one way and the others the other, in one
  // menu, under one kind of box.
  //
  // So: none and some both add, all removes. The destructive direction is the
  // one you reach by ticking a box that is already full, which is the only
  // state where it reads as *undo this*.
  // The field each action edits. `event` holds one value where tags and
  // access hold many, but the question the menu asks is the same one —
  // *do these files say this?* — so it is one control either way.
  const FIELD=MULTI[ctx.as]?MULTI[ctx.as][0]:(ctx.as==='event'?'event':null);

  async function toggle(value,row){
    // On the landing page this is every file the chosen folders hold, read
    // once and kept: the tri-state has to be able to say *some of them*, and
    // there is no way to know that about a folder without asking.
    const cs=await acting();
    if(!cs) return;
    if(!cs.length){workClose();say('nothing selected');return;}
    const state=shareState(cs,FIELD,value);
    if(FIELD==='event'){
      // Ticking the event they already have clears it; anything else sets
      // it. One value, so there is nothing to add to.
      await applyToSelection('event',state==='all'?null:value,true,cs);
    }else{
      await applyToSelection(ctx.as,value,state!=='all',cs);
    }
    if(FOLDERS) redrawFolders();
    mark(row,shareState(cs,FIELD,value));
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
// The next photograph you can actually see, or -1 where there is none that
// way. While a stack is open the rest of the grid is still in `cells` — hidden
// rather than removed, because it comes back when you are done — and paging
// walked straight through it into files that were not on screen.
function nextShown(from,dir){
  const start=from<0?(dir>0?-1:cells.length):from;
  for(let i=start+dir;i>=0&&i<cells.length;i+=dir){
    if(!cells[i].hidden) return i;
  }
  return -1;
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
  // The landing page has a selection too, and it is not made of these.
  if(FOLDERS){drawFolderSel();return;}
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
  // Anything selected can be downloaded, deleted or not: what it is on the
  // disk does not depend on what has been decided about it.
  show('download', live.length + dead > 0);
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
//: How long a press has to be to mean *and everything back to the last one*.
//: Long enough not to catch a slow tap, short enough that nobody lets go
//: first — which is the whole range either way.
const HOLD_MS=450;

function wire(c){
  const pick=c.querySelector('.pick');
  // A press that has already done its work. The finger coming off it is still
  // a tap as far as the browser is concerned, and that tap would untick the
  // far end of the range the press just made.
  let held=null,done=false;
  pick.addEventListener('click',e=>{
    e.stopPropagation();
    if(done){done=false;return;}
    const n=cells.indexOf(c);
    if(n<0) return;
    // The circle is the deliberate gesture: it adds and removes without
    // throwing away what is already ticked.
    if(e.shiftKey&&anchor>=0) range(anchor,n); else {togglePick(n); anchor=n;}
    setCur(n,true); drawSel();
  });
  // Holding a circle is what shift-clicking one is on a keyboard: everything
  // from the last circle you touched to this one. A phone has no shift key,
  // and 61,846 files one circle at a time is not a job anybody finishes —
  // which made a range the difference between the library being cullable on a
  // phone and not.
  if(typeof pick.addEventListener==='function'){
    const stop=()=>{ if(held){clearTimeout(held);held=null;} };
    pick.addEventListener('touchstart',()=>{
      done=false;
      stop();
      held=setTimeout(()=>{
        held=null;
        const n=cells.indexOf(c);
        if(n<0) return;
        // With nothing touched yet there is no range to make, so it is an
        // ordinary tick — and it becomes the end to measure the next one from.
        if(anchor>=0&&anchor!==n) range(anchor,n);
        else {togglePick(n,true); anchor=n;}
        setCur(n,true); drawSel();
        done=true;
      },HOLD_MS);
    },{passive:true});
    // Moving is scrolling, and letting go early is an ordinary tap. Either
    // way this was not a hold.
    pick.addEventListener('touchmove',stop,{passive:true});
    pick.addEventListener('touchend',stop,{passive:true});
    pick.addEventListener('touchcancel',stop,{passive:true});
  }
  c.addEventListener('click',e=>{
    // A link inside the cell is somewhere to go, not a photograph to open.
    // The stack badge did both: the viewer opened over the grid and then the
    // page left for the stack underneath it, so coming back restored a page
    // with a photograph on it that nobody had asked to see.
    if(e.target.closest('a')) return;
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
  if(FOLDERS){
    if(pickedFolders.size) pickedFolders.clear();
    else tiles.forEach(t=>pickedFolders.add(t));
    expanded=null; expandedBy.clear();
    drawFolderSel();
    return;
  }
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
  drawGet(c);
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
// Narrow enough and there is no room for a photograph and a column of numbers
// at once, so details is a tab rather than a rail — and a tap on a photograph
// opens the photograph. The stored preference is about the column; it says
// nothing about which tab you want to land on.
const tabbed=()=>media('(max-width: 720px)');
if(tabbed()) railOn=false;
function drawRail(){
  viewer.classList.toggle('norail',!railOn);
  // A tab is named for where it goes; a rail is named for what it does.
  railToggle.textContent=tabbed()?(railOn?'Photo':'Details')
                                 :(railOn?'Hide details':'Details');
  // Not remembered while it is a tab: the preference belongs to the column,
  // and writing it here would mean turning the phone sideways once decided
  // how every desktop viewer opened from then on.
  if(!tabbed()){
    try{localStorage.setItem('pix2.rail',railOn?'1':'0');}catch(e){}
  }
}
if(railToggle) railToggle.onclick=e=>{
  e.stopPropagation();railOn=!railOn;drawRail();
                       if(railOn&&cells[cur]) fill(cells[cur]);};
const viewClose=document.getElementById('viewclose');
if(viewClose) viewClose.onclick=e=>{e.stopPropagation(); closeViewer();};
// Where you have decided you want this one. A link rather than a button, so
// the browser does the transfer and a right-click still offers *save as*.
const viewGet=document.getElementById('viewget');
function drawGet(c){
  if(!viewGet||!c) return;
  const at='/download/'+encodeURIComponent(c.dataset.folder)
          +'/'+encodeURIComponent(c.dataset.name);
  viewGet.setAttribute('href',at);
  // The original is a second thing to want only where it is a different file
  // — which is the clips a browser will not play as they are, and nothing
  // else in the library.
  const touch=COARSE&&CAN_SHARE;
  // The drawing says it; the name is for the pointer and the screen reader.
  viewGet.setAttribute('aria-label',
    touch?'Save':(c.dataset.copy?'Download copy':'Download'));
  viewGet.title=touch
    ? 'Save this to Photos, Files, or anywhere else.'
    : (c.dataset.copy
       ? 'The H.264 copy. Hold shift for the original off the camera.'
       : 'The file as it came off the camera.');
  viewGet.onclick=e=>{
    e.stopPropagation();
    if(touch){
      // **The playable copy, where there is one.** Everywhere else in the app
      // the original is the thing to want, and from the grid it still is — but
      // this button puts a file in Photos, and the whole reason a render
      // exists is that the original is something that will not play. Saving
      // the camera's HEVC here would put a clip in the photo library that the
      // photo library cannot show.
      e.preventDefault();
      saveFiles([c],false);
      return;
    }
    if(e.shiftKey&&c.dataset.copy) viewGet.setAttribute('href',at+'?original=1');
    else viewGet.setAttribute('href',at);
  };
}
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
  // Under a heading of their own rather than mixed in with the tags. They are
  // pills either way, and *Mum* sitting in a row with *beach* and *sunset*
  // reads as a keyword — which is the one thing a person is not.
  const folk=(d.people||[]).map(p=>`<span class="pill">${esc(p)}</span>`).join('');
  const all=Object.entries(d.exif||{})
    .map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');

  return `<div class="rail-h">Decisions</div>`
    + kv([['Status',d.tier||'undecided',d.tier?null:'was'],
          ...eventRow])
    + (tags?`<div style="margin-top:6px">${tags}</div>`
          :'<div class="dim" style="margin-top:4px">no tags</div>')
    + (folk?`<div class="rail-h">People</div><div>${folk}</div>`:'')
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
// A page restored from the back/forward cache comes back exactly as it left,
// an open viewer included. Leaving a grid with one open is ordinary, and
// arriving back at that grid to find a photograph over it reads as the app
// having opened something on its own. Belt to the braces above: that stops
// the viewer opening on the way out, this closes it whatever opened it.
// A page restored from the back/forward cache comes back exactly as it left it,
// and what it left open it should not be holding open. The takeover is the one
// that matters: leaving for the share sheet and coming back can strand it over
// the whole screen with nothing that dismisses it, because Stop only acts while
// a write is running and by then none is.
window.addEventListener('pageshow',e=>{
  if(!e.persisted) return;
  if(viewer) closeViewer();
  closeMenu();
  busy=false; stopping=false; workClose();
});

// Android has no `-webkit-touch-callout`, so the stylesheet rule that closes
// this on iOS closes nothing there: long-pressing a thumbnail opens Chrome's
// own image menu, and its *Save image* saves the four-hundred-pixel
// derivative — or, in the viewer, the sixteen-hundred-pixel preview. Somebody
// walks off believing they have the photograph either way, and Save is how you
// get the photograph.
//
// Refusing the menu is the only lever there is, and it is taken only where a
// finger is doing the pressing: *Save image as* on a right-click is an
// ordinary thing to want at a desk, and nothing about it is wrong there.
if(COARSE){
  const noMenu=e=>e.preventDefault();
  if(grid&&typeof grid.addEventListener==='function')
    grid.addEventListener('contextmenu',noMenu);
  if(stage&&typeof stage.addEventListener==='function')
    stage.addEventListener('contextmenu',noMenu);
}

// --- swiping between photographs ---------------------------------------------
// Left and right are the arrow keys' job, and a phone has no arrow keys — so
// without this the only way from one photograph to the next is to close the
// viewer, find the thumbnail after it, and open that.
if(stage&&typeof stage.addEventListener==='function'){
  let sx=0,sy=0,swiping=false;
  stage.addEventListener('touchstart',e=>{
    const t=e.touches&&e.touches.length===1&&e.touches[0];
    // A drag beginning at the very edge of the screen belongs to the system —
    // that is how you go back — and an app that takes it over is one you
    // cannot get out of.
    swiping=!!t&&t.clientX>28&&t.clientX<(window.innerWidth||0)-28;
    if(t){sx=t.clientX;sy=t.clientY;}
  },{passive:true});
  stage.addEventListener('touchend',e=>{
    if(!swiping) return;
    swiping=false;
    const t=e.changedTouches&&e.changedTouches[0];
    if(!t) return;
    const dx=t.clientX-sx,dy=t.clientY-sy;
    // Far enough across to have been meant, and more across than down: without
    // the second test every slightly crooked scroll of the details turns a
    // page. Down is deliberately not a gesture — it is the system's in an
    // installed app, and there is a close button.
    if(Math.abs(dx)<48||Math.abs(dx)<Math.abs(dy)*1.6) return;
    const to=nextShown(cur,dx<0?1:-1);
    if(to<0) return;
    // Paging is looking, not choosing — the same rule the arrow keys follow.
    setCur(to,true);
    drawSel();
  },{passive:true});
}

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

// --- the landing page selects folders ----------------------------------------
// One zoom out, the same gesture. A folder *is* a set of filters — its own
// link says which — so a decision about a folder is a decision about every
// file that link would open, and the way to make one is to ask for those files
// and then do exactly what the grid does.
//
// Which is why nothing below writes anything. It turns folders into the same
// shape a thumbnail has, and `applyToSelection` takes it from there: the
// cascade into stacks, the field the person is allowed to write, the chunking,
// the log and the revert are all the ones that were already there.
const FOLDERS = PAGE==='/';
// Which filter each kind of chip narrows by, kept in step with the server's
// own map by the test that renders a card and presses one.
const SPREAD_FILTER={audience:'audience', people:'person', tags:'tag'};
const tiles = FOLDERS && grid
  ? [...grid.querySelectorAll('.tile')].filter(t=>t.getAttribute('href')) : [];
const pickedFolders = new Set();
// Which files sit behind each chosen folder. Kept per folder rather than in
// one heap, because a card is redrawn from its own — a percentage of
// everything selected is a percentage of the wrong thing.
const expandedBy = new Map();
// The files behind the chosen folders, once anything has asked. Thrown away
// whenever the selection changes, because it is an answer about that
// selection and nothing else.
let expanded = null;

// A fetched row, wearing enough of a thumbnail to be one. `applyToSelection`
// reads `dataset` and paints what it just wrote back onto the cell; there is
// no cell here, so the paint lands on nothing and the write is unaffected.
function ghost(row){
  return {
    dataset:{folder:row.folder, name:row.name,
             tags:row.tags||'', audience:row.audience||'',
             people:row.people||'',
             event:row.event||'', deleted:row.deleted?'1':''},
    classList:{add(){}, remove(){}, toggle(){}, contains(){return false;}},
    querySelector(){return null;}, appendChild(){}, remove(){},
  };
}

// Every file the chosen folders hold. Paged, because a folder is not bounded
// by what fits on a screen the way a selection of thumbnails is — an event
// can be thousands — and the takeover says so while it reads.
const PAGE_SIZE = 2000;
async function readFolders(){
  if(expanded) return expanded;
  const chosen=[...pickedFolders];
  if(!chosen.length) return [];
  if(busy){say('still writing…');return null;}
  busy=true;
  workOpen(chosen.length===1?'Reading the folder'
                            :`Reading ${chosen.length} folders`, chosen.length);
  const out=[], seen=new Set();
  try{
    for(let i=0;i<chosen.length;i++){
      if(stopping) break;
      const from=out.length;
      const q=new URLSearchParams(
        (chosen[i].getAttribute('href').split('?')[1])||'');
      // How the library is cut up says nothing about which files are in it,
      // and asking for a grouping the file listing does not use would only
      // give the server something to ignore.
      q.delete('group');
      for(let offset=0;;){
        q.set('limit',String(PAGE_SIZE)); q.set('offset',String(offset));
        const r=await fetch('/api/files?'+q);
        if(!r.ok) throw new Error((await r.text()).slice(0,200));
        const rows=await r.json();
        for(const row of rows){
          const key=row.folder+'\\n'+row.name;
          // Two folders can only overlap if the grouping lets them, but a
          // file written twice in one gesture is written twice in the log.
          if(seen.has(key)) continue;
          seen.add(key); out.push(ghost(row));
        }
        if(rows.length<PAGE_SIZE) break;
        offset+=rows.length;
      }
      expandedBy.set(chosen[i],out.slice(from));
      workProgress(i+1,chosen.length,'folder','folders');
    }
  }catch(e){
    busy=false; workClose();
    say('could not read that folder: '+e.message,true);
    return null;
  }
  const halted=stopping;
  busy=false;
  if(halted){workClose();say('stopped — nothing was written');return null;}
  // **Left open on purpose.** Reading is the first half of one gesture and
  // the write is the second; closing here and opening again in `send` puts a
  // gap of at least the takeover's own delay between them, so the screen
  // blinks empty in the middle of a job that never stopped. Whoever asked
  // closes it if they turn out not to write — there are three of them and
  // they are all in this file.
  expanded=out;
  return out;
}

// What an action applies to, whichever page is asking.
async function acting(){
  return FOLDERS ? await readFolders() : targets();
}

// A card is redrawn from its own files, which is where the answer already is:
// `applyToSelection` writes what it wrote onto each of them, so counting is
// all that is left. Nothing is fetched and nothing reloads — the menu stays
// open and the card underneath it changes, which is the whole point of a
// checklist you tick more than once.
function redrawFolder(t){
  const gs=expandedBy.get(t);
  if(!gs||!gs.length) return;
  const n=gs.length;
  const kinds=ADMIN?['audience','people','tags']:['people','tags'];
  for(const kind of kinds){
    const counts=new Map();
    for(const g of gs)
      for(const v of valuesOf(g,kind)) counts.set(v,(counts.get(v)||0)+1);
    // Commonest first, so what the whole folder carries leads and the partial
    // ones follow — the same order the server draws them in.
    const sorted=[...counts].sort((a,b)=>b[1]-a[1]||(a[0]<b[0]?-1:1));
    const none=kind==='audience'
      ? gs.filter(g=>!g.dataset.audience).length : 0;
    let el=t.querySelector('.spread.'+kind);
    if(!sorted.length&&!none){ if(el) el.remove(); continue; }
    if(!el){
      el=document.createElement('span');
      el.className='spread '+kind;
      t.appendChild(el);
    }
    // The same chip the server draws, filter and all: without `data-col` a
    // redrawn card would look the same and do nothing when pressed.
    const col=SPREAD_FILTER[kind];
    const chip=(v,c,cls,on)=>`<i class="${cls||''}"`
      +` data-col="${col}" data-val="${esc(on||v)}"`
      +` title="${esc(v)} — ${c} of ${n} — click for the ${esc(v)} ones`
      +` in here">${esc(v)}</i>`;
    el.innerHTML=(none?chip('undecided',none,'none',UNREVIEWED):'')
                +sorted.map(([v,c])=>chip(v,c)).join('');
  }
  // The green inside the blue: how much of this card has been decided.
  const fill=t.querySelector('.bar i b');
  if(fill){
    const done=gs.filter(g=>!!g.dataset.audience).length;
    fill.style.width=Math.round(done*100/n)+'%';
  }
}
function redrawFolders(){ pickedFolders.forEach(redrawFolder); }

function drawFolderSel(){
  tiles.forEach(t=>t.classList.toggle('picked',pickedFolders.has(t)));
  if(!actions) return;
  const n=pickedFolders.size;
  if(!n&&menuCtx&&(menuCtx.mode==='set'||menuCtx.mode==='date')) closeMenu();
  for(const g of actions.querySelectorAll('.grp')) g.hidden=!n;
  actions.dataset.state = !n ? 'none' : n===tiles.length ? 'all' : 'some';
  if(selcount) selcount.textContent = `${n} selected`;
}

// A chip on a card opens the folder narrowed to itself: the card's own link
// plus the one filter the chip names. Inside that link, so like the select
// circle it has to say it is not it.
tiles.forEach(t=>{
  // One listener on the card rather than one per chip: a write redraws the
  // chips from the files it just changed, and handlers hung on the old ones
  // go into the bin with them — so the chips would open the folder until
  // the first edit and then stop, which is the kind of thing nobody reports
  // because it looks like they never worked.
  t.addEventListener('click',e=>{
    const chip=e.target&&e.target.closest&&e.target.closest('[data-col]');
    if(!chip) return;
    e.preventDefault(); e.stopPropagation();
    const [path,query]=(t.getAttribute('href')||'').split('?');
    const q=new URLSearchParams(query||'');
    q.set(chip.dataset.col,chip.dataset.val);
    location.href=path+'?'+q;
  });
  const pick=t.querySelector('.pick');
  if(!pick) return;
  pick.addEventListener('click',e=>{
    // Inside the link that opens the folder, so it has to say it is not that.
    e.preventDefault(); e.stopPropagation();
    if(pickedFolders.has(t)) pickedFolders.delete(t); else pickedFolders.add(t);
    expanded=null; expandedBy.clear();
    drawFolderSel();
  });
});
if(FOLDERS) drawFolderSel();

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
  // Only in the ordinary view. Where the view is *only stacks* or *only
  // suggested*, refusing takes the whole thing out of it — the server says so
  // and the grid drops it — so there is nothing to fan back out into.
  const fan=!VIEW.stacks
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

// --- downloading -------------------------------------------------------------
// Two things can be meant by *the file*: what came off the camera, and the
// H.264 copy the app made so a browser can play it. They differ only for the
// clips a browser will not play as they are — so the choice is offered only
// when the selection holds one, and the rest of the time pressing Download
// downloads.
// *Download* names one of the two places a phone can put a file, and not the
// one this app is for. The sheet the button opens offers Photos and Files
// both, so the word has to cover both.
if(COARSE&&CAN_SHARE&&actions){
  const b=actions.querySelector('[data-act="download"] .word')
        ||actions.querySelector('[data-act="download"]');
  if(b) b.textContent='Save';
}

async function downloadMenu(anchorEl){
  const cs=await acting();
  if(!cs) return;
  // Closed either way: a download is the browser's job from here, and on a
  // desktop it shows nothing of its own to take the takeover's place.
  workClose();
  if(!cs.length){say('nothing selected');return;}
  if(!cs.some(c=>c.dataset.copy)){closeMenu();getFiles(cs,false);return;}
  const key='download';
  if(menuCtx&&menuCtx.key===key&&!menu.hidden){closeMenu();return;}
  menu.innerHTML='<div id="menulist"></div>';
  const list=menu.querySelector('#menulist');
  const head=document.createElement('div');
  head.className='band';
  head.textContent='Download';
  list.appendChild(head);
  for(const [label,orig] of [['Playable copies',false],['Originals',true]]){
    const d=document.createElement('div');
    d.className='opt';
    d.innerHTML=`<span>${esc(label)}</span>`;
    d.onclick=e=>{e.stopPropagation();closeMenu();getFiles(cs,orig);};
    list.appendChild(d);
  }
  placeMenu(anchorEl);
  menuCtx={key};
}

function fileUrl(c,original){
  return '/download/'+encodeURIComponent(c.dataset.folder)
        +'/'+encodeURIComponent(c.dataset.name)+(original?'?original=1':'');
}

// What the system will call this, worked out here rather than read off the
// response. The fallback paths never see a header, and the share sheet will
// only offer *Save Image* for something it has been told is an image — so the
// one place that must agree about a file's type is the client.
const MIME={jpg:'image/jpeg',jpeg:'image/jpeg',png:'image/png',
            gif:'image/gif',webp:'image/webp',heic:'image/heic',
            heif:'image/heif',avif:'image/avif',tif:'image/tiff',
            tiff:'image/tiff',dng:'image/x-adobe-dng',
            mp4:'video/mp4',m4v:'video/x-m4v',mov:'video/quicktime',
            avi:'video/x-msvideo',mkv:'video/x-matroska',webm:'video/webm',
            mts:'video/mp2t',m2ts:'video/mp2t','3gp':'video/3gpp'};
function mimeOf(name){
  const at=String(name).lastIndexOf('.');
  return (at<0?'':MIME[name.slice(at+1).toLowerCase()])||'application/octet-stream';
}

// Sharing holds every byte in memory — the fetched blob, the File made from
// it, and the sheet's own copy of the same bytes, in a process the phone will
// kill around a gigabyte. So there is a ceiling, and going over it is not a
// failure: it is the ordinary download, said plainly.
const SHARE_MAX_FILES=10;
const SHARE_MAX_BYTES=150*1024*1024;

// One file is a link; a selection is a posted form. Not a fetch either way:
// the browser has to own the transfer, or every byte of a selection of video
// is held in this page's memory before a file appears anywhere.
//
// Except on a phone, where the browser owning it means the file goes to Files
// if it goes anywhere at all, and the library's whole point is the photographs
// being *in Photos*. There, the bytes come here and go out through the share
// sheet — see `saveFiles`.
function getFiles(cs,original){
  if(COARSE&&CAN_SHARE&&cs.length<=SHARE_MAX_FILES){
    saveFiles(cs,original);
    return;
  }
  downloadFiles(cs,original);
}

function downloadFiles(cs,original){
  if(cs.length===1){
    location.href=fileUrl(cs[0],original);
    say('downloading 1 file');
    return;
  }
  const form=document.createElement('form');
  form.method='post';
  form.action='/download.zip';
  const files=document.createElement('input');
  files.type='hidden'; files.name='files';
  files.value=JSON.stringify(cs.map(
    c=>({folder:c.dataset.folder,name:c.dataset.name})));
  form.appendChild(files);
  if(original){
    const flag=document.createElement('input');
    flag.type='hidden'; flag.name='original'; flag.value='1';
    form.appendChild(flag);
  }
  document.body.appendChild(form);
  form.submit();
  form.remove();
  say(`downloading ${cs.length.toLocaleString()} files as a zip`);
}

// Down here, then out through the share sheet — which on a phone is where
// *Save to Photos* lives, and the only place it lives.
//
// **Two taps, always.** The sheet may only be opened by a gesture, and the
// gesture that started this was spent somewhere inside the fetch: a browser
// keeps the permission alive across a promise for about a second, so sharing
// straight after the await works for a photograph on wifi and fails for a
// forty-megabyte clip on a VPN. That is one button behaving two ways depending
// on the file and the network, and the failure is silent. So the fetch is one
// press and the sheet is another, every time, and the second one is a button
// that is not there until the bytes are.
let ready=null;
function saveFiles(cs,original){
  if(busy) return;
  busy=true; ready=null; say('');
  workOpen(cs.length===1?'Fetching':'Fetching '+cs.length+' files',cs.length);
  if(workSave) workSave.hidden=true;
  const files=[];
  let bytes=0;
  (async()=>{
    for(let i=0;i<cs.length;i++){
      if(stopping) break;
      const c=cs[i];
      const r=await fetch(fileUrl(c,original),{credentials:'same-origin'});
      if(!r.ok) throw new Error(c.dataset.name+': '+r.status);
      // Read before buffering. The header is there on every one of these —
      // they are files on a disk — so a selection that is too big to hold can
      // be turned down without first holding it.
      bytes+=+(r.headers&&r.headers.get&&r.headers.get('content-length'))||0;
      if(bytes>SHARE_MAX_BYTES) throw new Error('too big');
      files.push(new File([await r.blob()],c.dataset.name,
                          {type:mimeOf(c.dataset.name)}));
      workProgress(i+1,cs.length);
    }
    if(stopping){busy=false;workClose();return;}
    // The sheet decides what it will take, and it is the authority: a `.insv`
    // or a raw file is something no phone has an opinion about.
    if(!navigator.canShare({files:files})) throw new Error('not shareable');
    ready=files;
    if(workWhat) workWhat.innerHTML=files.length===1?'Ready to save'
      :'Ready to save '+files.length+' files';
    if(workTally) workTally.textContent=
      'Photos, Files, or anywhere else you send it.';
    if(workSave){workSave.hidden=false;}
  })().catch(err=>{
    // Whatever the reason — too big to hold, a type the sheet will not take,
    // a fetch that failed — the ordinary download is still there. Saying where
    // the file is going to land instead is the difference between a fallback
    // and a button that did something else without mentioning it.
    busy=false; workClose();
    downloadFiles(cs,original);
    // After, not before: `downloadFiles` says *downloading 1 file*, which is
    // true and is not the part worth reading.
    say(err&&err.message==='too big'
        ? 'too large to hand to Photos — downloading to Files instead'
        : 'Photos will not take this one — downloading to Files instead',true);
  });
}

// The second press. Synchronous from the tap: nothing is awaited between the
// gesture and the sheet, which is the whole reason the fetch was a separate
// press.
function shareReady(){
  if(!ready) return;
  const files=ready;
  navigator.share({files:files}).then(()=>{
    say(files.length===1?'saved 1 file':`saved ${files.length} files`);
  },err=>{
    // Closing the sheet is an answer, not a fault.
    if(!err||err.name!=='AbortError') say('could not save: '+(err&&err.message),true);
  });
  ready=null; busy=false; workClose();
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
  // Nothing to do one zoom out: this tidies away *cells*, and a folder page
  // has none — so every test below is about an empty list, and the last of
  // them would have replaced the folders with *nothing matches these filters
  // any more*.
  //
  // Files leaving the view is the one thing a card cannot be redrawn through,
  // because the folder now holds a different set than the one that was read.
  // That is rare and it is real, so it is the one case that reloads.
  if(FOLDERS){ if(gone.length) location.reload(); return; }
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
            people:['people','add_people','remove_people'],
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
// Whether this change would change *this* file. The page already knows what
// each one says — the grid reads it off the cell, the folder page off the
// files it fetched — so a file that already carries the value being added is
// a file with nothing to do.
//
// Only where the answer is certain. A date override, a stacking or a refusal
// is not written on the cell, so those are always sent: guessing *no change*
// wrongly is a decision silently not made, which is far worse than a write
// that turns out to be a no-op.
function changes(c,act,value,add){
  const multi=MULTI[act];
  if(multi) return valuesOf(c,multi[0]).includes(value)!==!!add;
  if(act==='event') return (c.dataset.event||'')!==(value||'');
  if(act==='deleted') return !!c.dataset.deleted!==!!value;
  return true;
}

async function applyToSelection(act,value,add,only,batch){
  const cs=only||targetsOn(sideOf(act,value));
  if(!cs.length){say('nothing selected');return;}
  const multi=MULTI[act];
  if(multi&&value===null){say('pick a name');return;}
  // Sharing a folder where all but two files are already shared is two
  // writes, not eight hundred and sixty-six. Each one it skips is a sidecar
  // read, a sidecar rewrite and an index row it never has to touch — and a
  // line in the progress it never has to count.
  const todo=cs.filter(c=>changes(c,act,value,add));
  if(!todo.length){say('already set on all of them');return {done:0};}
  const body = multi ? {[add?multi[1]:multi[2]]:[value]} : {[act]:value};
  // Everything each cell said before, so the ones that never got written can
  // be put back. All four fields rather than the one being edited: it costs
  // nothing and means the restore cannot be wrong about which was in play.
  const before=todo.map(c=>({tags:c.dataset.tags||'',
                           audience:c.dataset.audience||'',
                           event:c.dataset.event||'',
                           deleted:c.dataset.deleted||''}));
  if(multi) todo.forEach(c=>paint(c,multi[0],value,add));
  else if(act==='event') todo.forEach(c=>{c.dataset.event=value||'';});
  // Under `Including deleted` a restored file stays on screen, so the cross
  // has to go the moment the decision does. Under `Only deleted` it leaves
  // instead, and `drop` takes the cell with it.
  else if(act==='deleted') todo.forEach(c=>{
    c.dataset.deleted=value?'1':'';
    c.classList.toggle('gone',!!value);
  });
  const out=await send(todo,body,actLabel(act,value,add),batch);
  // Only the tail. A write that stops half way — cancelled, or a share that
  // dropped — has really written the first part, and painting all of it back
  // would leave the screen denying what is on disk. The cells that were
  // written keep what they now say; the rest go back to what they said.
  const wrote=out?out.done:0;
  todo.slice(wrote).forEach((c,i)=>{
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
const workSave=document.getElementById('worksave');
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
function workProgress(done,total,one,many){
  if(workBar) workBar.style.width=(total?Math.round(done/total*100):0)+'%';
  if(workTally) workTally.textContent=
    `${done.toLocaleString()} of ${total.toLocaleString()} `+
    (total===1?(one||'file'):(many||'files'));
}
// The standing count of what is waiting in the bin. It is rendered with the
// page, so every delete, restore and purge has to say what it is now — a
// number that only refreshes on reload is worse than no number, because it
// looks current.
function drawBin(n){
  if(binEl===null||n===null||n===undefined) return;
  binEl.textContent=`${n.toLocaleString()} deleted`;
  binEl.hidden=!n;
  // The dot is the whole of what you see without asking, so it follows the
  // count rather than the page load: deleting something has to light it up.
  const bell=document.getElementById('activity');
  if(bell) bell.dataset.any=n?'1':'';
}

function workClose(){
  clearTimeout(workTimer); workTimer=null;
  if(working) working.classList.remove('on');
  // Whatever was fetched and never sent goes with it. Holding a hundred
  // megabytes of blobs against a Save button that is no longer on screen is
  // the kind of thing a phone notices.
  ready=null;
  if(workSave) workSave.hidden=true;
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
if(workSave) workSave.onclick=e=>{e.stopPropagation();shareReady();};

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
  const b=actions&&(actions.querySelector('[data-act="'+act+'"] .word')
                   ||actions.querySelector('[data-act="'+act+'"]'));
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
  if(viewer&&viewer.classList.contains('on')&&cells[cur]) fill(cells[cur]);
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
const ACT_COLUMN={tags:'tag', people:'person', access:'audience',
                  event:'event'};
(actions?[...actions.querySelectorAll('[data-act]')]:[]).forEach(b=>{
  const act=b.dataset.act;
  b.onclick=e=>{
    e.stopPropagation();
    if(act==='delete'){closeMenu();deleteSelection();return;}
    if(act==='stack'){closeMenu();stackSelection();return;}
    if(act==='top'){closeMenu();makeTop();return;}
    if(act==='unstack'){closeMenu();unstack();return;}
    if(act==='nostack'){closeMenu();notAStack();return;}
    if(act==='download'){downloadMenu(b);return;}
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
// Whether a photograph is open, on a page that may have nothing to open one
// in. Asked from the key handler, which is bound to the document and so runs
// on every page the script is served to.
function open_(){ return !!viewer&&viewer.classList.contains('on'); }

document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT') return;
  if(e.key==='Escape'){
    // Dismissal rather than navigation. A full-screen viewer with no key out
    // is a trap, even though clicking beside the picture also closes it.
    if(busy){stopWork();return;}
    if(choosing){endChoosing(true);return;}
    // No viewer on the landing page: it is folders, not photographs. Escape
    // there is still the way out of a menu, and reaching for a viewer that is
    // not on the page threw every time somebody pressed it.
    if(open_()) closeViewer();
    else if(!menu.hidden) closeMenu();
    return;
  }
  if(!open_()) return;
  const step=e.key==='ArrowRight'?1:e.key==='ArrowLeft'?-1:0;
  if(!step) return;
  e.preventDefault();
  const to=nextShown(cur,step);
  if(to<0) return;
  // Paging is looking, not choosing, so whatever is selected stays selected.
  setCur(to, true);
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
    if not original:
        render = paths.render_path(media, RENDER_DIR)
        if render.is_file():
            return render, Path(name).with_suffix(render.suffix).name
    return media, name


#: What a file is, by the only thing a URL knows about it.
#:
#: Not a guess the app acts on — it serves the same bytes either way. It is what
#: lets a phone put a photograph in Photos: iOS will only offer *Save Image* for
#: something it has been told is an image, and a file handed over as
#: `application/octet-stream` is a file the share sheet can only put in Files.
_MIME: dict[str, str] = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic",
    ".heif": "image/heif", ".avif": "image/avif", ".tif": "image/tiff",
    ".tiff": "image/tiff", ".dng": "image/x-adobe-dng",
    ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mov": "video/quicktime",
    ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
    ".webm": "video/webm", ".mts": "video/mp2t", ".m2ts": "video/mp2t",
    ".3gp": "video/3gpp",
}


def _mime(name: str) -> str:
    """What to call this kind of file, or nothing useful if we do not know.

    Octet-stream for anything unlisted — an `.insv` is not a type any phone has
    an opinion about, and claiming one would be worse than admitting it.
    """
    return _MIME.get(Path(name).suffix.lower(), "application/octet-stream")


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


#: How many meta records to fetch at once.
#:
#: Each is 5KB of JSON behind an 11ms round trip to the NAS, and the wait is
#: latency rather than bandwidth — so the cure is having several in flight,
#: not asking for less. Eight because the archive runs on a four-core Atom and
#: the point is to keep its network busy, not its processor.
META_READERS: int = 8


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
        return t.folder, t.name, ix.record_of(META_DIR, t.folder, t.name)

    out: dict[tuple[str, str], dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=META_READERS) as pool:
        for folder, name, record in pool.map(one, targets):
            if record is not None:
                out[(folder, name)] = record
    return out


#: How many files one zip will hold. Not a technical limit — the stream is
#: constant-memory whatever goes through it — but a selection can run to
#: thousands, and a download nobody meant to start is a download nobody can
#: stop without noticing it is running.
ZIP_LIMIT: int = 500


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
                      "band", "camera", "source"):
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
    conn = ix.open_rw(DB_PATH) if DB_PATH.is_file() else None
    try:
        view = ix.Filters(stacks=_stacks(None, user))
        named = Target(folder=body.folder, name=body.name)
        targets = _mine(user, conn, _behind(
            conn, view, [named],
            members=isinstance(change.stacked_under, Unset)))
        was, decision, indexed = _decide(body.folder, body.name, change,
                                         conn=conn)
        undo = [history.Before(body.folder, body.name, was,
                               did=_recorded(change))]
        for target in targets:
            if (target.folder, target.name) == (body.folder, body.name):
                continue
            try:
                before, _, _ = _decide(target.folder, target.name, change,
                                       conn=conn)
            except HTTPException:
                # One unwritable take does not cost the decision about the
                # rest, the same way a bulk edit reports a failure and carries
                # on.
                continue
            undo.append(history.Before(target.folder, target.name, before,
                                       did=_recorded(change)))
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
    people: list[str] | None = None
    add_people: list[str] = []
    remove_people: list[str] = []
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
    conn = ix.open_rw(DB_PATH) if DB_PATH.is_file() else None
    # **After the expansion, not before it.** `_behind` adds files the request
    # never named — the rest of a stack — so checking what was sent would let
    # a household member reach the others through it.
    targets = _mine(user, conn, _behind(
        conn, view, body.files,
        members=isinstance(change.stacked_under, Unset)))
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
                    was, _, was_indexed = _decide(
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
    people: Sequence[str] | None | Unset = decisions.UNSET
    add_people: Sequence[str] = field(default_factory=tuple)
    remove_people: Sequence[str] = field(default_factory=tuple)
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
                   people=got("people"),
                   add_people=tuple(body.add_people),
                   remove_people=tuple(body.remove_people),
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
    for name in ("event", "date_override", "tags", "people", "audience",
                 "deleted",
                 "stacked_under", "no_stack"):
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
            commit: bool = True
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
    media = _master_file(folder, name)
    with _write_lock:
        try:
            was, decision = decisions.change(
                media, event=change.event,
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
                no_stack=change.no_stack)
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
        own = conn is None and DB_PATH.is_file()
        if own:
            conn = ix.open_rw(DB_PATH)
        if conn is not None:
            try:
                # Passed rather than left to the index's own constants, so the
                # path the decision was written to and the path the row is
                # rebuilt from are the same one.
                # The decision is the one just written, so the refresh
                # does not go back to the share to read it again.
                indexed = ix.refresh(conn, folder, name,
                                     meta_dir=META_DIR, master_dir=MASTER_DIR,
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


# --- installing it on a phone -------------------------------------------------
#
# What separates an app from a bookmark is three files and a header: a manifest
# saying what to call it and which icon to use, a service worker so the browser
# will offer to install it at all, and icons that survive being cropped to
# whatever shape the launcher likes. None of it is authenticated — a manifest and
# an icon say nothing about the library, and a login wall in front of them would
# only mean the install prompt never appears.

#: Pre-rendered, and committed, because the container has no Pillow in it: the
#: app never decodes anything, which is what keeps it viable on the Atom.
#: `tools/make_icons.py` re-emits them from the same geometry as `_logo_mark`.
_ICONS: Path = Path(__file__).parent / "icons"

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
_SERVICE_WORKER: str = """
const SHELL = 'pix2-shell-v3';
const KEEP = ['/offline', '/manifest.webmanifest', '/icon-192.png',
              '/icon-512.png', '/icon-maskable-512.png',
              '/apple-touch-icon.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(KEEP))
                                .then(() => self.skipWaiting()));
});

// Old shells go on activation, so a worker update cannot leave a previous
// version's files answering for this one.
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(names => Promise.all(names.filter(n => n !== SHELL)
                                    .map(n => caches.delete(n))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  // A page is always fetched fresh: its script is inside it, and a cached page
  // is a cached build. Offline, say so plainly instead of showing a library
  // that may no longer be what is there.
  if (req.mode === 'navigate') {
    // `no-store` because fetching is not the same as fetching *fresh*: a
    // plain fetch reads the HTTP cache, which is where a stale page lives.
    // The server says the same thing in a header; this says it from the side
    // that claims to.
    e.respondWith(fetch(req, { cache: 'no-store' })
                  .catch(() => caches.match('/offline')));
    return;
  }
  // Everything else is either one of the shell files or a photograph, and the
  // browser's own cache already handles photographs perfectly well.
  if (KEEP.indexOf(new URL(req.url).pathname) >= 0) {
    e.respondWith(caches.match(req).then(hit => hit || fetch(req)));
  }
});
"""


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
_OFFLINE_JS: str = """
(function () {
  var head = document.getElementById('offhead');
  var say = document.getElementById('offsay');
  var go = document.getElementById('offgo');

  function show(title, text, retry) {
    head.textContent = title;
    say.textContent = text;
    go.hidden = !retry;
  }

  if (go) go.onclick = function () { location.reload(); };

  function look() {
    show('One moment', 'Finding out what happened.', false);
    // `no-store` because the answer to *can I reach home* must never come from
    // a cache that was filled at home.
    fetch('/healthz', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (state) {
        if (state && state.index === false) {
          // Reachable, and telling us something specific: the app and the
          // projection it reads disagree about their shape. Naming that is the
          // difference between a five-second fix and an afternoon.
          show('The app needs updating',
               state.says || 'This app and the archive are out of step.',
               true);
          return;
        }
        // It answered and it is well, so whatever failed has stopped failing.
        show('It is back', 'The library is reachable again.', true);
      })
      .catch(function () {
        if (navigator.onLine === false) {
          show('No network',
               'This device is not on a network at all. The library is at '
               + 'home and will be here when you are back on one.',
               true);
        } else {
          // The distinction that matters on a phone: connected to something,
          // but not to home. Naming the two ways back is more use than any
          // description of the failure.
          show('Not at home',
               'This device is on a network, but the library cannot be '
               + 'reached from it. It lives on the NAS at home — connect to '
               + 'that network, or to the VPN, and it will be here.',
               true);
        }
      });
  }

  // A phone that rejoins a network should not need to be told to try again.
  window.addEventListener('online', look);
  look();
})();
"""


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
    state: dict[str, Any] = {"ok": True, "index": DB_PATH.is_file()}
    if DB_PATH.is_file():
        try:
            ix.open_ro(DB_PATH).close()
        except ix.StaleIndex as stale:
            state["index"] = False
            state["says"] = stale.say()
        except sqlite3.Error as e:
            state["index"] = False
            state["says"] = f"{type(e).__name__}: {e}"
    return state


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
.gate .logo { display:flex; align-items:center; gap:11px; margin:0 0 22px; }
/* Wide, because three letters at this size need the air, and the accent falls
   on the one letter that is a shape rather than a stroke. */
.gate .logo .word { font-size:31px; font-weight:600; letter-spacing:.16em;
                    line-height:1; color:var(--fg); }
.gate .logo .word b { color:var(--accent); font-weight:600; }
.gate h2 { font-size:16px; margin:0 0 14px; }
.gate label { display:block; color:var(--dim); font-size:12px; margin:10px 0 3px; }
.gate input { width:100%; background:#14161a; color:var(--fg);
              border:1px solid var(--line); border-radius:4px; padding:7px 9px;
              font:inherit; }
.gate button { width:100%; margin:16px 0 0; padding:8px; }
/* Sixteen pixels or the phone zooms in on the field and does not zoom back,
   which is a sign-in form at 130% on the one page where getting it wrong
   means not getting in. Said here rather than with the rest of the coarse
   rules because this sheet is served *after* that one and `font:inherit`
   above would win — two rules of equal weight, and the last one counts. */
@media (pointer: coarse) { .gate input { font-size:16px; } }
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
