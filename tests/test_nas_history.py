"""The operation log and revert (spec/nas-app.md §8).

A bulk edit can touch several hundred files from one click. *I just gave the
children access to three hundred photographs* has no other cure: the decisions
are individually correct in three hundred separate sidecars, and nothing in the
archive remembers they used to say something else.
"""

from __future__ import annotations

import json
from urllib.parse import unquote
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pix.nas import accounts, decisions, history
from pix.nas.decisions import Decision
from pix.nas.history import Before



@pytest.fixture
def log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "operations.jsonl"
    monkeypatch.setattr(history, "OPERATIONS_FILE", path)
    return path


# --- the log -----------------------------------------------------------------

def test_an_operation_round_trips(log: Path) -> None:
    history.record("admin", "gave family access to 2 files", [
        Before("f", "a.jpg", Decision(audience=("kid",))),
        Before("f", "b.jpg", None),
    ])

    ops = history.recent()
    assert len(ops) == 1
    assert ops[0].summary == "gave family access to 2 files"
    assert ops[0].who == "admin"
    assert ops[0].files[0].decision == Decision(audience=("kid",))
    assert ops[0].files[1].decision is None


def test_newest_comes_first(log: Path) -> None:
    """The mistake you are looking for is almost always the last thing done."""
    history.record("admin", "first", [Before("f", "a.jpg", None)])
    history.record("admin", "second", [Before("f", "a.jpg", None)])

    assert [op.summary for op in history.recent()] == ["second", "first"]


def test_a_damaged_line_costs_one_entry(log: Path) -> None:
    """The log is a convenience; one bad line should not take the history."""
    history.record("admin", "good", [Before("f", "a.jpg", None)])
    with log.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    history.record("admin", "also good", [Before("f", "b.jpg", None)])

    assert [op.summary for op in history.recent()] == ["also good", "good"]


def test_a_missing_log_is_empty_not_an_error(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history, "OPERATIONS_FILE", tmp_path / "nope.jsonl")

    assert history.recent() == []


