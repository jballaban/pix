"""The browse app (spec/nas-app.md §8).

It reads the index and serves the derived tiers. It never decodes anything —
`process` already made everything it displays. The one thing it writes is
curation decisions: an `.xmp` beside the master file, then that file's index row
— sidecar first, index follows.
"""

from __future__ import annotations

import io
import json
import zipfile
import re
import time

from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from pix.nas import accounts
from pix.nas import decisions
from pix.nas import derive
from pix.nas import history
from pix.nas import index as ix
from pix.nas import web
from pix.nas.web import _split
from pix.nas.decisions import Decision


# --- browse ------------------------------------------------------------------

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


def test_deleting_takes_a_file_out_of_every_listing(
    client: TestClient, writable: Path
) -> None:
    """A soft delete has to be a decision like any other *and* disappear from
    the grid. The second half is the one that can silently not happen: the
    exclusion lives in one shared `WHERE`, and a query that forgot it would put
    a deleted file back on screen."""
    assert "a.jpg" in client.get("/browse?event=Italy%20-%20Sicily").text

    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "deleted": True})
    assert r.status_code == 200, r.text

    # The judgement is in master, beside the file, like every other.
    assert decisions.read(writable / "a.jpg") == Decision(deleted=True)

    # And it is gone from the grid, the API, and the event listing alike.
    assert "a.jpg" not in client.get("/browse?event=Italy%20-%20Sicily").text
    assert not [f for f in client.get("/api/files").json()
                if f["name"] == "a.jpg"]


def test_deleting_leaves_the_other_decisions_alone(
    client: TestClient, writable: Path
) -> None:
    """Deleting is one field, not a verdict on the rest: restoring has to give
    back the file that was deleted, not a blank one."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "event": "Sicily Trip",
        "add_audience": ["family"]})
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "deleted": True})

    assert decisions.read(writable / "a.jpg") == Decision(
        event="Sicily Trip", audience=("family",), deleted=True)

    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "deleted": False})
    assert decisions.read(writable / "a.jpg") == Decision(
        event="Sicily Trip", audience=("family",))
    # Back on screen — under the event it was given, which is the point: what
    # comes back is the file that was deleted, not a blank one.
    assert "a.jpg" in client.get("/browse?event=Sicily%20Trip").text


def test_a_deletion_is_undone_by_the_ordinary_revert(
    client: TestClient, writable: Path
) -> None:
    """Nothing new was built for this. A deletion is a decision, the operation
    log already records what each file said before one, so History restores it
    the same way it restores anything else."""
    client.post("/api/decide/bulk", json={
        "files": [{"folder": "init_2026", "name": "a.jpg"}], "deleted": True})
    assert decisions.read(writable / "a.jpg") == Decision(deleted=True)

    page = client.get("/history").text
    assert "deleted 1 file" in page, page[:400]

    op = history.recent(1)[0]
    assert op.summary == "deleted 1 file", op.summary
    r = client.post("/history/revert", data={"id": op.id},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "no+such" not in r.headers.get("location", ""), r.headers

    # No sidecar at all, because there was none before: restoring writes the
    # previous value wholesale rather than inverting the change.
    assert decisions.read(writable / "a.jpg") is None
    assert "a.jpg" in client.get("/browse?event=Italy%20-%20Sicily").text


def test_the_deleted_filter_is_off_by_default(
    client: TestClient, writable: Path
) -> None:
    """Off is the default and clearing the chip is what says *the living*, so
    the ordinary grid is the one view nobody has to remember to ask for."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "deleted": True})

    assert "a.jpg" not in client.get("/browse?event=Italy%20-%20Sicily").text
    assert "a.jpg" in client.get(
        "/browse?event=Italy%20-%20Sicily&deleted=only").text
    both = client.get("/browse?event=Italy%20-%20Sicily&deleted=with").text
    assert "a.jpg" in both and "b.mp4" in both


def test_a_deleted_cell_says_so(client: TestClient, writable: Path) -> None:
    """Shown beside living files, it has to be told apart from them at a
    glance — the whole point of the `with` view."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "deleted": True})

    html = client.get("/browse?event=Italy%20-%20Sicily&deleted=with").text
    assert 'class="cell gone"' in html
    assert 'data-deleted="1"' in html
    assert ".cell.gone::after" in html, "nothing marks it"


def test_a_non_admin_cannot_ask_for_the_deleted(
    client: TestClient, sign_in: "Callable[[str, str], TestClient]",
    writable: Path
) -> None:
    """Hiding the chip stops it being offered. This is what stops it being
    asked for — the parameter is dropped for a non-admin rather than merely
    left out of their bar, because the address bar is not a control we own."""
    client.post("/accounts/save", data={"name": "kid", "password": "pw"})
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["kid"],
        "deleted": True})

    kid = sign_in("kid", "pw")
    assert "a.jpg" not in kid.get("/browse?deleted=only").text
    assert "a.jpg" not in kid.get("/browse?deleted=with").text
    assert not [f for f in kid.get("/api/files?deleted=with").json()
                if f["name"] == "a.jpg"]


def test_a_write_reports_what_is_left_in_the_bin(
    client: TestClient, writable: Path
) -> None:
    """The header count is rendered with the page, so the write that changes it
    has to say what it is now. A number that only refreshes on reload is worse
    than no number, because it looks current."""
    r = client.post("/api/decide/bulk", json={
        "files": [{"folder": "init_2026", "name": "a.jpg"}], "deleted": True})
    assert r.json()["binned"] == 1, r.text

    # Restoring takes it back down...
    r = client.post("/api/decide/bulk", json={
        "files": [{"folder": "init_2026", "name": "a.jpg"}], "deleted": False})
    assert r.json()["binned"] == 0, r.text

    # ...and so does destroying it.
    client.post("/api/decide/bulk", json={
        "files": [{"folder": "init_2026", "name": "a.jpg"}], "deleted": True})
    r = client.post("/api/purge", json={
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})
    assert r.json()["binned"] == 0, r.text


def test_the_bin_link_is_hideable(client: TestClient) -> None:
    """It is rendered at zero and hidden, rather than left out, so that
    deleting something can light it up without a reload — an element that is
    not there cannot be updated. Which means it has to be hideable: `.who-link`
    sets a `display`, and that outranks the user agent's `[hidden]`."""
    html = client.get("/browse?event=Italy%20-%20Sicily").text

    assert 'id="bincount"' in html
    assert ".who-link[hidden]" in html


def test_the_header_counts_what_is_waiting(
    client: TestClient, writable: Path
) -> None:
    """Deleting is cheap, so files pile up in a state nobody is looking at. A
    link saying "Deleted" reports nothing; a number says there is something to
    do, and says it on every page until there is not."""
    # Rendered but hidden, rather than absent: deleting has to light it up
    # without a reload, and an element that is not there cannot be updated.
    empty = client.get("/").text
    assert 'id="bincount"' in empty
    assert "hidden>0 deleted</a>" in empty, "a nag at zero"

    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "deleted": True})

    home = client.get("/").text
    assert "1 deleted" in home
    assert 'href="/browse?deleted=only"' in home, "the count leads nowhere"


def test_purging_requires_that_it_was_deleted_first(
    client: TestClient, writable: Path
) -> None:
    """The order is decide, then destroy. The page only offers Purge while the
    deleted filter is on, but this endpoint is reachable without it, and this
    check is all that stands between a URL and an original nobody said should
    go."""
    r = client.post("/api/purge", json={
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    assert r.status_code == 200
    assert r.json()["purged"] == 0
    assert "not deleted" in r.json()["failed"][0]["error"]
    assert (writable / "a.jpg").is_file(), "a living original was destroyed"


def test_purging_removes_the_original_and_everything_derived(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The irreversible one. Everything the file occupies has to go, or the
    archive keeps thumbnails of a photograph it no longer has."""
    from pix.nas import destroy as destroy_mod

    share = app_env["share"]
    for tier, suffix in (("thumb", ".jpg"), ("preview", ".jpg"),
                         ("meta", ".json")):
        d = share / tier / "init_2026"
        d.mkdir(parents=True, exist_ok=True)
        (d / ("a.jpg" + suffix)).write_bytes(b"derived")

    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "deleted": True})
    r = client.post("/api/purge", json={
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})
    assert r.json()["purged"] == 1, r.text

    for path in destroy_mod.targets(writable / "a.jpg"):
        assert not path.exists(), path
    assert not [f for f in client.get("/api/files").json()
                if f["name"] == "a.jpg"]
    assert "a.jpg" not in client.get("/browse?deleted=only").text


def test_only_an_admin_can_purge(
    client: TestClient, sign_in: "Callable[[str, str], TestClient]",
    writable: Path
) -> None:
    client.post("/accounts/save", data={"name": "kid", "password": "pw"})
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "deleted": True})

    kid = sign_in("kid", "pw")
    assert kid.post("/api/purge", json={
        "files": [{"folder": "init_2026", "name": "a.jpg"}]
    }).status_code in (401, 403)
    assert (writable / "a.jpg").is_file()


def test_purging_is_recorded_but_offers_no_revert(
    client: TestClient, writable: Path
) -> None:
    """*What happened to that photograph* is a real question, so it appears in
    History — without an undo that could not work."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "deleted": True})
    client.post("/api/purge", json={
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    op = history.recent(1)[0]
    assert op.summary == "purged 1 file", op.summary
    assert not op.files, "a purge that offers files to put back"


def test_restoring_puts_the_file_back_with_what_it_said(
    client: TestClient, writable: Path
) -> None:
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "event": "Sicily Trip",
        "add_audience": ["family"], "deleted": True})

    client.post("/api/decide/bulk", json={
        "files": [{"folder": "init_2026", "name": "a.jpg"}], "deleted": False})

    assert decisions.read(writable / "a.jpg") == Decision(
        event="Sicily Trip", audience=("family",))
    assert "a.jpg" in client.get("/browse?event=Sicily%20Trip").text


def test_everything_the_script_hides_can_actually_be_hidden(
    client: TestClient
) -> None:
    """`hidden` is a flag, not a guarantee. A CSS rule that gives an element a
    `display` outranks the user agent's `[hidden] { display:none }`, and then
    setting the property hides nothing — which is how the menu once became
    undismissable, and how every action stayed on screen with nothing selected.

    So: anything the script hides by property needs a rule saying what hidden
    means for it."""
    css = client.get("/browse?event=Italy%20-%20Sicily").text

    for sel in ("#menu[hidden]", "#actions .grp[hidden]"):
        assert sel in css, sel


def test_a_row_reserves_the_height_of_the_controls_in_it(
    client: TestClient
) -> None:
    """The action row empties and fills as the selection changes, and it sits
    above the grid — so if the height it reserves is not the height a button
    actually takes, every thumbnail on the page moves on the first click. The
    two have to be one number, not two that agree today."""
    css = client.get("/browse?event=Italy%20-%20Sicily").text

    assert "--ctl:" in css
    assert "min-height:var(--ctl)" in css
    # A stacked row carries its own padding and rule, and `box-sizing:
    # border-box` puts both *inside* the min-height — so it has to reserve the
    # control plus that chrome, or it reserves 22 pixels for a 31-pixel button.
    assert "box-sizing: border-box" in css
    assert "min-height:calc(var(--ctl) + 8px + 1px)" in css
    # Not on the buttons as well: `.tick`, `.grppick` and `.pick` are buttons
    # sized in fixed pixels, and a min-height outranks their `height` — which
    # would make an oval of every select circle in the grid.
    assert "button, .chip { min-height" not in css


def test_the_actions_and_filters_ask_the_same_questions_in_the_same_order(
    client: TestClient
) -> None:
    """Two bars that read the same way. Learning one teaches the other, and a
    control that moves between them is a control you have to find twice."""
    html = client.get("/browse?event=Italy%20-%20Sicily").text

    acts = re.findall(r'data-act="(\w+)"', html)
    # What a file is, then which of them speaks for the rest, then what
    # happens to it.
    assert acts[:11] == ["event", "tags",
                         # Who is *in* it sits with what it is a picture of,
                         # because that is the same kind of question. Who may
                         # *see* it is further along, with the rest of what
                         # the file is for.
                         "people", "date", "access",
                         "stack", "top", "unstack", "nostack",
                         # Taking a copy away is not doing anything to the
                         # library, so it sits with the rest rather than over
                         # the bar with the one thing that is.
                         "download", "delete"], acts

    chips = html[html.index("CHIPS="):html.index("FIXED=")]
    for earlier, later in (("event", "tag"), ("tag", "person"),
                           ("person", "date"),
                           ("date", "audience"), ("audience", "kind"),
                           ("kind", "band"), ("band", "stacks"),
                           ("stacks", "deleted")):
        assert chips.index(f'"{earlier}"') < chips.index(f'"{later}"'),             f"{earlier} should come before {later}: {chips}"


def test_one_bar_separates_what_it_is_from_what_happens_to_it(
    client: TestClient
) -> None:
    """Bars between every pair said there were four groups when there are two:
    the file's own facts, and the things you do to it."""
    html = client.get("/browse?event=Italy%20-%20Sicily").text
    row = html[html.index('id="actions"'):html.index("</div>",
                                                     html.index('id="actions"'))]

    assert row.count('class="sep"') == 1, row


def test_the_filter_chips_are_spaced(client: TestClient) -> None:
    """They had no container rule at all, so they sat against one another and
    read as one control."""
    html = client.get("/browse?event=Italy%20-%20Sicily").text

    # The rule for the container itself, not the one that says how it shares
    # the row — both mention `.chips` and only one of them is about spacing.
    at = html.index(chr(10) + ".chips {")
    assert "gap:9px" in html[at:at + 120]


def test_the_page_is_handed_every_filter_it_is_showing(
    client: TestClient
) -> None:
    """The page rebuilds its own address from this — for the chips, and for the
    query it sends with a write so the server can say which files have left the
    view. A filter missing here is a chip that cannot show what it is set to and
    an edit that reports itself as made somewhere else."""
    html = client.get("/browse?deleted=only&event=Italy%20-%20Sicily").text
    view = html[html.index("const VIEW="):html.index(",CHIPS")]

    for name in ("event", "tag", "date", "audience", "kind", "band", "deleted"):
        assert f'"{name}"' in view, f"{name} missing from {view}"
    assert '"deleted": "only"' in view, view


def test_the_bars_are_not_the_colour_of_the_page(client: TestClient) -> None:
    """They were, separated by a single line — which put the tick that selects
    the whole library a few pixels above the one that selects the first group,
    on the same background, looking like the same kind of control. Reaching for
    one and getting the other is a mistake the colouring was inviting."""
    css = client.get("/browse?event=Italy%20-%20Sicily").text

    assert "--chrome:" in css
    assert "background:var(--chrome)" in css
    # And not on the page itself, or there would be nothing to tell apart.
    body = css[css.index("body {"):css.index("body {") + 90]
    assert "var(--bg)" in body, body


def _two_files(writable: Path) -> None:
    """The fixture puts one file in master; a stack needs at least two."""
    (writable / "b.mp4").write_bytes(b"fake")


def test_stacking_folds_a_file_behind_another(
    client: TestClient, writable: Path
) -> None:
    """Each file records which one it defers to; the top records nothing,
    because being spoken for is the decision and speaking is what is left."""
    _two_files(writable)
    r = client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})
    assert r.status_code == 200, r.text

    assert decisions.read(writable / "b.mp4") == Decision(
        stacked_under="init_2026/a.jpg")
    assert decisions.read(writable / "a.jpg") is None, "the top recorded something"

    html = client.get("/browse?event=Italy%20-%20Sicily").text
    assert "b.mp4" not in html, "a stacked file appeared on its own"
    assert "a.jpg" in html
    assert 'class="stack"' in html, "the top is not badged"
    assert "within=init_2026%2Fa.jpg" in html, "no way to open the stack"


def test_opening_a_stack_shows_what_is_behind_it(
    client: TestClient, writable: Path
) -> None:
    _two_files(writable)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})

    html = client.get("/browse?within=init_2026/a.jpg").text
    assert "a.jpg" in html and "b.mp4" in html


