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
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Sequence, cast

from fastapi import Body, Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from pix.nas import auth
from pix.nas import decisions
from pix.nas import index as ix
from pix.nas.const import (
    INDEX_DB, MASTER_DIR, META_DIR, PREVIEW_DIR, RENDER_DIR, THUMB_DIR,
)
from pix.nas.decisions import Decision, Unset

#: Re-exported so the CLI and tests have one name for it.
DB_PATH: Path = INDEX_DB

app: FastAPI = FastAPI(title="pix2", docs_url=None, redoc_url=None)
_security = HTTPBasic(auto_error=False)

#: Serializes decision writes. Spec §8 makes last-write-wins the conflict policy
#: and leans on exactly this to keep it a *policy* question: two people tiering
#: the same photo pick a winner, they never interleave into a corrupt sidecar.
_write_lock: threading.Lock = threading.Lock()


# --- auth --------------------------------------------------------------------

def _users() -> dict[str, str]:
    """Configured credentials, or empty if none are set.

    Unset means **no auth at all**, which is only appropriate on a LAN with no
    reverse proxy in front. The landing page says so rather than leaving it a
    silent property of the deployment.
    """
    return auth.parse_users(os.environ.get("PIX2_USERS", ""))


def require_user(
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_security)],
) -> str:
    """Authenticate, unless no users are configured."""
    users = _users()
    if not users:
        return "anonymous"
    if credentials is None or credentials.username not in users:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "not authorised",
            headers={"WWW-Authenticate": "Basic"})
    if not auth.verify(users[credentials.username], credentials.password):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "not authorised",
            headers={"WWW-Authenticate": "Basic"})
    return credentials.username


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
         margin-left:auto; white-space:nowrap; }
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
#menu input { background:#14161a; color:var(--fg); border:0;
              border-bottom:1px solid var(--line); padding:9px 11px; font:inherit;
              border-radius:6px 6px 0 0; outline:none; width:100%; }
#menulist { overflow-y:auto; padding:4px 0; }
.opt { display:flex; gap:8px; padding:5px 11px; cursor:pointer;
       align-items:baseline; }
