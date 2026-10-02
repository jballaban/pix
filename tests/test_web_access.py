"""Who sees what: signing in, sharing, the access menu, and the metadata a viewer is allowed.

Split out of the one web test module, by area.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient
from web_helpers import has_rule

from pix.nas import accounts, decisions, index as ix, web
from pix.nas.webapp import vocab as w_vocab



# --- signing in -----------------------------------------------------------

def test_a_browser_is_sent_to_the_form(app_env: dict[str, Path]) -> None:
    """And **not** challenged with Basic: a `WWW-Authenticate` header makes the
    browser cache credentials it can never be asked to forget, which is the
    whole reason the cookie exists."""
    r = TestClient(web.app).get("/", headers={"accept": "text/html"},
                                follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")
    assert "www-authenticate" not in r.headers


def test_an_api_call_gets_json_not_a_redirect(app_env: dict[str, Path]) -> None:
    r = TestClient(web.app).get("/api/files")

    assert r.status_code == 401
    assert "www-authenticate" not in r.headers


def test_the_admin_account_is_built_in(app_env: dict[str, Path], sign_in: Callable[[str, str], TestClient]) -> None:
    """Hard-coded, so there is no way to lock yourself out by editing a file —
    and no way to delete the only account that can grant access."""
    assert sign_in(accounts.ADMIN, "admin").get("/").status_code == 200


def test_a_wrong_password_does_not_sign_in(app_env: dict[str, Path]) -> None:
    client = TestClient(web.app)
    r = client.post("/login", data={"name": "admin", "password": "wrong"},
                    follow_redirects=False)

    assert r.status_code == 303
    assert "bad=1" in r.headers["location"]
    assert client.get("/api/files").status_code == 401


def test_the_form_does_not_say_which_half_was_wrong(
    app_env: dict[str, Path]
) -> None:
    """Otherwise it becomes a way to ask whether an account exists."""
    client = TestClient(web.app)
    client.post("/login", data={"name": "zzunknownzz", "password": "x"})
    text = client.get("/login?bad=1").text

    assert "did not match" in text
    assert "zzunknownzz" not in text
    assert "no such" not in text.lower()


def test_signing_out_forgets_the_session(app_env: dict[str, Path], sign_in: Callable[[str, str], TestClient]) -> None:
    """The thing HTTP Basic cannot do, and the reason this exists."""
    client = sign_in(accounts.ADMIN, "admin")
    assert client.get("/api/files").status_code == 200

    client.post("/logout")
    assert client.get("/api/files").status_code == 401


def test_a_tampered_cookie_is_not_a_session(app_env: dict[str, Path], sign_in: Callable[[str, str], TestClient]) -> None:
    client = sign_in(accounts.ADMIN, "admin")
    token = client.cookies[accounts.COOKIE]
    client.cookies.set(accounts.COOKIE, token.replace("admin", "kid", 1))

    assert client.get("/api/files").status_code == 401


def test_an_account_is_created_and_can_sign_in(app_env: dict[str, Path], sign_in: Callable[[str, str], TestClient]) -> None:
    admin = sign_in(accounts.ADMIN, "admin")
    admin.post("/accounts/save",
               data={"name": "kid", "password": "pw", "groups": "family"})

    assert sign_in("kid", "pw").get("/api/files").status_code == 200


def test_a_removed_account_stops_working(app_env: dict[str, Path], sign_in: Callable[[str, str], TestClient]) -> None:
    """Read per request, not cached — a stale cache here means a removed
    account still works, the one staleness an access system cannot have."""
    admin = sign_in(accounts.ADMIN, "admin")
    admin.post("/accounts/save", data={"name": "kid", "password": "pw"})
    kid = sign_in("kid", "pw")
    assert kid.get("/api/files").status_code == 200

    admin.post("/accounts/delete", data={"name": "kid"})
    assert kid.get("/api/files").status_code == 401


def test_the_admin_account_cannot_be_removed(app_env: dict[str, Path], sign_in: Callable[[str, str], TestClient]) -> None:
    admin = sign_in(accounts.ADMIN, "admin")
    admin.post("/accounts/delete", data={"name": accounts.ADMIN})

    assert sign_in(accounts.ADMIN, "admin").get("/").status_code == 200


def test_changing_the_admin_password_takes_effect(app_env: dict[str, Path], sign_in: Callable[[str, str], TestClient]) -> None:
    admin = sign_in(accounts.ADMIN, "admin")
    admin.post("/accounts/save",
               data={"name": accounts.ADMIN, "password": "better"})

    assert sign_in(accounts.ADMIN, "better").get("/").status_code == 200
    bad = TestClient(web.app).post(
        "/login", data={"name": "admin", "password": "admin"},
        follow_redirects=False)
    assert "bad=1" in bad.headers["location"]


def test_the_shipped_admin_password_is_called_out(app_env: dict[str, Path], sign_in: Callable[[str, str], TestClient]) -> None:
    """A default password nobody is told about is a default password nobody
    changes."""
    admin = sign_in(accounts.ADMIN, "admin")
    assert "shipped password" in admin.get("/").text

    admin.post("/accounts/save",
               data={"name": accounts.ADMIN, "password": "better"})
    assert "shipped password" not in sign_in(
        accounts.ADMIN, "better").get("/").text


def test_only_an_admin_manages_accounts(app_env: dict[str, Path], sign_in: Callable[[str, str], TestClient], add_user: Callable[..., None]) -> None:
    add_user("kid", "pw")
    kid = sign_in("kid", "pw")

    assert kid.get("/accounts").status_code == 403
    assert kid.post("/accounts/save",
                    data={"name": "eve", "password": "x"}).status_code == 403


def test_a_group_reaches_what_was_shared_with_it(app_env: dict[str, Path], writable: Path, sign_in: Callable[[str, str], TestClient], add_user: Callable[..., None]) -> None:
    """A grant names a person or a group and the check cannot tell them apart."""
    add_user("kid", "pw", ("family",))
    admin = sign_in(accounts.ADMIN, "admin")
    admin.post("/api/decide", json={"folder": "init_2026", "name": "a.jpg",
                                    "add_audience": ["family"]})

    rows = sign_in("kid", "pw").get("/api/files").json()
    assert [r["name"] for r in rows] == ["a.jpg"]


def test_losing_a_group_loses_the_access(app_env: dict[str, Path], writable: Path, sign_in: Callable[[str, str], TestClient], add_user: Callable[..., None]) -> None:
    add_user("kid", "pw", ("family",))
    admin = sign_in(accounts.ADMIN, "admin")
    admin.post("/api/decide", json={"folder": "init_2026", "name": "a.jpg",
                                    "add_audience": ["family"]})
    admin.post("/accounts/save", data={"name": "kid", "groups": ""})

    assert sign_in("kid", "pw").get("/api/files").json() == []


def test_the_login_page_never_leaves_the_app(app_env: dict[str, Path]) -> None:
    """An open redirect turns the form into a way to send somebody elsewhere
    wearing this app's address."""
    client = TestClient(web.app)
    r = client.post("/login",
                    data={"name": "admin", "password": "admin",
                          "next": "//evil.example/"},
                    follow_redirects=False)

    assert r.headers["location"] == "/"


