"""Writing decisions: one file and many, the keep pass, tags, stacks and folders, what a household member may write, and archiving.

Split out of the one web test module, by area.
"""

from __future__ import annotations

import io
import json
import pytest
import re
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient
from web_helpers import (
    as_columns,
    burst,
    pix,
    relative,
    render_for,
    stacked,
    targets,
    three_files,
)

from pix.nas import accounts, decisions, history, index as ix, web
from pix.nas.decisions import Decision
from pix.nas.webapp import (
    pages as w_pages,
    permissions as w_permissions,
    vocab as w_vocab,
    writes as w_writes,
)
from pix.nas.webapp.text import split as _split


# --- curation -------------------------------------------------------------

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


# --- the keep pass --------------------------------------------------------

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
    assert '<button data-act="access"' in html
    assert 'id="selall"' in html


def test_bulk_writes_one_decision_across_a_selection(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Finishing an event is a few hundred sidecar writes from one click."""
    (writable / "b.mp4").write_bytes(b"fake")
    r = client.post("/api/decide/bulk", json={
        "add_audience": ["private"], "files": targets("a.jpg", "b.mp4")})

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
        "event": "France Trip", "files": targets("a.jpg")})

    assert r.json()["written"] == 1
    assert decisions.read(writable / "a.jpg") == Decision(event="France Trip")


def test_bulk_leaves_untouched_fields_alone(client: TestClient,
                                            writable: Path) -> None:
    """Finishing an event must not wipe the events people already assigned."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "event": "Sicily Trip"})
    client.post("/api/decide/bulk", json={"add_audience": ["private"],
                                          "files": targets("a.jpg")})

    assert decisions.read(writable / "a.jpg") == Decision(
        audience=("private",), event="Sicily Trip")


def test_one_bad_file_does_not_lose_the_rest(client: TestClient,
                                             writable: Path) -> None:
    """Partial success is the normal outcome over SMB. Failing the whole batch
    would leave the curator unsure what landed."""
    r = client.post("/api/decide/bulk", json={
        "add_audience": ["private"], "files": targets("a.jpg", "gone.jpg")})

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
    files = targets(*[f"f{n}.jpg" for n in range(w_vocab.BULK_LIMIT + 1)])
    r = client.post("/api/decide/bulk", json={"add_audience": ["private"], "files": files})

    assert r.status_code == 400


def test_bulk_refuses_an_unknown_tier(client: TestClient,
                                      writable: Path) -> None:
    r = client.post("/api/decide/bulk", json={
        "add_audience": ["x" * 200], "files": targets("a.jpg")})

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
                                              "files": targets("a.jpg")})

    assert r.status_code == 401
    assert not decisions.sidecar_path(writable / "a.jpg").exists()


# --- tagging --------------------------------------------------------------

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
        "add_tags": ["beach"], "files": targets("a.jpg", "b.mp4")})
    r = client.post("/api/decide/bulk", json={
        "remove_tags": ["beach"], "files": targets("a.jpg", "b.mp4")})

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


# --- taking a copy away ---------------------------------------------------

def test_a_download_is_named_by_its_date_not_its_path(
    client: TestClient, writable: Path
) -> None:
    """A seeded master is named for the old library's path — a folder, an
    event, sometimes a person — so none of it goes out with the copy."""
    r = client.get("/download/init_2026/a.jpg")

    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert "2026-08-30_153455.jpg" in r.headers["content-disposition"]
    assert "a.jpg" not in r.headers["content-disposition"]


def test_a_file_with_no_date_is_named_for_nothing_about_it(
    client: TestClient, writable: Path
) -> None:
    (writable / "b.mp4").write_bytes(b"clip")

    r = client.get("/download/init_2026/b.mp4")

    assert 'filename="pix_01234567.mp4"' in r.headers["content-disposition"]


def test_a_clip_downloads_as_the_copy_that_plays(
    client: TestClient, writable: Path
) -> None:
    """The H.264 rendition, because what you want from *download* is a file
    that opens — and the original is the one a browser would not play, which
    is why the copy exists at all."""
    (writable / "b.mp4").write_bytes(b"hevc original")
    render_for("b.mp4")

    r = client.get("/download/init_2026/b.mp4")

    assert r.content == b"h264 copy"