def test_unstacking_puts_a_file_back_on_its_own(
    client: TestClient, writable: Path
) -> None:
    _two_files(writable)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})
    client.post("/api/decide/bulk", json={
        "stacked_under": None,
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})

    assert decisions.read(writable / "b.mp4") is None
    assert "b.mp4" in client.get("/browse?event=Italy%20-%20Sicily").text


def test_stacking_leaves_the_other_decisions_alone(
    client: TestClient, writable: Path
) -> None:
    """It is one field like the rest: a file keeps its event and its audience
    when it goes behind another, and gets them back when it comes out."""
    _two_files(writable)
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "b.mp4", "event": "Sicily Trip",
        "add_audience": ["family"]})
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})

    assert decisions.read(writable / "b.mp4") == Decision(
        event="Sicily Trip", audience=("family",),
        stacked_under="init_2026/a.jpg")


def test_a_stack_is_recorded_and_can_be_put_back(
    client: TestClient, writable: Path
) -> None:
    """Like any other decision — nothing new was built for the undo."""
    _two_files(writable)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})

    op = history.recent()[0]
    assert op.summary == "stacked 1 file under a.jpg", op.summary

    client.post("/history/revert", data={"id": op.id})
    assert decisions.read(writable / "b.mp4") is None
    assert "b.mp4" in client.get("/browse?event=Italy%20-%20Sicily").text


def test_the_page_can_ask_which_photograph_to_show(
    client: TestClient
) -> None:
    """Stacking narrows the grid to the files being stacked and waits for one
    of them to be chosen, rather than taking whichever was ticked first — a
    rule nothing on screen ever said."""
    html = client.get("/browse?event=Italy%20-%20Sicily").text

    assert 'data-side="choose"' in html
    assert 'id="choosecancel"' in html
    # And the grid can actually hide what it narrows away. `.cell` sets no
    # display of its own, but four rules in this file have needed saying so.
    assert ".cell[hidden]" in html


def _three_files(writable: Path, app_env: dict[str, Path]) -> None:
    """A third file, in master *and* in the index.

    The cascade that keeps a stack together reads the index to find what is
    behind a file — so a file only on disk is a file it cannot see. That is the
    architecture working as intended (the index is behind, never wrong), and it
    made the first version of these tests lie.
    """
    import json

    from pix.nas import index as ix

    (writable / "b.mp4").write_bytes(b"fake")
    (writable / "c.jpg").write_bytes(b"fake")
    share = app_env["share"]
    (share / "meta" / "init_2026" / "c.jpg.json").write_text(json.dumps({
        "file": "c.jpg", "folder": "init_2026", "size": 30, "mtime_ns": 1,
        "exif": {"EXIF:DateTimeOriginal": "2026:08:30 15:40:00",
                 "XMP:EventAuto": "Italy - Sicily"},
    }), encoding="utf-8")
    ix.build(app_env["db"], meta_dir=share / "meta",
             master_dir=share / "master")


def test_stacking_a_stack_brings_its_files_up(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Stacks are flat. Stacking a file that already speaks for others reads as
    *put all of these together* — and the alternative is not a deeper stack, it
    is a stranded one: the members end up a level down where no listing reaches
    them, and the count on the outermost file is wrong about what it holds."""
    _three_files(writable, app_env)
    # b.mp4 goes behind a.jpg.
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})
    # Now a.jpg — which speaks for b.mp4 — goes behind c.jpg.
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/c.jpg",
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    assert decisions.read(writable / "a.jpg") == Decision(
        stacked_under="init_2026/c.jpg")
    assert decisions.read(writable / "b.mp4") == Decision(
        stacked_under="init_2026/c.jpg"), "left a level down"

    inside = client.get("/browse?within=init_2026/c.jpg").text
    for name in ("a.jpg", "b.mp4", "c.jpg"):
        assert name in inside, name
    # And nothing inside it claims a stack of its own.
    assert 'class="stack"' not in inside


def test_an_open_stack_says_which_one_is_the_top(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Everything in a stack looks alike — that is why they were stacked — so
    without this there is nothing to say which one the grid outside will show.

    Not the count again: a depth badge inside the thing it measures reads as a
    stack within a stack, and it would link to where you are standing."""
    _three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})

    outside = client.get("/browse?event=Italy%20-%20Sicily").text
    assert 'class="stack"' in outside
    assert 'class="top-mark"' not in outside, "marked where the count belongs"

    inside = client.get("/browse?within=init_2026/a.jpg").text
    assert 'class="stack"' not in inside
    assert inside.count('class="top-mark"') == 1, "one speaks, not none or both"
    # And it is on the right one.
    a_cell = inside[inside.index('data-name="a.jpg"'):]
    assert 'class="top-mark"' in a_cell[:a_cell.index("</div>") + 400]


def test_a_stack_mark_does_not_cover_the_tags(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Both marks stand where the tag chips do, and were simply sitting on top
    of them."""
    _three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_tags": ["beach"]})

    html = client.get("/browse?event=Italy%20-%20Sicily").text
    assert "cell marked" in html
    assert ".cell.marked .tags" in html


def test_bringing_a_stack_up_is_part_of_the_same_gesture(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The files that came with it are in the same operation, so putting it
    back puts all of it back."""
    _three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/c.jpg",
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    op = history.recent()[0]
    assert {f.name for f in op.files} == {"a.jpg", "b.mp4"}, [
        f.name for f in op.files]

    client.post("/history/revert", data={"id": op.id})
    assert decisions.read(writable / "b.mp4") == Decision(
        stacked_under="init_2026/a.jpg"), "it did not go back where it was"


def test_the_page_knows_it_is_inside_a_stack(client: TestClient) -> None:
    """It has to send that back with a write, or the server works out what left
    the view against a different view — and a file taken out of a stack sits
    there until the page is reloaded."""
    html = client.get("/browse?within=init_2026/a.jpg").text
    view = html[html.index("const VIEW="):html.index(",CHIPS")]

    assert '"within": "init_2026/a.jpg"' in view, view


def test_unstacking_takes_a_file_out_of_the_open_stack(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    _three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})

    r = client.post("/api/decide/bulk?within=init_2026/a.jpg", json={
        "stacked_under": None,
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})

    assert [d["name"] for d in r.json()["dropped"]] == ["b.mp4"], r.text


def test_unstacking_the_top_takes_the_whole_stack_apart(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """*Unstack this* said of the file that speaks means the stack, not the one
    photograph. Leaving the others deferring to a file that defers to nobody
    would leave a stack nobody asked to keep — and one with no way back to it,
    since the badge is drawn from what is behind the top."""
    _three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"},
                  {"folder": "init_2026", "name": "c.jpg"}]})

    client.post("/api/decide/bulk", json={
        "stacked_under": None,
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    for name in ("a.jpg", "b.mp4", "c.jpg"):
        assert decisions.read(writable / name) is None, name
    html = client.get("/browse?event=Italy%20-%20Sicily").text
    for name in ("a.jpg", "b.mp4", "c.jpg"):
        assert name in html, name
    assert 'class="stack"' not in html


def test_taking_one_photograph_out_leaves_the_rest_stacked(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The same rule, the other way up: a file that speaks for nobody has
    nothing to cascade."""
    _three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"},
                  {"folder": "init_2026", "name": "c.jpg"}]})

    client.post("/api/decide/bulk", json={
        "stacked_under": None,
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})

    assert decisions.read(writable / "b.mp4") is None
    assert decisions.read(writable / "c.jpg") == Decision(
        stacked_under="init_2026/a.jpg"), "the rest came out too"


def test_taking_a_stack_apart_is_one_gesture(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """So putting it back puts the stack back, rather than one photograph of
    it."""
    _three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"},
                  {"folder": "init_2026", "name": "c.jpg"}]})
    client.post("/api/decide/bulk", json={
        "stacked_under": None,
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    op = history.recent()[0]
    assert {f.name for f in op.files} == {"a.jpg", "b.mp4", "c.jpg"}, [
        f.name for f in op.files]

    client.post("/history/revert", data={"id": op.id})
    assert decisions.read(writable / "b.mp4") == Decision(
        stacked_under="init_2026/a.jpg")
    assert decisions.read(writable / "c.jpg") == Decision(
        stacked_under="init_2026/a.jpg")


def test_the_files_behind_a_stack_can_be_fetched_as_cells(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Merging two stacks has to offer every photograph in both of them, and
    the members are not on the page — that is what stacking them did. They come
    back as the same markup the grid is made of, because a second copy of a
    cell written in JavaScript would drift from this one."""
    _three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"},
                  {"folder": "init_2026", "name": "c.jpg"}]})

    cells = client.get("/api/behind/init_2026/a.jpg").json()["cells"]

    assert 'data-name="b.mp4"' in cells
    assert 'data-name="c.jpg"' in cells
    assert 'data-name="a.jpg"' not in cells, "returned the top as well"
    assert 'class="cell' in cells and 'class="pick"' in cells


def test_what_is_behind_a_stack_is_still_scoped_to_the_viewer(
    client: TestClient, sign_in: "Callable[[str, str], TestClient]",
    writable: Path, app_env: dict[str, Path]
) -> None:
    """Fetching cells is a listing like any other. A stack is not a way to be
    handed photographs nobody shared with you."""
    _three_files(writable, app_env)
    client.post("/accounts/save", data={"name": "kid", "password": "pw"})
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})

    kid = sign_in("kid", "pw")
    assert kid.get("/api/behind/init_2026/a.jpg").json()["cells"] == ""


def test_promoting_a_file_does_not_leave_it_behind_itself(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """A top is a file nothing is behind. Stacking onto one that is itself
    stacked left a ring — it deferred to the file now deferring to it — and a
    ring shows nowhere, because every file in it is behind something. The whole
    stack vanished from the library."""
    _three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})

    # Promote b.mp4: everything else comes to defer to it.
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/b.mp4",
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    assert decisions.read(writable / "b.mp4") is None, "left behind itself"
    assert decisions.read(writable / "a.jpg") == Decision(
        stacked_under="init_2026/b.mp4")
    # And the stack is on screen, with the promoted file speaking for it.
    html = client.get("/browse?event=Italy%20-%20Sicily").text
    assert "b.mp4" in html
    assert "a.jpg" not in html
    assert 'class="stack"' in html