.opt:hover, .opt.cur { background:#2a3340; }
.opt .n { margin-left:auto; color:var(--dim); font-variant-numeric:tabular-nums;
          font-size:12px; }
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
.cell { position:relative; aspect-ratio:1; background:#0d0f12; overflow:hidden;
        border-radius:3px; cursor:pointer; }
.cell img { width:100%; height:100%; object-fit:cover; display:block; }
.cell.cur { outline:2px solid var(--accent); outline-offset:-2px; z-index:1; }
.cell.picked { outline:3px solid var(--accent); outline-offset:-3px; z-index:1; }
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
/* Tier is an inset ring so it can coexist with the selection outline — the two
   answer different questions and a cull needs both at once. */
.cell[data-tier="photo"] { box-shadow: inset 0 0 0 3px var(--keep); }
.cell[data-tier="top"]   { box-shadow: inset 0 0 0 3px var(--top); }
.cell[data-tier="none"]  { opacity:.3; }
.cell[data-tier="photo"]::after, .cell[data-tier="top"]::after,
.cell[data-tier="none"]::after {
  position:absolute; right:4px; top:4px; padding:1px 5px; border-radius:3px;
  font-size:11px; font-weight:600; color:#0d0f12; }
.cell[data-tier="photo"]::after { content:"keep"; background:var(--keep); }
.cell[data-tier="top"]::after   { content:"top";  background:var(--top); }
.cell[data-tier="none"]::after  { content:"out";  background:var(--dim); }
.tags { position:absolute; left:5px; bottom:4px; right:4px; font-size:10px;
        color:#fff; text-shadow:0 1px 3px #000; overflow:hidden;
        white-space:nowrap; text-overflow:ellipsis; }

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
#railtoggle:hover { opacity:1; }
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


def _page(title: str, body: str, *, bar: str = "") -> HTMLResponse:
    """One shell. `bar` is extra rows inside the sticky header."""
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{_STYLE}</style></head><body>
<div class="topbar"><div class="row"><a class="brand" href="/">pix2</a>{bar}</div>
</div><main>{body}</main></body></html>""")


@app.get("/", response_class=HTMLResponse)
def home(user: Annotated[str, Depends(require_user)]) -> HTMLResponse:
    """The library by year, then by event — the two ways anyone looks for a photo.

    Every row is a filter: clicking a year opens that year, clicking an event
    opens that year *and* event. So the landing page is a shortcut into `/browse`
    rather than a separate way of seeing things.
    """
    conn = db()
    s = ix.summary(conn)
    rows = ix.events(conn)

    open_note = ("" if _users() else
                 '<span class="dim">&middot; no auth configured</span>')
    head = (f'{s["files"]:,} files &middot; {s["unreviewed"]:,} undecided '
            f'&middot; {s["undated"]:,} undated '
            f'&middot; indexed {_age(ix.built_at(conn))} {open_note}')

    if not rows:
        return _page("pix2", '<p class="empty">Nothing indexed yet.</p>')

    years: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        years.setdefault(str(row["year"]), []).append(row)

    sections = "".join(_year_section(year, group)
                       for year, group in years.items())
    return _page("pix2", f"""<p class="dim">{head}</p>
<p><a href="/browse">Browse everything &rarr;</a></p>{sections}""")


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


def filters(
    event: Annotated[str | None, Query()] = None,
    year: Annotated[str | None, Query()] = None,
    tag: Annotated[str | None, Query()] = None,
    tier: Annotated[str | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
    band: Annotated[str | None, Query()] = None,
) -> ix.Filters:
    """The current view, read off the query string.

    In the URL rather than in the page's memory, so a view is a link: shareable,
    bookmarkable, and survivable across the reload that a bulk edit sometimes
    wants. It is also what makes the browser's back button mean "the filter I
    had before", which is the only undo a filter needs.
    """
    return ix.Filters(event=event, year=year, tag=tag, tier=tier,
                      kind=kind, band=band)


#: How many files one grid renders. Enough to hold the largest seeded event
#: (1,766) in a single page, because paging through a cull loses your place.
PAGE_LIMIT: int = 2000


@app.get("/event/{event}", response_class=HTMLResponse)
def event_grid(event: str) -> RedirectResponse:
    """Kept so older links still land somewhere — an event is just a filter now."""
    return RedirectResponse(f"/browse?event={_q(event)}", status_code=307)


@app.get("/browse", response_class=HTMLResponse)
def browse(user: Annotated[str, Depends(require_user)],
           view: Annotated[ix.Filters, Depends(filters)]) -> HTMLResponse:
    """The one grid, filtered — select files, then say something about them.

    Selecting an event on the landing page is just this page with `?event=`, so
    there is one surface to learn rather than a browser and a separate editor.
    """
    conn = db()
    rows = ix.files(conn, view, limit=PAGE_LIMIT)
    total = ix.count(conn, view)

    cells = "".join(_cell(r) for r in rows)
    shown = (f"{total:,} files" if total <= PAGE_LIMIT else
             f"{len(rows):,} of {total:,} files")
    body = (f'<div class="grid" id="grid">{cells}</div>'
            if rows else '<p class="empty">Nothing matches these filters.</p>')
    return _page("pix2 browse", f"""<p class="note" id="note" hidden></p>{body}
<div id="viewer">
  <div class="stage"><img id="vimg">
  <video id="vvid" controls playsinline></video>
  <div class="meta" id="vmeta"></div></div>
  <button id="railtoggle" title="Details (I)">Details</button>
  <aside id="rail"></aside>
</div>
<div id="menu" hidden></div>
<script>const VIEW={_js(_view_dict(view))},CHIPS={_js(_CHIPS)},FIXED={_js(_FIXED)};</script>
<script>{_BROWSE_JS}</script>""", bar=f"""
<div class="chips" id="chips"></div>
<span class="count" id="count">{shown}</span>
</div>
<div class="row" id="actions" hidden>
  <span class="count" id="selcount" style="margin:0"></span>
  <button data-act="event">Event&hellip;</button>
  <button data-act="tag">Add tag&hellip;</button>
  <button data-act="untag">Remove tag&hellip;</button>
  <button data-act="date">Date&hellip;</button>
  <span class="sep"></span>
  <button data-tier="photo">Keep</button>
  <button data-tier="top">Top</button>
  <button data-tier="none">Reject</button>
  <button data-tier="">Undo</button>
  <span class="sep"></span>
  <button id="selnone">Deselect</button>
</div>
<div class="row">
  <span class="hint"><b>click</b> a circle to select &middot;
  <b>shift</b> for a range &middot; <b>ctrl</b> to add &middot;
  <b>P</b> keep &middot; <b>T</b> top &middot; <b>X</b> reject &middot;
  <b>0</b> undo &middot; <b>Enter</b> view &middot; <b>I</b> details</span>
  <button id="selall" style="margin-left:auto">Select all</button>""")


def _cell(row: sqlite3.Row) -> str:
    tags = str(row["tags"] or "").split("\n") if row["tags"] else []
    return (
        f'<div class="cell" data-folder="{_h(row["folder"])}" '
        f'data-name="{_h(row["name"])}" data-kind="{_h(row["kind"])}" '
        f'data-tier="{_h(row["tier"] or "")}" '
        f'data-tags="{_h("".join(tags))}" '
        f'data-date="{_h(str(row["effective_date"] or "no date"))}">'
        f'<img loading="lazy" src="/thumb/{_q(row["folder"])}/{_q(row["name"])}">'
        f'<button class="pick" aria-label="select"></button>'
        + (f'<span class="badge">{_dur(row["duration"])}</span>'
           if row["kind"] == "video" else "")
        + (f'<span class="tags">{_h(" ".join(tags))}</span>' if tags else "")
        + "</div>"
    )


def _view_dict(view: ix.Filters) -> dict[str, str | None]:
    return {name: getattr(view, name) for name in ix.Filters.NAMES}


#: Labels for the filter chips and the fixed vocabularies. Kept server-side so
#: the tier and band words are defined once, next to the columns they describe.
_CHIPS: tuple[tuple[str, str], ...] = (
    ("event", "Event"), ("year", "Year"), ("tag", "Tag"),
    ("tier", "Status"), ("kind", "Type"), ("band", "Size"),
)

_FIXED: dict[str, tuple[tuple[str, str], ...]] = {
    "tier": (("new", "New — undecided"), ("photo", "Keep"), ("top", "Top"),
             ("none", "Rejected")),
    "kind": (("image", "Photos"), ("video", "Video"), ("other", "Other")),
    "band": (("small", "Small / short"), ("medium", "Medium"),
             ("large", "Large / long")),
}

_BROWSE_JS = """
const grid=document.getElementById('grid');
const menu=document.getElementById('menu');
const chips=document.getElementById('chips');
const actions=document.getElementById('actions');
const selcount=document.getElementById('selcount');
const countEl=document.getElementById('count');
const note=document.getElementById('note');
const viewer=document.getElementById('viewer');
const vimg=document.getElementById('vimg'), vvid=document.getElementById('vvid');
const vmeta=document.getElementById('vmeta');
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
  q.placeholder = ctx.mode==='set'
    ? 'Type a new name, or pick one below'
    : 'Filter…';
  q.oninput=()=>render(q.value);
  q.onkeydown=e=>{
    if(e.key==='Enter'&&ctx.mode==='set'&&q.value.trim()){
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
  }
  if(menuCtx!==ctx) return;   // a later menu opened while this was loading

  function choose(value){
    closeMenu();
    if(ctx.mode==='filter') location.href=url({[ctx.column]:value});
    else applyToSelection(ctx.column,value);
  }
  function render(text){
    const t=(text||'').toLowerCase();
    const hits=opts.filter(o=>o.label.toLowerCase().includes(t));
    const list=document.createElement('div');
    list.id='menulist';
    if(ctx.mode==='filter'&&VIEW[ctx.column]){
      list.appendChild(opt({label:'Any '+ctx.column,n:null},()=>choose(null)));
    }
    const typed=(text||'').trim();
    if(ctx.mode==='set'&&typed&&!opts.some(o=>o.label===typed)){
      const o=opt({label:'Add “'+typed+'”',n:null},()=>choose(typed));
      o.classList.add('new'); list.appendChild(o);
    }
    if(ctx.mode==='set'){
      list.appendChild(opt({label:'Clear',n:null},()=>choose(null)));
    }
    // Three bands, most relevant first: values already used by what you are
    // looking at, then by anything one filter away, then the rest.
    for(const [scope,title] of [['all','In this view'],['any','Related'],
                                ['other','Elsewhere']]){
      const band=hits.filter(o=>o.scope===scope);
      if(!band.length) continue;
      if(hits.some(o=>o.scope!==scope)){
        const h=document.createElement('div');
        h.className='band'; h.textContent=title; list.appendChild(h);
      }
      band.forEach(o=>list.appendChild(opt(o,()=>choose(o.value))));
    }
    if(!hits.length&&!typed){
      list.innerHTML='<div class="band">nothing yet</div>';
    }
    menu.querySelector('#menulist').replaceWith(list);
  }
  function opt(o,fn){
    const d=document.createElement('div');
    d.className='opt';
    d.innerHTML=`<span>${esc(o.label)}</span>`
               +(o.n!==null&&o.n!==undefined?`<span class="n">${o.n}</span>`:'');
    d.onclick=fn;
    return d;
  }
  render('');
}

function drawDate(){
  // Year alone is a complete answer — that is the whole point of a partial
  // date, so month and day stay optional rather than being required to submit.
  menu.innerHTML=`<div class="form">
    <label>Year <input id="dy" maxlength="4" placeholder="1987"></label>
    <label>Month <input id="dm" maxlength="2" placeholder="*"></label>
    <label>Day <input id="dd" maxlength="2" placeholder="*"></label>
    <button class="primary" id="dok">Apply</button>
    <button id="dclr">Clear</button>
    <div class="hint">Leave a box empty to keep what the file already says.</div>
  </div>`;
  const pad=(v,n)=>v.trim()?v.trim().padStart(n,'0'):'*';
  menu.querySelector('#dok').onclick=()=>{
    const y=pad(dy.value,4), m=pad(dm.value,2), d=pad(dd.value,2);
    if(y==='*'&&m==='*'&&d==='*'){closeMenu();return;}
    closeMenu();
    applyToSelection('date_override',`${y}-${m}-${d}-*:*:*`);
  };
  menu.querySelector('#dclr').onclick=()=>{
    closeMenu(); applyToSelection('date_override',null);
  };
  setTimeout(()=>menu.querySelector('#dy').focus(),0);
}

// --- selection ---------------------------------------------------------------
function setCur(n){
  if(!cells.length){cur=-1;return;}
  n=Math.max(0,Math.min(cells.length-1,n));
  cells.forEach(c=>c.classList.remove('cur'));
  cur=n; cells[cur].classList.add('cur');
  cells[cur].scrollIntoView({block:'nearest'});
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
function drawSel(){
  actions.hidden = picked.size===0;
  selcount.textContent = `${picked.size} selected`;
}
cells.forEach((c,n)=>{
  c.querySelector('.pick').addEventListener('click',e=>{
    e.stopPropagation();
    if(e.shiftKey&&anchor>=0) range(anchor,n); else {togglePick(n); anchor=n;}
    setCur(n); drawSel();
  });
  c.addEventListener('click',e=>{
    if(e.shiftKey&&anchor>=0){range(anchor,n);setCur(n);drawSel();return;}
    if(e.ctrlKey||e.metaKey){togglePick(n);anchor=n;setCur(n);drawSel();return;}
    setCur(n); openViewer();
  });
});
document.getElementById('selall').onclick=()=>{
  cells.forEach((_,n)=>togglePick(n,true)); drawSel();};
document.getElementById('selnone').onclick=clearPicks;

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
viewer.addEventListener('click',e=>{if(e.target===viewer)closeViewer();});
rail.addEventListener('click',e=>e.stopPropagation());

// --- writing -----------------------------------------------------------------
function say(text){note.textContent=text||''; note.hidden=!text;}

function targets(){
  return picked.size?[...picked]:(cells[cur]?[cells[cur]]:[]);
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
  // Land where the cursor was, not where it would have been pushed to.
  cur=-1; anchor=-1;
  if(cells.length) setCur(cells.includes(at)?cells.indexOf(at):Math.max(0,was));
  if(!cells.length&&grid) grid.innerHTML=
    '<p class="empty">Nothing matches these filters any more.</p>';
  drawSel();
}

// Optimistic: the cell changes now and the write follows, because a cull is a
// rhythm and waiting on SMB between keystrokes destroys it. A failure puts the
// old value back rather than leaving the screen claiming something untrue.
async function setTier(cs,tier){
  const prev=cs.map(c=>c.dataset.tier||'');
  cs.forEach(c=>c.dataset.tier=tier);
  const out=await send(cs,{tier:tier||null});
  if(out===null) cs.forEach((c,i)=>c.dataset.tier=prev[i]);
}

async function applyToSelection(column,value){
  const cs=targets();
  if(!cs.length){say('nothing selected');return;}
  const body = column==='tag' ? {add_tags:[value]}
             : column==='untag' ? {remove_tags:[value]}
             : {[column]:value};
  const out=await send(cs,body);
  if(out===null) return;
  if(column==='tag'||column==='untag'){
    cs.forEach(c=>{
      const t=new Set(c.dataset.tags?c.dataset.tags.split('\\n'):[]);
      column==='tag'?t.add(value):t.delete(value);
      c.dataset.tags=[...t].sort().join('\\n');
      let el=c.querySelector('.tags');
      if(!el&&t.size){el=document.createElement('span');el.className='tags';
                      c.appendChild(el);}
      if(el) el.textContent=[...t].sort().join(' ');
    });
  }
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
      busy=false; say(`stopped after ${done} of ${cs.length}: ${e.message}`);
      return null;
    }
    done+=batch.length;
    if(cs.length>CHUNK) say(`writing… ${done} of ${cs.length}`);
  }
  busy=false;
  cs.forEach(c=>details.delete(c.dataset.folder+'\\n'+c.dataset.name));
  if(viewer.classList.contains('on')&&cells[cur]) fill(cells[cur]);
  drop(gone);
  if(total!==null&&countEl) countEl.textContent=`${total.toLocaleString()} files`;
  if(failed) say(`${failed} file(s) could not be written`);
  else if(gone.length) say(`${gone.length} file(s) no longer match — removed`);
  else say('');
  return true;
}

actions.querySelectorAll('[data-act]').forEach(b=>{
  b.onclick=e=>{e.stopPropagation();openMenu(b, b.dataset.act==='date'
    ? {mode:'date'}
    : {column:b.dataset.act==='untag'?'tag':b.dataset.act,
       mode:'set', as:b.dataset.act});};
});
actions.querySelectorAll('[data-tier]').forEach(b=>{
  b.onclick=()=>{const cs=targets(); if(cs.length) setTier(cs,b.dataset.tier);};
});

// --- keyboard ----------------------------------------------------------------
const KEYS={p:'photo',t:'top',x:'none','0':''};
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
  const k=e.key.toLowerCase();
  if(k in KEYS&&!e.ctrlKey&&!e.metaKey){
    e.preventDefault();
    if(cur<0&&!picked.size) setCur(0);
    const cs=targets();
    if(!cs.length) return;
    setTier(cs,KEYS[k]);
    // Advance only when working one at a time: with a selection the gesture is
    // deliberate and moving the cursor underneath it would be noise.
    if(!picked.size&&cur<cells.length-1) setCur(cur+1);
    return;
  }
  const cols=Math.max(1,Math.round(grid?grid.clientWidth/156:1));
  const step={ArrowRight:1,ArrowLeft:-1,ArrowDown:cols,ArrowUp:-cols}[e.key];
  if(step===undefined) return;
  e.preventDefault();
  const next=(cur<0?0:cur)+step;
  if(e.shiftKey&&cur>=0){range(cur,Math.max(0,Math.min(cells.length-1,next)));
                         drawSel();}
  setCur(next);
});