def test_the_original_can_be_asked_for(
    client: TestClient, writable: Path
) -> None:
    """Master is the archive. Anything that hands out *the file* has to be
    able to hand out that one."""
    (writable / "b.mp4").write_bytes(b"hevc original")
    render_for("b.mp4")

    r = client.get("/download/init_2026/b.mp4?original=1")

    assert r.content == b"hevc original"


def test_a_clip_with_no_copy_downloads_as_itself(
    client: TestClient, writable: Path
) -> None:
    """A third of the clips here were already H.264 and were never rendered."""
    (writable / "b.mp4").write_bytes(b"already h264")

    assert client.get("/download/init_2026/b.mp4").content == b"already h264"


def test_a_download_says_what_kind_of_file_it_is(
    client: TestClient, writable: Path
) -> None:
    """A phone will only offer *Save to Photos* for something it has been told
    is a photograph. Everything went out as `application/octet-stream`, which
    is a file the share sheet can only put in Files."""
    (writable / "b.mp4").write_bytes(b"clip")

    assert client.get("/download/init_2026/a.jpg"
                      ).headers["content-type"] == "image/jpeg"
    assert client.get("/download/init_2026/b.mp4"
                      ).headers["content-type"] == "video/mp4"


def test_a_named_type_is_still_an_attachment(
    client: TestClient, writable: Path
) -> None:
    """The reason it was safe to stop lying about the type: the disposition is
    what makes a browser download rather than display, and it outranks the
    type everywhere. A desktop download is unchanged."""
    r = client.get("/download/init_2026/a.jpg")

    assert r.headers["content-type"] == "image/jpeg"
    assert "attachment" in r.headers["content-disposition"]


def test_a_file_nothing_has_an_opinion_about_stays_unnamed(
    client: TestClient, writable: Path
) -> None:
    """An `.insv` is not a type any phone knows. Claiming one would be worse
    than admitting there is nothing useful to say."""
    (writable / "c.insv").write_bytes(b"360")

    assert client.get("/download/init_2026/c.insv"
                      ).headers["content-type"] == "application/octet-stream"


def test_a_viewer_cannot_download_what_was_not_shared(
    client: TestClient, writable: Path,
    sign_in: "Callable[[str, str], TestClient]",
    add_user: "Callable[..., None]"
) -> None:
    """The same check as every other route that serves bytes. A grid that
    omits a photograph while this hands it over is not access control."""
    (writable / "b.mp4").write_bytes(b"fake")
    add_user("kid", "pw")
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["kid"]})
    kid = sign_in("kid", "pw")

    assert kid.get("/download/init_2026/a.jpg").status_code == 200
    assert kid.get("/download/init_2026/b.mp4").status_code == 404


def test_a_selection_comes_back_as_one_zip(
    client: TestClient, writable: Path
) -> None:
    """A browser cannot be asked to start two hundred downloads at once, and
    a folder of files is what you wanted anyway."""
    (writable / "b.mp4").write_bytes(b"a clip")

    r = client.post("/download.zip", data={"files": json.dumps([
        {"folder": "init_2026", "name": "a.jpg"},
        {"folder": "init_2026", "name": "b.mp4"}])})

    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert ".zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert zf.namelist() == ["2026-08-30_153455.jpg", "pix_01234567.mp4"]
        assert zf.read("pix_01234567.mp4") == b"a clip"


def test_a_zip_is_flat_and_numbers_what_would_collide(
    client: TestClient, writable: Path
) -> None:
    """A folder inside the zip would hand out the master folder's name. Flat,
    two photographs from the same second would be one file — so the second
    is numbered."""
    r = client.post("/download.zip", data={"files": json.dumps([
        {"folder": "init_2026", "name": "a.jpg"},
        {"folder": "init_2026", "name": "a.jpg"}])})

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert zf.namelist() == ["2026-08-30_153455.jpg",
                                 "2026-08-30_153455_2.jpg"]


def test_a_zip_is_stored_rather_than_deflated(
    client: TestClient, writable: Path
) -> None:
    """Every file in here is already compressed, so deflating spends the
    processor to save nothing on the one path where throughput is the whole
    experience."""
    r = client.post("/download.zip", data={"files": json.dumps([
        {"folder": "init_2026", "name": "a.jpg"}])})

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert zf.infolist()[0].compress_type == zipfile.ZIP_STORED


