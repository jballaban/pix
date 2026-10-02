"""What makes the app installable and keeps it answering: the manifest, the
service worker and its offline page, the home-screen icons, and the health
check a deploy is confirmed by.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import HTTPException, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
)

from pix.nas import index as ix, webroots
from pix.nas.assets import asset
from pix.nas.webapp.app import app
from pix.nas.webapp.marks import ICONS
from pix.nas.webapp.shell import LOGIN_CSS, page


MANIFEST: dict[str, object] = {
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
SERVICE_WORKER: str = asset("js/sw.js")


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
OFFLINE_JS: str = asset("js/offline.js")


@app.get("/manifest.webmanifest")
def manifest() -> Response:
    """What to call this and which icon to use, for a launcher."""
    return JSONResponse(MANIFEST, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker() -> Response:
    """Served from the root, which is what gives it the whole app as its scope.

    A worker can only ever control paths below where it was served from, so this
    cannot live under `/static/` without also sending a header to widen it —
    a detail that is invisible until installing silently does nothing.
    """
    return Response(SERVICE_WORKER, media_type="application/javascript",
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
    return page("pix", """<div class="gate">
<h2 id="offhead">One moment</h2>
<p class="dim" id="offsay">Finding out what happened.</p>
<button class="primary" id="offgo" hidden>Try again</button>
</div>""" + f"<style>{LOGIN_CSS}</style>"
                 + f"<script>{OFFLINE_JS}</script>")


@app.get("/{icon}.png")
def icon(icon: str) -> Response:
    """One of the home-screen icons, by name."""
    path = (ICONS / f"{icon}.png").resolve()
    if path.parent != ICONS.resolve() or not path.is_file():
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
