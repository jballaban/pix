"""The browse app (spec/nas-app.md §8).

It reads the index and serves the derived tiers. It never decodes anything —
`process` already made everything it displays. The one thing it writes is
curation decisions: an `.xmp` beside the master file, then that file's index row
— sidecar first, index follows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from pix.nas import accounts
from pix.nas import auth
from pix.nas import decisions
from pix.nas import index as ix
from pix.nas import web
from pix.nas.web import _split
from pix.nas.decisions import Decision


@pytest.fixture
def app_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    share = tmp_path / "nas"
    meta, master = share / "meta", share / "master"
    thumb, preview = share / "thumb", share / "preview"
    for d in (meta, master, thumb, preview):
        d.mkdir(parents=True)

    (meta / "init_2026").mkdir()
    (meta / "init_2026" / "a.jpg.json").write_text(json.dumps({
        "file": "a.jpg", "folder": "init_2026", "size": 10, "mtime_ns": 1,
        "exif": {"EXIF:DateTimeOriginal": "2026:08:30 15:34:55",
                 "XMP:EventAuto": "Italy - Sicily"},
    }), encoding="utf-8")
    (meta / "init_2026" / "b.mp4.json").write_text(json.dumps({
        "file": "b.mp4", "folder": "init_2026", "size": 20, "mtime_ns": 1,
        "exif": {"QuickTime:Duration": "75 s", "XMP:EventAuto": "Italy - Sicily"},
    }), encoding="utf-8")

    for tier in (thumb, preview):
        (tier / "init_2026").mkdir()
        (tier / "init_2026" / "a.jpg.jpg").write_bytes(b"\xff\xd8fake-jpeg")
        (tier / "init_2026" / "b.mp4.jpg").write_bytes(b"\xff\xd8fake-jpeg")

    db = tmp_path / "index.db"
    ix.build(db, meta_dir=meta, master_dir=master)

    monkeypatch.setattr(web, "DB_PATH", db)
    monkeypatch.setattr(web, "THUMB_DIR", thumb)
    monkeypatch.setattr(web, "PREVIEW_DIR", preview)
    # Accounts live in the sandbox; the autouse NAS guard already keeps
    # ACCOUNTS_FILE off the real share, and this pins it per test.
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", tmp_path / "users.json")
    return {"share": share, "thumb": thumb, "db": db}


@pytest.fixture
def master(app_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> Path:
    """A master folder holding the real files the media endpoint streams."""
    m = app_env["share"] / "master" / "init_2026"
    m.mkdir(parents=True, exist_ok=True)
    (m / "b.mp4").write_bytes(bytes([0, 0, 0, 0x18]) + b"ftypmp42" + b"x" * 400)
    monkeypatch.setattr(web, "MASTER_DIR", app_env["share"] / "master")
    return m


def sign_in(name: str, password: str) -> TestClient:
    """A client holding a real session cookie for `name`.

    Through the form rather than by forging a cookie, so the tests exercise
    the path a browser actually takes.
    """
    client = TestClient(web.app)
    r = client.post("/login", data={"name": name, "password": password},
                    follow_redirects=False)
    assert r.status_code == 303, r.text[:200]
    return client


def add_user(name: str, password: str, roles: tuple[str, ...] = ()) -> None:
    book = accounts.load()
    book.users[name] = accounts.Account(
        name, auth.hash_password(password), roles)
    book.roles = sorted({*book.roles, *roles})
    accounts.save(book)


@pytest.fixture
def client(app_env: dict[str, Path]) -> TestClient:
    """Signed in as the built-in admin, which is what curating is done as."""
    return sign_in(accounts.ADMIN, "admin")


# --- browse ------------------------------------------------------------------

def test_home_lists_events(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "Italy - Sicily" in r.text
    assert "2 files" in r.text


def test_home_names_who_you_are_signed_in_as(client: TestClient) -> None:
    """This app is used as two different people — the owner curating and the
    admin granting access — and acting as the wrong one is invisible until
    something is shared with the wrong household."""
    html = client.get("/").text
    assert "admin" in html
    assert "Sign out" in html


def test_event_grid_shows_thumbnails(client: TestClient) -> None:
    r = client.get("/browse?event=Italy%20-%20Sicily")
    assert r.status_code == 200
    assert "/thumb/init_2026/a.jpg" in r.text
    assert "/thumb/init_2026/b.mp4" in r.text


def test_video_cells_are_badged_with_duration(client: TestClient) -> None:
    """A grid of stills gives no hint which are clips."""
    assert "1:15" in client.get("/browse?event=Italy%20-%20Sicily").text


def test_event_names_with_spaces_round_trip(client: TestClient) -> None:
    """Real events are 'Italy - Sicily', not slugs."""
    assert client.get("/browse?event=Italy - Sicily").status_code == 200


def test_unknown_event_is_empty_not_an_error(client: TestClient) -> None:
    r = client.get("/browse?event=Nope")
    assert r.status_code == 200
    assert "Nothing matches" in r.text


def test_an_old_event_link_still_lands(client: TestClient) -> None:
    """An event is a filter now; the old URL shape predates that."""
    r = client.get("/event/Italy%20-%20Sicily", follow_redirects=False)
    assert r.status_code == 307
    assert "event=Italy" in r.headers["location"]


# --- media -------------------------------------------------------------------

def test_thumb_is_served(client: TestClient) -> None:
    r = client.get("/thumb/init_2026/a.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


def test_preview_is_served(client: TestClient) -> None:
    assert client.get("/preview/init_2026/a.jpg").status_code == 200


def test_missing_derived_file_is_404(client: TestClient) -> None:
    assert client.get("/thumb/init_2026/never-made.jpg").status_code == 404


def test_path_traversal_is_refused(client: TestClient) -> None:
    """Both components come from a URL, so they are untrusted.

    Without the check, `..` reads arbitrary files off the share.
    """
    for bad in ("/thumb/..%2f..%2fmaster/a.jpg",
                "/thumb/init_2026/..%2f..%2fmaster%2fa.jpg",
                "/preview/..%2f..%2f..%2fetc/passwd"):
        assert client.get(bad).status_code in (400, 404), bad


def test_derived_images_are_cacheable(client: TestClient) -> None:
    """They never change: `process` rewrites nothing it has already made."""
    assert "max-age" in client.get("/thumb/init_2026/a.jpg").headers["cache-control"]


# --- api ---------------------------------------------------------------------

def test_api_events(client: TestClient) -> None:
    """Grouped by year, so the dated and undated halves of one event are
    separate rows — each is a different slice of work."""
    rows = client.get("/api/events").json()
    assert {(r["year"], r["event"], r["n"]) for r in rows} == {
        ("2026", "Italy - Sicily", 1),
        ("(undated)", "Italy - Sicily", 1),
    }


def test_the_home_page_groups_by_year(client: TestClient) -> None:
    """A flat list of every event across twenty-five years is a list nobody
    can find their place in."""
    html = client.get("/").text
    assert 'href="/browse?year=2026"' in html
    assert "events reviewed" in html


def test_an_event_row_links_to_that_year_and_event(
    client: TestClient
) -> None:
    html = client.get("/").text
    assert "year=2026&amp;event=Italy%20-%20Sicily" in html


def test_undated_files_are_reachable(client: TestClient) -> None:
    """374 of the seeded year have no date; that is a work item, not an
    absence to leave unlinked."""
    assert "(undated)" in client.get("/").text
    assert "b.mp4" in client.get("/browse?year=(undated)").text


def test_api_files_filters_by_event(client: TestClient) -> None:
    rows = client.get("/api/files", params={"event": "Italy - Sicily"}).json()
    assert {r["name"] for r in rows} == {"a.jpg", "b.mp4"}


def test_healthz_needs_no_auth(app_env: dict[str, Path],
                               monkeypatch: pytest.MonkeyPatch) -> None:
    """Container Manager's probe cannot log in."""
    assert TestClient(web.app).get("/healthz").status_code == 200