def test_a_zip_of_copies_or_of_originals(
    client: TestClient, writable: Path
) -> None:
    (writable / "b.mp4").write_bytes(b"hevc original")
    render_for("b.mp4")
    files = json.dumps([{"folder": "init_2026", "name": "b.mp4"}])

    copies = client.post("/download.zip", data={"files": files})
    with zipfile.ZipFile(io.BytesIO(copies.content)) as zf:
        assert zf.read(zf.namelist()[0]) == b"h264 copy"

    originals = client.post("/download.zip",
                            data={"files": files, "original": "1"})
    with zipfile.ZipFile(io.BytesIO(originals.content)) as zf:
        assert zf.read(zf.namelist()[0]) == b"hevc original"


def test_a_viewer_cannot_zip_what_was_not_shared(
    client: TestClient, writable: Path,
    sign_in: "Callable[[str, str], TestClient]",
    add_user: "Callable[..., None]"
) -> None:
    """Asking for a hundred files is not a way round the check that is made
    for one."""
    (writable / "b.mp4").write_bytes(b"fake")
    add_user("kid", "pw")
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["kid"]})

    r = sign_in("kid", "pw").post("/download.zip", data={"files": json.dumps([
        {"folder": "init_2026", "name": "a.jpg"},
        {"folder": "init_2026", "name": "b.mp4"}])})

    assert r.status_code == 404


def test_too_many_files_are_refused_rather_than_started(
    client: TestClient, writable: Path
) -> None:
    """A selection runs to thousands, and a download nobody meant to start is
    one nobody can stop without noticing it is running."""
    many = json.dumps([{"folder": "init_2026", "name": "a.jpg"}]
                      * (w_vocab.ZIP_LIMIT + 1))

    r = client.post("/download.zip", data={"files": many})

    assert r.status_code == 400
    assert str(w_vocab.ZIP_LIMIT) in r.text


def test_a_cell_says_whether_a_copy_of_it_exists(
    client: TestClient, writable: Path
) -> None:
    """So the page asks *original or copy* only where there is an answer, and
    downloads without asking everywhere else."""
    (writable / "b.mp4").write_bytes(b"fake")

    plain = client.get("/browse").text
    assert 'data-copy=""' in plain
    assert 'data-copy="1"' not in plain

    render_for("b.mp4")
    with_copy = client.get("/browse").text
    assert 'data-copy="1"' in with_copy


# --- what the household may write -----------------------------------------

def test_a_household_member_can_tag_what_was_shared_with_them(
    household: dict[str, object], writable: Path
) -> None:
    """The family curates (§8). This is the whole reason the bar opened."""
    kid = cast(TestClient, household["kid"])

    r = kid.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg",
        "add_tags": ["beach"], "add_people": ["Mom"]})

    assert r.status_code == 200
    after = decisions.read(writable / "a.jpg")
    assert after is not None
    assert after.tags == ("beach",) and after.people == ("Mom",)


def test_a_household_member_cannot_share_anything(
    household: dict[str, object], writable: Path
) -> None:
    """The one control that can show a photograph to somebody who should not
    see it. Refused by the endpoint, not by the absence of a button — a button
    is a suggestion and anyone can post."""
    kid = cast(TestClient, household["kid"])

    r = kid.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["kid"]})

    assert r.status_code == 403
    after = decisions.read(writable / "a.jpg")
    assert after is not None and after.audience == ("kid",), "audience changed"


def test_a_household_member_cannot_purge(
    household: dict[str, object]
) -> None:
    """It ends the file. There is no undo anywhere for it."""
    kid = cast(TestClient, household["kid"])

    r = kid.post("/api/purge", json={
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    assert r.status_code in (401, 403)


def test_a_household_member_can_delete_but_not_bring_it_back(
    household: dict[str, object], writable: Path
) -> None:
    """Deleting is soft and theirs; undeleting is not, because `/history` is
    not. Not refused by a missing field — the deleted are in nobody's view but
    an administrator's, so there is nothing for them to name."""
    kid = cast(TestClient, household["kid"])

    assert kid.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "deleted": True
    }).status_code == 200
    after = decisions.read(writable / "a.jpg")
    assert after is not None and after.deleted

    back = kid.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "deleted": False})

    assert back.status_code == 404, "a viewer restored a file out of the bin"
    still = decisions.read(writable / "a.jpg")
    assert still is not None and still.deleted