# --- access ---------------------------------------------------------------

def test_a_viewer_sees_only_what_was_shared(
    household: dict[str, object]
) -> None:
    kid = cast(TestClient, household["kid"])
    rows = kid.get("/api/files").json()

    assert [r["name"] for r in rows] == ["a.jpg"]


def test_an_admin_sees_everything(household: dict[str, object]) -> None:
    admin = cast(TestClient, household["admin"])
    rows = admin.get("/api/files").json()

    assert {r["name"] for r in rows} == {"a.jpg", "b.mp4"}


def test_a_viewer_cannot_fetch_an_unshared_file(
    household: dict[str, object]
) -> None:
    """A grid that omits a photograph while /preview still returns it is not
    access control; it is a tidier index. Anyone can type a URL."""
    kid = cast(TestClient, household["kid"])

    assert kid.get("/thumb/init_2026/b.mp4").status_code == 404
    assert kid.get("/preview/init_2026/b.mp4").status_code == 404
    assert kid.get("/media/init_2026/b.mp4").status_code == 404
    assert kid.get("/api/file/init_2026/b.mp4").status_code == 404


def test_a_viewer_can_fetch_what_was_shared(household: dict[str, object]) -> None:
    kid = cast(TestClient, household["kid"])

    assert kid.get("/thumb/init_2026/a.jpg").status_code == 200
    assert kid.get("/api/file/init_2026/a.jpg").status_code == 200


def test_an_unshared_file_is_missing_not_forbidden(
    household: dict[str, object]
) -> None:
    """403 would confirm the photograph exists to be asked for."""
    kid = cast(TestClient, household["kid"])
    r = kid.get("/thumb/init_2026/b.mp4")

    assert r.status_code == 404
    assert "forbidden" not in r.text.lower()


def test_a_viewer_cannot_widen_their_own_view(
    household: dict[str, object]
) -> None:
    """The scope comes from the credentials, never from the address bar — the
    one thing a URL-shaped filter model must not allow."""
    kid = cast(TestClient, household["kid"])

    for query in ("?audience=admin", "?audience=new", "?viewer=admin",
                  "?audience="):
        rows = kid.get("/api/files" + query).json()
        assert all(r["name"] == "a.jpg" for r in rows), query