def test_logging_never_breaks_the_write(tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Losing the ability to undo is bad; failing the edit the curator actually
    asked for because the *log* could not be written would be worse."""
    monkeypatch.setattr(history, "OPERATIONS_FILE",
                        tmp_path / "a-file" / "nested" / "ops.jsonl")
    (tmp_path / "a-file").write_text("not a directory", encoding="utf-8")

    op = history.record("admin", "still returns", [Before("f", "a.jpg", None)])
    assert op.summary == "still returns"


def test_a_revert_marks_what_it_undid(log: Path) -> None:
    first = history.record("admin", "gave access", [Before("f", "a.jpg", None)])
    history.record("admin", "reverted", [Before("f", "a.jpg", None)],
                   reverts=first.id)

    assert history.undone(history.recent()) == {first.id}


def test_previous_values_are_stored_in_full(log: Path) -> None:
    """Not as a diff: a diff has to be read against whatever the file says now,
    and now may already have moved — the whole reason somebody is reverting."""
    history.record("admin", "x", [
        Before("f", "a.jpg", Decision(event="Sicily", tags=("beach",),
                                      audience=("kid",),
                                      date_override="1987-*-*-*:*:*"))])

    line = json.loads(log.read_text(encoding="utf-8").strip())
    assert line["files"][0]["before"] == {
        "event": "Sicily", "date_override": "1987-*-*-*:*:*",
        "tags": ["beach"], "audience": ["kid"], "deleted": False}


def test_a_deletion_is_part_of_what_was_there_before(log: Path) -> None:
    """It is a decision like the other four, so an edit made to a file that was
    already deleted has to remember that. Without it a revert would quietly
    bring the file back — restoring something nobody asked to restore."""
    history.record("admin", "x", [
        Before("f", "a.jpg", Decision(event="Sicily", deleted=True))])

    line = json.loads(log.read_text(encoding="utf-8").strip())
    assert line["files"][0]["before"]["deleted"] is True
    assert history.recent()[0].files[0].decision == Decision(
        event="Sicily", deleted=True)


# --- through the app ----------------------------------------------------------

@pytest.fixture
def curating(app_env: dict[str, Path], writable: Path,
             log: Path, sign_in: Callable[[str, str], TestClient], add_user: Callable[..., None]) -> TestClient:
    (writable / "b.mp4").write_bytes(b"fake")
    return sign_in(accounts.ADMIN, "admin")


def _targets(*names: str) -> list[dict[str, str]]:
    return [{"folder": "init_2026", "name": n} for n in names]


def test_a_bulk_edit_is_recorded(curating: TestClient) -> None:
    curating.post("/api/decide/bulk", json={
        "add_audience": ["family"], "files": _targets("a.jpg", "b.mp4")})

    ops = history.recent()
    assert len(ops) == 1
    assert ops[0].summary == "gave family access to 2 files"
    assert len(ops[0].files) == 2


def test_reverting_puts_the_files_back(curating: TestClient,
                                       writable: Path) -> None:
    curating.post("/api/decide/bulk", json={
        "add_audience": ["family"], "files": _targets("a.jpg", "b.mp4")})
    assert decisions.read(writable / "a.jpg") == Decision(audience=("family",))

    op = history.recent()[0]
    curating.post("/history/revert", data={"id": op.id})

    assert decisions.read(writable / "a.jpg") is None
    assert decisions.read(writable / "b.mp4") is None


def test_reverting_restores_rather_than_inverts(curating: TestClient,
                                                writable: Path) -> None:
    """The inverse of *added family* is only *remove family* if nothing else
    touched the file since — and something might have."""
    curating.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["kid"]})
    curating.post("/api/decide/bulk", json={
        "audience": ["family"], "files": _targets("a.jpg")})

    op = history.recent()[0]
    curating.post("/history/revert", data={"id": op.id})

    assert decisions.read(writable / "a.jpg") == Decision(audience=("kid",))


def test_the_index_follows_a_revert(curating: TestClient,
                                    app_env: dict[str, Path]) -> None:
    curating.post("/api/decide/bulk", json={
        "add_audience": ["family"], "files": _targets("a.jpg")})
    assert curating.get("/api/files?audience=family").json() != []

    curating.post("/history/revert", data={"id": history.recent()[0].id})

    assert curating.get("/api/files?audience=family").json() == []


def test_a_revert_is_itself_recorded(curating: TestClient) -> None:
    """Which is the redo nobody has to build."""
    curating.post("/api/decide/bulk", json={
        "add_audience": ["family"], "files": _targets("a.jpg")})
    first = history.recent()[0]
    curating.post("/history/revert", data={"id": first.id})

    ops = history.recent()
    assert ops[0].reverts == first.id
    assert history.undone(ops) == {first.id}


def test_reverting_a_revert_reapplies_it(curating: TestClient,
                                         writable: Path) -> None:
    curating.post("/api/decide/bulk", json={
        "add_audience": ["family"], "files": _targets("a.jpg")})
    curating.post("/history/revert", data={"id": history.recent()[0].id})
    assert decisions.read(writable / "a.jpg") is None

    curating.post("/history/revert", data={"id": history.recent()[0].id})

    assert decisions.read(writable / "a.jpg") == Decision(audience=("family",))


def test_the_history_page_lists_and_offers(curating: TestClient) -> None:
    curating.post("/api/decide/bulk", json={
        "add_audience": ["family"], "files": _targets("a.jpg")})

    html = curating.get("/history").text
    assert "gave family access to 1 file" in html
    assert ">Revert<" in html


def test_an_already_reverted_operation_is_not_offered_again(
    curating: TestClient
) -> None:
    curating.post("/api/decide/bulk", json={
        "add_audience": ["family"], "files": _targets("a.jpg")})
    curating.post("/history/revert", data={"id": history.recent()[0].id})

    html = curating.get("/history").text
    assert "undone" in html


def test_an_unknown_operation_is_refused_kindly(curating: TestClient) -> None:
    r = curating.post("/history/revert", data={"id": "nope"},
                      follow_redirects=False)

    assert r.status_code == 303
    assert "no+such+operation" in r.headers["location"]


def test_only_an_admin_sees_or_reverts_history(app_env: dict[str, Path], writable: Path, log: Path, sign_in: Callable[[str, str], TestClient], add_user: Callable[..., None]) -> None:
    """Every write is admin-only, so the record of them is too."""
    add_user("kid", "pw")
    kid = sign_in("kid", "pw")

    assert kid.get("/history").status_code == 403
    assert kid.post("/history/revert", data={"id": "x"}).status_code == 403


def test_the_summary_says_what_it_did(curating: TestClient) -> None:
    """Read months later off a list: "gave family access to 312 files" is a
    thing you can recognise; "bulk edit" is not."""
    cases = [
        ({"add_tags": ["beach"]}, "tagged 1 file beach"),
        ({"remove_tags": ["beach"]}, "untagged beach on 1 file"),
        ({"event": "Sicily"}, "set the event on 1 file to Sicily"),
        ({"event": None}, "cleared the event on 1 file"),
        ({"date_override": "1987-*-*-*:*:*"}, "dated 1 file 1987-*-*-*:*:*"),
        ({"remove_audience": ["family"]}, "took family access from 1 file"),
    ]
    for body, expected in cases:
        curating.post("/api/decide/bulk", json={**body, "files": _targets("a.jpg")})
        assert history.recent()[0].summary == expected, expected


def test_nothing_written_means_nothing_logged(curating: TestClient) -> None:
    """A batch that failed every file did not happen, and a log full of
    non-events is a log nobody reads."""
    curating.post("/api/decide/bulk", json={
        "add_audience": ["family"], "files": _targets("gone.jpg")})

    assert history.recent() == []


def test_a_file_deleted_since_is_reported_not_fatal(
    curating: TestClient, writable: Path
) -> None:
    """Master is the record and it moves; a revert has to cope with that."""
    curating.post("/api/decide/bulk", json={
        "add_audience": ["family"], "files": _targets("a.jpg", "b.mp4")})
    (writable / "b.mp4").unlink()
    decisions.sidecar_path(writable / "b.mp4").unlink(missing_ok=True)

    r = curating.post("/history/revert",
                      data={"id": history.recent()[0].id},
                      follow_redirects=False)

    where = r.headers["location"]
    assert "1%20file%20put%20back" in where, where
    assert "1%20could%20not%20be" in where, where
    assert decisions.read(writable / "a.jpg") is None


def test_the_history_link_is_in_the_header(curating: TestClient) -> None:
    assert 'href="/history"' in curating.get("/browse").text


# --- one gesture, one entry ---------------------------------------------------

def test_the_chunks_of_one_edit_are_one_operation(log: Path) -> None:
    """Naming an event across four hundred files went out as four requests and
    came back as four identical lines in the log — so putting it back meant
    reverting each of them, and reading it meant working out that four entries
    saying the same thing were one thing."""
    for part in range(3):
        history.record("admin", "set the event on {n} to Misc",
                       [Before("f", f"{part}-{i}.jpg", None) for i in range(100)],
                       batch="one-gesture")

    ops = history.recent()
    assert len(ops) == 1, [op.summary for op in ops]
    assert ops[0].summary == "set the event on 300 files to Misc"
    assert len(ops[0].files) == 300
    # And reverting it reaches every one of them, not the first hundred.
    whole = history.get("one-gesture")
    assert whole is not None
    assert len(whole.files) == 300


def test_a_gesture_survives_somebody_else_working_at_the_same_time(
    log: Path
) -> None:
    """Two curators interleave their lines. A gesture is still one gesture when
    somebody else's landed in the middle of it, so the grouping is by id rather
    than by which lines happen to be next to each other."""
    history.record("james", "tagged {n} beach",
                   [Before("f", "a.jpg", None)], batch="mine")
    history.record("lola", "gave family access to {n}",
                   [Before("f", "z.jpg", None)], batch="theirs")
    history.record("james", "tagged {n} beach",
                   [Before("f", "b.jpg", None)], batch="mine")

    ops = {op.id: op for op in history.recent()}
    assert len(ops) == 2, [op.summary for op in history.recent()]
    assert len(ops["mine"].files) == 2
    assert ops["mine"].summary == "tagged 2 files beach"
    assert ops["theirs"].summary == "gave family access to 1 file"


def test_an_operation_that_offers_nothing_back_still_counts_itself(
    log: Path
) -> None:
    """A purge records no previous values — there are none — but it still did
    something to a number of files, and the log has to say how many."""
    history.record("admin", "purged {n}", [], count=100, batch="p")
    history.record("admin", "purged {n}", [], count=42, batch="p")

    ops = history.recent()
    assert len(ops) == 1
    assert ops[0].summary == "purged 142 files"
    assert ops[0].files == ()


def test_lines_written_before_any_of_this_still_read(log: Path) -> None:
    """The log is the record. A format that stopped reading what it had already
    written would be a format that lost it."""
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        '{"id":"old","when":1,"who":"admin","summary":"tagged 3 files beach",'
        '"files":[{"folder":"f","name":"a.jpg","before":null}]}\n',
        encoding="utf-8")

    ops = history.recent()
    assert len(ops) == 1
    # No placeholder and no count of its own: it says what it always said.
    assert ops[0].summary == "tagged 3 files beach"
    assert ops[0].n == 1


# --- a revert undoes its own operation, and only while it still holds ---------

def test_reverting_one_field_leaves_another_alone(curating: TestClient,
                                                  writable: Path) -> None:
    """Two edits to different fields do not touch each other, so either can be
    put back in any order. Writing the whole previous value back took the other
    one with it: setting a date and then an event, then reverting the date,
    destroyed the event."""
    curating.post("/api/decide/bulk", json={
        "date_override": "2026-*-*-*:*:*", "files": _targets("a.jpg")})
    curating.post("/api/decide/bulk", json={
        "event": "blah", "files": _targets("a.jpg")})

    dated = [op for op in history.recent() if "dated" in op.summary][0]
    curating.post("/history/revert", data={"id": dated.id})

    assert decisions.read(writable / "a.jpg") == Decision(event="blah")


def test_an_operation_that_has_been_overwritten_cannot_be_put_back(
    curating: TestClient, writable: Path
) -> None:
    """Set an event, set it again, and the first is no longer what the file
    says — so there is nothing of it left to undo. It used to "succeed",
    quietly replacing the second answer with a value nobody held."""
    curating.post("/api/decide/bulk", json={
        "event": "one", "files": _targets("a.jpg")})
    curating.post("/api/decide/bulk", json={
        "event": "two", "files": _targets("a.jpg")})

    first = [op for op in history.recent() if "one" in op.summary][0]
    r = curating.post("/history/revert", data={"id": first.id},
                      follow_redirects=False)

    said = unquote(r.headers["location"])
    assert "0 files put back" in said, said
    assert "1 changed since" in said, said
    assert decisions.read(writable / "a.jpg") == Decision(event="two")


def test_putting_the_newer_one_back_makes_the_older_one_reachable_again(
    curating: TestClient, writable: Path
) -> None:
    """Which is the whole point of checking rather than refusing: undo them in
    order and each becomes valid as the one above it is lifted."""
    curating.post("/api/decide/bulk", json={
        "event": "one", "files": _targets("a.jpg")})
    curating.post("/api/decide/bulk", json={
        "event": "two", "files": _targets("a.jpg")})

    second = [op for op in history.recent() if "two" in op.summary][0]
    curating.post("/history/revert", data={"id": second.id})
    assert decisions.read(writable / "a.jpg") == Decision(event="one")

    first = [op for op in history.recent() if op.summary.endswith("to one")][0]
    curating.post("/history/revert", data={"id": first.id})
    assert decisions.read(writable / "a.jpg") is None


def test_a_revert_takes_back_only_the_value_it_gave(curating: TestClient,
                                                    writable: Path) -> None:
    """*Gave family access* is undone by taking `family` away, not by restoring
    the whole list — somebody added `james` since, and that grant was never
    this operation's to remove."""
    curating.post("/api/decide/bulk", json={
        "add_audience": ["family"], "files": _targets("a.jpg")})
    curating.post("/api/decide/bulk", json={
        "add_audience": ["james"], "files": _targets("a.jpg")})

    gave = [op for op in history.recent() if "family" in op.summary][0]
    curating.post("/history/revert", data={"id": gave.id})

    assert decisions.read(writable / "a.jpg") == Decision(audience=("james",))


def test_a_value_the_file_already_had_is_not_taken_away(
    curating: TestClient, writable: Path
) -> None:
    """Adding a tag to a file that carried it did nothing. Undoing nothing by
    removing it would be this operation destroying a decision it never made."""
    curating.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_tags": ["beach"]})
    curating.post("/api/decide/bulk", json={
        "add_tags": ["beach"], "files": _targets("a.jpg")})

    bulk = history.recent()[0]
    curating.post("/history/revert", data={"id": bulk.id})

    assert decisions.read(writable / "a.jpg") == Decision(tags=("beach",))


def test_the_files_that_have_moved_on_are_counted_not_refused(
    curating: TestClient, writable: Path
) -> None:
    """At three hundred files some always will have moved. Refusing the whole
    operation for one of them would make revert useless exactly when it is
    needed most."""
    curating.post("/api/decide/bulk", json={
        "event": "trip", "files": _targets("a.jpg", "b.mp4")})
    curating.post("/api/decide/bulk", json={
        "event": "somewhere else", "files": _targets("b.mp4")})

    trip = [op for op in history.recent() if op.summary.endswith("to trip")][0]
    r = curating.post("/history/revert", data={"id": trip.id},
                      follow_redirects=False)

    said = unquote(r.headers["location"])
    assert "1 file put back" in said, said
    assert "1 changed since" in said, said
    assert decisions.read(writable / "a.jpg") is None
    assert decisions.read(writable / "b.mp4") == Decision(event="somewhere else")


def test_an_operation_recorded_before_this_says_it_cannot_be_put_back(
    curating: TestClient, writable: Path, log: Path
) -> None:
    """Without knowing what an operation set, there is no way to tell whether
    it is still in effect — so it is refused rather than guessed at. The old
    behaviour was to write the whole previous value back, which is the thing
    this replaced."""
    curating.post("/api/decide/bulk", json={
        "event": "trip", "files": _targets("a.jpg")})
    # Strip what it did, the way a line written last week has none.
    lines = [json.loads(line)
             for line in log.read_text(encoding="utf-8").splitlines()]
    for entry in lines:
        for f in entry["files"]:
            f.pop("did", None)
    log.write_text("".join(json.dumps(e) + "\n" for e in lines),
                   encoding="utf-8")

    op = history.recent()[0]
    assert not op.revertable()
    r = curating.post("/history/revert", data={"id": op.id},
                      follow_redirects=False)
    assert "cannot be put back" in unquote(r.headers["location"])
    assert decisions.read(writable / "a.jpg") == Decision(event="trip")


# --- looking at what an operation touched -------------------------------------

def test_an_operation_is_a_filter_into_the_grid(curating: TestClient) -> None:
    """*Show me what that did* is the question the log cannot answer on its own.
    It says what happened; looking at the photographs means getting them into
    the grid, where everything else already works."""
    curating.post("/api/decide/bulk", json={
        "event": "trip", "files": _targets("a.jpg")})
    op = history.recent()[0]

    html = curating.get(f"/browse?op={op.id}").text
    assert "a.jpg" in html
    assert "b.mp4" not in html, "showed a file the operation never touched"
    # And says why it is showing a handful of files rather than the library.
    assert "from-op" in html
    assert "to trip" in html


def test_the_ones_a_revert_left_alone_can_be_looked_at(
    curating: TestClient
) -> None:
    """A revert reports a number. That number is the work still to look at, so
    it has to be reachable — these are the files somebody edited after the
    operation, which is exactly the set worth a second look."""
    curating.post("/api/decide/bulk", json={
        "event": "trip", "files": _targets("a.jpg", "b.mp4")})
    curating.post("/api/decide/bulk", json={
        "event": "somewhere else", "files": _targets("b.mp4")})
    trip = [op for op in history.recent() if op.summary.endswith("to trip")][0]

    r = curating.post("/history/revert", data={"id": trip.id},
                      follow_redirects=False)
    assert f"look={trip.id}" in r.headers["location"]
    assert "see the ones it left" in curating.get(
        r.headers["location"]).text

    html = curating.get(f"/browse?op={trip.id}&stale=1").text
    assert "b.mp4" in html, "the file that had moved on is not offered"
    assert "a.jpg" not in html, "offered a file the revert did put back"


def test_the_whole_operation_and_the_stale_part_are_different_views(
    curating: TestClient
) -> None:
    curating.post("/api/decide/bulk", json={
        "event": "trip", "files": _targets("a.jpg", "b.mp4")})
    curating.post("/api/decide/bulk", json={
        "event": "somewhere else", "files": _targets("b.mp4")})
    trip = [op for op in history.recent() if op.summary.endswith("to trip")][0]

    whole = curating.get(f"/browse?op={trip.id}").text
    assert "a.jpg" in whole and "b.mp4" in whole

    stale = curating.get(f"/browse?op={trip.id}&stale=1").text
    assert "a.jpg" not in stale and "b.mp4" in stale


def test_an_operation_nobody_has_heard_of_shows_nothing(
    curating: TestClient
) -> None:
    """Not everything. A filter that fails open is a filter that lies about how
    much it is showing."""
    html = curating.get("/browse?op=not-a-real-id").text

    assert "a.jpg" not in html
    assert "b.mp4" not in html


def test_the_log_links_each_operation_to_its_files(curating: TestClient) -> None:
    curating.post("/api/decide/bulk", json={
        "event": "trip", "files": _targets("a.jpg")})
    op = history.recent()[0]

    assert f'href="/browse?op={op.id}"' in curating.get("/history").text


def test_looking_at_a_delete_shows_the_files_it_deleted(
    curating: TestClient
) -> None:
    """The most useful link there is, and the one the ordinary default
    answered with an empty grid: every file it named had just been deleted, and
    the grid shows the living by default."""
    curating.post("/api/decide/bulk", json={
        "deleted": True, "files": _targets("a.jpg")})
    op = history.recent()[0]

    assert "a.jpg" in curating.get(f"/browse?op={op.id}").text


def test_a_purge_is_not_offered_as_a_link_to_files(
    curating: TestClient
) -> None:
    """They are gone, and the index rows with them. A link would lead to an
    empty grid, which reads as broken rather than as *nothing left to look
    at*."""
    curating.post("/api/decide/bulk", json={
        "deleted": True, "files": _targets("a.jpg")})
    curating.post("/api/purge", json={"files": _targets("a.jpg")})

    purge = history.recent()[0]
    assert purge.summary == "purged 1 file"
    assert not purge.files
    page = curating.get("/history").text
    assert "purged 1 file" in page
    assert f"op={purge.id}" not in page, "linked to files that no longer exist"


def test_following_an_operation_never_widens_what_you_can_see(
    curating: TestClient, sign_in: Callable[[str, str], TestClient],
    writable: Path
) -> None:
    """The operation filter narrows a view; nothing about it widens one. A
    curator who cannot see the deleted does not see them by arriving from the
    log, and one who was never shared a file does not see it either."""
    curating.post("/accounts/save", data={"name": "kid", "password": "pw"})
    curating.post("/api/decide/bulk", json={
        "add_audience": ["kid"], "files": _targets("a.jpg")})
    curating.post("/api/decide/bulk", json={
        "deleted": True, "files": _targets("a.jpg", "b.mp4")})
    op = history.recent()[0]

    kid = sign_in("kid", "pw")
    html = kid.get(f"/browse?op={op.id}&deleted=with").text
    assert "a.jpg" not in html, "a deleted file reached somebody without the bin"
    assert "b.mp4" not in html, "a file nobody shared with them"