def test_a_household_member_cannot_edit_what_was_not_shared(
    household: dict[str, object], writable: Path
) -> None:
    """The check the route never needed while it was an administrator's. Every
    field they send is one they may write — it is the *file* that is not
    theirs, and naming it by hand is the whole attack."""
    kid = cast(TestClient, household["kid"])

    r = kid.post("/api/decide", json={
        "folder": "init_2026", "name": "b.mp4", "add_tags": ["mine"]})

    assert r.status_code == 404
    assert decisions.read(writable / "b.mp4") is None


def test_a_bulk_edit_reaches_only_what_was_shared(
    household: dict[str, object], writable: Path
) -> None:
    """Sent as one request naming both, which is what a page could never do —
    and exactly why the check cannot live in the page."""
    kid = cast(TestClient, household["kid"])

    r = kid.post("/api/decide/bulk", json={
        "add_tags": ["mine"],
        "files": [{"folder": "init_2026", "name": "a.jpg"},
                  {"folder": "init_2026", "name": "b.mp4"}]})

    assert r.status_code == 200
    assert r.json()["written"] == 1
    assert decisions.read(writable / "b.mp4") is None
    mine = decisions.read(writable / "a.jpg")
    assert mine is not None and mine.tags == ("mine",)


def test_a_bulk_edit_cannot_share_either(
    household: dict[str, object], writable: Path
) -> None:
    """The field check runs once for the batch, before any of it is written —
    a refusal that wrote the first two hundred would not be one."""
    kid = cast(TestClient, household["kid"])

    r = kid.post("/api/decide/bulk", json={
        "add_audience": ["kid"],
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    assert r.status_code == 403


def test_the_bar_and_the_endpoint_read_the_same_table() -> None:
    """Two lists would be two things to forget. Every action a household
    member is offered writes only fields they may write."""
    for act in w_permissions.HOUSEHOLD:
        for wrote in w_permissions.ACT_WRITES[act]:
            assert wrote in w_permissions.HOUSEHOLD_FIELDS, f"{act} writes {wrote}"
    # And the two that are withheld are withheld for a field, not by omission.
    assert "audience" not in w_permissions.HOUSEHOLD_FIELDS
    assert "access" not in w_permissions.HOUSEHOLD and "purge" not in w_permissions.HOUSEHOLD


def test_a_household_member_can_see_inside_a_stack(
    app_env: dict[str, Path], writable: Path,
    sign_in: "Callable[[str, str], TestClient]",
    add_user: "Callable[..., None]"
) -> None:
    """A bare `Filters` also hides whatever is stacked behind another file,
    which is a rule about what a *grid* shows and not about who may see what.

    Every file inside a stack answered 404 to a household member: they could
    open a stack and get a wall of broken thumbnails. An administrator has no
    scope and so never came through the check at all, which is why it went
    unnoticed — the one person who could not reproduce it was the only one
    looking.
    """
    burst(app_env, writable, "x.jpg", "y.jpg")
    add_user("kid", "pw")
    admin = sign_in(accounts.ADMIN, "admin")
    admin.post("/api/decide/bulk", json={
        "add_audience": ["kid"],
        "files": [{"folder": "init_2026", "name": "x.jpg"},
                  {"folder": "init_2026", "name": "y.jpg"}]})
    admin.post("/api/decide", json={
        "folder": "init_2026", "name": "y.jpg",
        "stacked_under": "init_2026/x.jpg"})
    kid = sign_in("kid", "pw")

    # Folded away in the grid, as a stacked file should be...
    assert [r["name"] for r in kid.get("/api/files").json()] == ["x.jpg"]
    # ...and still theirs to open, which is a different question entirely.
    assert kid.get("/api/file/init_2026/y.jpg").status_code == 200


def test_a_household_member_can_reach_every_action_they_are_offered(
    app_env: dict[str, Path], writable: Path,
    sign_in: "Callable[[str, str], TestClient]",
    add_user: "Callable[..., None]"
) -> None:
    """A button nothing can reach is worse than either answer.

    *Not a stack* refuses one of the app's suggestions, and with `firm` forced
    there was never a suggestion on screen to refuse — the control sat on the
    bar and could not be used. So the filter that makes a guess fold is on
    their bar too, and only the default still differs.
    """
    burst(app_env, writable, "x.jpg", "y.jpg")
    add_user("kid", "pw")
    sign_in(accounts.ADMIN, "admin").post("/api/decide/bulk", json={
        "add_audience": ["kid"],
        "files": [{"folder": "init_2026", "name": "x.jpg"},
                  {"folder": "init_2026", "name": "y.jpg"}]})
    kid = sign_in("kid", "pw")

    html = kid.get("/browse?stacks=guesses").text
    assert 'data-act="nostack"' in html
    # A suggestion is on screen, folded, and says how many are behind it.
    assert 'data-behind="1"' in html or 'class="stack' in html

    refused = kid.post("/api/decide", json={
        "folder": "init_2026", "name": "x.jpg", "no_stack": True})

    assert refused.status_code == 200
    after = decisions.read(writable / "x.jpg")
    assert after is not None and after.no_stack


# --- a decision about a stack is a decision about the stack ---------------

def test_tagging_a_stack_tags_every_take_in_it(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """A stack shows one photograph and hides the rest so that it works as
    though there is one file. A tag that reached only the one on top would
    make that false the moment anybody took it apart."""
    stacked(client, app_env, writable)

    client.post("/api/decide/bulk", json={
        "add_tags": ["blah"],
        "files": [{"folder": "init_2026", "name": "x.jpg"}]})

    behind = decisions.read(writable / "y.jpg")
    assert behind is not None and behind.tags == ("blah",)


def test_sharing_a_stack_survives_taking_it_apart(
    client: TestClient, writable: Path, app_env: dict[str, Path],
    sign_in: "Callable[[str, str], TestClient]",
    add_user: "Callable[..., None]"
) -> None:
    """The scenario this was found by. Share a stack, unstack it later, and
    what was shared must still be what is there — before, the audience reached
    the one photograph on top and unstacking produced files nobody could see.
    """
    stacked(client, app_env, writable)
    add_user("kid", "pw")

    client.post("/api/decide/bulk", json={
        "add_audience": ["kid"],
        "files": [{"folder": "init_2026", "name": "x.jpg"}]})
    # Taken apart afterwards, by the owner.
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "y.jpg", "stacked_under": None})

    kid = sign_in("kid", "pw")
    # Asked flat, because these two are also a *guess* — the app proposed
    # them as one moment — and the ordinary view folds a guess. What is
    # being checked here is who may see them, not how they are arranged.
    assert {r["name"] for r in kid.get("/api/files?stacks=firm").json()} == {
        "x.jpg", "y.jpg"}


def test_taking_a_share_back_reaches_the_whole_stack_too(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Symmetric, or it is a trapdoor: a grant that goes all the way down and
    a revocation that stops at the top cannot be taken back."""
    stacked(client, app_env, writable)
    client.post("/api/decide/bulk", json={
        "add_audience": ["kid"],
        "files": [{"folder": "init_2026", "name": "x.jpg"}]})

    client.post("/api/decide/bulk", json={
        "remove_audience": ["kid"],
        "files": [{"folder": "init_2026", "name": "x.jpg"}]})

    behind = decisions.read(writable / "y.jpg")
    assert behind is not None and behind.audience == ()


def test_the_scripting_route_cascades_the_same_way(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The page writes through `/api/decide/bulk`, but two routes that
    disagree about what a stack is are two libraries."""
    stacked(client, app_env, writable)

    client.post("/api/decide", json={
        "folder": "init_2026", "name": "x.jpg", "add_people": ["Mom"]})

    behind = decisions.read(writable / "y.jpg")
    assert behind is not None and behind.people == ("Mom",)


def test_a_cascade_reverts_as_one_gesture(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Every file the decision reached is in the log, or a revert puts back
    the one that was named and leaves the rest carrying it."""
    stacked(client, app_env, writable)
    client.post("/api/decide/bulk", json={
        "add_tags": ["blah"],
        "files": [{"folder": "init_2026", "name": "x.jpg"}]})

    op = history.recent()[0]
    assert {f.name for f in op.files} == {"x.jpg", "y.jpg"}

    client.post("/history/revert", data={"id": op.id})
    for name in ("x.jpg", "y.jpg"):
        after = decisions.read(writable / name)
        assert after is None or after.tags == (), name


def test_a_household_member_skips_the_takes_that_are_not_theirs(
    client: TestClient, writable: Path, app_env: dict[str, Path],
    sign_in: "Callable[[str, str], TestClient]",
    add_user: "Callable[..., None]"
) -> None:
    """Silently, and that is the deliberate part. A cascade reaches files they
    never named and may never have been shown; refusing the whole edit would
    fail an ordinary tag for a reason they cannot see, and naming what was
    skipped would tell them a photograph is there."""
    # Three files the app has *not* guessed are one moment, so the only stack
    # here is the one made below. Shared before it exists, which is how a
    # stack comes to hold a file somebody can be shown the top of and not the
    # rest — the cascade happens when a decision is made, not for ever after.
    three_files(writable, app_env)
    add_user("kid", "pw")
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["kid"]})
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "d.jpg",
        "stacked_under": "init_2026/a.jpg"})
    kid = sign_in("kid", "pw")

    r = kid.post("/api/decide/bulk", json={
        "add_tags": ["mine"],
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    assert r.status_code == 200
    top = decisions.read(writable / "a.jpg")
    assert top is not None and top.tags == ("mine",)
    hidden = decisions.read(writable / "d.jpg")
    assert hidden is not None and hidden.tags == (), "reached what is not theirs"


def test_promoting_a_take_still_moves_the_stack_rather_than_breaking_it(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The cascade must not touch a write that moves a stack about — that one
    has `_cascade`, which runs afterwards and knows not to point a file at
    itself. Following the members here as well gave the new top its own name
    as `stacked_under`, and the whole stack vanished from every listing."""
    three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "c.jpg"},
                  {"folder": "init_2026", "name": "d.jpg"}]})

    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/c.jpg",
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    top = decisions.read(writable / "c.jpg")
    assert top is None or top.stacked_under is None, "the top is behind itself"
    # `b.mp4` is in the index and in no stack: a clip cannot be in one with
    # photographs, which is what these three are.
    assert ({r["name"] for r in client.get("/api/files").json()}
            == {"c.jpg", "b.mp4"})