def test_a_viewer_cannot_write(household: dict[str, object]) -> None:
    kid = cast(TestClient, household["kid"])

    r = kid.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["kid"]})
    assert r.status_code == 403

    r = kid.post("/api/decide/bulk", json={
        "add_audience": ["kid"], "files": [{"folder": "init_2026",
                                            "name": "a.jpg"}]})
    assert r.status_code == 403


def test_a_viewer_is_offered_no_edit_controls(
    household: dict[str, object]
) -> None:
    """The family curates (§8), so a household member gets the edit bar — all
    of it but the two that somebody else noticing cannot undo.

    Not merely hidden either way: the endpoints refuse a field this person may
    not write, whatever their browser was showing them."""
    kid = cast(TestClient, household["kid"])
    html = kid.get("/browse").text

    assert 'id="actions"' in html
    for act in ("event", "tags", "people", "date", "delete", "download"):
        assert f'data-act="{act}"' in html, act
    for act in ("access", "purge", "restore"):
        assert f'data-act="{act}"' not in html, act


def test_the_admin_keeps_the_edit_controls(household: dict[str, object]) -> None:
    admin = cast(TestClient, household["admin"])
    html = admin.get("/browse").text

    assert '<button data-act="access"' in html


def test_granted_nothing_sees_nothing(app_env: dict[str, Path]) -> None:
    """An empty grant set is not the same as no restriction. Treating the two
    alike is the classic way an access check turns into an access grant."""
    conn = ix.open_ro(app_env["db"])

    assert ix.files(conn, ix.Filters(viewer=frozenset())) == []
    assert ix.count(conn, ix.Filters(viewer=frozenset())) == 0
    assert len(ix.files(conn, ix.Filters())) == 2


# --- names are case-insensitive -------------------------------------------

def test_signing_in_ignores_case(app_env: dict[str, Path], sign_in: Callable[[str, str], TestClient]) -> None:
    assert sign_in("ADMIN", "admin").get("/").status_code == 200
    assert sign_in("Admin", "admin").get("/").status_code == 200


def test_a_password_is_still_case_sensitive(app_env: dict[str, Path]) -> None:
    """Folding the name is a convenience; folding the secret is a weakness."""
    r = TestClient(web.app).post(
        "/login", data={"name": "admin", "password": "ADMIN"},
        follow_redirects=False)

    assert "bad=1" in r.headers["location"]


def test_one_person_cannot_become_two_accounts(app_env: dict[str, Path], sign_in: Callable[[str, str], TestClient]) -> None:
    admin = sign_in(accounts.ADMIN, "admin")
    admin.post("/accounts/save", data={"name": "Kid", "password": "pw"})
    admin.post("/accounts/save", data={"name": "KID", "password": "pw2"})

    book = accounts.load()
    assert list(book.users) == ["kid"]
    assert sign_in("kid", "pw2").get("/").status_code == 200


def test_a_grant_reaches_whatever_case_signed_in(app_env: dict[str, Path], writable: Path, sign_in: Callable[[str, str], TestClient]) -> None:
    """The failure this prevents looks exactly like a correctly-kept secret:
    signed in as `Kid`, a photo shared with `kid` simply is not there."""
    admin = sign_in(accounts.ADMIN, "admin")
    admin.post("/accounts/save", data={"name": "kid", "password": "pw"})
    admin.post("/api/decide", json={"folder": "init_2026", "name": "a.jpg",
                                    "add_audience": ["KID"]})

    rows = sign_in("KiD", "pw").get("/api/files").json()
    assert [r["name"] for r in rows] == ["a.jpg"]


def test_a_group_matches_regardless_of_case(app_env: dict[str, Path], writable: Path, sign_in: Callable[[str, str], TestClient]) -> None:
    admin = sign_in(accounts.ADMIN, "admin")
    admin.post("/accounts/save",
               data={"name": "kid", "password": "pw", "groups": "Family"})
    admin.post("/api/decide", json={"folder": "init_2026", "name": "a.jpg",
                                    "add_audience": ["family"]})

    rows = sign_in("kid", "pw").get("/api/files").json()
    assert [r["name"] for r in rows] == ["a.jpg"]


# --- the access menu ------------------------------------------------------

def test_audience_values_can_be_suggested(client: TestClient,
                                          writable: Path) -> None:
    """The endpoint refused `audience` outright, so the Access list was empty
    however many accounts existed."""
    client.post("/api/decide", json={"folder": "init_2026", "name": "a.jpg",
                                     "add_audience": ["family"]})

    got = client.get("/api/suggest?column=audience").json()
    assert [s["value"] for s in got if s["value"] != ix.UNREVIEWED] == [
        "family"]
    # *Nobody yet* is a state, counted like the names: the video has no
    # audience, so it is offered with its count — and Archived, which
    # nothing is, is not offered at all.
    nobody = [s for s in got if s["value"] == ix.UNREVIEWED]
    assert nobody and nobody[0]["n"] == 1
    assert decisions.ARCHIVED not in [s["value"] for s in got]