# --- signing in --------------------------------------------------------------

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


def test_the_admin_account_is_built_in(app_env: dict[str, Path]) -> None:
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


def test_signing_out_forgets_the_session(app_env: dict[str, Path]) -> None:
    """The thing HTTP Basic cannot do, and the reason this exists."""
    client = sign_in(accounts.ADMIN, "admin")
    assert client.get("/api/files").status_code == 200

    client.post("/logout")
    assert client.get("/api/files").status_code == 401


def test_a_tampered_cookie_is_not_a_session(app_env: dict[str, Path]) -> None:
    client = sign_in(accounts.ADMIN, "admin")
    token = client.cookies[accounts.COOKIE]
    client.cookies.set(accounts.COOKIE, token.replace("admin", "kid", 1))

    assert client.get("/api/files").status_code == 401


def test_an_account_is_created_and_can_sign_in(app_env: dict[str, Path]) -> None:
    admin = sign_in(accounts.ADMIN, "admin")
    admin.post("/accounts/save",
               data={"name": "kid", "password": "pw", "roles": "family"})

    assert sign_in("kid", "pw").get("/api/files").status_code == 200


def test_a_removed_account_stops_working(app_env: dict[str, Path]) -> None:
    """Read per request, not cached — a stale cache here means a removed
    account still works, the one staleness an access system cannot have."""
    admin = sign_in(accounts.ADMIN, "admin")
    admin.post("/accounts/save", data={"name": "kid", "password": "pw"})
    kid = sign_in("kid", "pw")
    assert kid.get("/api/files").status_code == 200

    admin.post("/accounts/delete", data={"name": "kid"})
    assert kid.get("/api/files").status_code == 401