# --- deciding about a folder ----------------------------------------------

def test_the_folder_bar_asks_only_what_a_set_can_answer(
    client: TestClient
) -> None:
    """One zoom out the selection is sets rather than photographs, and the
    question each control asks has to survive that.

    The four stack actions do not: every one needs *a photograph* — which of
    these takes speaks for the others, whether they are one moment — and
    across an event there is no such question to ask.
    """
    html = client.get("/?date=2026&group=month,event").text
    # The row itself. The page script names an action too, and a regex over
    # the whole document reads that as a second button.
    bar = html[html.index('id="actions"'):]
    bar = bar[:bar.index("</div>")]
    acts = re.findall(r'data-act="(\w+)"', bar)

    assert acts == ["event", "tags", "access", "download"], acts
    for gone in ("stack", "top", "unstack", "nostack", "delete", "purge"):
        assert f'data-act="{gone}"' not in html, gone


def test_a_folder_can_be_selected(client: TestClient) -> None:
    """The same circle the thumbnails carry, for the same gesture."""
    html = client.get("/?date=2026&group=month,event").text

    assert 'class="pick" aria-label="Select this folder"' in html
    assert 'id="selall"' in html and 'id="selcount"' in html


def test_a_folder_with_no_address_offers_no_circle(client: TestClient) -> None:
    """*No day* is every file whose date stops at the month, and there is no
    filter that says so — so that card cannot be opened, and a set nothing can
    name is a set nothing can be decided about either."""
    dead = '<div class="tile dead"'
    html = client.get("/?date=2026&group=month,day").text

    if dead in html:
        card = html[html.index(dead):]
        card = card[:card.index("</div>")]
        assert "pick" not in card, card


