"""The browse app (spec/nas-app.md §8).

It reads the index and serves the derived tiers. It never decodes anything —
`process` already made everything it displays. The one thing it writes is
curation decisions: an `.xmp` beside the master file, then that file's index row
— sidecar first, index follows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pix.nas import auth
from pix.nas import decisions
from pix.nas import index as ix
from pix.nas import web
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
    monkeypatch.delenv("PIX2_USERS", raising=False)
    return {"share": share, "thumb": thumb, "db": db}


@pytest.fixture
def master(app_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> Path:
    """A master folder holding the real files the media endpoint streams."""
    m = app_env["share"] / "master" / "init_2026"
    m.mkdir(parents=True, exist_ok=True)
    (m / "b.mp4").write_bytes(bytes([0, 0, 0, 0x18]) + b"ftypmp42" + b"x" * 400)
    monkeypatch.setattr(web, "MASTER_DIR", app_env["share"] / "master")
    return m


@pytest.fixture
def client(app_env: dict[str, Path]) -> TestClient:
    return TestClient(web.app)


# --- browse ------------------------------------------------------------------

def test_home_lists_events(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "Italy - Sicily" in r.text
    assert "2 files" in r.text


def test_home_warns_when_no_auth_is_configured(client: TestClient) -> None:
    """Running open is a deployment choice, not something to discover later."""
    assert "no auth configured" in client.get("/").text


def test_event_grid_shows_thumbnails(client: TestClient) -> None:
    r = client.get("/event/Italy%20-%20Sicily")
    assert r.status_code == 200
    assert "/thumb/init_2026/a.jpg" in r.text
    assert "/thumb/init_2026/b.mp4" in r.text


def test_video_cells_are_badged_with_duration(client: TestClient) -> None:
    """A grid of stills gives no hint which are clips."""
    assert "1:15" in client.get("/event/Italy%20-%20Sicily").text


def test_event_names_with_spaces_round_trip(client: TestClient) -> None:
    """Real events are 'Italy - Sicily', not slugs."""
    assert client.get("/event/Italy - Sicily").status_code == 200


def test_unknown_event_is_empty_not_an_error(client: TestClient) -> None:
    r = client.get("/event/Nope")
    assert r.status_code == 200
    assert "No files" in r.text


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
    rows = client.get("/api/events").json()
    assert rows[0]["event"] == "Italy - Sicily"
    assert rows[0]["n"] == 2


def test_api_files_filters_by_event(client: TestClient) -> None:
    rows = client.get("/api/files", params={"event": "Italy - Sicily"}).json()
    assert {r["name"] for r in rows} == {"a.jpg", "b.mp4"}


def test_healthz_needs_no_auth(app_env: dict[str, Path],
                               monkeypatch: pytest.MonkeyPatch) -> None:
    """Container Manager's probe cannot log in."""
    monkeypatch.setenv("PIX2_USERS", f"jim:{auth.hash_password('x')}")
    assert TestClient(web.app).get("/healthz").status_code == 200


# --- auth --------------------------------------------------------------------