def test_the_admin_account_cannot_be_removed(app_env: dict[str, Path]) -> None:
    admin = sign_in(accounts.ADMIN, "admin")
    admin.post("/accounts/delete", data={"name": accounts.ADMIN})

    assert sign_in(accounts.ADMIN, "admin").get("/").status_code == 200


def test_changing_the_admin_password_takes_effect(
    app_env: dict[str, Path]
) -> None:
    admin = sign_in(accounts.ADMIN, "admin")
    admin.post("/accounts/save",
               data={"name": accounts.ADMIN, "password": "better"})

    assert sign_in(accounts.ADMIN, "better").get("/").status_code == 200
    bad = TestClient(web.app).post(
        "/login", data={"name": "admin", "password": "admin"},
        follow_redirects=False)
    assert "bad=1" in bad.headers["location"]


def test_the_shipped_admin_password_is_called_out(
    app_env: dict[str, Path]
) -> None:
    """A default password nobody is told about is a default password nobody
    changes."""
    admin = sign_in(accounts.ADMIN, "admin")
    assert "shipped password" in admin.get("/").text

    admin.post("/accounts/save",
               data={"name": accounts.ADMIN, "password": "better"})
    assert "shipped password" not in sign_in(
        accounts.ADMIN, "better").get("/").text


def test_only_an_admin_manages_accounts(app_env: dict[str, Path]) -> None:
    add_user("kid", "pw")
    kid = sign_in("kid", "pw")

    assert kid.get("/accounts").status_code == 403
    assert kid.post("/accounts/save",
                    data={"name": "eve", "password": "x"}).status_code == 403


def test_a_role_reaches_what_was_shared_with_it(app_env: dict[str, Path],
                                                writable: Path) -> None:
    """A grant names a person or a role and the check cannot tell them apart."""
    add_user("kid", "pw", ("family",))
    admin = sign_in(accounts.ADMIN, "admin")
    admin.post("/api/decide", json={"folder": "init_2026", "name": "a.jpg",
                                    "add_audience": ["family"]})

    rows = sign_in("kid", "pw").get("/api/files").json()
    assert [r["name"] for r in rows] == ["a.jpg"]


def test_losing_a_role_loses_the_access(app_env: dict[str, Path],
                                        writable: Path) -> None:
    add_user("kid", "pw", ("family",))
    admin = sign_in(accounts.ADMIN, "admin")
    admin.post("/api/decide", json={"folder": "init_2026", "name": "a.jpg",
                                    "add_audience": ["family"]})
    admin.post("/accounts/save", data={"name": "kid", "roles": ""})

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


# --- index not built ---------------------------------------------------------