def test_the_landing_page_asks_the_actions_in_the_bar_order(
    client: TestClient
) -> None:
    """Learning one bar teaches the other, at either zoom — so the four it
    keeps are in the order the grid puts them in."""
    html = client.get("/?date=2026&group=month,event").text
    acts = as_columns(re.findall(r'data-act="(\w+)"', html))
    bar = [col for col, _ in w_vocab.CHIPS]
    assert acts[:4] == ["event", "tag", "audience", "download"], acts
    shared = set(bar) & set(acts)

    assert relative(bar, shared) == relative(acts, shared)


# --- hidden, and video out of stacking (spec/clips.md §3, §4) -------------

def test_hiding_takes_a_file_out_of_the_curators_own_grid(
    client: TestClient, writable: Path
) -> None:
    """What `hidden` adds over sharing with nobody: the administrator does not
    see it either, until they ask for it by name."""
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg",
        "add_audience": [decisions.ARCHIVED]})
    assert r.status_code == 200, r.text
    assert decisions.read(writable / "a.jpg") == Decision(
        audience=(decisions.ARCHIVED,))

    assert "a.jpg" not in client.get("/browse?event=Italy%20-%20Sicily").text
    assert "a.jpg" in client.get(
        "/browse?event=Italy%20-%20Sicily&audience=archived").text


