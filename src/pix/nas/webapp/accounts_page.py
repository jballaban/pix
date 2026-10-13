"""Who can sign in and what they belong to: the Accounts page an administrator
manages the household from, and the four forms it posts.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from pix.nas import accounts, auth, decisions
from pix.nas.webapp.app import app, Principal, require_admin, store
from pix.nas.webapp.session import read_form
from pix.nas.webapp.shell import ACCOUNTS_CSS, page
from pix.nas.webapp.text import h, q


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
        f'<tr><td>{h(a.name)}</td>'
        f'<td class="dim">{h(", ".join(a.groups)) or "&mdash;"}</td>'
        f'<td><form method="post" action="/accounts/save">'
        f'<input type="hidden" name="name" value="{h(a.name)}">'
        f'<input name="groups" value="{h(", ".join(a.groups))}" '
        f'placeholder="groups, comma separated" size="22">'
        f'<input name="password" type="password" placeholder="new password" '
        f'size="14" autocomplete="new-password">'
        f'<button>Save</button></form></td>'
        f'<td><form method="post" action="/accounts/delete" '
        f'onsubmit="return confirm(\'Remove {h(a.name)}?\')">'
        f'<input type="hidden" name="name" value="{h(a.name)}">'
        f'<button>Remove</button></form></td></tr>'
        for a in sorted(book.users.values(), key=lambda a: a.name))

    note = f'<p class="note">{h(msg)}</p>' if msg else ""
    shipped = [w for w, initial in (
        ("the administrator password",
         accounts.elevation_is_initial(book)),
        (f"{user.name}'s sign-in password",
         accounts.login_is_initial(book, user.name))) if initial]
    warn = ("" if not shipped else
            f'<p class="note">{h(" and ".join(shipped)).capitalize()} '
            + ("is still the shipped one. " if len(shipped) == 1
               else "are still the shipped ones. ") +
            'Change it below.</p>')
    # An administrator signing in on the shipped password has no row to
    # change it from until they have one, so they always do.
    if not any(a.name == user.name for a in book.users.values()):
        rows += (f'<tr><td>{h(user.name)}</td><td class="dim">&mdash;</td>'
                 f'<td><form method="post" action="/accounts/save">'
                 f'<input type="hidden" name="name" value="{h(user.name)}">'
                 f'<input name="password" type="password" '
                 f'placeholder="new password" size="14" '
                 f'autocomplete="new-password">'
                 f'<button>Save</button></form></td><td></td></tr>')
    return page("Accounts", f"""{note}{warn}
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
<input name="usual" value="{h(book.usual)}" size="20"
       placeholder="family" autocomplete="off">
<button>Save</button></form>

<h2 class="year">Groups</h2>
<p class="dim">A grant names a person or a group and access cannot tell them
apart, so sharing with <b>family</b> reaches everyone in it.
{h(", ".join(groups)) or "None yet."}</p>
<form method="post" action="/accounts/groups" class="acct">
<input name="groups" value="{h(", ".join(groups))}" size="40"
       placeholder="family, parents, tv">
<button>Save groups</button></form>

<h2 class="year">The administrator password</h2>
<p class="dim">What switches <b>Administrator</b> on, from the account menu
— separate from anybody's sign-in, so a signed-in tablet is not the
archive's keys. Changing it switches Administrator off everywhere
else.</p>
<form method="post" action="/accounts/elevation" class="acct">
<input name="password" type="password" placeholder="new administrator password"
       size="24" autocomplete="new-password">
<button>Change</button></form>
<style>{ACCOUNTS_CSS}</style>""", user=user)


@app.post("/accounts/save")
async def accounts_save(request: Request,
                        user: Annotated[Principal,
                                        Depends(require_admin)]) -> Response:
    """Create or update one account."""
    form = await read_form(request)
    name = form.get("name", "")
    password = form.get("password", "")
    book = store()
    who = accounts.canonical(name)
    if not who:
        return back_with("a name is required")
    if who == decisions.ARCHIVED:
        return back_with(f"{who} is reserved — it is how a file is kept out of "
                     "every view")
    if who in accounts.RESERVED:
        return back_with(f"{who} is reserved — Administrator is switched on "
                         "from the account menu, not signed in as")

    existing = book.users.get(who)
    if existing is None and not password and who not in accounts.ADMINISTRATORS:
        return back_with(f"{who} needs a password to sign in with")

    hashed = auth.hash_password(password) if password else (
        existing.password if existing else "")
    # Absent and empty are different: the admin form submits no groups field
    # at all and must not clear them, while an empty box on the people form is
    # how you take somebody out of every group.
    kept = (tuple(sorted({accounts.canonical(g)
                          for g in form["groups"].split(",") if g.strip()}
                         - accounts.RESERVED))
            if "groups" in form else (existing.groups if existing else ()))
    book.users[who] = accounts.Account(who, hashed, kept)
    # A group used here should exist without having to be declared twice.
    book.groups = sorted({*book.groups, *kept})
    accounts.save(book)
    return back_with(f"saved {who}")


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
    name = (await read_form(request)).get("name", "")
    name = accounts.canonical(name)
    book = store()
    # Removing an administrator's account would not remove them — they are
    # hard-coded — it would put their login back to the shipped password.
    if name in accounts.ADMINISTRATORS:
        return back_with(f"{name} is an administrator and cannot be removed")
    book.users.pop(name, None)
    accounts.save(book)
    return back_with(f"removed {name}")


@app.post("/accounts/elevation")
async def accounts_elevation(
    request: Request,
    user: Annotated[Principal, Depends(require_admin)],
) -> Response:
    """Change the administrator password — and with it, switch every
    Administrator off but this one (`accounts.mint_elevated`)."""
    password = (await read_form(request)).get("password", "")
    if not password:
        return back_with("the administrator password cannot be empty")
    book = store()
    book.elevation = auth.hash_password(password)
    accounts.save(book)
    response = back_with("changed the administrator password")
    # Re-minted against the new hash, or changing it would throw the person
    # who just changed it out of the page they changed it on.
    response.set_cookie(
        accounts.ELEVATED_COOKIE, accounts.mint_elevated(book, user.name),
        max_age=accounts.ELEVATED_HOURS * 3600,
        httponly=True, samesite="lax", path="/")
    return response


@app.post("/accounts/usual")
async def accounts_usual(
    request: Request,
    user: Annotated[Principal, Depends(require_admin)],
) -> Response:
    """Name the audience the grid should stay quiet about."""
    book = store()
    book.usual = accounts.canonical((await read_form(request)).get("usual", ""))
    accounts.save(book)
    return back_with(f"the usual audience is {book.usual or 'unset'}")


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
    raw = (await read_form(request)).get("groups", "")
    book = store()
    book.groups = sorted({accounts.canonical(g) for g in raw.split(",")
                          if g.strip()} - accounts.RESERVED)
    accounts.save(book)
    return back_with("saved groups")


def back_with(message: str) -> Response:
    return RedirectResponse(f"/accounts?msg={q(message)}", status_code=303)