def test_a_missing_index_says_what_to_run(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """'503' is useless on its own; the fix is one command."""
    monkeypatch.setattr(web, "DB_PATH", tmp_path / "nope.db")
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", tmp_path / "users.json")

    r = sign_in(accounts.ADMIN, "admin").get("/")

    assert r.status_code == 503
    assert "pix2 index" in r.text


# --- video playback ----------------------------------------------------------

def test_master_video_is_streamable(master: Path, app_env: dict[str, Path]) -> None:
    """Videos need the master file: there is no delivery rendition to serve.

    The seeded library is MP4 throughout, so master *is* the playable copy.
    """
    r = sign_in(accounts.ADMIN, "admin").get("/media/init_2026/b.mp4")

    assert r.status_code == 200
    assert r.content.startswith(bytes([0, 0, 0, 0x18]) + b"ftyp")


def test_media_advertises_range_support(master: Path,
                                        app_env: dict[str, Path]) -> None:
    """Without ranges a browser cannot seek, only play from the start."""
    r = sign_in(accounts.ADMIN, "admin").get("/media/init_2026/b.mp4")
    assert r.headers.get("accept-ranges") == "bytes"


def test_media_serves_a_byte_range(master: Path, app_env: dict[str, Path]) -> None:
    r = sign_in(accounts.ADMIN, "admin").get("/media/init_2026/b.mp4",
                                headers={"Range": "bytes=8-15"})
    assert r.status_code == 206
    assert len(r.content) == 8


def test_media_refuses_traversal(master: Path, app_env: dict[str, Path]) -> None:
    """The only endpoint touching master, so the guard matters most here."""
    c = TestClient(web.app)
    for bad in ("/media/..%2f..%2fetc/passwd",
                "/media/init_2026/..%2f..%2f..%2fetc%2fpasswd"):
        assert c.get(bad).status_code in (400, 404), bad


def test_media_is_missing_for_an_unknown_file(master: Path,
                                              app_env: dict[str, Path]) -> None:
    assert sign_in(accounts.ADMIN, "admin").get("/media/init_2026/nope.mp4").status_code == 404


def test_grid_marks_which_cells_are_video(client: TestClient) -> None:
    """The viewer picks <video> or <img> from this, so it has to be present."""
    html = client.get("/browse?event=Italy - Sicily").text
    assert 'data-kind="video"' in html
    assert 'data-kind="image"' in html


# --- staleness ---------------------------------------------------------------

def test_home_shows_when_the_index_was_built(client: TestClient) -> None:
    """Nothing watches the share, so age is the only signal an index is stale."""
    assert "indexed" in client.get("/").text


def test_age_is_rendered_in_words() -> None:
    import time as _t

    assert web._age(_t.time()) == "just now"
    assert web._age(_t.time() - 600) == "10m ago"
    assert web._age(_t.time() - 7200) == "2h ago"
    assert web._age(_t.time() - 86400 * 3) == "3d ago"


def test_an_index_without_a_timestamp_still_renders() -> None:
    """Older index files predate the built_at row; they must not 500."""
    assert web._age(None) == "at an unknown time"


# --- curation ----------------------------------------------------------------

@pytest.fixture
def writable(app_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> Path:
    """Master with the real files a decision attaches to, and the tier roots the
    endpoint resolves against."""
    m = app_env["share"] / "master" / "init_2026"
    m.mkdir(parents=True, exist_ok=True)
    (m / "a.jpg").write_bytes(b"\xff\xd8original")
    monkeypatch.setattr(web, "MASTER_DIR", app_env["share"] / "master")
    monkeypatch.setattr(web, "META_DIR", app_env["share"] / "meta")
    return m


def test_a_decision_writes_a_sidecar_and_updates_the_row(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["family"]})

    assert r.status_code == 200
    assert r.json()["audience"] == ["family"]
    assert r.json()["indexed"] is True
    assert decisions.read(writable / "a.jpg") == Decision(audience=("family",))

    conn = ix.open_ro(app_env["db"])
    row = ix.one(conn, "init_2026", "a.jpg")
    assert row is not None
    assert (_split(row["audience"]), row["has_sidecar"]) == (["family"], 1)


def test_the_index_follows_rather_than_leads(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Sidecar first (§4): the row must never carry a decision that is not on
    disk, so a refresh failure still leaves the record written."""
    conn = ix.open_ro(app_env["db"])
    before = conn.execute("SELECT * FROM files WHERE name = 'a.jpg'").fetchone()
    conn.close()
    assert before["event"] == "Italy - Sicily"

    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "event": "Sicily Trip"})

    assert decisions.read(writable / "a.jpg") == Decision(event="Sicily Trip")


def test_omitting_a_field_leaves_it_alone(client: TestClient,
                                          writable: Path) -> None:
    """One-field gestures are the whole UI; a whole-record write would erase the
    rest of what had been decided."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "event": "Sicily Trip"})
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["family"]})

    assert decisions.read(writable / "a.jpg") == Decision(
        audience=("family",), event="Sicily Trip")