def test_hiding_is_offered_in_the_access_menu(client: TestClient) -> None:
    html = client.get("/browse").text
    assert pix(html)["ARCHIVED"] == decisions.ARCHIVED
    assert "out of every view" in html


def test_nobody_can_be_called_archived(client: TestClient) -> None:
    """A login called that would be granted exactly the files nobody is
    meant to see."""
    client.post("/accounts/save", data={"name": "Archived", "password": "pw"})
    client.post("/accounts/save", data={"name": "kid", "password": "pw",
                                        "groups": "family,archived"})
    client.post("/accounts/groups", data={"groups": "family,archived"})

    book = accounts.load()
    assert decisions.ARCHIVED not in book.users
    assert decisions.ARCHIVED not in book.groups
    assert book.users["kid"].groups == ("family",)


def test_video_cannot_be_stacked_yet(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Whether one clip can speak for another has not been decided, and a
    stack is a fold — nothing should leave the grid on a rule nobody made."""
    share = app_env["share"]
    for name in ("b.mp4", "c.mp4"):
        (writable / name).write_bytes(b"fake")
    (share / "meta" / "init_2026" / "c.mp4.json").write_text(json.dumps({
        "file": "c.mp4", "folder": "init_2026", "size": 20, "mtime_ns": 1,
        "exif": {"QuickTime:Duration": "10 s",
                 "XMP:EventAuto": "Italy - Sicily"},
    }), encoding="utf-8")
    ix.build(app_env["db"], meta_dir=share / "meta",
             master_dir=share / "master")

    r = client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/b.mp4",
        "files": [{"folder": "init_2026", "name": "c.mp4"}]})

    assert r.status_code == 400, r.text
    assert "video cannot be stacked" in r.text, r.text
    assert decisions.read(writable / "c.mp4") is None, "written anyway"


def test_the_bar_does_not_offer_to_stack_video() -> None:
    js = w_pages.BROWSE_JS
    assert "show('stack', stackable &&" in js
    assert "show('top', stackable &&" in js


def test_a_page_put_back_by_the_browser_is_loaded_again(
    client: TestClient
) -> None:
    """Back is not allowed to show the grid as it was left: clips cut on the
    splice page were missing from it until it was reloaded by hand."""
    html = client.get("/browse").text
    assert "if (e.persisted) location.reload();" in html


def test_archiving_is_said_as_archiving_in_history() -> None:
    """It is written as an audience, but *gave archived access to 3* is not
    how anybody says it."""
    arch = (decisions.ARCHIVED,)
    assert w_writes.summary(w_writes.Change(add_audience=arch)) == "archived {n}"  # pyright: ignore[reportPrivateUsage]
    assert (w_writes.summary(w_writes.Change(remove_audience=arch))  # pyright: ignore[reportPrivateUsage]
            == "took {n} out of the archive")
    assert (w_writes.summary(w_writes.Change(add_audience=("family",)))  # pyright: ignore[reportPrivateUsage]
            == "gave family access to {n}")


def test_the_viewer_offers_what_can_be_done_to_one_file(
    client: TestClient
) -> None:
    """Everything the bar does that makes sense of a single photograph, and
    none of the stack actions — each of those asks about several."""
    html = client.get("/browse").text
    row = html[html.index('<div id="viewacts">'):]
    row = row[:row.index('<aside id="rail">')]

    for act in ("event", "tags", "people", "date", "access", "delete",
                "restore", "purge"):
        assert f'data-vact="{act}"' in row, act
    assert 'id="viewget"' in row and 'id="viewsplice"' in row
    for act in ("stack", "top", "unstack", "nostack"):
        assert f'data-vact="{act}"' not in row, act


def test_a_household_member_gets_the_viewer_actions_they_get_in_the_bar(
    household: dict[str, object]
) -> None:
    kid = cast(TestClient, household["kid"])
    html = kid.get("/browse").text
    row = html[html.index('<div id="viewacts">'):]
    row = row[:row.index('<aside id="rail">')]

    assert 'data-vact="tags"' in row
    assert 'data-vact="access"' not in row and 'data-vact="purge"' not in row
