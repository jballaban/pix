"""The app itself and who is asking: the FastAPI instance, the sign-in that
every route depends on, the account store, and the connection to the index.
Everything else in the web app hangs off what is here.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Annotated, Any, cast
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from pix.nas import accounts, index as ix, webroots
from pix.nas.webapp.compress import Compress
from pix.nas.webapp.logs import Activity, refused


app: FastAPI = FastAPI(title="pix2", docs_url=None, redoc_url=None)
# Pages and JSON leave gzipped; nothing else does. See `compress`.
app.add_middleware(Compress)  # pyright: ignore[reportArgumentType]
# Outermost, so its time is the whole request's. See `logs`.
app.add_middleware(Activity)  # pyright: ignore[reportArgumentType]
app.add_exception_handler(HTTPException, refused)
app.add_exception_handler(RequestValidationError, refused)


@app.exception_handler(status.HTTP_401_UNAUTHORIZED)
async def unauthenticated(  # pyright: ignore[reportUnusedFunction]
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


security = HTTPBasic(auto_error=False)


#: Serializes decision writes. Spec §8 makes last-write-wins the conflict policy
#: and leans on exactly this to keep it a *policy* question: two people tiering
#: the same photo pick a winner, they never interleave into a corrupt sidecar.
write_lock: threading.Lock = threading.Lock()


def store() -> accounts.Store:
    """The account store, read per request.

    Re-read rather than cached because it is small and changes rarely, and a
    stale cache here means a removed account still works — the one kind of
    staleness an access system cannot have.
    """
    return accounts.load()


USUAL: dict[str, Any] = {"key": None, "value": None, "checked": 0.0,
                          "path": None}


def usual() -> str | None:
    """The household's usual audience, read again only when the file changes.

    Not `store()`: that is read fresh on every request on purpose, because a
    stale copy is a removed account that still works. This is the one value
    a thumbnail needs — whether its audience is the ordinary one — and it is
    display, not access. Read per thumbnail it was one file read over SMB for
    every cell: 1,583 of them to draw 2,000 thumbnails, most of the time
    spent drawing the grid.
    """
    # Looked at no more than every two seconds: a grid draws two thousand
    # thumbnails in one go, and asking the share two thousand times whether
    # the file changed costs what reading it did.
    now = time.monotonic()
    path = accounts.ACCOUNTS_FILE
    if (USUAL["key"] is not None and USUAL["path"] == path
            and now - float(USUAL["checked"]) < 2.0):
        return cast("str | None", USUAL["value"])
    USUAL["checked"] = now
    USUAL["path"] = path
    try:
        st = path.stat()
        key: object = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is None or key != USUAL["key"]:
        USUAL["value"] = store().usual
        USUAL["key"] = key
    return cast("str | None", USUAL["value"])


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


def principal(book: accounts.Store, name: str) -> Principal:
    return Principal(name, is_admin=(name == accounts.ADMIN),
                     grants=book.grants(name))


def signed_in(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(security)],
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
    who: Principal | None = None
    if name and (name == accounts.ADMIN or name in book.users):
        who = principal(book, name)
    elif credentials and accounts.check(book, credentials.username,
                                        credentials.password):
        who = principal(book, accounts.canonical(credentials.username))
    # For the activity log, which reads it once the response is sent.
    if who is not None:
        request.state.user = who.name
    return who


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


def db() -> sqlite3.Connection:
    """A connection to the index, or a clear error if it has not been built."""
    if not webroots.DB_PATH.is_file():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"index not built — run `pix2 index` (expected at {webroots.DB_PATH})")
    return ix.open_ro(webroots.DB_PATH)