def test_the_access_list_is_seeded_from_the_accounts(app_env: dict[str, Path], sign_in: Callable[[str, str], TestClient], add_user: Callable[..., None]) -> None:
    """Sharing has to be possible on the very first file, before any decision
    exists to draw a suggestion from — and only real accounts and roles are
    offered, because a grant to anything else reaches nobody."""
    add_user("james", "pw", ("family",))
    html = sign_in(accounts.ADMIN, "admin").get("/browse").text

    assert '"james"' in html
    assert '"family"' in html


def test_the_admin_is_never_offered_as_an_audience(
    app_env: dict[str, Path]
) -> None:
    """An administrator sees everything already, so sharing with one is a
    no-op dressed as a decision."""
    assert accounts.ADMIN not in w_vocab.audience_names()


def test_an_action_asks_for_the_column_it_edits(client: TestClient) -> None:
    """`unshare` writes the audience field; using the action name as the column
    asked the server for one called `share`, which is a 400 and an empty list."""
    js = client.get("/browse").text

    assert "access:'audience'" in js
    assert "tags:'tag'" in js


def test_the_menu_can_actually_be_hidden(client: TestClient) -> None:
    """An id selector beats the user agent's `[hidden] { display:none }`, so
    `#menu { display:flex }` quietly won and the menu could be opened but never
    dismissed."""
    css = client.get("/browse").text

    assert has_rule(css, '#menu[hidden]', 'display:none')


def test_the_filter_is_called_access(client: TestClient) -> None:
    html = client.get("/browse").text

    assert '"Access"' in html
    assert "Access&hellip;" in html
    assert "Tags&hellip;" in html


# --- metadata is scoped too -----------------------------------------------

def test_a_viewer_sees_only_events_they_can_open(
    household: dict[str, object]
) -> None:
    """An event name is information. A list of every trip and birthday, shown
    to somebody who can open none of the photographs, leaks exactly what the
    audience model exists to keep."""
    kid = cast(TestClient, household["kid"])
    admin = cast(TestClient, household["admin"])

    assert len(admin.get("/api/events").json()) >= 1
    rows = kid.get("/api/events").json()
    assert all(r["n"] == 1 for r in rows)


def test_someone_shared_nothing_sees_an_empty_home(app_env: dict[str, Path], writable: Path, sign_in: Callable[[str, str], TestClient], add_user: Callable[..., None]) -> None:
    add_user("nobody", "pw")

    assert sign_in("nobody", "pw").get("/api/events").json() == []


def test_suggestions_are_scoped_to_the_viewer(
    household: dict[str, object]
) -> None:
    """A dropdown listing every event in the house to somebody who can open
    none of them tells them exactly what they were not shown."""
    kid = cast(TestClient, household["kid"])
    admin = cast(TestClient, household["admin"])

    assert admin.get("/api/suggest?column=event").json() != []
    for column in ("event", "date", "tag", "kind", "band"):
        for s in kid.get(f"/api/suggest?column={column}").json():
            assert s["n"] <= 1, column


def test_the_access_filter_is_an_admin_control(
    household: dict[str, object]
) -> None:
    """Everyone else sees only what was shared with them, so filtering by who
    else can see it offers a choice between their whole world and nothing."""
    kid = cast(TestClient, household["kid"])
    admin = cast(TestClient, household["admin"])

    assert '"Access"' in admin.get("/browse").text
    assert '"Access"' not in kid.get("/browse").text


def test_a_grant_naming_nobody_can_still_be_removed(
    client: TestClient, writable: Path
) -> None:
    """A grant left behind by a renamed or deleted account names nobody, and
    an Access list drawn only from the account list would make it permanent."""
    client.post("/api/decide", json={"folder": "init_2026", "name": "a.jpg",
                                     "add_audience": ["ghost"]})

    got = [s["value"] for s in client.get("/api/suggest?column=audience").json()]
    assert "ghost" in got

    client.post("/api/decide", json={"folder": "init_2026", "name": "a.jpg",
                                     "remove_audience": ["ghost"]})
    assert decisions.read(writable / "a.jpg") is None


def test_one_menu_shows_the_three_states(client: TestClient) -> None:
    """A selection is not one thing, and a two-state box would have to lie."""
    js = client.get("/browse").text

    assert "function shareState" in js
    assert "'none':(n===cs.length?'all':'some')" in js
