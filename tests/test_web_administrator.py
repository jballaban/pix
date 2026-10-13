"""Administrator is a switch, not an account.

The people in `accounts.ADMINISTRATORS` sign in as themselves and see what is
shared with them; with **Administrator** switched on — against a password of
its own — they see everything and may do anything. These tests hold the
switch to that: off means exactly a household member, on needs the second
password and the right person, and nothing else turns it on.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient
from web_helpers import ADMIN_NAME, SHIPPED, administrator

from pix.nas import accounts, auth, web


def _switch(client: TestClient, password: str, next: str = "/browse") -> str:
    r = client.post("/admin/on", data={"password": password, "next": next},
                    follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"]


def _names(client: TestClient) -> set[str]:
    return {r["name"] for r in client.get("/api/files").json()}


def test_off_is_exactly_a_household_member(
        household: dict[str, object],
        sign_in: Callable[[str, str], TestClient]) -> None:
    """Signed in, switch off: what is shared with them and nothing more —
    no unshared files, no Accounts, no access control, no bin."""
    me = sign_in(ADMIN_NAME, SHIPPED)

    assert _names(me) == set()
    assert me.get("/thumb/init_2026/b.mp4").status_code == 404
    r = me.get("/accounts")
    assert r.status_code == 403
    assert "switch Administrator on" in r.text
    html = me.get("/browse").text
    assert 'data-act="access"' not in html
    assert 'href="/accounts"' not in html
    r = me.post("/api/decide", json={"folder": "init_2026", "name": "a.jpg",
                                     "add_audience": [ADMIN_NAME]})
    assert r.status_code in (403, 404)


def test_on_sees_everything_and_off_goes_back(
        household: dict[str, object],
        sign_in: Callable[[str, str], TestClient]) -> None:
    me = sign_in(ADMIN_NAME, SHIPPED)

    assert _switch(me, SHIPPED) == "/browse"
    assert _names(me) == {"a.jpg", "b.mp4"}
    assert me.get("/accounts").status_code == 200
    assert 'data-act="access"' in me.get("/browse").text

    r = me.post("/admin/off", data={"next": "/browse"},
                follow_redirects=False)
    assert r.headers["location"] == "/browse"
    assert _names(me) == set()


def test_off_from_an_administrators_page_lands_somewhere_allowed(
        app_env: dict[str, Path],
        sign_in: Callable[[str, str], TestClient]) -> None:
    me = administrator(sign_in)
    r = me.post("/admin/off", data={"next": "/accounts?msg=x"},
                follow_redirects=False)
    assert r.headers["location"] == "/"


def test_the_switch_wants_its_own_password(
        household: dict[str, object],
        sign_in: Callable[[str, str], TestClient]) -> None:
    """Not the login: a signed-in session must not be enough. Once the login
    is changed, it does not open the switch, and a wrong guess says so on the
    page it was typed on."""
    admin = administrator(sign_in)
    admin.post("/accounts/save", data={"name": ADMIN_NAME, "password": "login"})
    admin.post("/accounts/elevation", data={"password": "switch"})

    me = sign_in(ADMIN_NAME, "login")
    assert "#adminbad" in _switch(me, "login", next="/browse?date=2026")
    assert _names(me) == set()
    assert "adminbad" not in _switch(me, "switch")
    assert _names(me) == {"a.jpg", "b.mp4"}


def test_nobody_else_can_switch_it_on(
        household: dict[str, object]) -> None:
    """Knowing the administrator password is not being an administrator."""
    from typing import cast

    kid = cast(TestClient, household["kid"])
    assert "#adminbad" in _switch(kid, SHIPPED)
    assert _names(kid) == {"a.jpg"}
    assert kid.get("/accounts").status_code == 403


def test_a_switch_is_for_the_person_it_was_minted_for(
        household: dict[str, object],
        sign_in: Callable[[str, str], TestClient]) -> None:
    """Carried to somebody else's session — copied off a shared browser —
    it turns nothing on."""
    from typing import cast

    me = administrator(sign_in)
    kid = cast(TestClient, household["kid"])
    kid.cookies.set(accounts.ELEVATED_COOKIE,
                    me.cookies[accounts.ELEVATED_COOKIE])

    assert _names(kid) == {"a.jpg"}
    assert kid.get("/accounts").status_code == 403


def test_a_session_token_is_not_a_switch(
        app_env: dict[str, Path],
        sign_in: Callable[[str, str], TestClient]) -> None:
    """Both are signed with the same secret; they must not be
    interchangeable."""
    me = sign_in(ADMIN_NAME, SHIPPED)
    me.cookies.set(accounts.ELEVATED_COOKIE, me.cookies[accounts.COOKIE])

    assert me.get("/accounts").status_code == 403


def test_changing_the_password_turns_every_other_switch_off(
        app_env: dict[str, Path],
        sign_in: Callable[[str, str], TestClient]) -> None:
    """The answer to *somebody else knows it now* — but not for the person
    changing it, who would otherwise be thrown off the page they did it on."""
    tablet = administrator(sign_in)
    desk = administrator(sign_in)

    desk.post("/accounts/elevation", data={"password": "new"})

    assert desk.get("/accounts").status_code == 200
    assert tablet.get("/accounts").status_code == 403


def test_signing_out_turns_it_off(
        app_env: dict[str, Path],
        sign_in: Callable[[str, str], TestClient]) -> None:
    """Or the next person to sign in on this browser would find it waiting."""
    me = administrator(sign_in)
    r = me.post("/logout", follow_redirects=False)

    assert accounts.ELEVATED_COOKIE in r.headers.get("set-cookie", "")
    assert accounts.ELEVATED_COOKIE not in me.cookies


def test_the_switch_wears_off(app_env: dict[str, Path]) -> None:
    book = accounts.load()
    token = accounts.mint_elevated(book, ADMIN_NAME, now=0)

    assert accounts.elevated(book, token, ADMIN_NAME, now=60)
    assert not accounts.elevated(
        book, token, ADMIN_NAME, now=accounts.ELEVATED_HOURS * 3600 + 1)


def test_basic_auth_never_switches_it_on(
        household: dict[str, object]) -> None:
    """A script is whoever it signs in as, with Administrator off."""
    r = TestClient(web.app).get("/accounts", auth=(ADMIN_NAME, SHIPPED))
    assert r.status_code == 403


def test_the_menu_offers_the_switch_to_administrators_only(
        household: dict[str, object],
        sign_in: Callable[[str, str], TestClient]) -> None:
    from typing import cast

    off = sign_in(ADMIN_NAME, SHIPPED).get("/browse").text
    assert 'action="/admin/on"' in off and 'name="password"' in off
    on = administrator(sign_in).get("/browse").text
    assert 'action="/admin/off"' in on
    kid = cast(TestClient, household["kid"]).get("/browse").text
    assert "/admin/on" not in kid and "/admin/off" not in kid


def test_administrator_is_not_a_name_anyone_can_take(
        app_env: dict[str, Path],
        sign_in: Callable[[str, str], TestClient]) -> None:
    admin = administrator(sign_in)
    for name in (accounts.ADMINISTRATOR, accounts.FORMER_ADMIN):
        admin.post("/accounts/save", data={"name": name, "password": "x"})
        assert name not in accounts.load().users
    admin.post("/accounts/save", data={"name": "kid", "password": "x",
                                       "groups": accounts.ADMINISTRATOR})
    assert accounts.load().users["kid"].groups == ()


def test_the_old_admin_account_becomes_the_switch_password(
        app_env: dict[str, Path],
        sign_in: Callable[[str, str], TestClient]) -> None:
    """A store from before keeps the password that already meant
    *administrator* in this house — as the switch's — and the account goes."""
    accounts.ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    accounts.ACCOUNTS_FILE.write_text(json.dumps({
        "users": {"admin": {"password": auth.hash_password("old-admin"),
                            "groups": []}},
        "groups": [], "usual": "", "secret": "s" * 64}), encoding="utf-8")

    book = accounts.load()
    assert "admin" not in book.users
    assert accounts.check_elevation(book, "old-admin")
    assert not accounts.check_elevation(book, SHIPPED)
    # Nobody signs in as it any more.
    r = TestClient(web.app).post("/login", data={
        "name": "admin", "password": "old-admin"}, follow_redirects=False)
    assert "bad=1" in r.headers["location"]
    administrator(sign_in, SHIPPED, "old-admin")
