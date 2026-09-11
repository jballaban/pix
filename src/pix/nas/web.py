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

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from pix.nas import auth
from pix.nas import decisions
from pix.nas import index as ix
from pix.nas.const import (
    INDEX_DB, MASTER_DIR, META_DIR, PREVIEW_DIR, RENDER_DIR, THUMB_DIR,
)

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
        --line:#272b33; --accent:#6aa3ff; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5
       system-ui,-apple-system,Segoe UI,sans-serif; }
header { padding:14px 20px; border-bottom:1px solid var(--line);
         display:flex; gap:18px; align-items:baseline; flex-wrap:wrap; }
h1 { font-size:15px; margin:0; letter-spacing:.02em; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
.dim { color:var(--dim); }
main { padding:20px; }
table { border-collapse:collapse; width:100%; max-width:900px; }
th,td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); }
th { color:var(--dim); font-weight:500; font-size:12px;
     text-transform:uppercase; letter-spacing:.06em; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
.grid { display:grid; gap:6px;
        grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); }
.cell { position:relative; aspect-ratio:1; background:#0d0f12; overflow:hidden;
        border-radius:3px; cursor:pointer; }
.cell img { width:100%; height:100%; object-fit:cover; display:block; }
.cell.sel { outline:2px solid var(--accent); outline-offset:-2px; }
.badge { position:absolute; right:4px; bottom:4px; background:#000a;
         padding:1px 5px; border-radius:3px; font-size:11px; }
#viewer { position:fixed; inset:0; background:#000e; display:none;
          align-items:center; justify-content:center; flex-direction:column; }
#viewer.on { display:flex; }
#viewer img, #viewer video { max-width:94vw; max-height:86vh;
                             object-fit:contain; display:none; }
#viewer img.on, #viewer video.on { display:block; }
#viewer .meta { padding:10px; color:var(--dim); font-size:12px; }
.empty { color:var(--dim); padding:40px 0; }
"""


def _page(title: str, body: str, *, crumb: str = "") -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{_STYLE}</style></head><body>
<header><h1><a href="/">pix2</a></h1><span class="dim">{crumb}</span></header>
<main>{body}</main></body></html>""")


@app.get("/", response_class=HTMLResponse)
def home(user: Annotated[str, Depends(require_user)]) -> HTMLResponse:
    """Events, largest first — because that is where the work is."""
    conn = db()
    s = ix.summary(conn)
    rows = ix.events(conn)

    open_note = ("" if _users() else
                 '<span class="dim">&middot; no auth configured</span>')
    head = (f'{s["files"]:,} files &middot; {s["events"]} events &middot; '
            f'{s["unreviewed"]:,} unreviewed &middot; {s["undated"]:,} undated '
            f'&middot; indexed {_age(ix.built_at(conn))} {open_note}')

    if not rows:
        return _page("pix2", '<p class="empty">Nothing indexed yet.</p>')

    cells = "".join(
        f'<tr><td><a href="/event/{_q(r["event"])}">{_h(r["event"])}</a></td>'
        f'<td class="num">{r["n"]:,}</td>'
        f'<td class="num dim">{r["unreviewed"]:,}</td>'
        f'<td class="dim">{_h(str(r["first_seen"] or "")[:10])}</td>'
        f'<td class="dim">{_h(str(r["last_seen"] or "")[:10])}</td></tr>'
        for r in rows
    )
    return _page("pix2", f"""<p class="dim">{head}</p>
<table><thead><tr><th>Event</th><th class="num">Files</th>
<th class="num">Unreviewed</th><th>First</th><th>Last</th></tr></thead>
<tbody>{cells}</tbody></table>""")


@app.get("/event/{event}", response_class=HTMLResponse)
def event_grid(event: str,
               user: Annotated[str, Depends(require_user)]) -> HTMLResponse:
    """A grid for one event. Thumbnails only — previews load on demand."""
    conn = db()
    rows = ix.files(conn, event=event, limit=2000)
    if not rows:
        return _page(event, '<p class="empty">No files.</p>', crumb=_h(event))

    cells = "".join(
        f'<div class="cell" data-folder="{_h(r["folder"])}" '
        f'data-name="{_h(r["name"])}" data-kind="{_h(r["kind"])}" '
        f'data-date="{_h(str(r["capture_date"] or "no date"))}">'
        f'<img loading="lazy" src="/thumb/{_q(r["folder"])}/{_q(r["name"])}">'
        + (f'<span class="badge">{_dur(r["duration"])}</span>'
           if r["kind"] == "video" else "")
        + "</div>"
        for r in rows
    )
    return _page(event, f"""
<p class="dim">{len(rows):,} files &middot; arrow keys to move, Esc to close</p>
<div class="grid" id="grid">{cells}</div>
<div id="viewer"><img id="vimg"><video id="vvid" controls playsinline></video>
<div class="meta" id="vmeta"></div></div>
<script>{_GRID_JS}</script>""", crumb=_h(event))


