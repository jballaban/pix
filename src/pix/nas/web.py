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

**This module only assembles it.** The app lives in `pix.nas.webapp`, a module
per concern — `app` (the instance and sign-in), `shell` (the frame every page
is drawn in), `grid`, `folders`, `pages`, `api`, `writes` (the edit engine),
`clipping`, `media_routes`, and the rest — with the stylesheets and scripts as
files in `static/`. Importing the route modules is what registers their routes;
`uvicorn pix.nas.web:app` and the container's entrypoint both find the app
here, which is why this file stays rather than becoming a package.
"""

from __future__ import annotations

from pix.nas.webapp import (
    accounts_page,
    api,
    clipping,
    history_routes,
    media_routes,
    pages,
    pwa,
    session,
)
from pix.nas.webapp.app import Principal, app

__all__ = ["Principal", "app"]

#: The modules whose routes this app serves. Imported for what importing them
#: does — each registers its routes on `app` — and named here so that is
#: plainly a use. The order is the order they register in; only `/{icon}.png`
#: is a pattern another route could fall into, and nothing else has the shape.
_ROUTES: tuple[object, ...] = (pages, media_routes, api, clipping,
                               pwa, session, accounts_page, history_routes)