def test_requests_are_refused_without_credentials(
    app_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PIX2_USERS", f"jim:{auth.hash_password('secret')}")
    r = TestClient(web.app).get("/")
    assert r.status_code == 401
    assert "Basic" in r.headers.get("www-authenticate", "")


def test_correct_credentials_are_accepted(
    app_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PIX2_USERS", f"jim:{auth.hash_password('secret')}")
    r = TestClient(web.app).get("/", auth=("jim", "secret"))
    assert r.status_code == 200


def test_a_wrong_password_is_refused(
    app_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PIX2_USERS", f"jim:{auth.hash_password('secret')}")
    assert TestClient(web.app).get("/", auth=("jim", "wrong")).status_code == 401


def test_an_unknown_user_is_refused(
    app_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PIX2_USERS", f"jim:{auth.hash_password('secret')}")
    assert TestClient(web.app).get("/", auth=("eve", "secret")).status_code == 401


# --- index not built ---------------------------------------------------------

def test_a_missing_index_says_what_to_run(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """'503' is useless on its own; the fix is one command."""
    monkeypatch.setattr(web, "DB_PATH", tmp_path / "nope.db")
    monkeypatch.delenv("PIX2_USERS", raising=False)

    r = TestClient(web.app).get("/")

    assert r.status_code == 503
    assert "pix2 index" in r.text


# --- video playback ----------------------------------------------------------

def test_master_video_is_streamable(master: Path, app_env: dict[str, Path]) -> None:
    """Videos need the master file: there is no delivery rendition to serve.

    The seeded library is MP4 throughout, so master *is* the playable copy.
    """
    r = TestClient(web.app).get("/media/init_2026/b.mp4")

    assert r.status_code == 200
    assert r.content.startswith(bytes([0, 0, 0, 0x18]) + b"ftyp")


def test_media_advertises_range_support(master: Path,
                                        app_env: dict[str, Path]) -> None:
    """Without ranges a browser cannot seek, only play from the start."""
    r = TestClient(web.app).get("/media/init_2026/b.mp4")
    assert r.headers.get("accept-ranges") == "bytes"


def test_media_serves_a_byte_range(master: Path, app_env: dict[str, Path]) -> None:
    r = TestClient(web.app).get("/media/init_2026/b.mp4",
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
    assert TestClient(web.app).get("/media/init_2026/nope.mp4").status_code == 404


def test_grid_marks_which_cells_are_video(client: TestClient) -> None:
    """The viewer picks <video> or <img> from this, so it has to be present."""
    html = client.get("/event/Italy - Sicily").text
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
        "folder": "init_2026", "name": "a.jpg", "tier": "top"})

    assert r.status_code == 200
    assert r.json()["tier"] == "top"
    assert r.json()["indexed"] is True
    assert decisions.read(writable / "a.jpg") == Decision(tier="top")

    conn = ix.open_ro(app_env["db"])
    row = conn.execute("SELECT * FROM files WHERE name = 'a.jpg'").fetchone()
    assert (row["tier"], row["has_sidecar"]) == ("top", 1)


def test_the_index_follows_rather_than_leads(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Sidecar first (§4): the row must never carry a decision that is not on
    disk, so a refresh failure still leaves the record written."""
    conn = ix.open_ro(app_env["db"])
    before = conn.execute("SELECT tier FROM files WHERE name = 'a.jpg'").fetchone()
    conn.close()
    assert before["tier"] is None

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
        "folder": "init_2026", "name": "a.jpg", "tier": "top"})

    assert decisions.read(writable / "a.jpg") == Decision(
        tier="top", event="Sicily Trip")


def test_null_clears_where_omission_does_not(client: TestClient,
                                             writable: Path) -> None:
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "tier": "top",
        "event": "Sicily Trip"})
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "event": None})

    assert r.json()["event"] is None
    assert decisions.read(writable / "a.jpg") == Decision(tier="top")


def test_clearing_everything_marks_it_unreviewed_again(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "tier": "top"})
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "tier": None})

    assert r.json()["has_sidecar"] is False
    assert not decisions.sidecar_path(writable / "a.jpg").exists()

    conn = ix.open_ro(app_env["db"])
    row = conn.execute("SELECT * FROM files WHERE name = 'a.jpg'").fetchone()
    assert (row["tier"], row["has_sidecar"]) == (None, 0)


def test_an_unknown_tier_is_rejected(client: TestClient, writable: Path) -> None:
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "tier": "five-stars"})

    assert r.status_code == 400
    assert not decisions.sidecar_path(writable / "a.jpg").exists()


def test_traversal_cannot_drop_a_sidecar_off_the_tier(
    client: TestClient, writable: Path, tmp_path: Path
) -> None:
    """Body components are as untrusted as URL ones, and this endpoint writes."""
    r = client.post("/api/decide", json={
        "folder": "../../..", "name": "escape.jpg", "tier": "top"})

    assert r.status_code == 400
    assert list(tmp_path.rglob("escape.jpg.xmp")) == []


def test_a_file_not_in_master_is_refused(client: TestClient,
                                         writable: Path) -> None:
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "nothere.jpg", "tier": "top"})

    assert r.status_code == 404


def test_a_sidecar_is_not_a_decidable_file(client: TestClient,
                                           writable: Path) -> None:
    decisions.write(writable / "a.jpg", Decision(tier="top"))
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg.xmp", "tier": "photo"})

    assert r.status_code == 400
    assert not (writable / "a.jpg.xmp.xmp").exists()


def test_deciding_needs_the_same_auth_as_browsing(
    app_env: dict[str, Path], writable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write endpoint must not be the hole in the auth wall."""
    monkeypatch.setenv("PIX2_USERS", f"james:{auth.hash_password('pw')}")
    client = TestClient(web.app)

    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "tier": "top"})

    assert r.status_code == 401
    assert not decisions.sidecar_path(writable / "a.jpg").exists()


def test_there_is_no_reindex_endpoint(client: TestClient) -> None:
    """A rebuild reads ~62k records and would block the single worker for
    minutes, at the request of anyone holding the URL."""
    assert client.post("/api/reindex").status_code == 404