def test_taking_a_file_out_of_a_stack_is_part_of_promoting_it(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """One gesture, so one entry — and putting it back puts the stack back the
    way round it was."""
    _three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"},
                  {"folder": "init_2026", "name": "c.jpg"}]})
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/b.mp4",
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    op = history.recent()[0]
    assert {f.name for f in op.files} == {"a.jpg", "b.mp4", "c.jpg"}, [
        f.name for f in op.files]

    client.post("/history/revert", data={"id": op.id})
    assert decisions.read(writable / "b.mp4") == Decision(
        stacked_under="init_2026/a.jpg")
    assert decisions.read(writable / "a.jpg") is None


def test_a_stale_index_cannot_put_a_file_behind_itself(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The cascade asks the index what is behind a file, and the index is
    allowed to be behind — that is the whole bargain. So the two can disagree
    about whether the file being stacked onto is already in the stack, and the
    one that thinks it is would write it behind itself."""
    _three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})

    # Master says b.mp4 is free; the index still says it is behind a.jpg.
    decisions.sidecar_path(writable / "b.mp4").unlink()

    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/b.mp4",
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    assert decisions.read(writable / "b.mp4") is None, "put behind itself"


def test_the_grid_has_three_thumbnail_sizes(client: TestClient) -> None:
    """The third is where the thumbnail runs out. Cells stretch past their
    minimum to fill the row, so 230px already renders around 263 on a wide
    screen — from a 400px derived thumbnail, which is spent at that point. The
    largest reads `large`, derived at 1000px for exactly this."""
    html = client.get("/browse?event=Italy%20-%20Sicily").text

    assert 'id="sizepick"' in html
    # At the far end of the row with the account, not among the filters: it
    # changes how you are looking, never which photographs are here. Past the
    # spacer is what puts it there; after the chips is true of anything in the
    # row.
    row = html[html.index('class="row"'):html.index("</div><main")]
    assert row.index('id="sizepick"') > row.index("spacer")
    assert row.index('id="sizepick"') < row.index("who-link")

    for rule in ("minmax(150px,1fr)", "minmax(230px,1fr)", "minmax(380px,1fr)"):
        assert rule in html, rule
    for size in ("medium", "large"):
        assert f'.grid[data-size="{size}"]' in html, size


def test_promoting_renames_the_stack_so_its_old_address_empties(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """A stack is named by the file that speaks for it, so promoting one
    renames it. The old address then holds one photograph and no stack — which
    is what stranded somebody who did this from inside the stack and had
    nowhere to go but the browser's back button.

    The page follows the rename; this is the server half of why it has to."""
    _three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"},
                  {"folder": "init_2026", "name": "c.jpg"}]})
    assert client.get("/browse?within=init_2026/a.jpg").text.count(
        "data-name=") == 3

    # Promote c.jpg the way `Make top` does.
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/c.jpg",
        "files": [{"folder": "init_2026", "name": "a.jpg"},
                  {"folder": "init_2026", "name": "b.mp4"}]})

    was = client.get("/browse?within=init_2026/a.jpg").text
    assert was.count("data-name=") == 1, "the old address still holds a stack"
    now = client.get("/browse?within=init_2026/c.jpg").text
    assert now.count("data-name=") == 3, "the stack is not at its new address"


def _burst(app_env: dict[str, Path], writable: Path, *names: str) -> None:
    """Two files a second apart on one camera, in master and in the index."""
    import json

    from pix.nas import index as ix

    share = app_env["share"]
    for i, name in enumerate(names):
        (writable / name).write_bytes(b"fake")
        (share / "meta" / "init_2026" / f"{name}.json").write_text(json.dumps({
            "file": name, "folder": "init_2026", "size": 30, "mtime_ns": 1,
            "exif": {"EXIF:DateTimeOriginal": f"2026:08:30 11:00:0{i}",
                     "EXIF:Model": "iPhone 17 Pro"},
        }), encoding="utf-8")
    ix.build(app_env["db"], meta_dir=share / "meta",
             master_dir=share / "master")


def test_the_apps_guesses_can_be_switched_off(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """They fold by default — most of a burst is one photograph shot eight
    times, and a library showing all eight is the pile you started with. But
    *only the stacks I made* is a real question, and this is it."""
    _burst(app_env, writable, "x.jpg", "y.jpg")

    html = client.get("/browse?stacks=firm").text

    assert 'data-name="x.jpg"' in html and 'data-name="y.jpg"' in html
    assert "stack guessed" not in html


def test_a_guessed_stack_folds_like_a_stack_and_says_it_is_a_guess(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """One photograph on screen and the rest behind it — otherwise it is not a
    suggestion, it is the pile you already had. Marked as a guess, because a
    count nobody confirmed and a count somebody decided are different claims,
    and the curator is deciding which to trust."""
    _burst(app_env, writable, "x.jpg", "y.jpg")

    html = client.get("/browse").text

    assert 'data-name="x.jpg"' in html, "nothing speaks for the group"
    assert 'data-name="y.jpg"' not in html, "the group is not folded"
    assert "stack guessed" in html
    assert 'data-proposed="1"' in html


def test_a_viewer_never_has_a_photograph_hidden_by_a_guess(
    client: TestClient, writable: Path, app_env: dict[str, Path],
    sign_in: "Callable[[str, str], TestClient]",
    add_user: "Callable[..., None]"
) -> None:
    """Folding on a guess is the app deciding, on its own evidence, that
    several files are one. That is a curator's call, and somebody who cannot
    make it has no way to see what was folded away — so for them a stack is
    only ever one a person made.

    Not a matter of dropping the parameter any more: folding is what the
    default does, so leaving it unset would fold for everyone."""
    _burst(app_env, writable, "x.jpg", "y.jpg")
    add_user("kid", "pw")
    client.post("/api/decide/bulk", json={
        "add_audience": ["kid"],
        "files": [{"folder": "init_2026", "name": "x.jpg"},
                  {"folder": "init_2026", "name": "y.jpg"}]})
    kid = sign_in("kid", "pw")

    for url in ("/browse", "/browse?stacks=only", "/browse?stacks=guesses"):
        html = kid.get(url).text
        assert 'data-name="x.jpg"' in html, url
        assert 'data-name="y.jpg"' in html, f"{url} folded on a guess"


def test_only_stacks_is_every_stack_however_it_was_made(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The chip is about stacks, so *only stacks* means the ones somebody made
    as well as the ones the app proposes — they are the same thing decided by
    different parties."""
    (writable / "b.mp4").write_bytes(b"fake")
    _burst(app_env, writable, "x.jpg", "y.jpg")
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})

    html = client.get("/browse?stacks=only").text

    assert 'data-name="a.jpg"' in html, "a stack somebody made is not a stack"
    assert 'data-name="x.jpg"' in html, "a stack the app proposed is not one"

    # Where *only suggested* is the narrower question: what is still to answer.
    guesses = client.get("/browse?stacks=guesses").text
    assert 'data-name="x.jpg"' in guesses
    assert 'data-name="a.jpg"' not in guesses, "already answered"


def test_the_stacks_chip_offers_only_what_narrows_the_view(
    client: TestClient
) -> None:
    """Three short answers to one question — *what about the stacks* — and no
    entry for the ordinary view, the same as every other chip. *Not filtering
    on this* is what the cross says, and a value that only clears the filter
    is a second way to say it sitting among the ones that do something."""
    html = client.get("/browse").text
    fixed = html[html.index("FIXED="):html.index("EXTRA=")]
    stacks = fixed[fixed.index('"stacks"'):]

    for label in ("Only stacks", "Only suggested", "No suggestions"):
        assert label in stacks, stacks
    assert '[""' not in stacks and '""]' not in stacks, "a value that clears"
    assert "Including suggestions" not in fixed, "the long way round"


