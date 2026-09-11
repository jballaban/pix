"""The operation log and revert (spec/nas-app.md §8).

A bulk edit can touch several hundred files from one click. *I just gave the
children access to three hundred photographs* has no other cure: the decisions
are individually correct in three hundred separate sidecars, and nothing in the
archive remembers they used to say something else.
"""

from __future__ import annotations

import json
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
        "tags": ["beach"], "audience": ["kid"]}


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