def test_null_clears_where_omission_does_not(client: TestClient,
                                             writable: Path) -> None:
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["family"],
        "event": "Sicily Trip"})
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "event": None})

    assert r.json()["event"] is None
    assert decisions.read(writable / "a.jpg") == Decision(audience=("family",))


def test_clearing_everything_marks_it_unreviewed_again(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["family"]})
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "audience": []})

    assert r.json()["has_sidecar"] is False
    assert not decisions.sidecar_path(writable / "a.jpg").exists()

    conn = ix.open_ro(app_env["db"])
    row = ix.one(conn, "init_2026", "a.jpg")
    assert row is not None
    assert (row["audience"], row["has_sidecar"]) == (None, 0)


def test_an_unknown_tier_is_rejected(client: TestClient, writable: Path) -> None:
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["x" * 200]})

    assert r.status_code == 400
    assert not decisions.sidecar_path(writable / "a.jpg").exists()


def test_traversal_cannot_drop_a_sidecar_off_the_tier(
    client: TestClient, writable: Path, tmp_path: Path
) -> None:
    """Body components are as untrusted as URL ones, and this endpoint writes."""
    r = client.post("/api/decide", json={
        "folder": "../../..", "name": "escape.jpg", "add_audience": ["family"]})

    assert r.status_code == 400
    assert list(tmp_path.rglob("escape.jpg.xmp")) == []


def test_a_file_not_in_master_is_refused(client: TestClient,
                                         writable: Path) -> None:
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "nothere.jpg", "add_audience": ["family"]})

    assert r.status_code == 404


def test_a_sidecar_is_not_a_decidable_file(client: TestClient,
                                           writable: Path) -> None:
    decisions.write(writable / "a.jpg", Decision(audience=("family",)))
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg.xmp", "add_audience": ["kids"]})

    assert r.status_code == 400
    assert not (writable / "a.jpg.xmp.xmp").exists()


