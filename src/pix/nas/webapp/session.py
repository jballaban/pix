"""Signing in and out: the form, the cookie it sets, and the one place a 'next'
address from a query string is checked before it is followed.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import parse_qs

from fastapi import Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from pix.nas import accounts
from pix.nas.webapp.app import app, Principal, require_user, signed_in, store
from pix.nas.webapp.marks import logo_mark
from pix.nas.webapp.shell import LOGIN_CSS, page
from pix.nas.webapp.text import h, q


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request,
               user: Annotated[Principal | None, Depends(signed_in)],
               next: Annotated[str, Query()] = "/",
               bad: Annotated[int, Query()] = 0) -> Response:
    """The form. Already signed in? Then there is nothing to ask."""
    if user is not None and not bad:
        return RedirectResponse(safe_next(next), status_code=303)
    warn = ('<p class="note">That did not match.</p>' if bad else "")
    book = store()
    first = sorted(n for n in accounts.ADMINISTRATORS
                   if accounts.login_is_initial(book, n))
    hint = ("" if not first else
            '<p class="dim" style="margin-top:14px">First run: sign in as '
            f'<b>{h(first[0])}</b> with the password <b>admin</b>, then '
            'change it.</p>')
    return page("Sign in", f"""<div class="gate">
<div class="logo">{logo_mark(34)}<span class="word">pi<b>x</b></span></div>
<h2>Sign in</h2>{warn}
<form method="post" action="/login">
<input type="hidden" name="next" value="{h(safe_next(next))}">
<label>Name</label><input name="name" autofocus autocomplete="username">
<label>Password</label>
<input name="password" type="password" autocomplete="current-password">
<button class="primary">Sign in</button>
</form>{hint}</div>""" + f"<style>{LOGIN_CSS}</style>")


async def read_form(request: Request) -> dict[str, str]:
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
    form = await read_form(request)
    name = accounts.canonical(form.get("name", ""))
    password = form.get("password", "")
    next = form.get("next", "/")
    book = store()
    if not accounts.check(book, name, password):
        # No detail about which half was wrong: it would turn the form into a
        # way to ask whether an account exists.
        return RedirectResponse(
            f"/login?bad=1&next={q(safe_next(next))}", status_code=303)

    response = RedirectResponse(safe_next(next), status_code=303)
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
    """Forget the session — and the Administrator switch with it, or the
    next person to sign in on this browser would find it waiting."""
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(accounts.COOKIE, path="/")
    response.delete_cookie(accounts.ELEVATED_COOKIE, path="/")
    return response


@app.post("/admin/on")
async def admin_on(
    request: Request,
    user: Annotated[Principal, Depends(require_user)],
) -> Response:
    """Switch Administrator on, against the administrator password.

    Not the login password: the point of a second one is that a signed-in
    session is not enough. Refused the same way whether this person may not
    be an administrator at all or typed it wrong — the form is one place to
    ask both questions, and it answers neither.
    """
    form = await read_form(request)
    back = safe_next(form.get("next", "/"))
    book = store()
    if not (user.may_elevate
            and accounts.check_elevation(book, form.get("password", ""))):
        # In the fragment, which never reaches the server: a query flag
        # would be copied into every link the next page builds from its own
        # address, and following one would say it all over again.
        return RedirectResponse(back + "#" + ADMIN_BAD_MARK, status_code=303)
    response = RedirectResponse(back, status_code=303)
    response.set_cookie(
        accounts.ELEVATED_COOKIE, accounts.mint_elevated(book, user.name),
        max_age=accounts.ELEVATED_HOURS * 3600,
        # As the session's, and for the same reasons (see `login`).
        httponly=True, samesite="lax", path="/")
    return response


@app.post("/admin/off")
async def admin_off(request: Request) -> Response:
    """Switch Administrator off: back to what is shared with you.

    Needs nothing — turning a power off is never something to refuse. From
    a page that is an administrator's alone, it lands on the front door
    instead of on that page refusing them.
    """
    back = safe_next((await read_form(request)).get("next", "/"))
    if back.startswith(ADMIN_PAGES):
        back = "/"
    response = RedirectResponse(back, status_code=303)
    response.delete_cookie(accounts.ELEVATED_COOKIE, path="/")
    return response


#: The pages only an administrator is shown at all.
ADMIN_PAGES: tuple[str, ...] = ("/accounts", "/history", "/splice/")


#: The fragment a refused switch comes back with (`shell.ADMIN_BAD`).
ADMIN_BAD_MARK: str = "adminbad"


def safe_next(target: str) -> str:
    """Only ever redirect inside this app.

    An open redirect turns the login form into a way to send somebody to
    somewhere else entirely, wearing this app's address.
    """
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    return target
