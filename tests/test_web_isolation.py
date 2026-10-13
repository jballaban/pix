"""What somebody may not see stays unseen — not in a listing, not behind a
URL, and not as a count, a name or a stack badge about it either.

Two promises, and these tests hold the app to both from every side a page or
a script can reach: **an archived file is in nobody's view but an
administrator's**, whatever else its sidecar says; and **a file not shared
with somebody is not there**, to them, under any circumstance.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient
from web_helpers import administrator, three_files

from pix.nas import accounts, decisions, index as ix
from pix.nas.decisions import Decision


def _arrange(app_env: dict[str, Path], writable: Path,
             **sidecars: Decision) -> None:
    """Write sidecars directly — past the app's own rules, the way something
    else on the share could — and rebuild the index from them."""
    for name, decision in sidecars.items():
        decisions.write(writable / name.replace("_", "."), decision)
    share = app_env["share"]
    ix.build(app_env["db"], meta_dir=share / "meta",
             master_dir=share / "master")


def _under(path: Path) -> str | None:
    got = decisions.read(path)
    return got.stacked_under if got is not None else None


def _kid(sign_in: Callable[[str, str], TestClient],
         add_user: Callable[..., None]) -> TestClient:
    add_user("kid", "pw", ("family",))
    return sign_in("kid", "pw")


ALL_BYTES = ("/thumb", "/large", "/preview", "/strip", "/media",
             "/download", "/api/file")


# --- archived ---------------------------------------------------------------

def test_archived_and_shared_is_still_archived(
        app_env: dict[str, Path], writable: Path,
        sign_in: Callable[[str, str], TestClient],
        add_user: Callable[..., None]) -> None:
    """The app never writes *archived and shared* — archiving clears every
    other audience — but a sidecar is a file on a share, and one written by
    something else can say both. Archived wins, everywhere."""
    _arrange(app_env, writable,
             a_jpg=Decision(audience=(decisions.ARCHIVED, "kid", "family")))
    kid = _kid(sign_in, add_user)

    assert kid.get("/api/files").json() == []
    assert kid.get("/api/files?audience=archived").json() == []
    assert kid.get("/api/events").json() == []
    for route in ALL_BYTES:
        assert kid.get(f"{route}/init_2026/a.jpg").status_code == 404, route
    r = kid.post("/download.zip", data={
        "files": '[{"folder": "init_2026", "name": "a.jpg"}]'})
    assert r.status_code == 404
    for column in ("event", "date", "tag", "audience", "kind"):
        # A fixed list (kinds) is offered with its counts; every one is zero.
        got = kid.get(f"/api/suggest?column={column}").json()
        assert all(s["n"] == 0 for s in got), (column, got)
    assert "a.jpg" not in kid.get("/browse?audience=archived").text


def test_archived_is_never_granted_by_name(
        app_env: dict[str, Path], writable: Path,
        sign_in: Callable[[str, str], TestClient],
        add_user: Callable[..., None]) -> None:
    """A login or group called `archived`, edited into `users.json` by hand,
    would be granted exactly the files nobody is meant to see."""
    _arrange(app_env, writable, a_jpg=Decision(audience=(decisions.ARCHIVED,)))
    add_user("archived", "pw")
    add_user("kid", "pw", (decisions.ARCHIVED,))

    assert decisions.ARCHIVED not in accounts.load().grants("kid")
    assert decisions.ARCHIVED not in accounts.load().grants("archived")
    for who in ("archived", "kid"):
        client = sign_in(who, "pw")
        assert client.get("/api/files").json() == [], who
        assert client.get("/thumb/init_2026/a.jpg").status_code == 404, who


def test_an_archived_stack_top_is_not_named(
        app_env: dict[str, Path], writable: Path,
        sign_in: Callable[[str, str], TestClient],
        add_user: Callable[..., None]) -> None:
    """A shared take behind an archived top stands on its own: it is not
    folded out of sight, and nothing on it names the top."""
    three_files(writable, app_env)
    _arrange(app_env, writable,
             c_jpg=Decision(audience=(decisions.ARCHIVED,)),
             d_jpg=Decision(audience=("kid",),
                            stacked_under="init_2026/c.jpg"))
    kid = _kid(sign_in, add_user)

    rows = kid.get("/api/files").json()
    assert [r["name"] for r in rows] == ["d.jpg"]
    assert rows[0]["stacked_under"] is None
    html = kid.get("/browse").text
    assert "c.jpg" not in html
    assert kid.get("/stack/init_2026/c.jpg").status_code == 404
    assert kid.get("/api/behind/init_2026/c.jpg").status_code == 404


# --- deleted ----------------------------------------------------------------

def test_a_viewer_never_reaches_the_bin(
        app_env: dict[str, Path], writable: Path,
        sign_in: Callable[[str, str], TestClient],
        add_user: Callable[..., None]) -> None:
    _arrange(app_env, writable,
             a_jpg=Decision(audience=("kid",), deleted=True))
    kid = _kid(sign_in, add_user)

    for q in ("", "?deleted=only", "?deleted=with", "?deleted=gone"):
        assert kid.get("/api/files" + q).json() == [], q
    assert kid.get("/api/suggest?column=deleted").json() == []
    assert kid.get("/thumb/init_2026/a.jpg").status_code == 404


def test_the_index_holds_the_bin_shut_on_its_own(
        app_env: dict[str, Path], writable: Path) -> None:
    """Not only the address: a `Filters` built anywhere with a viewer in it
    gets the living, whatever it asks for."""
    _arrange(app_env, writable,
             a_jpg=Decision(audience=("kid",), deleted=True))
    conn = ix.open_ro(app_env["db"])
    viewer = frozenset({"kid"})

    for side in ("only", "with"):
        assert ix.files(conn, ix.Filters(viewer=viewer, deleted=side)) == []
        assert ix.count(conn, ix.Filters(viewer=viewer, deleted=side)) == 0
    assert ix.count(conn, ix.Filters(deleted="only")) == 1


# --- stacks -----------------------------------------------------------------

def test_a_stack_counts_only_what_the_viewer_may_see(
        app_env: dict[str, Path], writable: Path,
        sign_in: Callable[[str, str], TestClient],
        add_user: Callable[..., None]) -> None:
    """A badge reading *3* over a photograph whose two other takes were
    never shared is a way of being told there are two more."""
    three_files(writable, app_env)
    _arrange(app_env, writable,
             a_jpg=Decision(audience=("kid",)),
             c_jpg=Decision(stacked_under="init_2026/a.jpg"),
             d_jpg=Decision(audience=(decisions.ARCHIVED, "kid"),
                            stacked_under="init_2026/a.jpg"))
    kid = _kid(sign_in, add_user)

    rows = kid.get("/api/files").json()
    assert [(r["name"], r["behind"]) for r in rows] == [("a.jpg", 0)]
    html = kid.get("/browse").text
    assert 'data-behind="0"' in html
    assert "c.jpg" not in html and "d.jpg" not in html
    # And the administrator's count is untouched.
    admin = administrator(sign_in)
    top = [r for r in admin.get("/api/files").json() if r["name"] == "a.jpg"]
    assert top[0]["behind"] == 2


def test_a_take_behind_an_unshared_top_stands_on_its_own(
        app_env: dict[str, Path], writable: Path,
        sign_in: Callable[[str, str], TestClient],
        add_user: Callable[..., None]) -> None:
    three_files(writable, app_env)
    _arrange(app_env, writable,
             c_jpg=Decision(stacked_under="init_2026/a.jpg"),
             d_jpg=Decision(audience=("kid",),
                            stacked_under="init_2026/a.jpg"))
    kid = _kid(sign_in, add_user)

    rows = kid.get("/api/files").json()
    assert [(r["name"], r["stacked_under"]) for r in rows] == [("d.jpg", None)]
    assert "a.jpg" not in kid.get("/browse?group=stack").text
    assert kid.get("/stack/init_2026/a.jpg").status_code == 404
    assert kid.get("/api/behind/init_2026/a.jpg").status_code == 404


def test_stacking_onto_an_unshared_file_is_refused_as_missing(
        app_env: dict[str, Path], writable: Path,
        sign_in: Callable[[str, str], TestClient],
        add_user: Callable[..., None]) -> None:
    """The file a stack is made onto is named in the change, not chosen from
    the grid — so it has to be checked like one, or it is a way to ask about
    any name and to write to it."""
    three_files(writable, app_env)
    _arrange(app_env, writable,
             a_jpg=Decision(stacked_under="init_2026/d.jpg"),
             c_jpg=Decision(audience=("kid",)))
    kid = _kid(sign_in, add_user)

    for route, body in (
            ("/api/decide", {"folder": "init_2026", "name": "c.jpg",
                             "stacked_under": "init_2026/a.jpg"}),
            ("/api/decide/bulk", {"files": [{"folder": "init_2026",
                                             "name": "c.jpg"}],
                                  "stacked_under": "init_2026/a.jpg"}),
            ("/api/decide/bulk", {"files": [{"folder": "init_2026",
                                             "name": "c.jpg"}],
                                  "stacked_under": "init_2026/nope.jpg"})):
        r = kid.post(route, json=body)
        assert r.status_code == 404, (route, r.text)
    # Nothing was written: a is still behind d, c behind nothing.
    assert _under(writable / "a.jpg") == "init_2026/d.jpg"
    assert _under(writable / "c.jpg") is None


def test_a_reply_names_only_the_files_that_were_theirs(
        app_env: dict[str, Path], writable: Path,
        sign_in: Callable[[str, str], TestClient],
        add_user: Callable[..., None]) -> None:
    """Moving a stack moves all of it — members not shared with whoever
    moved it follow the top, or they would be stranded — but the reply's
    *these left the view* lists only what they could see, and the bin count
    is an administrator's."""
    three_files(writable, app_env)
    _arrange(app_env, writable,
             a_jpg=Decision(audience=("kid",)),
             c_jpg=Decision(audience=("kid",)),
             d_jpg=Decision(stacked_under="init_2026/a.jpg"))
    kid = _kid(sign_in, add_user)

    r = kid.post("/api/decide/bulk", json={
        "files": [{"folder": "init_2026", "name": "a.jpg"}],
        "stacked_under": "init_2026/c.jpg"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert "d.jpg" not in r.text
    assert [d["name"] for d in out["dropped"]] == ["a.jpg"]
    assert out["binned"] is None
    # The stack held together: d followed its top.
    assert _under(writable / "d.jpg") == "init_2026/c.jpg"


# --- clips ------------------------------------------------------------------

def test_cuts_of_an_unseen_source_are_not_gathered(
        app_env: dict[str, Path], writable: Path) -> None:
    """*Cut from X* answers *was this cut from that?* for any name somebody
    cared to type. To a viewer who may not see X, there is no such view."""
    conn = ix.open_ro(app_env["db"])
    viewer = frozenset({"kid"})

    # Whatever the library holds, the cuts of a source the viewer cannot see
    # list nothing — the clause itself requires the source be seen.
    assert ix.files(conn, ix.Filters(viewer=viewer,
                                     cuts="init_2026/b.mp4")) == []


# --- the operation log ------------------------------------------------------

def test_a_link_into_the_log_is_an_administrators(
        household: dict[str, object]) -> None:
    """An operation's summary names what was done to files the viewer may not
    see, and its id can be one the page chose."""
    admin = cast(TestClient, household["admin"])
    kid = cast(TestClient, household["kid"])
    admin.post("/api/decide/bulk", json={
        "files": [{"folder": "init_2026", "name": "b.mp4"}],
        "add_tags": ["secret-trip"], "batch": "known-id"})

    html = kid.get("/browse?op=known-id").text
    assert "secret-trip" not in html
    assert 'class="chip on from-op"' not in html


# --- caching ----------------------------------------------------------------

def test_a_picture_is_cached_for_the_session_that_fetched_it(
        household: dict[str, object]) -> None:
    """Nothing between here and the browser keeps a copy, and the browser's
    own is good only while the same sign-in holds."""
    kid = cast(TestClient, household["kid"])
    r = kid.get("/thumb/init_2026/a.jpg")

    if r.status_code == 200:
        assert "public" not in r.headers["cache-control"]
        assert "private" in r.headers["cache-control"]
        assert "cookie" in r.headers.get("vary", "").lower()