def test_deciding_needs_the_same_auth_as_browsing(
    app_env: dict[str, Path], writable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write endpoint must not be the hole in the auth wall."""
    client = TestClient(web.app)

    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["family"]})

    assert r.status_code == 401
    assert not decisions.sidecar_path(writable / "a.jpg").exists()


def test_there_is_no_reindex_endpoint(client: TestClient) -> None:
    """A rebuild reads ~62k records and would block the single worker for
    minutes, at the request of anyone holding the URL."""
    assert client.post("/api/reindex").status_code == 404


# --- the keep pass -----------------------------------------------------------

def _targets(*names: str) -> list[dict[str, str]]:
    return [{"folder": "init_2026", "name": n} for n in names]


def test_the_review_page_carries_the_current_audience(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The grid is the state — promoting must survive a reload, not live in the
    browser."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["family"]})

    r = client.get("/browse?event=Italy%20-%20Sicily")
    assert 'data-audience="family"' in r.text
    assert 'data-audience=""' in r.text


def test_the_grid_offers_the_sharing_actions(client: TestClient) -> None:
    """Finishing an event is now filter to New, Select all, Share — the same
    outcome through the general mechanism rather than a button that only one
    page could have."""
    html = client.get("/browse?event=Italy%20-%20Sicily").text
    assert '<button data-act="share"' in html
    assert 'id="selall"' in html


def test_bulk_writes_one_decision_across_a_selection(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Finishing an event is a few hundred sidecar writes from one click."""
    (writable / "b.mp4").write_bytes(b"fake")
    r = client.post("/api/decide/bulk", json={
        "add_audience": ["private"], "files": _targets("a.jpg", "b.mp4")})

    assert r.status_code == 200
    assert r.json()["written"] == 2
    assert r.json()["failed"] == []
    assert decisions.read(writable / "a.jpg") == Decision(audience=("private",))
    assert decisions.read(writable / "b.mp4") == Decision(audience=("private",))

    conn = ix.open_ro(app_env["db"])
    tiers = {r["name"]: _split(r["audience"])
             for r in ix.files(conn)}
    assert tiers == {"a.jpg": ["private"], "b.mp4": ["private"]}


def test_bulk_names_a_selection(client: TestClient, writable: Path) -> None:
    """Pass 1's gesture: a range is a *selection*, written once — never a stored
    rule that decides membership later."""
    r = client.post("/api/decide/bulk", json={
        "event": "France Trip", "files": _targets("a.jpg")})

    assert r.json()["written"] == 1
    assert decisions.read(writable / "a.jpg") == Decision(event="France Trip")


def test_bulk_leaves_untouched_fields_alone(client: TestClient,
                                            writable: Path) -> None:
    """Finishing an event must not wipe the events people already assigned."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "event": "Sicily Trip"})
    client.post("/api/decide/bulk", json={"add_audience": ["private"],
                                          "files": _targets("a.jpg")})

    assert decisions.read(writable / "a.jpg") == Decision(
        audience=("private",), event="Sicily Trip")


def test_one_bad_file_does_not_lose_the_rest(client: TestClient,
                                             writable: Path) -> None:
    """Partial success is the normal outcome over SMB. Failing the whole batch
    would leave the curator unsure what landed."""
    r = client.post("/api/decide/bulk", json={
        "add_audience": ["private"], "files": _targets("a.jpg", "gone.jpg")})

    assert r.status_code == 200
    assert r.json()["written"] == 1
    assert [f["name"] for f in r.json()["failed"]] == ["gone.jpg"]
    assert decisions.read(writable / "a.jpg") == Decision(audience=("private",))


def test_an_empty_selection_is_not_an_error(client: TestClient,
                                            writable: Path) -> None:
    """Finishing an already-finished event is a no-op, not a failure."""
    r = client.post("/api/decide/bulk", json={"add_audience": ["private"], "files": []})

    assert r.status_code == 200
    assert r.json()["written"] == 0


def test_an_oversized_batch_is_refused(client: TestClient,
                                       writable: Path) -> None:
    """Bounds one request so a 1,766-file event cannot hold the single worker."""
    files = _targets(*[f"f{n}.jpg" for n in range(web.BULK_LIMIT + 1)])
    r = client.post("/api/decide/bulk", json={"add_audience": ["private"], "files": files})

    assert r.status_code == 400


def test_bulk_refuses_an_unknown_tier(client: TestClient,
                                      writable: Path) -> None:
    r = client.post("/api/decide/bulk", json={
        "add_audience": ["x" * 200], "files": _targets("a.jpg")})

    assert r.json()["written"] == 0
    assert not decisions.sidecar_path(writable / "a.jpg").exists()


def test_bulk_cannot_escape_the_tier(client: TestClient, writable: Path,
                                     tmp_path: Path) -> None:
    r = client.post("/api/decide/bulk", json={
        "tier": "none",
        "files": [{"folder": "../../..", "name": "escape.jpg"}]})

    assert r.json()["written"] == 0
    assert list(tmp_path.rglob("escape.jpg.xmp")) == []


def test_bulk_needs_the_same_auth_as_browsing(
    app_env: dict[str, Path], writable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(web.app)

    r = client.post("/api/decide/bulk", json={"tier": "none",
                                              "files": _targets("a.jpg")})

    assert r.status_code == 401
    assert not decisions.sidecar_path(writable / "a.jpg").exists()


# --- filtering ---------------------------------------------------------------

def test_the_bar_carries_every_filter(client: TestClient) -> None:
    """Filters are the address of what you are looking at; losing them 2,000
    thumbnails down is losing your place."""
    html = client.get("/browse").text
    for column in ("event", "year", "tag", "audience", "kind", "band"):
        assert f'"{column}"' in html


def test_a_view_is_a_link(client: TestClient) -> None:
    """In the URL rather than the page's memory, so it is shareable and the back
    button means the filter you had before."""
    assert client.get("/browse?kind=video").status_code == 200
    assert "b.mp4" in client.get("/browse?kind=video").text
    assert "a.jpg" not in client.get("/browse?kind=video").text


def test_filters_combine(client: TestClient) -> None:
    assert "Nothing matches" in client.get(
        "/browse?kind=video&event=Nope").text


def test_the_new_filter_is_the_unreviewed_ones(client: TestClient,
                                               writable: Path) -> None:
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["family"]})

    html = client.get("/browse?audience=new").text
    assert "a.jpg" not in html
    assert "b.mp4" in html


def test_a_video_shows_its_length_not_the_word_video(client: TestClient) -> None:
    """91 of the seeded year's clips report `0:00:38` rather than `38.0 s`."""
    assert "1:15" in client.get("/browse").text


# --- suggestions -------------------------------------------------------------

def test_suggestions_come_back_ranked(client: TestClient) -> None:
    got = client.get("/api/suggest?column=event").json()

    assert [s["value"] for s in got] == ["Italy - Sicily"]
    assert got[0]["scope"] == "all"


def test_suggestions_respect_the_current_view(client: TestClient,
                                              writable: Path) -> None:
    """Reaching for an event while looking at `tag:tv` should offer the events
    already used there first."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_tags": ["tv"]})

    got = client.get("/api/suggest?column=tag&event=Italy%20-%20Sicily").json()
    assert [(s["value"], s["scope"]) for s in got] == [("tv", "all")]


def test_an_unsuggestable_column_is_refused(client: TestClient) -> None:
    assert client.get("/api/suggest?column=camera").status_code == 400
    assert client.get("/api/suggest?column=folder").status_code == 400


def test_suggestions_need_auth(app_env: dict[str, Path],
                               monkeypatch: pytest.MonkeyPatch) -> None:
    assert TestClient(web.app).get(
        "/api/suggest?column=event").status_code == 401


# --- tagging -----------------------------------------------------------------

def test_a_tag_is_added_not_replaced(client: TestClient, writable: Path) -> None:
    """Tagging a selection of 200 files has to add to what each already carries."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_tags": ["beach"]})
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_tags": ["kids"]})

    assert r.json()["tags"] == ["beach", "kids"]
    stored = decisions.read(writable / "a.jpg")
    assert stored is not None and stored.tags == ("beach", "kids")