def test_only_suggested_is_the_shelf_of_what_is_still_to_answer(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """A view of nothing but the unanswered. Grouped by stack it is the whole
    review: every guess, open, one section each."""
    _burst(app_env, writable, "x.jpg", "y.jpg")

    folded = client.get("/browse?stacks=guesses").text
    assert 'data-name="x.jpg"' in folded
    assert 'data-name="a.jpg"' not in folded, "offered one with nothing to say"

    opened = client.get("/browse?stacks=guesses&group=stack").text
    assert 'data-name="x.jpg"' in opened and 'data-name="y.jpg"' in opened
    assert 'data-name="a.jpg"' not in opened
    assert opened.count("h3 class=") == 1, "not one section per guess"


def test_a_stack_section_is_headed_by_its_moment(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """A stack is identified by the file that speaks for it, and a generated
    name is the date, the camera and the extension run together — forty
    characters of machinery where the useful part is *when*."""
    _burst(app_env, writable, "x.jpg", "y.jpg")

    html = client.get("/browse?stacks=only&group=stack").text

    assert "Sunday 30 August 2026, 11:00" in html, "headed by a filename"

    nested = client.get("/browse?stacks=only&group=day,stack").text
    assert ">11:00<" in nested, "the day is repeated inside itself"


def test_grouping_by_stack_opens_what_is_stacked_and_leaves_the_rest(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The members come out from behind what speaks for them — that is what
    opening is. A photograph in no stack is not a stack of one, and a heading
    per thumbnail would bury the sections that mean something."""
    _burst(app_env, writable, "x.jpg", "y.jpg")

    html = client.get("/browse?group=stack").text

    assert 'data-name="x.jpg"' in html and 'data-name="y.jpg"' in html
    assert 'data-name="a.jpg"' in html, "the rest of the library left the view"
    assert "Not in a stack" in html


def test_the_count_agrees_with_the_grid_when_stacks_are_opened(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """One place decides what a listing holds. The header count, the grid and
    the *has this left the view* check had three chances to disagree."""
    _burst(app_env, writable, "x.jpg", "y.jpg")

    folded = client.get("/browse?stacks=guesses").text
    assert "1 files" in folded, "the count does not match the one cell"
    opened = client.get("/browse?stacks=guesses&group=stack").text
    assert "2 files" in opened, "the count does not match the opened stack"


def test_a_guess_is_only_for_the_person_who_can_answer_it(
    client: TestClient, writable: Path, app_env: dict[str, Path],
    sign_in: "Callable[[str, str], TestClient]",
    add_user: "Callable[..., None]"
) -> None:
    """It hides photographs on the strength of a guess. Somebody who cannot
    accept or refuse one should not be able to turn it on — and asking for it
    in the address bar is how *hidden from the bar* would have been found out
    to mean nothing."""
    _burst(app_env, writable, "x.jpg", "y.jpg")
    add_user("kid", "pw")
    client.post("/api/decide/bulk", json={
        "add_audience": ["kid"],
        "files": [{"folder": "init_2026", "name": "x.jpg"},
                  {"folder": "init_2026", "name": "y.jpg"}]})

    html = sign_in("kid", "pw").get("/browse").text

    assert 'data-name="x.jpg"' in html and 'data-name="y.jpg"' in html
    assert '"stacks"' not in html[html.index("CHIPS="):html.index("FIXED=")]


def test_a_decision_on_a_folded_guess_reaches_what_it_hides(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The whole point of folding is to work as though there is one file, so a
    decision made about what is on screen is a decision about all of them —
    exactly the rule a stack somebody made already follows."""
    _burst(app_env, writable, "x.jpg", "y.jpg")

    client.post("/api/decide/bulk", json={
        "event": "Sports Day",
        "files": [{"folder": "init_2026", "name": "x.jpg"}]})

    assert decisions.read(writable / "y.jpg") == Decision(event="Sports Day")


def test_a_decision_with_the_guessing_off_reaches_only_what_was_picked(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Nothing is hiding behind it there, so there is nothing to follow."""
    _burst(app_env, writable, "x.jpg", "y.jpg")

    client.post("/api/decide/bulk?stacks=firm", json={
        "event": "Sports Day",
        "files": [{"folder": "init_2026", "name": "x.jpg"}]})

    assert decisions.read(writable / "y.jpg") is None


def test_refusing_a_guess_reaches_every_photograph_in_it(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Written to all of them before any of them is answered. Recorded on the
    one that speaks first, the index would regroup the rest behind a new
    leader and offer the same guess again tomorrow."""
    _burst(app_env, writable, "x.jpg", "y.jpg")

    client.post("/api/decide/bulk", json={
        "no_stack": True,
        "files": [{"folder": "init_2026", "name": "x.jpg"}]})

    assert decisions.read(writable / "x.jpg") == Decision(no_stack=True)
    assert decisions.read(writable / "y.jpg") == Decision(no_stack=True)
    assert "stack guessed" not in client.get("/browse").text
    assert ('data-name="x.jpg"'
            not in client.get("/browse?stacks=guesses").text)


def test_accepting_a_guess_is_an_ordinary_stack(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Nothing special is written. A guess accepted is a stack, made the way
    any other is — so it unstacks, reverts and behaves like one."""
    _burst(app_env, writable, "x.jpg", "y.jpg")

    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/x.jpg",
        "files": [{"folder": "init_2026", "name": "y.jpg"}]})

    assert decisions.read(writable / "y.jpg") == Decision(
        stacked_under="init_2026/x.jpg")
    html = client.get("/browse").text
    assert "stack guessed" not in html, "still offered as a guess"
    assert 'class="stack"' in html, "not a stack"
    assert ('data-name="x.jpg"'
            not in client.get("/browse?stacks=guesses").text)


def test_refusing_is_recorded_and_can_be_taken_back(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """It is a decision like the others, so it is in the log and revertible —
    which is the way back if a shelf of them is waved off by mistake."""
    _burst(app_env, writable, "x.jpg", "y.jpg")
    client.post("/api/decide/bulk", json={
        "no_stack": True,
        "files": [{"folder": "init_2026", "name": "x.jpg"}]})

    op = history.recent()[0]
    assert op.summary == "said 2 files are not a stack", op.summary

    client.post("/history/revert", data={"id": op.id})
    assert decisions.read(writable / "x.jpg") is None
    assert "stack guessed" in client.get("/browse").text


def test_an_opened_stack_holds_what_it_hides_however_it_got_there(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Opening one is how the choosing works, and a guessed stack is chosen
    between exactly like a decided one — in a view that is folding guesses."""
    _burst(app_env, writable, "x.jpg", "y.jpg")

    behind = client.get("/api/behind/init_2026/x.jpg").json()["cells"]

    assert 'data-name="y.jpg"' in behind
    assert 'data-name="x.jpg"' not in behind, "the stack holds itself"


def test_a_guess_is_nothing_at_all_until_it_is_turned_on(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The badge said nothing and the grid treated the file as a stack: no
    mark on it, but *Stack* opened it and more photographs came back than had
    been selected. One rule decides whether a guess counts, and the cell the
    page is built from has to answer to it like everything else."""
    _burst(app_env, writable, "x.jpg", "y.jpg")

    off = client.get("/browse?stacks=firm").text
    assert 'data-proposed="0"' in off
    assert 'data-proposed="1"' not in off, "the grid sees a stack nobody drew"

    # And nothing is gathered up by opening the file it resembles.
    assert client.get(
        "/api/behind/init_2026/x.jpg?stacks=firm").json()["cells"] == ""

    # On, it is a stack in every sense at once.
    on = client.get("/browse").text
    assert 'data-proposed="1"' in on and "stack guessed" in on


def test_the_grid_draws_no_cursor(client: TestClient) -> None:
    """The dashed ring said which cell the keyboard was on, and the grid has no
    keyboard. It stayed behind after that was removed and turned up unasked on
    whatever a delete happened to land the cursor on, reading as a selection
    nobody had made. The class survives — the viewer needs to know which file
    it is showing — but nothing paints it."""
    html = client.get("/browse?event=Italy%20-%20Sicily").text
    assert ".cell.cur" not in html


def test_every_page_says_which_version_it_is(client: TestClient) -> None:
    """The CLI prints it on every run so dev and tester stay aligned; the app
    had no equivalent, and a stale browser tab was indistinguishable from a
    broken build for an afternoon. Read dynamically: a hard-coded number here
    would be one more thing to forget to bump."""
    from pix import __version__

    for path in ("/", "/browse?event=Italy%20-%20Sicily", "/login", "/accounts"):
        assert f"v{__version__}" in client.get(path).text, path


def test_the_write_takeover_is_on_the_page(client: TestClient) -> None:
    """The script hides and shows it, so it has to be there to find.

    Every part of this shipped and none of it appeared, because the page in
    the browser was one the old server had sent: a stale tab and a broken
    build look identical. The script is tested where it runs; this is the
    other half — that the markup it reaches for was actually sent.
    """
    html = client.get("/browse?event=Italy%20-%20Sicily").text
    for hook in ('id="working"', 'id="workwhat"', 'id="workbar"',
                 'id="worktally"'):
        assert hook in html, hook
    assert "#working.on" in html, "the takeover has no way to become visible"


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


def test_the_landing_page_is_folders_of_whatever_it_is_grouped_by(
    client: TestClient
) -> None:
    """It was a fixed table of years and their events. The grid already knows
    how to cut a library eight ways, and those are the same questions asked of
    the same files — so this is the grid at a coarser zoom, not a second idea
    of what the library looks like."""
    by_year = client.get("/?group=year").text
    assert 'class="tile"' in by_year
    assert ">2026<" in by_year

    by_event = client.get("/?group=event").text
    assert "Italy - Sicily" in by_event
    assert ">2026<" not in by_event, "still cut by year"

    by_camera = client.get("/?group=camera").text
    assert "(unknown)" in by_camera


def _spread(app_env: dict[str, Path], writable: Path,
            dates: dict[str, str]) -> None:
    """Files on given days, in master and in the index."""
    import json

    from pix.nas import index as ix

    share = app_env["share"]
    for name, when in dates.items():
        (writable / name).write_bytes(b"fake")
        (share / "meta" / "init_2026" / f"{name}.json").write_text(json.dumps({
            "file": name, "folder": "init_2026", "size": 30, "mtime_ns": 1,
            "exif": {"EXIF:DateTimeOriginal": when},
        }), encoding="utf-8")
    ix.build(app_env["db"], meta_dir=share / "meta",
             master_dir=share / "master")


def _shelf(folders: str, heading: str) -> str:
    """One shelf of the landing page, from its heading to the next."""
    at = folders.index(heading)
    end = folders.find("<h3", at)
    return folders[at:end if end > 0 else len(folders)]


def _folders(html: str) -> str:
    """Just the folders. The page script is inlined below them and mentions
    `/thumb/` and half the words on the card."""
    return html[html.index('id="grid"'):html.index("</div></main>")]


def test_the_outer_groupings_are_shelves_not_prefixes(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """`year › event` is a row of events under each year, not a flat list of
    cards each repeating which year it is in. The grid reads that way and this
    is the same library."""
    _burst(app_env, writable, "x.jpg", "y.jpg")
    client.post("/api/decide/bulk", json={
        "event": "Sports Day",
        "files": [{"folder": "init_2026", "name": "x.jpg"}]})

    folders = _folders(client.get("/?group=year,event").text)

    assert folders.count("<h3") >= 1, "no shelves"
    # The heading says where you are and what the cards are cut by; either
    # half can be clicked to change that level.
    assert ">2026<" in folders and "By event" in folders
    # And the card says only what it is.
    names = re.findall(r'<b class="name">([^<]*)</b>', folders)
    assert "Sports Day" in names, names
    assert not any("2026" in n for n in names), names


def test_one_grouping_is_one_shelf(client: TestClient) -> None:
    """Nothing above it to say, so the heading is the cut itself — which is
    still the control, because it is the only way to change it."""
    folders = _folders(client.get("/?group=year").text)

    assert folders.count("<h3") == 1
    assert "By year" in folders


def test_the_front_door_opens_on_this_year(client: TestClient) -> None:
    """A library of twenty-five years opened on all of it, which is not where
    anybody is working. This year, by month and then by event."""
    r = client.get("/", follow_redirects=False)

    assert r.status_code == 303
    where = r.headers["location"]
    assert f"date={time.localtime().tm_year}" in where, where
    assert "group=month%2Cevent" in where, where


def test_the_default_can_be_taken_off(client: TestClient) -> None:
    """Which is why it is a redirect and not a default inside the page. Taken
    off invisibly, the year would go straight back on and the chip would be a
    control that does nothing."""
    cleared = client.get("/?group=year", follow_redirects=False)

    assert cleared.status_code == 200
    assert 'class="tile"' in cleared.text


def test_the_shelf_reads_the_other_way_round(client: TestClient) -> None:
    """On a shelf the last crumb names the *cut* — *By event* — and it is the
    same two words over every shelf on the page. What says which shelf this is
    is the value in front of it, so the emphasis runs the other way."""
    html = client.get("/?group=year,event").text
    css = html[html.index("<style>"):html.index("</style>")]

    assert 'class="group shelf"' in html, "the heading is not marked as one"
    at = css.index("h3.group.shelf .crumb:last-child .grpname {")
    assert "var(--dim)" in css[at:at + 110], css[at:at + 110]
    at = css.index("h3.group.shelf .crumb:not(:last-child) .grpname {")
    assert "font-weight:600" in css[at:at + 180], css[at:at + 180]


def test_a_folder_says_what_is_in_it_rather_than_showing_one_of_it(
    client: TestClient
) -> None:
    """A cover was whichever file happened to be first, which said what one
    picture in there looks like and nothing about the section. What you want
    before opening a folder is how much, when, and how much is left to do."""
    folders = _folders(client.get("/?group=event").text)

    assert "/thumb/" not in folders, "still picking a photograph to stand for it"
    assert "2 files" in folders
    assert "1 video" in folders, "no sense of what kind of files"
    assert "2026" in folders, "no sense of when"
    assert "undecided" in folders


def test_a_folder_does_not_print_the_date_its_own_name_is(
    client: TestClient
) -> None:
    """Grouped by day the name *is* the date, and a card saying it twice looks
    like it is telling you two different things."""
    by_day = _folders(client.get("/?group=day").text)
    assert 'class="when"' not in by_day, "the day is printed twice"

    # Where the name is not the date, when it happened is worth saying.
    by_event = _folders(client.get("/?group=event").text)
    assert 'class="when"' in by_event


def test_a_folder_split_by_the_grouping_says_so(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """An event running from one month into the next is a card under each. A
    folder saying *2 files* with nothing to say it is part of five is a folder
    describing the grouping rather than the library."""
    _spread(app_env, writable, {"jan.jpg": "2026:01:20 10:00:00",
                                "feb1.jpg": "2026:02:02 10:00:00",
                                "feb2.jpg": "2026:02:03 10:00:00"})
    client.post("/api/decide/bulk", json={
        "event": "Ski Trip",
        "files": [{"folder": "init_2026", "name": n}
                  for n in ("jan.jpg", "feb1.jpg", "feb2.jpg")]})

    folders = _folders(client.get("/?date=2026&group=month,event").text)

    assert "1 <i>of</i> 3 files" in folders, folders
    assert "2 <i>of</i> 3 files" in folders, folders
    assert "3 files in all" in folders, "no explanation of what it is part of"


def test_a_folder_that_is_whole_says_nothing_about_being_split(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Most cards are slices the moment you group by month and event. The ones
    that are not have to read as plainly as they ever did."""
    _spread(app_env, writable, {"feb1.jpg": "2026:02:02 10:00:00",
                                "feb2.jpg": "2026:02:03 10:00:00"})
    client.post("/api/decide/bulk", json={
        "event": "One Weekend",
        "files": [{"folder": "init_2026", "name": n}
                  for n in ("feb1.jpg", "feb2.jpg")]})

    folders = _folders(client.get("/?date=2026&group=month,event").text)
    card = folders[folders.index("One Weekend"):]

    assert "2 files" in card[:200], card[:200]
    assert "<i>of</i>" not in card[:200], "a whole folder claiming to be a part"


def test_nothing_is_split_when_nothing_is_above_it(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """One level of grouping cuts nothing up, so there is nothing to warn
    about and no second query to run finding that out."""
    _spread(app_env, writable, {"jan.jpg": "2026:01:20 10:00:00",
                                "feb1.jpg": "2026:02:02 10:00:00"})

    folders = _folders(client.get("/?date=2026&group=event").text)

    assert "<i>of</i>" not in folders


def test_the_bar_is_this_card_within_its_group(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The track is the whole group, so the bar answers the question the two
    numbers beside it ask: *1 of 3* is a third of a bar and looks like one."""
    _spread(app_env, writable, {"jan.jpg": "2026:01:20 10:00:00",
                                "feb1.jpg": "2026:02:02 10:00:00",
                                "feb2.jpg": "2026:02:03 10:00:00"})
    client.post("/api/decide/bulk", json={
        "event": "Ski Trip",
        "files": [{"folder": "init_2026", "name": n}
                  for n in ("jan.jpg", "feb1.jpg", "feb2.jpg")]})

    folders = _folders(client.get("/?date=2026&group=month,event").text)
    jan = _shelf(folders, "January")

    assert '<i style="width:33%"' in jan, jan
    assert "1 of 3 files" in jan


def test_a_sliver_is_drawn_as_one() -> None:
    """Two files out of eleven hundred rounds to nothing, and an empty bar
    reads as *nothing here* rather than as a sliver of something big. Asked of
    the arithmetic directly, because the ratio that needs saying is one no
    fixture of a dozen files can produce."""
    admin = web.Principal(name="admin", is_admin=True, grants=frozenset())

    assert '<i style="width:0%"' not in web._bar(2, 1143, 2, admin)
    assert '<i style="width:2%"' in web._bar(2, 1143, 2, admin)
    # And it does not invent a share where there is none to round up.
    assert '<i style="width:100%"' in web._bar(9, 9, 9, admin)


def test_a_folder_says_how_much_of_it_is_done(
    client: TestClient, writable: Path
) -> None:
    """A year you have finished and a year you have not started are the same
    sentence and different bars."""
    (writable / "b.mp4").write_bytes(b"fake")
    folders = _folders(client.get("/?group=event").text)
    assert 'class="bar"' in folders and "2 undecided" in folders

    client.post("/api/decide/bulk", json={
        "add_audience": ["family"],
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})
    half = _folders(client.get("/?group=event").text)
    assert "1 undecided" in half
    assert 'width:50%' in half, "the bar does not move"

    client.post("/api/decide/bulk", json={
        "add_audience": ["family"],
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})
    done = _folders(client.get("/?group=event").text)
    assert "all decided" in done
    assert "undecided" not in done


def test_opening_a_folder_is_this_view_plus_what_the_folder_is(
    client: TestClient
) -> None:
    """The point of the page. Every level of the grouping becomes a filter, so
    what opens is the section that was clicked and nothing else."""
    html = client.get("/?group=year,event").text

    assert "/browse?event=Italy%20-%20Sicily&amp;date=2026" in html, html[:0]
    assert "&amp;amp;" not in html, "the link is escaped twice"


def test_a_folder_keeps_the_filters_already_set(client: TestClient) -> None:
    """Narrowing the library and then opening a folder has to give you that
    folder *within* what you narrowed to — otherwise the filters were a
    decoration on the way past."""
    html = client.get("/?group=event&kind=image").text

    assert "kind=image" in html
    assert "event=Italy%20-%20Sicily" in html


def test_a_folder_that_cannot_be_said_as_a_filter_does_not_pretend(
    client: TestClient
) -> None:
    """*No day* means dated less precisely than a day, and the date filter
    answers `undated` or a prefix with nothing in between. Sending it to the
    month would open a folder holding more than the one that was clicked."""
    html = client.get("/?group=day").text

    assert 'class="tile dead"' in html, "offered a door to somewhere else"
    assert "No day" in html


def test_an_undated_folder_is_reachable(client: TestClient) -> None:
    """374 of the seeded year have no date; that is a work item, not an
    absence to leave unlinked. A year nobody knows really is *undated* —
    unlike a day nobody knows, which is a file dated to its month."""
    html = client.get("/?group=year").text

    assert "date=%28undated%29" in html
    assert "b.mp4" in client.get("/browse?date=(undated)").text


def test_the_landing_page_says_what_is_left_to_do(client: TestClient) -> None:
    """The one thing the old table was for. Per folder, because that is the
    unit of work — a year with nothing left in it should look finished."""
    html = client.get("/").text

    assert "undecided" in html


def test_both_pages_carry_the_controls_the_script_wires(
    client: TestClient
) -> None:
    """One script serves both pages, so an element it reaches for has to be on
    both of them. The landing page shipped without `#menu`: the script threw
    on load, every handler died with it, and the page looked like one whose
    filters and grouping had simply never been built.

    The script's own `getElementById` strings are inlined into the HTML, so
    the scripts are cut out before looking — otherwise every page contains
    every id it merely mentions.
    """
    shared = {"grid", "menu", "chips", "note"}
    for url in ("/", "/browse"):
        markup = re.sub(r"<script.*?</script>", "",
                        client.get(url).text, flags=re.S)
        have = set(re.findall(r'id="([^"]+)"', markup))
        assert shared <= have, f"{url} is missing {sorted(shared - have)}"


def test_the_same_filters_are_on_both_pages(client: TestClient) -> None:
    """Learning one teaches the other. They are the same controls over the
    same library, and a chip that exists on one page and not the other is a
    filter you have to go somewhere else to set."""
    home = client.get("/").text
    grid = client.get("/browse").text

    for page in (home, grid):
        chips = page[page.index("CHIPS="):page.index("FIXED=")]
        assert '"event"' in chips and '"camera"' in chips, chips
    assert 'id="chips"' in home


def test_where_a_photograph_came_from_is_a_filter(
    client: TestClient, app_env: dict[str, Path], writable: Path
) -> None:
    """The name the import was given — `james`, `alina`, the folder tree that
    seeded the library. Every import from that phone lands in a folder of its
    own, so the folder is no use as a filter and the name it was given is."""
    import json as _json

    (writable / ".import.jsonl").write_text(
        _json.dumps({"name": "james", "source": "device"}) + chr(10),
        encoding="utf-8")
    ix.build(app_env["db"], meta_dir=app_env["share"] / "meta",
             master_dir=app_env["share"] / "master")

    assert client.get("/api/suggest?column=source").status_code == 200
    html = client.get("/browse?source=james").text
    assert 'data-name="a.jpg"' in html
    assert 'data-name="a.jpg"' not in client.get("/browse?source=alina").text

    # And a way to see who is in the library at all, which is the landing page
    # doing what it does with every other grouping.
    folders = client.get("/?group=source").text
    assert ">james<" in folders
    assert "/browse?source=james" in folders


def test_the_camera_a_photograph_came_from_is_a_filter(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """It was a way to cut the library and not a way to narrow it, so the
    landing page could make a folder per camera and then had nowhere to send
    you when you opened one."""
    _burst(app_env, writable, "x.jpg", "y.jpg")
    assert client.get("/api/suggest?column=camera").status_code == 200

    html = client.get("/browse?camera=iPhone%2017%20Pro").text
    assert 'data-name="x.jpg"' in html
    assert 'data-name="a.jpg"' not in html, "not scoped to that camera"

    # The ones nobody recorded a camera for are a section too, and the folder
    # standing over them has to open like any other.
    unknown = client.get("/browse?camera=%28unknown%29").text
    assert 'data-name="a.jpg"' in unknown
    assert 'data-name="x.jpg"' not in unknown


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


# --- index not built ---------------------------------------------------------

def test_a_missing_index_says_what_to_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sign_in: Callable[[str, str], TestClient]) -> None:
    """'503' is useless on its own; the fix is one command."""
    monkeypatch.setattr(web, "DB_PATH", tmp_path / "nope.db")
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", tmp_path / "users.json")

    r = sign_in(accounts.ADMIN, "admin").get("/")

    assert r.status_code == 503
    assert "pix2 index" in r.text


# --- video playback ----------------------------------------------------------

def test_master_video_is_streamable(master: Path, app_env: dict[str, Path], sign_in: Callable[[str, str], TestClient]) -> None:
    """Videos need the master file: there is no delivery rendition to serve.

    The seeded library is MP4 throughout, so master *is* the playable copy.
    """
    r = sign_in(accounts.ADMIN, "admin").get("/media/init_2026/b.mp4")

    assert r.status_code == 200
    assert r.content.startswith(bytes([0, 0, 0, 0x18]) + b"ftyp")


def test_media_advertises_range_support(master: Path, app_env: dict[str, Path], sign_in: Callable[[str, str], TestClient]) -> None:
    """Without ranges a browser cannot seek, only play from the start."""
    r = sign_in(accounts.ADMIN, "admin").get("/media/init_2026/b.mp4")
    assert r.headers.get("accept-ranges") == "bytes"


def test_media_serves_a_byte_range(master: Path, app_env: dict[str, Path], sign_in: Callable[[str, str], TestClient]) -> None:
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


def test_media_is_missing_for_an_unknown_file(master: Path, app_env: dict[str, Path], sign_in: Callable[[str, str], TestClient]) -> None:
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
    assert '<button data-act="access"' in html
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
    for column in ("event", "date", "tag", "audience", "kind", "band"):
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


def test_a_selection_span_proposes_the_events_covering_it(
    client: TestClient
) -> None:
    """The page knows what the selection spans and the server does not, so the
    dates ride along with the request."""
    got = client.get("/api/suggest?column=event"
                     "&near_from=2026-08-30-00:00:00"
                     "&near_to=2026-08-30-23:59:59").json()

    assert [(s["value"], s["scope"]) for s in got] == [("Italy - Sicily", "near")]


def test_half_a_range_is_not_a_range(client: TestClient) -> None:
    """Guessing the missing end would propose events on evidence nobody gave."""
    for query in ("&near_from=2026-08-30-00:00:00", "&near_to=2026-08-30-00:00:00"):
        got = client.get("/api/suggest?column=event" + query).json()
        assert all(s["scope"] != "near" for s in got), query


def test_an_unsuggestable_column_is_refused(client: TestClient) -> None:
    assert client.get("/api/suggest?column=folder").status_code == 400
    assert client.get("/api/suggest?column=size").status_code == 400


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
    rows = client.get("/api/files?date=1987").json()
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
    r = client.post("/api/decide/bulk?date=(undated)", json={
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


# --- taking a copy away -------------------------------------------------------


def _render_for(name: str) -> Path:
    """The playable copy of one master file, made to exist."""
    from pix.nas import derive

    at = derive.render_path(derive.MASTER_DIR / "init_2026" / name)
    at.parent.mkdir(parents=True, exist_ok=True)
    at.write_bytes(b"h264 copy")
    return at


def test_a_photograph_downloads_as_itself(
    client: TestClient, writable: Path
) -> None:
    """There is nothing to convert: the original is the file anything opens,
    so *original or copy* is not a question about it."""
    r = client.get("/download/init_2026/a.jpg")

    assert r.status_code == 200
    assert r.content == (writable / "a.jpg").read_bytes()
    assert "attachment" in r.headers["content-disposition"]
    assert "a.jpg" in r.headers["content-disposition"]


def test_a_clip_downloads_as_the_copy_that_plays(
    client: TestClient, writable: Path
) -> None:
    """The H.264 rendition, because what you want from *download* is a file
    that opens — and the original is the one a browser would not play, which
    is why the copy exists at all."""
    (writable / "b.mp4").write_bytes(b"hevc original")
    _render_for("b.mp4")

    r = client.get("/download/init_2026/b.mp4")

    assert r.content == b"h264 copy"


def test_the_original_can_be_asked_for(
    client: TestClient, writable: Path
) -> None:
    """Master is the archive. Anything that hands out *the file* has to be
    able to hand out that one."""
    (writable / "b.mp4").write_bytes(b"hevc original")
    _render_for("b.mp4")

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
        assert zf.namelist() == ["init_2026/a.jpg", "init_2026/b.mp4"]
        assert zf.read("init_2026/b.mp4") == b"a clip"


def test_a_zip_holds_the_folder_each_file_came_from(
    client: TestClient, writable: Path
) -> None:
    """Two master folders can hold the same name, and a flat zip would quietly
    keep one of them."""
    r = client.post("/download.zip", data={"files": json.dumps([
        {"folder": "init_2026", "name": "a.jpg"}])})

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert zf.namelist() == ["init_2026/a.jpg"]


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
    _render_for("b.mp4")
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
                      * (web.ZIP_LIMIT + 1))

    r = client.post("/download.zip", data={"files": many})

    assert r.status_code == 400
    assert str(web.ZIP_LIMIT) in r.text


def test_a_cell_says_whether_a_copy_of_it_exists(
    client: TestClient, writable: Path
) -> None:
    """So the page asks *original or copy* only where there is an answer, and
    downloads without asking everywhere else."""
    (writable / "b.mp4").write_bytes(b"fake")

    plain = client.get("/browse").text
    assert 'data-copy=""' in plain
    assert 'data-copy="1"' not in plain

    _render_for("b.mp4")
    with_copy = client.get("/browse").text
    assert 'data-copy="1"' in with_copy


# --- access ------------------------------------------------------------------

@pytest.fixture
def household(app_env: dict[str, Path], writable: Path, sign_in: Callable[[str, str], TestClient], add_user: Callable[..., None]) -> dict[str, object]:
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

    assert '<button data-act="access"' not in html
    assert 'id="actions"' not in html


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


# --- names are case-insensitive ----------------------------------------------

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


# --- the access menu ----------------------------------------------------------

def test_audience_values_can_be_suggested(client: TestClient,
                                          writable: Path) -> None:
    """The endpoint refused `audience` outright, so the Access list was empty
    however many accounts existed."""
    client.post("/api/decide", json={"folder": "init_2026", "name": "a.jpg",
                                     "add_audience": ["family"]})

    got = client.get("/api/suggest?column=audience").json()
    assert [s["value"] for s in got] == ["family"]


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
    assert accounts.ADMIN not in web._audience_names()


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

    assert "#menu[hidden] { display:none; }" in css


def test_the_filter_is_called_access(client: TestClient) -> None:
    html = client.get("/browse").text

    assert '"Access"' in html
    assert "Access&hellip;" in html
    assert "Tags&hellip;" in html


# --- layout and the viewer ----------------------------------------------------

def test_counts_and_messages_live_in_the_footer(client: TestClient) -> None:
    """Every row of chrome at the top is a row of photographs pushed off."""
    html = client.get("/browse").text

    assert 'class="footbar"' in html
    assert html.index('class="footbar"') > html.index('id="grid"')


def test_the_identity_controls_sit_top_right(client: TestClient) -> None:
    html = client.get("/browse").text
    # The row itself: the stylesheet above it talks about Sign out too, and an
    # index into the whole document finds that first.
    bar = html[html.index('class="row"'):html.index("</div><main")]

    assert 'class="spacer"' in bar
    assert bar.index('class="spacer"') < bar.index("Sign out")


def test_nothing_is_underlined_on_hover(client: TestClient) -> None:
    """An underline lands across the descenders of the very word you are
    reading at the moment you are trying to read it, and these pages are lists
    of names — events, days, accounts, cameras — where the letters are the
    content. Everything else here already says *this one* with a border or a
    background."""
    css = client.get("/browse").text
    css = css[css.index("<style>"):css.index("</style>")]

    assert "text-decoration:underline" not in css
    at = css.index("a:hover {")
    assert "var(--tint)" in css[at:at + 140], css[at:at + 140]


def _shaped(app_env: dict[str, Path], writable: Path, name: str,
            w: int, h: int) -> None:
    """A file of known proportions, in master and in the index."""
    import json

    share = app_env["share"]
    (writable / name).write_bytes(b"fake")
    (share / "meta" / "init_2026" / f"{name}.json").write_text(json.dumps({
        "file": name, "folder": "init_2026", "size": 30, "mtime_ns": 1,
        "exif": {"EXIF:DateTimeOriginal": "2026:08:30 11:00:00",
                 "File:ImageWidth": w, "File:ImageHeight": h},
    }), encoding="utf-8")
    ix.build(app_env["db"], meta_dir=share / "meta",
             master_dir=share / "master")


def test_a_cell_says_what_shape_it_is(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The grid shows squares and the tiers are capped on the long edge, so
    the square taken out of a 16:9 frame in the 1000px tier is 563 across —
    a bit over half the number the tier is named for. The page cannot pick a
    tier from the cap alone, so each cell carries its own proportions."""
    html = client.get("/browse").text

    # Nothing known about a file's size is a square, which asks the tiers for
    # the most they can give rather than assuming they have it.
    assert 'data-ar="1"' in html

    _shaped(app_env, writable, "wide.jpg", 3840, 2160)
    wide = client.get("/browse").text
    assert 'data-ar="0.56' in wide, wide[wide.index("data-ar"):][:40]


def test_the_page_is_told_what_each_tier_holds(client: TestClient) -> None:
    """Arithmetic there rather than a second copy of these numbers here —
    they are `derive`'s to choose, and have already changed once."""
    html = client.get("/browse").text
    tiers = html[html.index("TIERS="):html.index("GRID_GROUPS=")]

    assert str(derive.THUMB_PX) in tiers and str(derive.LARGE_PX) in tiers
    assert str(derive.PREVIEW_PX) in tiers, tiers
    assert "/preview/" in tiers


def _corner(html: str) -> str:
    """The mark in the top-left, with whatever it is wrapped in."""
    at = html.index('class="brand"')
    return html[html.index("<a", at - 40):html.index("</a>", at) + 4]


def test_the_page_has_a_mark_of_its_own(client: TestClient) -> None:
    """A mark in the corner and a tab icon. Both are how you find this among
    twenty other tabs."""
    html = client.get("/browse").text

    assert 'rel="icon"' in html and "data:image/svg+xml" in html
    bar = _corner(html)
    assert "<svg" in bar, "the brand is still text"
    assert ">pix2<" not in bar, "the word is still there beside the mark"
    assert "aria-label=" in bar, "a mark nothing can read out"


def test_the_corner_is_the_way_between_files_and_folders(
    client: TestClient
) -> None:
    """The same library at two zooms, and the corner is how you change which.

    It carries the view across, because a zoom that dropped the filters would
    be a different library rather than the same one seen closer — and it says
    what it will do, since the mark under the pointer is a picture of the page
    rather than of the destination.
    """
    grid = _corner(client.get("/browse?event=Italy+-+Sicily&group=day").text)
    assert 'href="/?event=Italy+-+Sicily&amp;group=day"' in grid, grid
    assert "Show the folders" in grid, grid

    folders = _corner(client.get("/?event=Italy+-+Sicily&group=event").text)
    assert 'href="/browse?event=Italy+-+Sicily&amp;group=event"' in folders
    assert "Show the files" in folders, folders


def test_a_stack_is_not_carried_up_to_the_folders(client: TestClient) -> None:
    """A folder view of one stack is the stack, so there is nothing coarser to
    show. Everything else about the view goes up with you."""
    bar = _corner(client.get("/browse?event=Italy+-+Sicily&within=x%2Fy.jpg"
                             "&group=day").text)

    assert "within" not in bar, bar
    assert 'href="/?event=Italy+-+Sicily&amp;group=day"' in bar, bar


def test_the_sign_in_page_still_carries_the_logo(
    app_env: dict[str, Path]
) -> None:
    """The corner became a control on the library pages, so what the app *is*
    has to be somewhere that never changes under you.

    Signed out deliberately: the form redirects away for anyone who is not, so
    the shared client would be handed the grid and this would pass on a page
    that has no logo on it at all.
    """
    html = TestClient(web.app).get("/login").text

    assert "pi<b>x</b>" in html, "no wordmark on the way in"
    assert "Show the folders" not in html and "Show the files" not in html
    assert "<svg" in _corner(html), "no mark beside it"


def test_what_is_about_you_lives_under_your_name(client: TestClient) -> None:
    """History, Accounts and the way out are things you do rarely. Spread
    along the bar they were three permanent controls competing with the
    filters, which are what the bar is for."""
    html = client.get("/browse").text
    bar = html[html.index('class="row"'):html.index("</div><main")]

    menu = bar[bar.index('class="memenu"'):]
    for item in ("/history", "/accounts", "Sign out"):
        assert item in menu, f"{item} is not under the name"
    assert "admin" in bar[:bar.index('class="memenu"')], "the name is hidden"


def test_what_is_waiting_is_an_icon_not_a_sentence(
    client: TestClient, writable: Path
) -> None:
    """*8 deleted* stood in the bar on every page whether or not it was news,
    and the next thing worth reporting would have been a second phrase beside
    it. The dot is the whole of what you see without asking, so it is the part
    that has to be right."""
    quiet = client.get("/browse").text
    bar = quiet[quiet.index('class="row"'):quiet.index("</div><main")]
    assert 'class="bell"' in bar
    assert 'data-any=""' in bar, "a dot with nothing behind it"
    assert "Nothing waiting" in bar

    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "deleted": True})

    loud = client.get("/browse").text
    bar = loud[loud.index('class="row"'):loud.index("</div><main")]
    assert 'data-any="1"' in bar, "nothing says there is something"
    assert "1 deleted" in bar
    assert 'id="bincount"' in bar, "the page can no longer update it"


def test_the_dot_follows_a_delete_without_a_reload(client: TestClient) -> None:
    """The count is rendered with the page, so the write that changes it has
    to say so — and the dot is what anybody actually sees."""
    js = web._BROWSE_JS
    at = js.index("function drawBin(")

    assert "bell.dataset.any" in js[at:at + 400], "the dot is left stale"


def test_the_name_menu_needs_no_script(client: TestClient) -> None:
    """It has to work on /history and /accounts, which carry no page script at
    all — and a Sign out that only worked where the grid was loaded would be
    missing from the page you are most likely to be stuck on."""
    for url in ("/history", "/accounts"):
        html = client.get(url).text
        assert 'class="memenu"' in html, url
        assert "Sign out" in html, url

    css = client.get("/browse").text
    assert ".me:hover .memenu" in css
    assert ".me:focus-within .memenu" in css, "unreachable from the keyboard"


def test_the_filters_wrap_without_carrying_the_way_out_with_them(
    client: TestClient
) -> None:
    """The filters are the one part of the top row that grows without limit,
    so they are the one part allowed a second line — inside their own box. The
    row wrapping as a whole took the account and Sign out down with it, and
    the way out is what you reach for when something has gone wrong.
    """
    html = client.get("/browse").text
    row = html[html.index('class="row"'):html.index("</div><main")]

    # One box holds them, so the row has one thing to keep in place rather
    # than four loose ones to wrap between.
    assert 'class="right"' in row
    assert row.index('class="right"') < row.index("Sign out")

    css = html[html.index("<style>"):html.index("</style>")]
    assert ".topbar > .row:first-child { flex-wrap:nowrap" in css
    # On the chips themselves: a flex item will not shrink below its own
    # content without it, so without it they push the row wide instead of
    # wrapping. Other rules carry the same property for other reasons.
    at = css.index(".topbar > .row:first-child > .chips")
    assert "min-width:0" in css[at:at + 120], css[at:at + 120]


def test_only_the_filters_in_use_are_on_the_bar(client: TestClient) -> None:
    """Every filter, always, was a row of controls that grew each time the app
    learned to ask something new — most of them saying nothing, in front of
    the one or two that are the address of what you are looking at."""
    js = web._BROWSE_JS

    assert "if(!v) continue;" in js, "unset filters are still drawn"
    assert "addchip" in js and "filterMenu" in js


def test_select_all_is_reachable_with_nothing_selected(
    client: TestClient
) -> None:
    """The same requirement as before, met a different way. It used to sit up
    beside the filters because the selection row came and went; now the row is
    always on screen — it carries the count and the tick — and only the actions
    within it appear and disappear. So the tick is reachable with nothing
    selected, which is the one moment it is needed most."""
    html = client.get("/browse").text
    row = html[html.index('id="actions"'):]

    assert 'id="selall"' in row
    assert 'id="actions"' in html and 'id="actions" hidden' not in html


def test_the_admin_is_not_badged(client: TestClient) -> None:
    assert "admin-badge" not in client.get("/browse").text


def test_the_viewer_closes_on_a_click_beside_the_picture(
    client: TestClient
) -> None:
    """The stage fills the viewer, so a click beside the picture lands on it
    rather than on the viewer — the old check never matched and there was no
    way back out except the keyboard."""
    js = client.get("/browse").text

    assert "e.target===stage" in js
    assert 'id="viewclose"' in js


def test_access_cannot_be_invented_from_the_menu(client: TestClient) -> None:
    """Somebody who can be given access is an account or a role, made under
    Accounts. Offering to create one here would write a grant reaching nobody."""
    js = client.get("/browse").text

    assert "ctx.column!=='audience'" in js


# --- metadata is scoped too ---------------------------------------------------

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


# --- the script must be able to see the page ---------------------------------

def test_the_page_script_comes_last(client: TestClient) -> None:
    """The bug this exists to prevent: the count and the message line moved
    into the footer, below the script that looks them up, so both were `null`.
    Every write began by setting a message, so every write threw before it sent
    anything — and said nothing, because saying things was the broken part."""
    html = client.get("/browse").text

    assert html.index("<footer") < html.index("const VIEW=")
    assert html.index('id="note"') < html.index("const VIEW=")
    assert html.index('id="count"') < html.index("const VIEW=")
    assert html.index('id="grid"') < html.index("const VIEW=")


def test_every_element_the_script_looks_up_exists(client: TestClient) -> None:
    """Each of these is fetched by id at load; a missing one is a null that
    only shows up when somebody clicks."""
    html = client.get("/browse").text

    for wanted in ("grid", "menu", "chips", "actions", "selcount", "count",
                   "note", "viewer", "vimg", "vvid", "vmeta", "rail",
                   "railtoggle", "viewclose", "selall"):
        assert f'id="{wanted}"' in html, wanted


def test_a_failed_write_is_loud(client: TestClient) -> None:
    """A write that fails without saying so is indistinguishable from one that
    worked, and the curator finds out much later that nothing was recorded."""
    js = client.get("/browse").text

    assert "unhandledrejection" in js
    assert "could not be written`,true)" in js


# --- what a thumbnail shows ---------------------------------------------------

def test_each_value_is_its_own_chip_with_a_tooltip(client: TestClient,
                                                   writable: Path) -> None:
    """A thumbnail is 150px and three role names are not: one run of text just
    gets cut off mid-word with no way to find out what it said."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg",
        "add_audience": ["family"], "add_tags": ["beach", "kids"]})

    html = client.get("/browse").text
    assert '<i title="family">family</i>' in html
    assert '<i title="beach">beach</i>' in html
    assert 'title="beach, kids"' in html


def test_the_three_corners_do_not_collide(client: TestClient) -> None:
    """Access bottom-left, tags top-right, duration bottom-right."""
    css = client.get("/browse").text

    assert ".who  { left:5px; bottom:4px; }" in css
    assert ".tags { right:4px; top:4px;" in css
    assert ".badge { position:absolute; right:4px; bottom:4px;" in css


def test_an_edited_cell_looks_like_a_fetched_one(client: TestClient) -> None:
    """The client repaints chips in the same shape the server renders, or a
    photo you just tagged would look different from one you reloaded."""
    js = client.get("/browse").text

    assert "el.innerHTML=list.map(v=>`<i title=" in js


# --- the grid reports deviation, not the norm ---------------------------------

def test_the_usual_audience_is_not_printed_on_every_thumbnail(
    client: TestClient, writable: Path
) -> None:
    """If nine files in ten say `family`, printing `family` on nine thumbnails
    in ten is noise that tells you nothing you did not already assume."""
    client.post("/accounts/usual", data={"usual": "family"})
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["family"]})

    html = client.get("/browse").text
    assert "<i title=\"family\">" not in html


def test_an_unusual_audience_is_named(client: TestClient,
                                      writable: Path) -> None:
    """That is the exception, and the whole reason to look."""
    client.post("/accounts/usual", data={"usual": "family"})
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg",
        "add_audience": ["family", "tv"]})

    html = client.get("/browse").text
    assert '<i title="tv">tv</i>' in html
    assert "<i title=\"family\">" not in html


def test_no_access_is_marked(client: TestClient, writable: Path) -> None:
    """The work still to do."""
    assert 'class="unshared"' in client.get("/browse").text


def test_the_mark_goes_once_something_is_shared(client: TestClient,
                                                writable: Path) -> None:
    client.post("/accounts/usual", data={"usual": "family"})
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["family"]})

    cell = client.get("/browse").text.split('data-name="a.jpg"')[1][:400]
    assert "unshared" not in cell


def test_the_usual_audience_survives_a_reload(client: TestClient) -> None:
    client.post("/accounts/usual", data={"usual": "Family"})

    assert accounts.load().usual == "family"
    assert 'value="family"' in client.get("/accounts").text


def test_the_client_hides_the_usual_audience_too(client: TestClient) -> None:
    """Or a photo you just shared would look different from one you reloaded."""
    js = client.get("/browse").text

    assert "all.filter(v=>v!==USUAL)" in js


# --- grouping -----------------------------------------------------------------

def test_the_grid_is_grouped_by_day_by_default(client: TestClient) -> None:
    """A day is the unit people remember photographs in — the afternoon at the
    lake — where an event is usually several of them."""
    html = client.get("/browse").text

    assert 'class="group"' in html
    assert "30 August 2026" in html


def test_a_group_heading_counts_its_files(client: TestClient) -> None:
    assert ">1</span>" in client.get("/browse").text


def test_undated_files_group_somewhere_rather_than_nowhere(
    client: TestClient
) -> None:
    """b.mp4 has no date at all, and a grouping is not a filter."""
    assert "No day" in client.get("/browse").text
    assert "No date" in client.get("/browse?group=year").text


def test_grouping_can_be_turned_off(client: TestClient) -> None:
    """One heading survives even ungrouped: the heading *is* the control, so a
    grid with none would offer no way to start."""
    html = client.get("/browse?group=none").text

    assert "Ungrouped" in html
    assert "30 August 2026" not in html


def test_grouping_by_event_uses_the_event_name(client: TestClient) -> None:
    html = client.get("/browse?group=event").text

    assert "Italy - Sicily" in html


def test_an_unknown_grouping_falls_back_to_the_default(
    client: TestClient
) -> None:
    """A URL is typed by people and edited by hand; an unrecognised value
    should not be a 500."""
    r = client.get("/browse?group=nonsense")

    assert r.status_code == 200
    assert "30 August 2026" in r.text


def test_the_heading_is_the_grouping_control(client: TestClient) -> None:
    """The thing you want to regroup is the thing you click, and it costs no
    row at the top."""
    html = client.get("/browse?group=month").text

    assert 'class="grpname"' in html
    assert 'class="addgrp"' in html
    assert "August 2026" in html


def test_groupings_nest(client: TestClient) -> None:
    """A day inside an event is the obvious pair, and needs two levels."""
    html = client.get("/browse?group=event,day").text

    assert 'class="crumb" data-level="0"' in html
    assert 'class="crumb" data-level="1"' in html
    assert "Italy - Sicily" in html
    assert "30 August 2026" in html


def test_a_section_has_one_heading_reading_as_a_path(
    client: TestClient
) -> None:
    """Nested headings cost an indent and a row per level, and the deeper ones
    said less and less. A section's identity is the whole path."""
    html = client.get("/browse?group=year,event").text
    heading = html[html.index('<h3 class="group"'):]
    heading = heading[:heading.index("</h3>")]

    assert heading.count("crumb") >= 2
    assert "&rsaquo;" in heading
    assert html.count('<h3 class="group"') == html.count('class="crumbs"')


def test_every_crumb_can_be_removed(client: TestClient) -> None:
    """Removal on the crumb rather than inside a menu: *take this away* is a
    thing you should be able to see, not go and find."""
    html = client.get("/browse?group=year,event").text

    assert html.count('class="rmgrp"') >= 2


def test_a_group_heading_can_select_its_files(client: TestClient) -> None:
    assert 'class="grppick"' in client.get("/browse").text


def test_a_repeated_level_is_dropped(client: TestClient) -> None:
    """Grouping by day inside day is not a thing, and a URL is hand-edited."""
    html = client.get("/browse?group=day,day").text

    assert 'data-level="1"' not in html


def test_order_is_by_effective_date(client: TestClient, writable: Path) -> None:
    """Not by filename, and not by capture date — by the date the file actually
    has, after any override."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg",
        "date_override": "1987-*-*-*:*:*"})

    rows = client.get("/api/files").json()
    assert [r["name"] for r in rows][0] == "a.jpg"
    assert rows[0]["effective_date"].startswith("1987")


# --- sub-grouping -------------------------------------------------------------

def test_the_add_button_sits_beside_the_name(client: TestClient) -> None:
    """At the end of the row it went unnoticed, which is the whole failure a
    control can have."""
    html = client.get("/browse").text
    heading = html[html.index('<h3 class="group"'):][:500]

    assert heading.index("grpname") < heading.index("addgrp")
    assert heading.index("addgrp") < heading.index('<span class="dim"')


def test_the_add_button_is_visible_without_hovering(
    client: TestClient
) -> None:
    """A control you cannot see until you hover the right thing is a control
    you never learn is there."""
    css = client.get("/browse").text

    assert "visibility:hidden" not in css.split(".addgrp")[1][:120]
    assert "opacity:.3" in css.split(".addgrp")[1][:120]


def test_three_levels_is_the_limit(client: TestClient) -> None:
    """Past three the headings outnumber the photographs."""
    html = client.get("/browse?group=event,year,month,day").text

    assert 'data-level="2"' in html
    assert 'data-level="3"' not in html
    assert 'class="addgrp"' not in html





def _stamp_shape(db: Path, shape: int) -> None:
    """Write a shape number into an index, as a build of that age would."""
    import sqlite3

    conn = sqlite3.connect(db)
    with conn:
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)",
                     (str(shape),))
    conn.close()


def test_an_app_older_than_the_index_says_so_rather_than_breaking(
    client: TestClient, app_env: dict[str, Path]
) -> None:
    """The desktop rebuilds the index and the container reads it, and the two
    are updated by different acts on different machines — so they go out of
    step, most often while the app is being worked on.

    Before this it surfaced wherever a query first touched a column that had
    moved: a 500 and a traceback in a log nobody is watching, which reads as
    *the app is broken*.
    """
    _stamp_shape(app_env["db"], ix.SCHEMA_VERSION + 1)

    r = client.get("/browse", follow_redirects=False)

    assert r.status_code == 503, r.status_code
    assert "Out of step" in r.text
    assert "needs updating" in r.text, "told the wrong side to move"
    assert "pix2 index" not in r.text, "rebuilding cannot fix a newer index"


def test_an_index_older_than_the_app_asks_for_a_rebuild(
    client: TestClient, app_env: dict[str, Path]
) -> None:
    """The opposite direction, and the opposite fix. Guessing wrong here is
    what costs the afternoon."""
    _stamp_shape(app_env["db"], 1)

    r = client.get("/browse", follow_redirects=False)

    assert r.status_code == 503
    assert "pix2 index" in r.text
    assert "needs updating" not in r.text


def test_health_reports_the_disagreement_without_calling_itself_dead(
    client: TestClient, app_env: dict[str, Path]
) -> None:
    """`ok` stays true: the app is running and answering. Reporting it as dead
    would have Container Manager restart a container that works perfectly, and
    the restart would not fix it."""
    _stamp_shape(app_env["db"], ix.SCHEMA_VERSION + 1)

    body = client.get("/healthz").json()

    assert body["ok"] is True
    assert body["index"] is False
    assert "newer pix" in body["says"]


def test_health_is_plain_when_the_two_agree(client: TestClient) -> None:
    body = client.get("/healthz").json()

    assert body == {"ok": True, "index": True}


# --- installing it on a phone -------------------------------------------------

def test_the_manifest_says_what_to_install(app_env: dict[str, Path]) -> None:
    """Signed out on purpose: a launcher fetches these before anybody has typed
    a password, and a login wall in front of them means the install prompt
    simply never appears."""
    anon = TestClient(web.app)

    r = anon.get("/manifest.webmanifest")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    body = r.json()
    assert body["name"] == "pix" and body["short_name"] == "pix"
    assert body["display"] == "standalone"
    assert body["start_url"] == "/" and body["scope"] == "/"
    # A launch that flashes white before a dark app is the tell that something
    # is a web page rather than an app.
    assert body["background_color"] == body["theme_color"] == "#14161a"


def test_every_icon_the_manifest_names_is_actually_there(
    app_env: dict[str, Path]
) -> None:
    """The classic reason an install offer never appears: a manifest naming an
    icon that 404s. Nothing says so — the prompt just does not happen."""
    anon = TestClient(web.app)
    icons = anon.get("/manifest.webmanifest").json()["icons"]

    assert {i["sizes"] for i in icons} >= {"192x192", "512x512"}
    assert any(i["purpose"] == "maskable" for i in icons), "no cropped shape"

    for spec in [*icons, {"src": "/apple-touch-icon.png"}]:
        got = anon.get(str(spec["src"]))
        assert got.status_code == 200, spec["src"]
        assert got.headers["content-type"] == "image/png", spec["src"]
        assert got.content[:8] == b"\x89PNG\r\n\x1a\n", spec["src"]


def test_an_icon_that_is_not_ours_is_not_served(
    app_env: dict[str, Path]
) -> None:
    """The route takes a name, so it has to refuse one that walks out of the
    directory it owns."""
    anon = TestClient(web.app)

    assert anon.get("/nothing-like-this.png").status_code == 404
    assert anon.get("/..%2F..%2Fusers.png").status_code in (404, 400)


def test_the_worker_is_served_from_the_root(app_env: dict[str, Path]) -> None:
    """A worker controls only what sits below where it was served from, so this
    one has to come from `/` — and a browser will not offer to install an app
    whose worker has no fetch handler."""
    anon = TestClient(web.app)

    r = anon.get("/sw.js")

    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "addEventListener('fetch'" in r.text


def test_the_worker_never_keeps_a_page(app_env: dict[str, Path]) -> None:
    """The page script is inlined into its HTML, so a cached page is a cached
    *build* — and a tab running last week's script against this week's API is
    the failure this project has already lost an afternoon to."""
    body = TestClient(web.app).get("/sw.js").text
    kept = body.split("KEEP = [")[1].split("]")[0]

    assert "/browse" not in kept and "'/'" not in kept
    assert "/offline" in kept
    # Navigations go to the network and fall back to the offline page.
    assert "req.mode === 'navigate'" in body
    assert "fetch(req).catch(() => caches.match('/offline'))" in body


def test_the_offline_page_does_not_pretend_to_be_the_library(
    app_env: dict[str, Path]
) -> None:
    """It carries the three elements it fills in, and none of the library."""
    anon = TestClient(web.app)

    r = anon.get("/offline")

    assert r.status_code == 200
    assert 'id="offhead"' in r.text and 'id="offsay"' in r.text
    assert 'id="offgo"' in r.text, "no way to retry"
    assert 'class="grid"' not in r.text
    # Cached by the worker, so it must decide at run time rather than be
    # served knowing the answer.
    assert "/healthz" in r.text


def test_the_worker_cache_is_bumped_when_the_shell_changes(
    app_env: dict[str, Path]
) -> None:
    """An installed app keeps the shell it cached. Changing the offline page
    without changing the cache name leaves every phone in the house holding the
    old one — which, for a page whose whole job is to explain a failure, means
    explaining it the wrong way for as long as the install lasts."""
    body = TestClient(web.app).get("/sw.js").text

    assert "pix2-shell-v2" in body, "shell cache not bumped"


def test_the_page_head_offers_the_app_to_both_phones(
    client: TestClient
) -> None:
    """Android reads the manifest; iOS reads its own tags and ignores it."""
    head = client.get("/browse").text.split("</head>")[0]

    assert '<link rel="manifest" href="/manifest.webmanifest">' in head
    assert '<meta name="theme-color" content="#14161a">' in head
    assert '<link rel="apple-touch-icon" href="/apple-touch-icon.png">' in head
    assert 'name="apple-mobile-web-app-capable"' in head
    # Without this the page stops at the notch and the app looks inset.
    assert "viewport-fit=cover" in head


def test_the_chrome_keeps_clear_of_the_notch(client: TestClient) -> None:
    """`env()` is zero in a browser tab, so this costs nothing there and is the
    difference between an app and a web page in a window everywhere else."""
    css = client.get("/browse").text

    assert "env(safe-area-inset-top)" in css
    assert "env(safe-area-inset-bottom)" in css


def test_the_install_offer_is_on_every_page(client: TestClient) -> None:
    """In the shell rather than in the grid's script, so it works on the pages
    that carry no page script at all — the same reason the account menu opens
    on CSS alone."""
    for path in ("/browse", "/history", "/accounts"):
        assert "pix2.install" in client.get(path).text, path


def test_the_offer_is_not_made_to_a_desktop_browser(client: TestClient) -> None:
    """Asserted on the snippet rather than on a rendered page, because the
    decision is the browser's to make at run time: the server sends the same
    HTML to every device."""
    js = web._INSTALL_JS

    assert "iPhone|iPad|iPod" in js and "/Android/" in js
    assert "if (!ios && !android) return;" in js
    # And never inside the thing it is offering.
    assert "display-mode: standalone" in js
    assert "navigator.standalone" in js


# --- reachable with a thumb ---------------------------------------------------

def _media_block(sheet: str, query: str) -> str:
    """One `@media` block's body, braces balanced.

    A rule's presence in the stylesheet says nothing; which block it is in is
    the whole of what these tests are about, and a nested block means the
    first closing brace is not the end.
    """
    at = sheet.index("@media " + query)
    depth, start = 0, sheet.index("{", at)
    for i in range(start, len(sheet)):
        depth += (sheet[i] == "{") - (sheet[i] == "}")
        if depth == 0:
            return sheet[start + 1:i]
    raise AssertionError(f"unclosed @media {query}")


def test_a_finger_gets_a_control_it_can_hit() -> None:
    """Apple asks for 44 points square; the bar was drawn to 31.

    `--ctl` is the one number: the height the action row reserves and the
    height a button measures both come off it, so a block that raised the
    buttons without raising it would put a 44px control in a 31px hole.
    """
    coarse = _media_block(web._STYLE, "(pointer: coarse)")

    assert "--ctl:44px" in coarse
    assert "min-height:44px" in coarse


def test_the_three_circles_are_left_out_of_it() -> None:
    """The stylesheet's own warning, and it has been paid for once: a
    `min-height` outranks their fixed `height` and would make an oval of every
    select circle in the grid. They get an invisible slug instead, so twenty
    pixels on screen is forty-four to a thumb."""
    coarse = _media_block(web._STYLE, "(pointer: coarse)")

    assert "button:not(.tick):not(.grppick):not(.pick)" in coarse
    assert ".pick::before, .tick::before, .grppick::before" in coarse
    assert "inset:-12px" in coarse
    # Positioned, or the slug is laid out against the page instead.
    assert ".tick, .grppick { position:relative; }" in coarse


def test_hiding_and_sizing_are_asked_as_two_questions() -> None:
    """A touchscreen laptop has a pointer and wants nothing revealed; a phone
    on a trackpad has a coarse one and wants nothing enlarged. Answering both
    with one query gets one of them wrong."""
    hover = _media_block(web._STYLE, "(hover: none)")

    # What hover hides is unreachable here, and only that.
    assert ".choose { opacity:1; }" in hover
    assert "--ctl" not in hover


def test_a_sign_in_field_does_not_zoom_the_page() -> None:
    """Sixteen pixels is the exact threshold below which the phone zooms in on
    a focused field and does not zoom back.

    In the login sheet rather than the main one because that sheet is served
    *after* it: `.gate input { font:inherit }` is the same weight and the last
    one counts, so the rule would have been written and quietly lost.
    """
    assert "font-size:16px" in _media_block(web._LOGIN_CSS, "(pointer: coarse)")
    assert "font-size:16px" in _media_block(web._STYLE, "(pointer: coarse)")


def test_every_control_in_the_bar_grows_together() -> None:
    """Two of the chips are spans rather than buttons — the one saying which
    operation you arrived from, and the one saying which stack you are in — so
    a rule about buttons alone leaves 31px chips in a 44px row, which is the
    misalignment `--ctl` exists to prevent."""
    coarse = _media_block(web._STYLE, "(pointer: coarse)")
    rule = coarse[coarse.index("button:not(.tick)"):]

    assert rule.split("{")[0].strip().endswith(".chip")


def test_a_dropdown_row_outranks_the_rule_above_it() -> None:
    """Three `:not()`s count as three classes, so the rule that makes every
    button 44px is the *heavier* selector and a plain `.memenu button` loses
    to it — which shrink-wraps Sign out to its own text under History and
    Accounts, which stay full width because they are links and that rule never
    touched them.

    Locked down because it is invisible until somebody opens the account menu
    on a phone, and because the obvious tidy-up is to drop the `:not()`s.
    """
    coarse = _media_block(web._STYLE, "(pointer: coarse)")
    rows = coarse[coarse.index(".memenu a,"):].split("{")[0]

    assert ".memenu button:not(.tick):not(.grppick):not(.pick)" in rows
    # Equal weight, so the later one counts — and it has to be the later one.
    assert coarse.index("button:not(.tick)") < coarse.index(".memenu button:not(")


# --- the filters are drawn, not spelled out -----------------------------------

def test_every_filter_has_a_drawing() -> None:
    """The bar says which question a chip asks by drawing it, so a filter
    added to `_CHIPS` without a mark is a button with nothing in it. The page
    falls back to the name rather than rendering an empty control — this is
    what stops that fallback from being the thing anybody actually sees."""
    missing = [col for col, _ in web._CHIPS if col not in web._MARKS]

    assert not missing, f"no drawing for: {', '.join(missing)}"


def test_no_two_filters_are_drawn_the_same() -> None:
    """They are told apart at seventeen pixels and only by their shape."""
    marks = [web._MARKS[col] for col, _ in web._CHIPS]

    assert len(set(marks)) == len(marks)


def test_a_drawing_takes_the_colour_of_whatever_it_is_in() -> None:
    """`currentColor` throughout, so a chip that is doing something and one
    that is not are the same drawing and not two of them — and the dim state,
    the hover and the accent all come free."""
    svg = web._mark("event")

    assert 'stroke="currentColor"' in svg and "fill=\"none\"" in svg
    # The same grid and weight as the bell in the bar beside them, which is
    # what makes the set read as one family.
    assert 'viewBox="0 0 24 24"' in svg and 'stroke-width="1.7"' in svg


def test_an_unknown_name_draws_nothing_rather_than_a_broken_shape() -> None:
    assert web._mark("nonesuch") == ""


def test_the_footer_carries_no_instructions(client: TestClient) -> None:
    """A standing sentence about clicking and holding is read once and then
    occupies a fixed strip at the bottom of every screen for as long as the
    app exists — which on a phone was three lines of it."""
    html = client.get("/browse").text
    footer = html[html.index('<footer'):html.index("</footer>")]

    assert "shift" not in footer and "circle to select" not in footer
    # What is left is what this page is now.
    assert 'id="count"' in footer and 'id="note"' in footer


def test_taking_a_copy_away_is_drawn_too(client: TestClient) -> None:
    """One mark for it on every platform, because it is one gesture: into
    something, downwards. The word stays beside it in the bar — it is one of
    ten actions there and the other nine are words — and goes in the viewer,
    where three controls sit across the top of a photograph."""
    html = client.get("/browse").text

    assert '<button data-act="download" title="Download" '            'aria-label="Download"><svg' in html
    assert '<span class="word">Download</span>' in html
    assert '<a id="viewget" class="who-link" download><svg' in html


def test_details_is_a_tab_where_there_is_no_room_for_a_column() -> None:
    """330px of rail on a 393px phone leaves sixty pixels of photograph, which
    is the thing the viewer is for. So the two stop sharing: the control that
    opened the column switches between them instead."""
    narrow = _media_block(web._STYLE, "(max-width: 720px)")

    assert "#viewer:not(.norail) .stage { display:none; }" in narrow
    # And the rail takes the whole of it rather than a slice.
    assert "max-height:none" in narrow


def test_the_unused_filters_fold_away_where_they_do_not_fit() -> None:
    """Ten glyphs fit across a desktop bar and do not fit across a phone. Both
    the glyphs and the `+` are always rendered and the stylesheet picks, since
    which one applies can change while the page is open by turning the phone
    over."""
    narrow = _media_block(web._STYLE, "(max-width: 720px)")

    assert ".chips .spare { display:none; }" in narrow
    assert ".chips .addchip { display:inline-flex; }" in narrow
    # The other way round outside it.
    assert ".chips .addchip { display:none; }" in web._STYLE


def test_the_two_bars_wear_the_same_drawings() -> None:
    """*Event* the filter and *Event* the action are one question asked twice
    — once about what you are looking at, once about what it should become.
    The bars already ask them in the same order; a control that changes its
    face between them is a control you have to learn twice."""
    assert web._ACT_MARKS["event"] == "event"
    assert web._ACT_MARKS["tags"] == "tag"
    assert web._ACT_MARKS["access"] == "audience"
    assert web._ACT_MARKS["stack"] == "stacks"
    assert web._ACT_MARKS["delete"] == "deleted"
    # Which is the same pairing the script uses to decide what a menu writes,
    # and the two must not disagree about what an action is about.
    for act, col in (("tags", "tag"), ("access", "audience"),
                     ("event", "event")):
        assert f"{act}:'{col}'" in web._BROWSE_JS


def test_every_action_is_drawn() -> None:
    """Eleven buttons in a row and four of them illustrated is not a style,
    it is an unfinished edit."""
    html = web._actions(web.Principal(name="admin", is_admin=True))
    acts = set(re.findall(r'data-act="(\w+)"', html))

    assert acts
    undrawn = [a for a in acts if not web._mark(web._ACT_MARKS.get(a, ""))]
    assert not undrawn, f"no drawing for: {', '.join(sorted(undrawn))}"


def test_no_two_actions_are_drawn_the_same() -> None:
    """Delete and Purge are the nearest pair — both the bin — and the cross
    inside one of them is the whole difference between recoverable and not."""
    marks = [web._mark(name) for name in web._ACT_MARKS.values()]

    assert len(set(marks)) == len(marks)


def test_an_action_keeps_its_word_where_the_script_can_find_it() -> None:
    """The takeover names an action by reading it off its own button rather
    than keeping a second vocabulary for the same four words. With a drawing
    in there too, the word has to be its own element — `textContent` on the
    button would take the drawing with it, and one control already sets it."""
    html = web._actions(web.Principal(name="admin", is_admin=True))

    assert '<span class="word">Event&hellip;</span>' in html
    assert "[data-act=\"'+act+'\"] .word" in web._BROWSE_JS


def test_an_action_is_its_drawing_alone_on_a_phone() -> None:
    """Eleven drawings and eleven words is two rows of bar on a screen with
    none to give, and the drawing is the half that survives being small."""
    narrow = _media_block(web._STYLE, "(max-width: 720px)")

    assert "#actions .grp button .word { display:none; }" in narrow
    assert "min-width:44px" in narrow


def test_an_action_keeps_a_name_where_the_word_is_hidden() -> None:
    """A control whose only name is switched off by a media query has no name
    at all — not to a screen reader, and not to anyone hovering it on a
    desktop either. So the word is carried three times over."""
    html = web._actions(web.Principal(name="admin", is_admin=True))

    assert 'title="Make top" aria-label="Make top"' in html
    # The ellipsis says *this one asks something next*, which is a fact about
    # the button rather than part of what it is called.
    assert 'title="Event" aria-label="Event"' in html
    assert '<span class="word">Event&hellip;</span>' in html


# --- who is in the photograph -------------------------------------------------

def test_people_can_be_put_on_a_file_and_read_back(
    client: TestClient, writable: Path
) -> None:
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_people": ["Mom", "Dad"]})

    assert r.status_code == 200
    assert r.json()["people"] == ["Dad", "Mom"]
    after = decisions.read(writable / "a.jpg")
    assert after is not None and after.people == ("Dad", "Mom")


def test_people_do_not_become_an_audience(
    client: TestClient, writable: Path
) -> None:
    """The one confusion this feature exists to avoid. Putting Mum in a
    photograph must not share it with her, and a household where it did would
    be one where every picture of the children was visible to them."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_people": ["Mom"]})

    after = decisions.read(writable / "a.jpg")
    assert after is not None
    assert after.people == ("Mom",)
    assert after.audience == ()


def test_an_audience_does_not_become_a_person(
    client: TestClient, writable: Path
) -> None:
    """And the other way round, which is the same mistake read backwards."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["family"]})

    after = decisions.read(writable / "a.jpg")
    assert after is not None
    assert after.audience == ("family",) and after.people == ()


def test_the_library_can_be_filtered_to_one_person(
    client: TestClient, writable: Path
) -> None:
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_people": ["Mom"]})

    with_her = client.get("/api/files?person=Mom").json()
    assert [f["name"] for f in with_her] == ["a.jpg"]
    assert client.get("/api/files?person=Nobody").json() == []


def test_a_person_filter_is_not_an_audience_filter(
    client: TestClient, writable: Path
) -> None:
    """Two tables, two clauses. Sharing with `family` must not make a file
    turn up under *pictures of family*."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["family"]})

    assert client.get("/api/files?person=family").json() == []
    assert client.get("/api/files?audience=family").json()


def test_the_names_already_in_use_are_offered(
    client: TestClient, writable: Path
) -> None:
    """What keeps *Mom* and *mom* from becoming two people across a library is
    the menu offering the names already there, so they are picked rather than
    retyped — the same thing that keeps tags tidy."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_people": ["Mom"]})

    assert "Mom" in [s["value"] for s in
                     client.get("/api/suggest?column=person").json()]


def test_a_thumbnail_carries_who_is_in_it(
    client: TestClient, writable: Path
) -> None:
    """The grid reads the selection's current people off the cells, so a bulk
    edit can tell *all of them*, *some of them* and *none* apart."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_people": ["Mom"]})

    assert 'data-people="Mom"' in client.get("/browse").text


def test_people_are_asked_about_separately_from_access(
    client: TestClient
) -> None:
    """Two chips, two actions, two drawings — and the drawings must not be the
    same one, because telling these two apart is the whole point."""
    assert ("person", "People") in web._CHIPS
    assert web._ACT_MARKS["people"] == "person"
    assert web._ACT_MARKS["access"] == "audience"
    assert web._mark("person") != web._mark("audience")