_GRID_JS = """
const cells=[...document.querySelectorAll('.cell')];
const viewer=document.getElementById('viewer');
const vimg=document.getElementById('vimg'), vvid=document.getElementById('vvid');
const vmeta=document.getElementById('vmeta');
let i=-1;
function show(n){
  if(n<0||n>=cells.length) return;
  cells[i]?.classList.remove('sel');
  i=n; const c=cells[i];
  c.classList.add('sel');
  c.scrollIntoView({block:'nearest'});
  if(!viewer.classList.contains('on')) return;
  const f=encodeURIComponent(c.dataset.folder), n2=encodeURIComponent(c.dataset.name);
  // Always stop the previous clip: moving on while audio keeps playing from the
  // one before is the kind of thing that makes a viewer feel broken.
  vvid.pause(); vvid.removeAttribute('src'); vvid.load();
  if(c.dataset.kind==='video'){
    vimg.classList.remove('on'); vvid.classList.add('on');
    vvid.src=`/media/${f}/${n2}`; vvid.play().catch(()=>{});
  }else{
    vvid.classList.remove('on'); vimg.classList.add('on');
    vimg.src=`/preview/${f}/${n2}`;
  }
  vmeta.textContent=`${c.dataset.name} — ${c.dataset.date}`;
}
cells.forEach((c,n)=>c.addEventListener('click',()=>{show(n);open_();}));
function open_(){viewer.classList.add('on');show(i<0?0:i);}
function close_(){viewer.classList.remove('on');vvid.pause();}
document.addEventListener('keydown',e=>{
  const cols=Math.max(1,Math.round(document.getElementById('grid').clientWidth/156));
  if(e.key==='Escape'){close_();return;}
  if(e.key==='Enter'){viewer.classList.contains('on')?close_():open_();return;}
  const step={ArrowRight:1,ArrowLeft:-1,ArrowDown:cols,ArrowUp:-cols}[e.key];
  if(step===undefined) return;
  e.preventDefault(); show((i<0?0:i)+step);
});
// Clicking the video itself must reach its controls, not close the viewer.
viewer.addEventListener('click',e=>{if(e.target===viewer)close_();});
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
              event: Annotated[str | None, Query()] = None,
              tier: Annotated[str | None, Query()] = None,
              limit: Annotated[int, Query(le=2000)] = 500,
              offset: Annotated[int, Query(ge=0)] = 0) -> JSONResponse:
    rows = ix.files(db(), event=event, tier=tier, limit=limit, offset=offset)
    return JSONResponse([dict(r) for r in rows])


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


@app.post("/api/decide")
def api_decide(user: Annotated[str, Depends(require_user)],
               body: Annotated[DecideBody, Body()]) -> JSONResponse:
    """Write a decision to master, then bring its index row up to date.

    **Sidecar first, index follows** (§4). If the sidecar write fails nothing
    happened; if the index update fails the decision still stands and a
    `pix2 index` catches up — drift is only ever "the index is behind", never
    "the record is wrong". `indexed` in the response says which happened.
    """
    media = _master_file(body.folder, body.name)
    sent = body.model_fields_set
    with _write_lock:
        try:
            decision = decisions.apply(
                media,
                tier=body.tier if "tier" in sent else decisions.UNSET,
                event=body.event if "event" in sent else decisions.UNSET,
                date_override=(body.date_override if "date_override" in sent
                               else decisions.UNSET),
            )
        except decisions.DecisionError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
        except OSError as e:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"could not write the sidecar: {e}") from e

        indexed = False
        if DB_PATH.is_file():
            conn = ix.open_rw(DB_PATH)
            try:
                # Passed rather than left to the index's own constants, so
                # the path the decision was written to and the path the row is
                # rebuilt from are the same one.
                indexed = ix.refresh(conn, body.folder, body.name,
                                     meta_dir=META_DIR, master_dir=MASTER_DIR)
            except sqlite3.Error:
                indexed = False
            finally:
                conn.close()

    return JSONResponse({
        "folder": body.folder,
        "name": body.name,
        "tier": decision.tier,
        "event": decision.event,
        "date_override": decision.date_override,
        "has_sidecar": not decision.is_empty(),
        "indexed": indexed,
    })


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