def test_a_tag_can_be_removed_across_a_selection(client: TestClient,
                                                 writable: Path) -> None:
    (writable / "b.mp4").write_bytes(b"fake")
    client.post("/api/decide/bulk", json={
        "add_tags": ["beach"], "files": _targets("a.jpg", "b.mp4")})
    r = client.post("/api/decide/bulk", json={
        "remove_tags": ["beach"], "files": _targets("a.jpg", "b.mp4")})

    assert r.json()["written"] == 2
    assert decisions.read(writable / "a.jpg") is None


def test_tagging_leaves_the_tier_alone(client: TestClient,
                                       writable: Path) -> None:
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["family"]})
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_tags": ["beach"]})

    assert r.json()["audience"] == ["family"]
    assert r.json()["tags"] == ["beach"]


def test_a_tagged_file_is_findable_by_that_tag(client: TestClient,
                                               writable: Path) -> None:
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_tags": ["beach"]})

    rows = client.get("/api/files?tag=beach").json()
    assert [r["name"] for r in rows] == ["a.jpg"]


# --- partial dates -----------------------------------------------------------

def test_a_year_only_date_moves_only_the_year(client: TestClient,
                                              writable: Path) -> None:
    """`1987` is a complete answer; inventing a month and day to store it would
    publish a precision nobody claimed."""
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg",
        "date_override": "1987-*-*-*:*:*"})

    assert r.json()["date_override"] == "1987-*-*-*:*:*"
    rows = client.get("/api/files?year=1987").json()
    assert [row["name"] for row in rows] == ["a.jpg"]
    assert rows[0]["effective_date"] == "1987-08-30-15:34:55"


