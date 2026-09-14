"""The browse app (spec/nas-app.md §8).

It reads the index and serves the derived tiers. It never decodes anything —
`process` already made everything it displays. The one thing it writes is
curation decisions: an `.xmp` beside the master file, then that file's index row
— sidecar first, index follows.
"""

from __future__ import annotations

import re

from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from pix.nas import accounts
from pix.nas import decisions
from pix.nas import history
from pix.nas import index as ix
from pix.nas import web
from pix.nas.web import _split
from pix.nas.decisions import Decision


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
    assert acts[:8] == ["event", "tags", "date", "access",
                        "stack", "top", "unstack", "delete"], acts

    chips = html[html.index("CHIPS="):html.index("FIXED=")]
    for earlier, later in (("event", "tag"), ("tag", "date"),
                           ("date", "audience"), ("audience", "kind"),
                           ("kind", "band"), ("band", "deleted")):
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

    assert ".chips {" in html
    assert "gap:9px" in html[html.index(".chips {"):html.index(".chips {") + 120]


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


def test_suggestions_are_shown_open_and_marked_as_proposals(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Expanded, because the question is *are these the same photograph* and a
    collapsed stack shows one of them. Marked as a proposal, because nothing
    here has been decided and a suggestion dressed as a stack would be the app
    having quietly made up its mind."""
    _burst(app_env, writable, "x.jpg", "y.jpg")

    html = client.get("/browse?suggest=1").text

    assert "group proposal" in html
    assert 'data-name="x.jpg"' in html and 'data-name="y.jpg"' in html
    assert "Not a stack" in html
    assert "2 photographs" in html


def test_the_review_shows_only_what_has_something_to_answer(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """A photograph resembling none of its neighbours has nothing to review."""
    _burst(app_env, writable, "x.jpg", "y.jpg")

    assert "a.jpg" in client.get("/browse").text, "fixture never held it"
    assert "a.jpg" not in client.get("/browse?suggest=1").text, (
        "offered a file with nothing to answer")


def test_declining_a_suggestion_is_remembered(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Scrolling past the same refusal every time is worse than never having
    been offered it."""
    _burst(app_env, writable, "x.jpg", "y.jpg")
    assert "group proposal" in client.get("/browse?suggest=1").text

    client.post("/api/decide/bulk", json={
        "no_stack": True,
        "files": [{"folder": "init_2026", "name": "x.jpg"},
                  {"folder": "init_2026", "name": "y.jpg"}]})

    assert decisions.read(writable / "x.jpg") == Decision(no_stack=True)
    assert "group proposal" not in client.get("/browse?suggest=1").text


def test_accepting_a_suggestion_is_an_ordinary_stack(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Nothing special is written. A suggestion accepted is a stack, made the
    way any other is — so it unstacks, reverts and behaves like one."""
    _burst(app_env, writable, "x.jpg", "y.jpg")

    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/x.jpg",
        "files": [{"folder": "init_2026", "name": "y.jpg"}]})

    assert decisions.read(writable / "y.jpg") == Decision(
        stacked_under="init_2026/x.jpg")
    assert "group proposal" not in client.get("/browse?suggest=1").text


def test_declining_is_recorded_and_can_be_taken_back(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """It is a decision like the others, so it is in the log and revertible —
    which is the way back if you dismiss a screenful by mistake."""
    _burst(app_env, writable, "x.jpg", "y.jpg")
    client.post("/api/decide/bulk", json={
        "no_stack": True,
        "files": [{"folder": "init_2026", "name": "x.jpg"},
                  {"folder": "init_2026", "name": "y.jpg"}]})

    op = history.recent()[0]
    assert op.summary == "said 2 files are not a stack", op.summary

    client.post("/history/revert", data={"id": op.id})
    assert decisions.read(writable / "x.jpg") is None
    assert "group proposal" in client.get("/browse?suggest=1").text


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


def test_the_home_page_groups_by_year(client: TestClient) -> None:
    """A flat list of every event across twenty-five years is a list nobody
    can find their place in."""
    html = client.get("/").text
    assert 'href="/browse?date=2026"' in html
    assert "events reviewed" in html


def test_an_event_row_links_to_that_year_and_event(
    client: TestClient
) -> None:
    html = client.get("/").text
    assert "date=2026&amp;event=Italy%20-%20Sicily" in html


def test_undated_files_are_reachable(client: TestClient) -> None:
    """374 of the seeded year have no date; that is a work item, not an
    absence to leave unlinked."""
    assert "(undated)" in client.get("/").text
    assert "b.mp4" in client.get("/browse?date=(undated)").text


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

    assert 'class="spacer"' in html
    assert html.index('class="spacer"') < html.index('Sign out')


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