drawChips(); drawSel();
"""


# --- media -------------------------------------------------------------------

@app.get("/thumb/{folder}/{name}")
def thumb(folder: str, name: str,
          user: Annotated[str, Depends(require_user)]) -> FileResponse:
    return _serve(THUMB_DIR, folder, name)


@app.get("/preview/{folder}/{name}")
def preview(folder: str, name: str,
            user: Annotated[str, Depends(require_user)]) -> FileResponse:
    return _serve(PREVIEW_DIR, folder, name)


@app.get("/media/{folder}/{name}")
def media(folder: str, name: str,
          user: Annotated[str, Depends(require_user)]) -> FileResponse:
    """Stream the master file itself, for video playback.

    The **only** endpoint that touches master, and strictly read-only — the
    archive is served, never modified.

    Serves the **render** when one exists and master otherwise. For an HEVC
    master the render is the only copy a browser can play — 421 of the seeded
    year's 724 clips — and where both exist they are the same footage.
    """
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
def api_files(user: Annotated[str, Depends(require_user)],
              view: Annotated[ix.Filters, Depends(filters)],
              limit: Annotated[int, Query(le=2000)] = 500,
              offset: Annotated[int, Query(ge=0)] = 0) -> JSONResponse:
    rows = ix.files(db(), view, limit=limit, offset=offset)
    return JSONResponse([dict(r) for r in rows])


@app.get("/api/suggest")
def api_suggest(user: Annotated[str, Depends(require_user)],
                view: Annotated[ix.Filters, Depends(filters)],
                column: Annotated[str, Query()]) -> JSONResponse:
    """Existing values for a column, most relevant to the current view first.

    A library ends up with hundreds of events and tags, and an alphabetical
    list of all of them buries the handful that apply to what is on screen.
    Ranking by how much of the current view already uses a value puts the
    likely answer in the first few rows — see `index.suggest`.
    """
    if column not in ("event", "tag", "year", "kind", "band"):
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
    ("date_override", "DateOverride"), ("tier", "Tier"),
)


@app.get("/api/file/{folder}/{name}")
def api_file(folder: str, name: str,
             user: Annotated[str, Depends(require_user)]) -> JSONResponse:
    """Everything known about one file, with fact and judgement kept apart.

    The rail's whole job is that separation. `capture_date` is what the camera
    wrote and can never change; `date_override` is what a person decided;
    `effective_date` is the composition of the two. Collapsing them into one
    "date" would hide the only interesting question — whether this is what the
    file says or what somebody chose.
    """
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
        "tier": row["tier"],
        "tags": str(row["tags"] or "").split("\n") if row["tags"] else [],
        "has_sidecar": bool(row["has_sidecar"]),
        "decided": ({"tier": decision.tier, "event": decision.event,
                     "date_override": decision.date_override,
                     "tags": list(decision.tags)} if decision else None),
        "inherited": inherited,
        "facts": facts,
        "exif": {k: str(v) for k, v in sorted(exif.items())},
        "has_render": (RENDER_DIR / folder / (name + ".mp4")).is_file(),
    })


def _under(root: Path, target: Path) -> bool:
    """Whether `target` really sits inside `root`, after resolving `..`."""
    return root.resolve() in target.resolve().parents


@app.get("/api/events")
def api_events(user: Annotated[str, Depends(require_user)]) -> JSONResponse:
    return JSONResponse([dict(r) for r in ix.events(db())])


class DecideBody(BaseModel):
    """A curation decision about one master file.

    Every field is optional *and* nullable, and the two mean different things:
    omitting `event` leaves it alone, sending `null` clears it. Without that
    distinction a one-field UI gesture — tier this photo — would silently erase
    whatever else had been decided about it, so the wire format has to carry it.
    """

    folder: str
    name: str
    tier: str | None = None
    event: str | None = None
    date_override: str | None = None
    tags: list[str] | None = None
    add_tags: list[str] = []
    remove_tags: list[str] = []


@app.post("/api/decide")
def api_decide(user: Annotated[str, Depends(require_user)],
               body: Annotated[DecideBody, Body()]) -> JSONResponse:
    """Write a decision to master, then bring its index row up to date.

    **Sidecar first, index follows** (§4). If the sidecar write fails nothing
    happened; if the index update fails the decision still stands and a
    `pix2 index` catches up — drift is only ever "the index is behind", never
    "the record is wrong". `indexed` in the response says which happened.
    """
    decision, indexed = _decide(body.folder, body.name, _change(body))
    return JSONResponse({
        "folder": body.folder,
        "name": body.name,
        "tier": decision.tier,
        "event": decision.event,
        "date_override": decision.date_override,
        "tags": list(decision.tags),
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
    tier: str | None = None
    event: str | None = None
    date_override: str | None = None
    tags: list[str] | None = None
    add_tags: list[str] = []
    remove_tags: list[str] = []


#: Bounds one request rather than the whole gesture. Finishing a 1,766-file
#: event is chunked by the client, which keeps each request short enough not to
#: hold the single worker and gives a progress reading for free.
BULK_LIMIT: int = 500


@app.post("/api/decide/bulk")
def api_decide_bulk(user: Annotated[str, Depends(require_user)],
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
    dropped: list[dict[str, str]] = []
    total: int | None = None
    try:
        for target in body.files:
            try:
                _, was_indexed = _decide(target.folder, target.name, change,
                                         conn=conn)
            except HTTPException as e:
                failed.append({"folder": target.folder, "name": target.name,
                               "error": str(e.detail)})
                continue
            written += 1
            indexed += 1 if was_indexed else 0
            done.append((target.folder, target.name))
        if conn is not None and done:
            stays = ix.matching(conn, view, done)
            dropped = [{"folder": f, "name": n}
                       for f, n in done if (f, n) not in stays]
            total = ix.count(conn, view)
    finally:
        if conn is not None:
            conn.close()

    return JSONResponse({"written": written, "indexed": indexed,
                         "failed": failed, "dropped": dropped,
                         "total": total})


# --- the write path ----------------------------------------------------------

@dataclass(frozen=True)
class _Change:
    """One decision edit, with "leave it alone" distinct from "clear it"."""

    tier: str | None | Unset = decisions.UNSET
    event: str | None | Unset = decisions.UNSET
    date_override: str | None | Unset = decisions.UNSET
    tags: Sequence[str] | None | Unset = decisions.UNSET
    add_tags: Sequence[str] = field(default_factory=tuple)
    remove_tags: Sequence[str] = field(default_factory=tuple)


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
    return _Change(tier=got("tier"), event=got("event"),
                   date_override=got("date_override"), tags=got("tags"),
                   add_tags=tuple(body.add_tags),
                   remove_tags=tuple(body.remove_tags))


def _decide(folder: str, name: str, change: _Change,
            *, conn: sqlite3.Connection | None = None) -> tuple[Decision, bool]:
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
            decision = decisions.apply(
                media, tier=change.tier, event=change.event,
                date_override=change.date_override, tags=change.tags,
                add_tags=change.add_tags, remove_tags=change.remove_tags)
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
    return decision, indexed


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
    from urllib.parse import quote

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