def test_a_date_that_pins_nothing_is_refused(client: TestClient,
                                             writable: Path) -> None:
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg",
        "date_override": "*-*-*-*:*:*"})

    assert r.status_code == 400


# --- the grid drops what no longer belongs -----------------------------------

def test_dating_a_file_drops_it_from_the_undated_view(
    client: TestClient, writable: Path
) -> None:
    """The regression this exists to prevent: adding a date while filtered to
    undated left the file sitting in a view it no longer belonged to."""
    (writable / "b.mp4").write_bytes(b"fake")
    r = client.post("/api/decide/bulk?year=(undated)", json={
        "date_override": "1987-*-*-*:*:*", "files": _targets("b.mp4")})

    assert r.json()["written"] == 1
    assert r.json()["dropped"] == [{"folder": "init_2026", "name": "b.mp4"}]


def test_a_file_that_still_matches_is_not_dropped(client: TestClient,
                                                  writable: Path) -> None:
    r = client.post("/api/decide/bulk?event=Italy%20-%20Sicily", json={
        "add_audience": ["family"], "files": _targets("a.jpg")})

    assert r.json()["dropped"] == []


def test_deciding_drops_a_file_from_the_new_view(client: TestClient,
                                                 writable: Path) -> None:
    """Culling with the New filter on: each decision should take the file out."""
    r = client.post("/api/decide/bulk?audience=new", json={
        "add_audience": ["kids"], "files": _targets("a.jpg")})

    assert [d["name"] for d in r.json()["dropped"]] == ["a.jpg"]


def test_the_response_carries_the_new_total(client: TestClient,
                                            writable: Path) -> None:
    """So the header count stops claiming files the view no longer holds."""
    before = len(client.get("/api/files?audience=new").json())
    r = client.post("/api/decide/bulk?audience=new", json={
        "add_audience": ["kids"], "files": _targets("a.jpg")})

    assert r.json()["total"] == before - 1


def test_a_failed_file_is_not_reported_as_dropped(client: TestClient,
                                                  writable: Path) -> None:
    """Dropped means "written, and it left" — a file that was never written has
    not moved anywhere."""
    r = client.post("/api/decide/bulk?audience=new", json={
        "add_audience": ["kids"], "files": _targets("gone.jpg")})

    assert r.json()["written"] == 0
    assert r.json()["dropped"] == []


def test_an_empty_batch_reports_nothing_dropped(client: TestClient,
                                                writable: Path) -> None:
    r = client.post("/api/decide/bulk", json={"add_audience": ["private"], "files": []})

    assert r.json()["dropped"] == []


# --- access ------------------------------------------------------------------

@pytest.fixture
def household(app_env: dict[str, Path], writable: Path) -> dict[str, object]:
    """One shared photo and one that is not, with an admin and a viewer."""
    (writable / "b.mp4").write_bytes(b"fake")
    add_user("kid", "pw")
    admin = sign_in(accounts.ADMIN, "admin")
    admin.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["kid"]})
    return {"admin": admin, "kid": sign_in("kid", "pw")}


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
    """Not merely hidden — the endpoints refuse them. Showing a control that
    would fail reads as brokenness rather than as policy."""
    kid = cast(TestClient, household["kid"])
    html = kid.get("/browse").text

    assert '<button data-act="share"' not in html
    assert 'id="actions"' not in html


def test_the_admin_keeps_the_edit_controls(household: dict[str, object]) -> None:
    admin = cast(TestClient, household["admin"])
    html = admin.get("/browse").text

    assert '<button data-act="share"' in html


def test_granted_nothing_sees_nothing(app_env: dict[str, Path]) -> None:
    """An empty grant set is not the same as no restriction. Treating the two
    alike is the classic way an access check turns into an access grant."""
    conn = ix.open_ro(app_env["db"])

    assert ix.files(conn, ix.Filters(viewer=frozenset())) == []
    assert ix.count(conn, ix.Filters(viewer=frozenset())) == 0
    assert len(ix.files(conn, ix.Filters())) == 2


