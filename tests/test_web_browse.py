"""The grid: what it shows, how it pages, what a thumbnail says, the viewer, and the page script's view of the page.

Split out of the one web test module, by area.
"""

from __future__ import annotations

import json
import pytest
import re
from collections.abc import Callable
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient
from web_helpers import (
    as_columns,
    burst,
    card_of,
    cell_html,
    corner,
    has_rule,
    media_block,
    pix,
    relative,
    shaped,
    targets,
    three_files,
    two_files,
)

from pix import __version__ as PIX_VERSION
from pix.nas import (
    accounts,
    decisions,
    derive,
    history,
    index as ix,
    web,
    webroots,
)
from pix.nas.decisions import Decision
from pix.nas.webapp import (
    marks as w_marks,
    pages as w_pages,
    shell as w_shell,
    text as w_text,
    vocab as w_vocab,
)


# --- browse ---------------------------------------------------------------

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
    assert '.cell.gone .ov.top .fix::before { content:"\\2715";' in html, \
        "nothing marks it"


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
    assert not has_rule(css, 'button, .chip', 'min-height')


def test_every_list_of_questions_is_in_the_same_order(
    client: TestClient
) -> None:
    """One arrangement, learned once.

    The app asks the same questions in four places — the filter bar, the edit
    bar, the grouping menu and the list of filterable columns — and each holds
    its own subset in its own order. Where two of them share a question, it has
    to fall in the same place in both, or the bar teaches you an order that the
    menu below it then contradicts.

    Asserted as the *rule* rather than as four hard-coded lists, which is the
    point: a list that is merely correct today gets a question appended to the
    end of it by whoever adds the next one. This fails instead.

    Only shared questions are compared, so each list stays free to hold what
    the others do not, and to put it where it likes: the grouping menu opens
    with day, month and year because that is how the library is mostly read,
    and no filter is displaced by it.
    """
    html = client.get("/browse?event=Italy%20-%20Sicily").text
    bar = [col for col, _ in w_vocab.CHIPS]

    others = {
        "the edit bar": as_columns(re.findall(r'data-act="(\w+)"', html)),
        "the grouping menu": [g for g, _ in w_vocab.GRID_GROUPS],
        "the filterable columns": list(ix.Filters.NAMES),
    }
    for what, order in others.items():
        shared = set(bar) & set(order)
        assert relative(bar, shared) == relative(order, shared), (
            f"{what} asks these in a different order from the filter bar: "
            f"bar={relative(bar, shared)} {what}={relative(order, shared)}")


def test_the_two_bars_ask_the_same_questions_first(client: TestClient) -> None:
    """What a file *is* comes before what happens to it, in both bars — so the
    run they share is a prefix of each rather than five things scattered
    through it."""
    html = client.get("/browse?event=Italy%20-%20Sicily").text
    acts = as_columns(re.findall(r'data-act="(\w+)"', html))

    assert acts[:5] == ["event", "tag", "person", "date", "audience"]
    assert [c for c, _ in w_vocab.CHIPS][:5] == acts[:5]


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
    view = html[html.index('"VIEW": '):html.index(', "CHIPS"')]

    for name in ("event", "tag", "date", "audience", "kind", "band", "deleted"):
        assert f'"{name}"' in view, f"{name} missing from {view}"
    assert '"deleted": "gone"' in view, view


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


def test_stacking_folds_a_file_behind_another(
    client: TestClient, writable: Path,
    app_env: dict[str, Path]
) -> None:
    """Each file records which one it defers to; the top records nothing,
    because being spoken for is the decision and speaking is what is left."""
    two_files(writable, app_env)
    r = client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.jpg"}]})
    assert r.status_code == 200, r.text

    assert decisions.read(writable / "b.jpg") == Decision(
        stacked_under="init_2026/a.jpg")
    assert decisions.read(writable / "a.jpg") is None, "the top recorded something"

    html = client.get("/browse?event=Italy%20-%20Sicily").text
    assert "b.jpg" not in html, "a stacked file appeared on its own"
    assert "a.jpg" in html
    assert 'class="stack"' in html, "the top is not badged"
    assert "/stack/init_2026/a.jpg" in html, "no way to open the stack"


def test_opening_a_stack_keeps_the_view_you_opened_it_from(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The badge said `/browse?within=…` and nothing else, so opening a stack
    threw away every filter and the grouping with them: four filters deep in a
    review pass, click a badge, and the way back out is the undivided library.

    It survived for as long as the way out was the browser's own back button,
    which restores a page rather than rebuilding one. The moment anything
    navigated forward instead — which is what agreeing with a suggestion now
    does — the view the page thought it was in was empty, because this link is
    where it came from.
    """
    two_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.jpg"}]})

    html = client.get("/browse?event=Italy%20-%20Sicily&group=event").text
    at = html.index('class="stack"')
    href = html[html.index('href="', at) + 6:html.index('"', html.index('href="', at) + 6)]

    assert href.startswith("/stack/init_2026/a.jpg?back="), href
    # The whole address it was opened from, carried rather than guessed at:
    # the browser would do it on the way out and cannot on the way in, and a
    # stack reached from a bookmark has nothing behind it at all.
    assert "event%3DItaly%2520-%2520Sicily" in href or \
        "event%3DItaly%20-%20Sicily" in href, href
    assert "group%3Devent" in href, "the grouping went"
    # One parameter, so the whole grid address is percent-encoded inside it
    # rather than carrying bare `&`s of its own — the same rule the
    # `&amp;amp;` bug taught, arrived at from the other side: escape once, at
    # the boundary being crossed.
    assert "&" not in href, href


def test_the_page_draws_the_same_badge_the_server_does(
    client: TestClient
) -> None:
    """A stack agreed with from the grid gets its badge rewritten in place
    rather than fetched, and the two have to say the same thing — a badge
    written here that dropped the filters would put the curator back at the
    undivided library from one half of the app and not the other."""
    js = w_pages.BROWSE_JS
    at = js.index("function markStack(")
    body = js[at:js.index("\n}", at)]

    assert "badge.href=stackUrl(keyOf(c));" in body, body
    assert "'/browse?within='" not in js, "a second way of writing the address"


def test_a_stack_cannot_hold_two_kinds_of_thing(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """A stack says *these are the same shot, and this one speaks for the
    rest*. A photograph and a clip are not the same shot whatever else they
    share, and neither can stand in for the other — so folding one behind the
    other hides a thing nothing on screen represents.

    Refused here as well as in the page, because this is the scripting
    surface: a rule only the page holds is one the next client does not."""
    two_files(writable, app_env)

    r = client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})

    assert r.status_code == 400, r.text
    assert "one shot" in r.text, r.text
    # And it says what it found, rather than only that it refused.
    assert "photograph" in r.text and "video" in r.text, r.text
    assert decisions.read(writable / "b.mp4") is None, "written anyway"

    # Both doors: the page writes through one of these and a script through
    # the other, and a rule that only one of them holds is not a rule.
    one = client.post("/api/decide", json={
        "folder": "init_2026", "name": "b.mp4",
        "stacked_under": "init_2026/a.jpg"})

    assert one.status_code == 400, one.text
    assert decisions.read(writable / "b.mp4") is None, "written anyway"


def test_a_clip_can_still_be_taken_out_of_a_stack_made_before_the_rule(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Unstacking names no file to defer to, so there is nothing for it to be
    the same kind as. A library that already holds a mixed stack has to be
    able to take it apart, or the rule would strand what it came too late to
    prevent."""
    two_files(writable, app_env)
    decisions.change(writable / "b.mp4", stacked_under="init_2026/a.jpg")

    r = client.post("/api/decide/bulk", json={
        "stacked_under": None,
        "files": [{"folder": "init_2026", "name": "b.mp4"}]})

    assert r.status_code == 200, r.text
    assert decisions.read(writable / "b.mp4") is None


def test_opening_a_stack_shows_what_is_behind_it(
    client: TestClient, writable: Path,
    app_env: dict[str, Path]
) -> None:
    two_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.jpg"}]})

    html = client.get("/browse?within=init_2026/a.jpg").text
    assert "a.jpg" in html and "b.jpg" in html


def test_an_opened_stack_shows_its_top_first(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The one that speaks leads, whoever said so. `a.jpg` is four minutes
    older than `b.jpg`, so newest-first puts it second — and it is the
    photograph the stack is drawn as, which makes it the first thing worth
    looking at when the stack is opened."""
    two_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.jpg"}]})

    html = client.get("/browse?within=init_2026/a.jpg").text
    grid = html[html.index('<div class="cells">'):]

    assert grid.index('data-name="a.jpg"') < grid.index('data-name="b.jpg"'), \
        "the one the grid outside shows is not the first one in here"


def test_a_stack_is_a_page_rather_than_a_filtered_grid(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The takes of one photograph are not a view of the library, and
    everything the grid brought with it answered nothing in there: eleven
    filters over eight frames of one moment, a grouping control for a section
    that is the whole page, and a *Stack* button offering to stack what is
    already a stack — on two of them, offering to stack a subset, which is not
    a thing a stack can be.

    Each of those had to be reasoned about separately every time anything
    changed, and every stack bug this app has had was one of them leaking."""
    two_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.jpg"}]})

    html = client.get("/stack/init_2026/a.jpg").text
    bar = html[html.index('<div class="topbar">'):html.index("<main>")]

    assert 'id="chips"' not in bar, "the library's filters, over one moment"
    assert 'class="whatis"' in bar and "of one photograph" in bar
    assert 'class="leave"' in bar, "no way out"

    acts = bar[bar.index('id="actions"'):]
    for gone in ("event", "tags", "people", "date", "access", "stack", "top"):
        assert f'data-act="{gone}"' not in acts, gone
    for kept in ("nostack", "unstack", "download", "delete"):
        assert f'data-act="{kept}"' in acts, kept
    # And no heading, because the page is the section.
    assert 'class="group' not in html[html.index("<main>"):]


def test_a_guess_says_that_it_is_one(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Guesses and decisions are the same page — a suggestion is the app's
    answer to the same question, offered rather than recorded — so the only
    things that differ are the words and whether *Take out* means anything
    yet. Undoing a decision is what that is for, and a guess has none."""
    burst(app_env, writable, "g1.jpg", "g2.jpg", "g3.jpg")
    html = client.get("/stack/init_2026/g1.jpg").text

    assert "the app&#x27;s guess" in html or "the app's guess" in html, \
        html[html.index('class="whatis"'):html.index('class="whatis"') + 200]
    acts = html[html.index('id="actions"'):html.index("</main>")]
    assert 'data-act="unstack"' not in acts, "nothing to undo"
    assert 'data-act="nostack"' in acts


def test_the_old_address_of_a_stack_says_where_it_lives_now(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """`?within=` was the address for as long as a stack was a filter, and
    links to it are in bookmarks and in the operation log. One place to see a
    stack rather than two that have to agree about it."""
    two_files(writable, app_env)
    r = client.get("/browse?within=init_2026/a.jpg&event=Italy+-+Sicily",
                   follow_redirects=False)

    assert r.status_code == 307
    where = r.headers["location"]
    assert where.startswith("/stack/init_2026/a.jpg?back="), where
    # Carrying the grid it was asked from, so the way out is the way in
    # reversed rather than the undivided library.
    assert "event" in where, where


def test_a_stack_is_no_longer_somewhere_the_library_can_be_narrowed_to(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """A filter narrows the library, and the narrowed library is still the
    library: the same page, the same controls, one fewer thing on it. The
    takes of one photograph are not that — they are a question about a
    photograph — and asking it through the grid is what put eleven filters
    over eight frames of one moment.

    Out of `NAMES`, which is what a chip is drawn from and what the page
    rebuilds its own address out of. What is left is the query the stack page
    runs, and the one thing a write has to say about where it was made."""
    assert "within" not in ix.Filters.NAMES
    assert "within" not in [name for name, _ in w_vocab.CHIPS]

    two_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.jpg"}]})

    # Asked of the grid, it is a forwarding address rather than a filter.
    r = client.get("/browse?within=init_2026/a.jpg", follow_redirects=False)
    assert r.status_code == 307, r.status_code
    # And nothing on the grid can put one back.
    grid = client.get("/browse").text
    assert "within" not in grid[grid.index('"VIEW": '):grid.index(', "CHIPS"')]


def test_a_write_still_says_which_stack_it_was_made_in(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The one thing about *where* a write was made that changes what it does.
    Refusing a guess writes `no_stack` to the photograph that speaks for it
    and the server carries that to the rest of the group — unless they are
    already in front of the curator, which on a stack's page they are, and
    following them again would be a second write to a file somebody can see
    they picked."""
    burst(app_env, writable, "g1.jpg", "g2.jpg", "g3.jpg")

    folded = client.post("/api/decide/bulk", json={
        "no_stack": True,
        "files": [{"folder": "init_2026", "name": "g1.jpg"}]})
    assert folded.status_code == 200, folded.text
    reached = history.recent()[0]
    assert len(reached.files) == 3, [f.name for f in reached.files]

    burst(app_env, writable, "h1.jpg", "h2.jpg", "h3.jpg", at="13:00:0")
    opened = client.post("/api/decide/bulk?within=init_2026/h1.jpg", json={
        "no_stack": True,
        "files": [{"folder": "init_2026", "name": "h1.jpg"}]})
    assert opened.status_code == 200, opened.text
    named = history.recent()[0]
    assert len(named.files) == 1, [f.name for f in named.files]


def test_the_way_out_of_a_stack_cannot_be_pointed_elsewhere(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """`back` arrives in a query string, which is a place anybody can type
    anything, and a link on a page people trust is exactly what you would want
    to aim somewhere else."""
    two_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.jpg"}]})

    for bad in ("https://example.com/", "//example.com/", "javascript:alert(1)"):
        html = client.get("/stack/init_2026/a.jpg",
                          params={"back": bad}).text
        out = html[html.index('class="leave"'):]
        assert 'href="/"' in out[:40], (bad, out[:80])


def test_the_answer_can_be_given_from_the_photograph_itself(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Blown up is where the difference between two takes of one moment
    actually shows — which eyes are open, which one is sharp — and the answer
    was five gestures away: close the viewer, find the thumbnail you liked
    among seven that look alike, and hope it was that one."""
    two_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.jpg"}]})

    assert 'id="viewtop"' in client.get("/stack/init_2026/a.jpg").text
    # Not on the grid, where the viewer is for looking: a button deciding the
    # shape of a stack has no business over an ordinary photograph.
    assert 'id="viewtop"' not in client.get("/browse").text

    js = w_pages.BROWSE_JS
    at = js.index("function drawTop(")
    body = js[at:js.index("\n}", at)]
    assert "viewTop.hidden=!c||!STACK||(here&&!guessed(c));" in body, body
    assert "viewTop.textContent=here?'Confirm top':'Show this one';" in body


def test_unstacking_puts_a_file_back_on_its_own(
    client: TestClient, writable: Path,
    app_env: dict[str, Path]
) -> None:
    two_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.jpg"}]})
    client.post("/api/decide/bulk", json={
        "stacked_under": None,
        "files": [{"folder": "init_2026", "name": "b.jpg"}]})

    assert decisions.read(writable / "b.jpg") is None
    assert "b.jpg" in client.get("/browse?event=Italy%20-%20Sicily").text


def test_stacking_leaves_the_other_decisions_alone(
    client: TestClient, writable: Path,
    app_env: dict[str, Path]
) -> None:
    """It is one field like the rest: a file keeps its event and its audience
    when it goes behind another, and gets them back when it comes out."""
    two_files(writable, app_env)
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "b.jpg", "event": "Sicily Trip",
        "add_audience": ["family"]})
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.jpg"}]})

    assert decisions.read(writable / "b.jpg") == Decision(
        event="Sicily Trip", audience=("family",),
        stacked_under="init_2026/a.jpg")


def test_a_stack_is_recorded_and_can_be_put_back(
    client: TestClient, writable: Path,
    app_env: dict[str, Path]
) -> None:
    """Like any other decision — nothing new was built for the undo."""
    two_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.jpg"}]})

    op = history.recent()[0]
    assert op.summary == "stacked 1 file under a.jpg", op.summary

    client.post("/history/revert", data={"id": op.id})
    assert decisions.read(writable / "b.jpg") is None
    assert "b.jpg" in client.get("/browse?event=Italy%20-%20Sicily").text


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


def test_stacking_a_stack_brings_its_files_up(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Stacks are flat. Stacking a file that already speaks for others reads as
    *put all of these together* — and the alternative is not a deeper stack, it
    is a stranded one: the members end up a level down where no listing reaches
    them, and the count on the outermost file is wrong about what it holds."""
    three_files(writable, app_env)
    # c.jpg goes behind a.jpg.
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "c.jpg"}]})
    # Now a.jpg — which speaks for c.jpg — goes behind d.jpg.
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/d.jpg",
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    assert decisions.read(writable / "a.jpg") == Decision(
        stacked_under="init_2026/d.jpg")
    assert decisions.read(writable / "c.jpg") == Decision(
        stacked_under="init_2026/d.jpg"), "left a level down"

    inside = client.get("/browse?within=init_2026/d.jpg").text
    for name in ("a.jpg", "c.jpg", "d.jpg"):
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
    three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "c.jpg"}]})

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
    three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "c.jpg"}]})
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_tags": ["beach"]})

    html = client.get("/browse?event=Italy%20-%20Sicily").text
    assert "cell marked" in html
    # The badge is in the fixed end of the top lane and the tags flow in the
    # lane beside it, so neither can be drawn over the other.
    top = cell_html(html, "a.jpg")
    top = top[top.index('class="ov top"'):top.index('class="ov bot"')]
    lane, fix = top.split('class="fix"')
    assert 'class="tags"' in lane
    assert 'class="stack' in fix


def test_bringing_a_stack_up_is_part_of_the_same_gesture(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The files that came with it are in the same operation, so putting it
    back puts all of it back."""
    three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "c.jpg"}]})
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/d.jpg",
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    op = history.recent()[0]
    assert {f.name for f in op.files} == {"a.jpg", "c.jpg"}, [
        f.name for f in op.files]

    client.post("/history/revert", data={"id": op.id})
    assert decisions.read(writable / "c.jpg") == Decision(
        stacked_under="init_2026/a.jpg"), "it did not go back where it was"


def test_the_page_knows_it_is_inside_a_stack(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """It has to send that back with a write, or the server works out what left
    the view against a different view — and a file taken out of a stack sits
    there until the page is reloaded."""
    two_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "b.jpg"}]})
    html = client.get("/stack/init_2026/a.jpg").text
    view = html[html.index('"VIEW": '):html.index(', "CHIPS"')]

    # Not through the filters. `within` is out of `Filters.NAMES`, so the view
    # the page rebuilds its own address from cannot contain one — a stack is
    # not somewhere the library can be narrowed to.
    assert "within" not in view, view
    assert pix(html)["STACK"] == "init_2026/a.jpg", pix(html)["STACK"]

    # It is said on the write instead, which is the one place it changes
    # anything: with the members in front of the curator a cascade must not
    # follow them again.
    js = w_pages.BROWSE_JS
    at = js.index("await fetch('/api/decide/bulk?'")
    assert "if(STACK) p.set('within',STACK);" in js[at - 400:at], js[at - 400:at]


def test_agreeing_with_a_guess_leaves_the_stack_it_was_asked_in(
    client: TestClient
) -> None:
    """Agreeing is the question an opened suggestion was there to ask,
    answered. Nothing in there has anything left to say, and what changed is
    out in the grid it was folded into — which is still drawing it as a guess.

    Asked for afresh rather than gone back to. Going back is right for a stack
    you were only looking at, because the same address reached again is the
    same photographs at the top of the page rather than where you were
    standing; it is wrong the moment something has been written, because the
    page behind is the one that has just stopped being true. Going back and
    then reloading is what somebody had to do by hand for every suggestion
    they agreed with."""
    js = w_pages.BROWSE_JS

    assert "location.href=(BACK||'/')+'#'+encodeURIComponent(" in js
    # Standing on the photograph, not at the top of the page. Every other
    # stack gesture keeps your place by never navigating at all; this one has
    # to fetch, and a fetched page starts three thousand pixels above a review
    # pass somebody was part way through.
    land = js[js.index("let want='';"):]
    assert "location.hash" in land[:400], land[:400]
    assert "c.scrollIntoView({block:'center'})" in land[:800], land[:800]
    # **One rule, in one place.** The chooser and the bar end in the same
    # write, and each used to carry its own copy of where to go afterwards —
    # one navigated only when the stack had been renamed, the other whenever
    # it was open at all. The same rule written twice, already drifting, which
    # is how two ways into one decision came to need thinking about
    # separately.
    at = js.index("function afterStacking(")
    rule = js[at:js.index("\n}", at)]
    assert "if(!STACK) return;" in rule, rule
    assert "if(wasGuess) leaveStack(keyOf(top));" in rule, rule
    # Rearranging a stack somebody already made is not a question being
    # answered: it stays put — at the stack's new address, because a stack is
    # named by the file that speaks for it and promoting one renames it.
    assert ("else if(STACK!==keyOf(top)) "
            "location.href=stackUrl(keyOf(top));") in rule, rule

    # And both ways in end there, rather than each deciding for itself.
    for name in ("async function makeTop(", "async function chooseTop("):
        body = js[js.index(name):js.index("\n}", js.index(name))]
        assert "afterStacking(top,wasGuess)" in body, name
        assert "leaveStack(" not in body, f"{name} decides for itself"


def test_a_badge_stops_saying_guessed_once_it_has_been_decided(
    client: TestClient
) -> None:
    """The badge keeps its element and its place in the corner when a guess is
    agreed with from the grid, so without being told it keeps the amber that
    means *these look alike and nobody has said yet* over a number somebody
    has just decided — and the bar goes on offering to refuse a suggestion
    that is now a stack."""
    js = w_pages.BROWSE_JS
    at = js.index("function markStack(")
    body = js[at:js.index("\n}", at)]

    assert "c.dataset.proposed='0';" in body, body
    assert "badge.classList.remove('guessed');" in body, body
    # Which is the class the server draws it with, so the two agree about the
    # one word that distinguishes them.
    assert has_rule(w_shell.STYLE, '.stack.guessed', 'border-color:var(--top)')


def test_unstacking_takes_a_file_out_of_the_open_stack(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "c.jpg"}]})

    r = client.post("/api/decide/bulk?within=init_2026/a.jpg", json={
        "stacked_under": None,
        "files": [{"folder": "init_2026", "name": "c.jpg"}]})

    assert [d["name"] for d in r.json()["dropped"]] == ["c.jpg"], r.text


def test_unstacking_the_top_takes_the_whole_stack_apart(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """*Unstack this* said of the file that speaks means the stack, not the one
    photograph. Leaving the others deferring to a file that defers to nobody
    would leave a stack nobody asked to keep — and one with no way back to it,
    since the badge is drawn from what is behind the top."""
    three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "c.jpg"},
                  {"folder": "init_2026", "name": "d.jpg"}]})

    client.post("/api/decide/bulk", json={
        "stacked_under": None,
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    for name in ("a.jpg", "c.jpg", "d.jpg"):
        assert decisions.read(writable / name) is None, name
    html = client.get("/browse?event=Italy%20-%20Sicily").text
    for name in ("a.jpg", "c.jpg", "d.jpg"):
        assert name in html, name
    assert 'class="stack"' not in html


def test_taking_one_photograph_out_leaves_the_rest_stacked(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The same rule, the other way up: a file that speaks for nobody has
    nothing to cascade."""
    three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "c.jpg"},
                  {"folder": "init_2026", "name": "d.jpg"}]})

    client.post("/api/decide/bulk", json={
        "stacked_under": None,
        "files": [{"folder": "init_2026", "name": "c.jpg"}]})

    assert decisions.read(writable / "c.jpg") is None
    assert decisions.read(writable / "d.jpg") == Decision(
        stacked_under="init_2026/a.jpg"), "the rest came out too"


def test_taking_a_stack_apart_is_one_gesture(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """So putting it back puts the stack back, rather than one photograph of
    it."""
    three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "c.jpg"},
                  {"folder": "init_2026", "name": "d.jpg"}]})
    client.post("/api/decide/bulk", json={
        "stacked_under": None,
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    op = history.recent()[0]
    assert {f.name for f in op.files} == {"a.jpg", "c.jpg", "d.jpg"}, [
        f.name for f in op.files]

    client.post("/history/revert", data={"id": op.id})
    assert decisions.read(writable / "c.jpg") == Decision(
        stacked_under="init_2026/a.jpg")
    assert decisions.read(writable / "d.jpg") == Decision(
        stacked_under="init_2026/a.jpg")


def test_the_files_behind_a_stack_can_be_fetched_as_cells(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Merging two stacks has to offer every photograph in both of them, and
    the members are not on the page — that is what stacking them did. They come
    back as the same markup the grid is made of, because a second copy of a
    cell written in JavaScript would drift from this one."""
    three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "c.jpg"},
                  {"folder": "init_2026", "name": "d.jpg"}]})

    cells = client.get("/api/behind/init_2026/a.jpg").json()["cells"]

    assert 'data-name="c.jpg"' in cells
    assert 'data-name="d.jpg"' in cells
    assert 'data-name="a.jpg"' not in cells, "returned the top as well"
    assert 'class="cell' in cells and 'class="pick"' in cells


def test_what_is_behind_a_stack_is_still_scoped_to_the_viewer(
    client: TestClient, sign_in: "Callable[[str, str], TestClient]",
    writable: Path, app_env: dict[str, Path]
) -> None:
    """Fetching cells is a listing like any other. A stack is not a way to be
    handed photographs nobody shared with you."""
    three_files(writable, app_env)
    client.post("/accounts/save", data={"name": "kid", "password": "pw"})
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "c.jpg"}]})

    kid = sign_in("kid", "pw")
    assert kid.get("/api/behind/init_2026/a.jpg").json()["cells"] == ""


def test_promoting_a_file_does_not_leave_it_behind_itself(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """A top is a file nothing is behind. Stacking onto one that is itself
    stacked left a ring — it deferred to the file now deferring to it — and a
    ring shows nowhere, because every file in it is behind something. The whole
    stack vanished from the library."""
    three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "c.jpg"}]})

    # Promote c.jpg: everything else comes to defer to it.
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/c.jpg",
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    assert decisions.read(writable / "c.jpg") is None, "left behind itself"
    assert decisions.read(writable / "a.jpg") == Decision(
        stacked_under="init_2026/c.jpg")
    # And the stack is on screen, with the promoted file speaking for it.
    html = client.get("/browse?event=Italy%20-%20Sicily").text
    assert "c.jpg" in html
    assert "a.jpg" not in html
    assert 'class="stack"' in html


def test_taking_a_file_out_of_a_stack_is_part_of_promoting_it(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """One gesture, so one entry — and putting it back puts the stack back the
    way round it was."""
    three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "c.jpg"},
                  {"folder": "init_2026", "name": "d.jpg"}]})
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/c.jpg",
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    op = history.recent()[0]
    assert {f.name for f in op.files} == {"a.jpg", "c.jpg", "d.jpg"}, [
        f.name for f in op.files]

    client.post("/history/revert", data={"id": op.id})
    assert decisions.read(writable / "c.jpg") == Decision(
        stacked_under="init_2026/a.jpg")
    assert decisions.read(writable / "a.jpg") is None


def test_a_stale_index_cannot_put_a_file_behind_itself(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The cascade asks the index what is behind a file, and the index is
    allowed to be behind — that is the whole bargain. So the two can disagree
    about whether the file being stacked onto is already in the stack, and the
    one that thinks it is would write it behind itself."""
    three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "c.jpg"}]})

    # Master says c.jpg is free; the index still says it is behind a.jpg.
    decisions.sidecar_path(writable / "c.jpg").unlink()

    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/c.jpg",
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    assert decisions.read(writable / "c.jpg") is None, "put behind itself"


def test_a_heading_stays_at_the_top_while_you_are_in_its_section(
    client: TestClient
) -> None:
    """Two thousand thumbnails go past in a few seconds and they all look
    alike from four feet away. Without this the only thing saying which day
    you are in left the screen a long way back, and finding out costs you the
    place you were reading."""
    html = client.get("/browse?event=Italy%20-%20Sicily").text
    css = html[html.index("<style>"):html.index("</style>")]
    at = css.index("h3.group { ")
    rule = css[at:css.index("}", at)]

    assert "position:sticky" in rule, rule
    # Under the bar, not at the top of the window — and by a measurement
    # rather than a guess, because the bar is not one height.
    assert "top:var(--bar)" in rule, rule
    # A heading standing over its own section has photographs sliding under
    # it, so for the first time it needs something to stand on.
    assert "background:var(--bg)" in rule, rule
    # And nothing of the section may show between the two. A margin is outside
    # the background; padding is inside it.
    assert "margin:18px 0 0;" in rule, rule


def test_a_section_is_a_box_so_the_next_heading_pushes_the_last_one_off(
    client: TestClient
) -> None:
    """A sticky grid item is confined to its own grid area, and a heading's
    grid area is the single row it occupies — so as a child of the one grid
    it had nowhere to travel and stuck to nothing. Inside a section it has the
    section to travel through, and the next one arrives and displaces it,
    which is the whole behaviour with no scroll handler anywhere.
    """
    html = client.get("/?date=2026&group=year").text

    assert '<section class="sect" data-key=' in html
    # The heading first, then the things it names, both inside the box.
    sect = html[html.index('<section class="sect" data-key='):]
    assert sect.index("<h3 class=\"group") < sect.index('<div class="cells">')

    # And the grid is no longer the grid: it is a column of them, one per
    # section, which is what gives each heading a box to be confined to.
    css = html[html.index("<style>"):html.index("</style>")]
    assert has_rule(css, '.grid', 'display:flex; flex-direction:column')
    assert has_rule(css, '.cells', 'display:grid')
    # The columns still have to line up across sections, which they do because
    # every one of them resolves the same track list over the same width.
    for size, px in (("medium", "230px"), ("large", "380px")):
        assert f'.grid[data-size="{size}"] .cells' in css, size
        assert px in css, px


def test_the_bar_says_how_tall_it_is_rather_than_being_guessed_at(
    client: TestClient
) -> None:
    """The filters take a second line when there are enough of them, the
    selection row is on the grid and not on the landing page, and an installed
    app adds the strip behind the clock. A heading pinned to a constant sits
    over the bar or a gap below it, and it is wrong exactly when the page is
    busiest.

    A `ResizeObserver`, because what matters is the bar *changing height* —
    which is what the filters wrapping does, and which neither `scroll` nor
    `resize` reports."""
    js = w_shell.BAR_JS

    assert "ResizeObserver" in js and ".topbar" in js
    assert "setProperty(" in js and "'--bar'" in js
    # The fallback is the bar at its shortest, so the first frame is close
    # rather than wrong.
    assert "--bar:calc(49px + env(safe-area-inset-top,0px))" in w_shell.STYLE

    # On every page that has a bar, which is every page.
    for url in ("/browse", "/", "/history", "/accounts"):
        assert "'--bar'" in client.get(url).text, url


def test_the_grid_has_three_thumbnail_sizes(client: TestClient) -> None:
    """The third is where the thumbnail runs out. Cells stretch past their
    minimum to fill the row, so 230px already renders around 263 on a wide
    screen — from a 400px derived thumbnail, which is spent at that point. The
    largest reads `large`, derived at 1000px for exactly this."""
    html = client.get("/browse?event=Italy%20-%20Sicily").text

    # All three on show, with the one you are in pressed — not one button
    # carrying a letter for the size you would get by pressing it.
    for size in ("small", "medium", "large"):
        assert f'data-size="{size}"' in html, size
    assert 'aria-pressed' in html and 'aria-label="Thumbnail size"' in html
    # Whichever copy, it is at the far end of the row and never among the
    # chips: it changes how you are looking, never which photographs are here.
    row = html[html.index('class="row"'):html.index("</div><main")]
    assert row.index('class="sizeset"') > row.index("spacer")

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
    three_files(writable, app_env)
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "c.jpg"},
                  {"folder": "init_2026", "name": "d.jpg"}]})
    assert client.get("/stack/init_2026/a.jpg").text.count(
        "data-name=") == 3

    # Promote d.jpg the way `Make top` does.
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/d.jpg",
        "files": [{"folder": "init_2026", "name": "a.jpg"},
                  {"folder": "init_2026", "name": "c.jpg"}]})

    # A page, not a filtered grid, so the old address is not an empty view of
    # the library — it is a stack that is not there.
    was = client.get("/stack/init_2026/a.jpg")
    assert was.status_code == 404, "the old address still holds a stack"
    now = client.get("/stack/init_2026/d.jpg").text
    assert now.count("data-name=") == 3, "the stack is not at its new address"


def test_the_apps_guesses_can_be_switched_off(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """They fold by default — most of a burst is one photograph shot eight
    times, and a library showing all eight is the pile you started with. But
    *only the stacks I made* is a real question, and this is it."""
    burst(app_env, writable, "x.jpg", "y.jpg")

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
    burst(app_env, writable, "x.jpg", "y.jpg")

    html = client.get("/browse").text

    assert 'data-name="x.jpg"' in html, "nothing speaks for the group"
    assert 'data-name="y.jpg"' not in html, "the group is not folded"
    assert "stack guessed" in html
    assert 'data-proposed="1"' in html


def test_folding_a_guess_is_the_off_position_for_everybody(
    client: TestClient, writable: Path, app_env: dict[str, Path],
    sign_in: "Callable[[str, str], TestClient]",
    add_user: "Callable[..., None]"
) -> None:
    """A suggestion is the app saying *these look like one photograph*, and
    reading them as one is what makes a thousand of them reviewable — so it
    is what the library does until somebody says otherwise.

    A household member used to be pinned to `firm`, on the grounds that they
    could not accept or refuse a guess. They can now, and what was left was a
    filter whose cleared state and whose *No suggestions* value did the same
    thing — a control with an off position that is also one of its values,
    which reads as stuck rather than as careful.
    """
    burst(app_env, writable, "x.jpg", "y.jpg")
    add_user("kid", "pw")
    client.post("/api/decide/bulk", json={
        "add_audience": ["kid"],
        "files": [{"folder": "init_2026", "name": "x.jpg"},
                  {"folder": "init_2026", "name": "y.jpg"}]})
    kid = sign_in("kid", "pw")

    # Cleared, and the guess behaves as a stack: one card, the rest behind it.
    off = kid.get("/browse").text
    assert 'data-name="x.jpg"' in off
    assert 'data-name="y.jpg"' not in off, "the off position did not fold"

    # And `firm` is how to say otherwise — a value that now does something
    # the cleared chip does not.
    flat = kid.get("/browse?stacks=firm").text
    assert 'data-name="x.jpg"' in flat and 'data-name="y.jpg"' in flat

    # The same either way round for the owner, which is the point: one rule.
    assert ('data-name="y.jpg"' in client.get("/browse?stacks=firm").text)
    assert ('data-name="y.jpg"' not in client.get("/browse").text)


def test_an_opened_guess_says_what_each_photograph_is_in(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """A guessed stack lists its members through `suggested_under`, and the
    cell carried only what a file had been *decided* to be behind — so with
    the guess open, nothing in it was in a stack as far as the page was
    concerned. Neither question could be asked of a photograph in there:
    *this is the one to show*, and *this one does not belong*.

    Only while the view folds guesses, the same rule the count follows. With
    them off these are ordinary photographs sitting in the grid on their own
    and nothing is behind anything."""
    burst(app_env, writable, "x.jpg", "y.jpg")

    inside = client.get("/browse?within=init_2026/x.jpg").text
    cell = inside[inside.index('data-name="y.jpg"'):]
    cell = cell[:cell.index(">") + 1]

    assert 'data-proposed-under="init_2026/x.jpg"' in cell, cell

    # And nothing to be in, where the guesses are switched off.
    firm = client.get("/browse?within=init_2026/x.jpg&stacks=firm").text
    assert 'data-proposed-under=""' in firm, "a guess that is not being folded"


def test_only_stacks_is_every_stack_however_it_was_made(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The chip is about stacks, so *only stacks* means the ones somebody made
    as well as the ones the app proposes — they are the same thing decided by
    different parties."""
    burst(app_env, writable, "x.jpg", "y.jpg")
    # Hours from the burst and on another camera, so the app proposes nothing
    # about it — what goes behind `a.jpg` here is a stack somebody made. A
    # photograph, because a stack is one kind of thing.
    burst(app_env, writable, "m.jpg", at="18:00:0")
    client.post("/api/decide/bulk", json={
        "stacked_under": "init_2026/a.jpg",
        "files": [{"folder": "init_2026", "name": "m.jpg"}]})

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
    fixed = json.dumps(pix(html)["FIXED"])
    stacks = fixed[fixed.index('"stacks"'):]

    for label in ("Stacked", "Suggested", "Not in a stack"):
        assert label in stacks, stacks
    # Folding is how a view is drawn, not which files it holds, so it is in
    # Display rather than among these.
    assert "No suggestions" not in stacks
    assert '[""' not in stacks and '""]' not in stacks, "a value that clears"
    assert "Including suggestions" not in fixed, "the long way round"


def test_only_suggested_is_the_shelf_of_what_is_still_to_answer(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """A view of nothing but the unanswered. Grouped by stack it is the whole
    review: every guess, open, one section each."""
    burst(app_env, writable, "x.jpg", "y.jpg")

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
    burst(app_env, writable, "x.jpg", "y.jpg")

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
    burst(app_env, writable, "x.jpg", "y.jpg")

    html = client.get("/browse?group=stack").text

    assert 'data-name="x.jpg"' in html and 'data-name="y.jpg"' in html
    assert 'data-name="a.jpg"' in html, "the rest of the library left the view"
    assert "Not in a stack" in html


def test_the_count_agrees_with_the_grid_when_stacks_are_opened(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """One place decides what a listing holds. The header count, the grid and
    the *has this left the view* check had three chances to disagree."""
    burst(app_env, writable, "x.jpg", "y.jpg")

    folded = client.get("/browse?stacks=guesses").text
    assert "1 files" in folded, "the count does not match the one cell"
    opened = client.get("/browse?stacks=guesses&group=stack").text
    assert "2 files" in opened, "the count does not match the opened stack"


def test_a_guess_is_only_for_the_person_who_can_answer_it(
    client: TestClient, writable: Path, app_env: dict[str, Path],
    sign_in: "Callable[[str, str], TestClient]",
    add_user: "Callable[..., None]"
) -> None:
    """A household member may ask for the app's guesses — they can refuse
    one now, and *Not a stack* cannot be reached unless suggestions fold — so
    the filter is on their bar."""
    burst(app_env, writable, "x.jpg", "y.jpg")
    add_user("kid", "pw")
    client.post("/api/decide/bulk", json={
        "add_audience": ["kid"],
        "files": [{"folder": "init_2026", "name": "x.jpg"},
                  {"folder": "init_2026", "name": "y.jpg"}]})

    html = sign_in("kid", "pw").get("/browse").text

    assert "stacks" in [c[0] for c in pix(html)["CHIPS"]]
    # And the guess is on screen as a stack, which is the only way there is
    # ever a suggestion in front of them to refuse.
    assert 'data-name="x.jpg"' in html
    assert 'data-name="y.jpg"' not in html


def test_a_decision_on_a_folded_guess_reaches_what_it_hides(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The whole point of folding is to work as though there is one file, so a
    decision made about what is on screen is a decision about all of them —
    exactly the rule a stack somebody made already follows."""
    burst(app_env, writable, "x.jpg", "y.jpg")

    client.post("/api/decide/bulk", json={
        "event": "Sports Day",
        "files": [{"folder": "init_2026", "name": "x.jpg"}]})

    assert decisions.read(writable / "y.jpg") == Decision(event="Sports Day")


def test_a_decision_with_the_guessing_off_reaches_only_what_was_picked(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Nothing is hiding behind it there, so there is nothing to follow."""
    burst(app_env, writable, "x.jpg", "y.jpg")

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
    burst(app_env, writable, "x.jpg", "y.jpg")

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
    burst(app_env, writable, "x.jpg", "y.jpg")

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
    burst(app_env, writable, "x.jpg", "y.jpg")
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
    burst(app_env, writable, "x.jpg", "y.jpg")

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
    burst(app_env, writable, "x.jpg", "y.jpg")

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


# --- index not built ------------------------------------------------------

def test_a_missing_index_says_what_to_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sign_in: Callable[[str, str], TestClient]) -> None:
    """'503' is useless on its own; the fix is one command."""
    monkeypatch.setattr(webroots, "DB_PATH", tmp_path / "nope.db")
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", tmp_path / "users.json")

    r = sign_in(accounts.ADMIN, "admin").get("/")

    assert r.status_code == 503
    assert "pix2 index" in r.text


# --- video playback -------------------------------------------------------

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


# --- staleness ------------------------------------------------------------

def test_home_shows_when_the_index_was_built(client: TestClient) -> None:
    """Nothing watches the share, so age is the only signal an index is stale."""
    assert "indexed" in client.get("/").text


def test_age_is_rendered_in_words() -> None:
    import time as _t

    assert w_text.age(_t.time()) == "just now"
    assert w_text.age(_t.time() - 600) == "10m ago"
    assert w_text.age(_t.time() - 7200) == "2h ago"
    assert w_text.age(_t.time() - 86400 * 3) == "3d ago"


def test_an_index_without_a_timestamp_still_renders() -> None:
    """Older index files predate the built_at row; they must not 500."""
    assert w_text.age(None) == "at an unknown time"


# --- the grid drops what no longer belongs --------------------------------

def test_dating_a_file_drops_it_from_the_undated_view(
    client: TestClient, writable: Path
) -> None:
    """The regression this exists to prevent: adding a date while filtered to
    undated left the file sitting in a view it no longer belonged to."""
    (writable / "b.mp4").write_bytes(b"fake")
    r = client.post("/api/decide/bulk?date=(undated)", json={
        "date_override": "1987-*-*-*:*:*", "files": targets("b.mp4")})

    assert r.json()["written"] == 1
    assert r.json()["dropped"] == [{"folder": "init_2026", "name": "b.mp4"}]


def test_a_file_that_still_matches_is_not_dropped(client: TestClient,
                                                  writable: Path) -> None:
    r = client.post("/api/decide/bulk?event=Italy%20-%20Sicily", json={
        "add_audience": ["family"], "files": targets("a.jpg")})

    assert r.json()["dropped"] == []


def test_deciding_drops_a_file_from_the_new_view(client: TestClient,
                                                 writable: Path) -> None:
    """Culling with the New filter on: each decision should take the file out."""
    r = client.post("/api/decide/bulk?audience=new", json={
        "add_audience": ["kids"], "files": targets("a.jpg")})

    assert [d["name"] for d in r.json()["dropped"]] == ["a.jpg"]


def test_the_response_carries_the_new_total(client: TestClient,
                                            writable: Path) -> None:
    """So the header count stops claiming files the view no longer holds."""
    before = len(client.get("/api/files?audience=new").json())
    r = client.post("/api/decide/bulk?audience=new", json={
        "add_audience": ["kids"], "files": targets("a.jpg")})

    assert r.json()["total"] == before - 1


def test_a_failed_file_is_not_reported_as_dropped(client: TestClient,
                                                  writable: Path) -> None:
    """Dropped means "written, and it left" — a file that was never written has
    not moved anywhere."""
    r = client.post("/api/decide/bulk?audience=new", json={
        "add_audience": ["kids"], "files": targets("gone.jpg")})

    assert r.json()["written"] == 0
    assert r.json()["dropped"] == []


def test_an_empty_batch_reports_nothing_dropped(client: TestClient,
                                                writable: Path) -> None:
    r = client.post("/api/decide/bulk", json={"add_audience": ["private"], "files": []})

    assert r.json()["dropped"] == []


# --- layout and the viewer ------------------------------------------------

def test_nothing_stands_along_the_bottom(client: TestClient) -> None:
    """A version, a count and an index age do not change while you read them
    and nobody is waiting for any of them — so a fixed band of screen spent
    on saying so is a band of photographs not shown. They are under the
    account now, where you go when you want to know."""
    html = client.get("/browse").text

    assert "footbar" not in html
    for moved in ("v" + PIX_VERSION, "indexed "):
        assert moved in html, moved


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

    shaped(app_env, writable, "wide.jpg", 3840, 2160)
    wide = client.get("/browse").text
    assert 'data-ar="0.56' in wide, wide[wide.index("data-ar"):][:40]


def test_the_page_is_told_what_each_tier_holds(client: TestClient) -> None:
    """Arithmetic there rather than a second copy of these numbers here —
    they are `derive`'s to choose, and have already changed once."""
    html = client.get("/browse").text
    tiers = json.dumps(pix(html)["TIERS"])

    assert str(derive.THUMB_PX) in tiers and str(derive.LARGE_PX) in tiers
    assert str(derive.PREVIEW_PX) in tiers, tiers
    assert "/preview/" in tiers


def test_the_page_has_a_mark_of_its_own(client: TestClient) -> None:
    """A mark in the corner and a tab icon. Both are how you find this among
    twenty other tabs."""
    html = client.get("/browse").text

    assert 'rel="icon"' in html and "data:image/svg+xml" in html
    bar = corner(html)
    assert "<svg" in bar, "the brand is still text"
    assert ">pix2<" not in bar, "the word is still there beside the mark"
    assert "aria-label=" in bar, "a mark nothing can read out"


def test_the_corner_is_the_way_between_files_and_folders(
    client: TestClient
) -> None:
    """The same library at two zooms, and the corner is how you change which.

    It carries the filters across, because a zoom that dropped them would be
    a different library rather than the same one seen closer — and it says
    what it will do, since the mark under the pointer is a picture of the page
    rather than of the destination.

    **Not the grouping.** Each page reads best its own way, and opening a
    folder already leaves the folders' grouping behind for the grid's. The
    corner carrying the grid's *by day* back up lost a hand-built year ›
    month › event on every round trip.
    """
    grid = corner(client.get("/browse?event=Italy+-+Sicily&group=day").text)
    assert 'href="/?event=Italy+-+Sicily&amp;group=month%2Cevent"' in grid, grid
    assert "Show the folders" in grid, grid

    folders = corner(client.get(
        "/?event=Italy+-+Sicily&group=year,month,event").text)
    assert 'href="/browse?event=Italy+-+Sicily"' in folders, folders
    assert "Show the files" in folders, folders


def test_an_unfiltered_grid_zooms_out_to_the_whole_library(
    client: TestClient
) -> None:
    """Not to this year of it: an address with nothing in it is sent to the
    current year, so the corner names the default grouping outright."""
    grid = corner(client.get("/browse").text)
    assert 'href="/?group=month%2Cevent"' in grid, grid


def test_a_stack_is_not_carried_up_to_the_folders(client: TestClient) -> None:
    """A folder view of one stack is the stack, so there is nothing coarser to
    show — and a stack is a page of its own now, which has no corner to go up
    from at all. What is left to check is that the grid it came from still
    goes up with everything else about the view intact."""
    bar = corner(client.get("/browse?event=Italy+-+Sicily&group=day").text)

    assert "within" not in bar, bar
    assert 'href="/?event=Italy+-+Sicily&amp;group=month%2Cevent"' in bar, bar


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
    assert "<svg" in corner(html), "no mark beside it"


def test_what_is_about_you_lives_under_your_name(client: TestClient) -> None:
    """History, Accounts and the way out are things you do rarely. Spread
    along the bar they were three permanent controls competing with the
    filters, which are what the bar is for."""
    html = client.get("/browse").text
    bar = html[html.index('class="row"'):html.index("</div><main")]
    account = bar[bar.index('id="me"'):]

    menu = account[account.index('class="memenu"'):]
    for item in ("/history", "/accounts", "Sign out"):
        assert item in menu, f"{item} is not under the name"
    assert "admin" in account[:account.index('class="memenu"')], \
        "the name is hidden"


def test_what_is_waiting_is_an_icon_not_a_sentence(
    client: TestClient, writable: Path
) -> None:
    """*8 deleted* stood in the bar on every page whether or not it was news,
    and the next thing worth reporting would have been a second phrase beside
    it. The dot is the whole of what you see without asking, so it is the part
    that has to be right — and it rides on the one control the account, the
    size and what is waiting were folded into."""
    quiet = client.get("/browse").text
    bar = quiet[quiet.index('class="row"'):quiet.index("</div><main")]
    assert 'class="me"' in bar and 'class="dot"' in bar
    assert 'data-any=""' in bar, "a dot with nothing behind it"
    assert "Nothing waiting" in bar

    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "deleted": True})

    loud = client.get("/browse").text
    bar = loud[loud.index('class="row"'):loud.index("</div><main")]
    assert 'data-any="1"' in bar, "nothing says there is something"
    assert "1 deleted" in bar
    assert 'id="bincount"' in bar, "the page can no longer update it"


def test_the_menu_does_not_answer_its_own_question_twice(
    client: TestClient, writable: Path
) -> None:
    """*Nothing waiting* and *0 deleted*, one above the other. The link is
    rendered with `hidden` at zero, and every rule that gives it a `display`
    — and they all do, because it is a row of a menu — outranks the user
    agent's `[hidden]`. So it has to be said in a selector heavier than any of
    them, and said about the element it is actually about."""
    css = client.get("/browse").text
    assert has_rule(css, '.memenu .bin-link[hidden]', 'display:none')

    # The two halves of it, each shown only when it is the true one.
    quiet = client.get("/browse").text
    assert 'id="bincount" href="/browse?deleted=only" hidden>' in quiet
    assert "Nothing waiting" in quiet

    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "deleted": True})
    loud = client.get("/browse").text
    assert 'id="bincount" href="/browse?deleted=only">1 deleted' in loud
    # The sentence saying there is nothing goes the other way, by a rule on
    # the control rather than by not being rendered — the page flips the dot
    # without a reload and the two have to move together.
    assert 'data-any="1"' in loud
    assert '.me[data-any="1"] .memenu .quiet { display:none; }' in loud


def test_the_dot_follows_a_delete_without_a_reload(client: TestClient) -> None:
    """The count is rendered with the page, so the write that changes it has
    to say so — and the dot is what anybody actually sees.

    Checking the *id*, not that a line of code exists. It looked one up called
    `activity`, which is the name of a variable in the header builder and the
    id of nothing at all: the lookup returned null on every page, the dot kept
    whatever it was rendered with, and the assertion here — that a line
    mentioning it was present — went on passing for as long as the line was.
    """
    js = w_pages.BROWSE_JS
    at = js.index("function drawBin(")
    body = js[at:js.index("\n}", at)]
    found = re.search(r"getElementById\('([a-z]+)'\)", body)

    assert found, "nothing carries the dot"
    html = client.get("/browse").text
    assert f'id="{found.group(1)}"' in html, \
        f"drawBin lights up #{found.group(1)}, which the page does not render"
    assert f'{found.group(1)}.dataset.any' in body


def test_the_name_menu_needs_no_script(client: TestClient) -> None:
    """It has to work on /history and /accounts, which carry no page script at
    all — and a Sign out that only worked where the grid was loaded would be
    missing from the page you are most likely to be stuck on.

    Which is why it is a `<details>`: the trigger is a real button with a real
    expanded state, and opening it is the browser's job rather than a
    handler's."""
    for url in ("/history", "/accounts"):
        html = client.get(url).text
        assert "<details class=\"me\"" in html, url
        assert 'class="memenu"' in html and "Sign out" in html, url

    css = client.get("/browse").text
    assert has_rule(css, '.me[open] > .memenu', 'display:flex')
    # And never on hover. A menu that opens by being walked past has no closed
    # state to return to on a touchscreen, where the first tap is the hover
    # and the second lands on whatever the panel has just put under the finger.
    assert ".me:hover .memenu" not in css
    assert ".me:focus-within .memenu" not in css


def test_the_menu_can_be_dismissed_without_going_back_to_the_trigger(
    client: TestClient
) -> None:
    """A menu goes away when you have finished with it, and the ways people
    finish with one are Escape, a click elsewhere, and tabbing off the end.
    None of the three is what a `<details>` does on its own.

    In the shell, so it is on /history, /accounts and the login screen too —
    the same reason the menu carries no page script of its own."""
    js = w_shell.MENU_JS

    assert "'Escape'" in js and "me.open = false" in js
    assert "!me.contains(e.target)" in js, "a click elsewhere leaves it open"
    # And on the way down. Every filter chip stops a click propagating, so a
    # dismissal listening on the bubble never hears one — which left this
    # menu standing open underneath the filter menu that had just opened over
    # it.
    at = js.index("document.addEventListener('click'")
    assert "}, true);" in js[at:at + 200], js[at:at + 200]
    assert "focusout" in js and "relatedTarget" in js
    # Escape puts the keyboard back where it came from, or the next Tab starts
    # from the top of the document.
    assert "s.focus()" in js

    # Every menu of that shape, the account's and Display's alike.
    for url in ("/browse", "/history", "/accounts", "/"):
        assert "querySelectorAll('details.me')" in client.get(url).text, url


def test_the_menu_comes_out_of_the_control_that_opened_it(
    client: TestClient
) -> None:
    """A panel that is simply present on the next frame reads as the page
    having changed. One that arrives from under its trigger reads as that
    trigger having opened, which is the difference between a menu and a
    second page — and it is twelve hundredths of a second.

    Never for somebody who has asked not to be moved: here that setting is
    about vestibular symptoms rather than taste."""
    css = client.get("/browse").text
    at = css.index("@media (prefers-reduced-motion: no-preference)")
    block = css[at:at + 260]

    assert has_rule(block, '.me[open] > .memenu', 'animation:menuopen'), block
    assert "@keyframes menuopen" in block, block


def test_the_menu_says_which_kind_of_account_you_are_signed_in_as(
    client: TestClient
) -> None:
    """The trigger says a name on a desk and a gear on a phone, and neither
    says which *kind* of account it is. This app is used as two different
    people, and acting as the wrong one is invisible until something has been
    shared with the wrong household."""
    html = client.get("/browse").text
    menu = html[html.index('class="memenu"'):]

    assert '<span class="role">Administrator</span>' in menu


def test_the_name_is_clipped_on_a_phone_rather_than_dropped(
    client: TestClient
) -> None:
    """It is the accessible name of the control. Hidden with `display:none`
    the trigger is a gear whose only label is a drawing, which announces
    itself as a button called nothing — at the one width where checking who
    you are signed in as is hardest."""
    coarse = media_block(w_shell.STYLE, "(max-width: 720px)")
    at = coarse.index(".me .name {")
    rule = coarse[at:coarse.index("}", at)]

    assert "clip-path:inset(50%)" in rule, rule
    assert "display:none" not in rule, rule
    # And the gear itself carries no label of its own to compete with it.
    assert 'class="gearbtn" aria-hidden="true"' in client.get("/browse").text


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
    assert has_rule(css, '.topbar > .row:first-child', 'flex-wrap:nowrap')
    # On the chips themselves: a flex item will not shrink below its own
    # content without it, so without it they push the row wide instead of
    # wrapping. Other rules carry the same property for other reasons.
    at = css.index(".topbar > .row:first-child > .chips")
    assert "min-width:0" in css[at:at + 120], css[at:at + 120]


def test_a_filter_keeps_its_place_whether_or_not_it_is_set() -> None:
    """One order, always.

    The bar was drawn in two passes — the filters in use, then the rest — so
    setting one made its glyph jump from ninth place to first and clearing it
    threw the glyph back. Nothing could be reached from memory: the camera was
    wherever the camera happened to be that second, and the place you reached
    for belonged to whatever had last been switched on.

    Lit against dim is what tells them apart; position is what finds them.
    """
    js = w_pages.BROWSE_JS
    body = js[js.index("function drawChips()"):js.index("function filterMenu")]

    # One loop over every filter, rather than one over the set and one over
    # the rest.
    assert body.count("of CHIPS)") == 1, body
    assert "if(!v) continue;" not in body, "the bar skips unset filters again"


def test_the_ones_doing_nothing_fold_away_on_a_phone() -> None:
    """Ten glyphs fit across a desktop bar and do not fit across a phone. They
    are hidden rather than moved, so the ones that remain are still where they
    were — which is the whole point of the rule above."""
    js = w_pages.BROWSE_JS
    body = js[js.index("function drawChips()"):js.index("function filterMenu")]

    assert "'chip off spare'" in body
    assert "addchip" in js and "filterMenu" in js
    narrow = media_block(w_shell.STYLE, "(max-width: 720px)")
    assert has_rule(narrow, '.chips .spare', 'display:none')


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


# --- the script must be able to see the page ------------------------------

def test_the_page_script_comes_last(client: TestClient) -> None:
    """The bug this exists to prevent: the count and the message line moved
    into the footer, below the script that looks them up, so both were `null`.
    Every write began by setting a message, so every write threw before it sent
    anything — and said nothing, because saying things was the broken part."""
    html = client.get("/browse").text

    assert html.index('id="note"') < html.index('"VIEW": ')
    assert html.index('id="count"') < html.index('"VIEW": ')
    assert html.index('id="grid"') < html.index('"VIEW": ')


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


# --- what a thumbnail shows -----------------------------------------------

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

    # Two lanes, each held at its right-hand end by what is always there.
    assert has_rule(css, '.ov.top', 'top:4px; align-items:flex-start')
    assert has_rule(css, '.ov.bot', 'bottom:4px; align-items:flex-end')
    assert has_rule(css, '.ov .fix', 'flex:none')
    # And each wraps from its own edge inward.
    assert has_rule(css, '.ov.bot .lane', 'flex-wrap:wrap-reverse')


def test_an_edited_cell_looks_like_a_fetched_one(client: TestClient) -> None:
    """The client repaints chips in the same shape the server renders, or a
    photo you just tagged would look different from one you reloaded."""
    js = client.get("/browse").text

    assert "el.innerHTML=list.map(v=>`<i title=" in js


# --- the grid reports deviation, not the norm -----------------------------

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


# --- who is in the photograph ---------------------------------------------

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


def test_a_thumbnail_says_who_is_in_it(
    client: TestClient, writable: Path
) -> None:
    """It was the one fact a cell carried and never showed. The name was in
    `data-people` for the script, on the folder card, in the viewer rail, in
    its own filter and its own bulk action — everywhere except the thing you
    are looking at while you decide."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_people": ["Mom", "Dad"]})

    html = client.get("/browse").text
    cell = cell_html(html, "a.jpg")

    folk = cell[cell.index('class="folk"'):]
    assert "<i title=\"Dad\">Dad</i>" in folk, folk[:200]
    assert "<i title=\"Mom\">Mom</i>" in folk, folk[:200]


def test_who_is_in_it_is_not_who_can_see_it(
    client: TestClient, writable: Path
) -> None:
    """Two facts about people in one corner, one line each. They are opposite
    questions that happen to take the same kind of word — *pictures of Mum*
    and *pictures Mum may see* — and on a 150px tile the colour must not be
    asked to carry the whole difference."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg",
        "add_people": ["Mom"], "add_audience": ["bob"]})

    html = client.get("/browse").text
    cell = cell_html(html, "a.jpg")

    # Two kinds of chip, each its own run, not one run in two colours.
    assert 'class="folk"' in cell and 'class="who"' in cell
    assert "Mom" in cell[cell.index('class="folk"'):cell.index('class="who"')]

    css = html[html.index("<style>"):html.index("</style>")]
    # Both in the bottom lane by default, the people first.
    bottom = cell[cell.index('class="ov bot"'):]
    assert bottom.index('class="folk"') < bottom.index('class="who"')
    assert has_rule(css, '.ov .who, .ov .tags, .ov .folk', 'display:contents')
    # And in the blue people wear — the same value a folder card gives them,
    # because one kind of thing is one colour on every surface.
    assert has_rule(css, '.folk i', 'color:#a6c8ff')
    assert has_rule(css, '.spread i.people', 'color:#a6c8ff')


def test_a_thumbnail_leaves_off_the_person_the_view_is_already_about(
    client: TestClient, writable: Path
) -> None:
    """Filtered to `person:Mom` every thumbnail on screen says Mom, and a chip
    that is true of everything is furniture. The same rule access applies to
    the usual audience and a sub-event applies to an event the heading already
    names."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_people": ["Mom", "Dad"]})

    narrowed = client.get("/browse?person=Mom").text
    cell = cell_html(narrowed, "a.jpg")
    folk = cell[cell.index('class="folk"'):]

    assert "Dad" in folk, "the name that is still news is gone too"
    assert ">Mom<" not in folk, folk[:200]
    # Still on the cell, because the page reads it off there.
    assert "Mom" in cell[:cell.index('class="folk"')]


def test_a_person_is_painted_into_their_own_chips_not_the_access_ones(
    client: TestClient
) -> None:
    """The page paints what it has just written straight onto the cell, and
    the rule that said which chips was a ternary reading *tags, or else
    access* — so putting Mum in a photograph wrote her name into the chips
    saying who can see it, and took away the mark saying nobody could.

    Three fields is one too many for *or else*."""
    js = w_pages.BROWSE_JS

    assert "const PAINTS={tags:'tags', people:'folk', audience:'who'};" in js
    assert "field==='tags'?'tags':'who'" not in js, "the ternary is back"

    # And the page keeps the server's own silences, or a cell edited here and
    # a cell fetched fresh could look different.
    at = js.index("function repaint(")
    body = js[at:js.index("\n}", at)]
    assert "v!==USUAL" in body and "v!==VIEW.person" in body, body


def test_a_half_written_people_change_is_put_back(
    client: TestClient
) -> None:
    """A write that stops part way — cancelled, or a share that dropped — has
    really written the first part, so the cells it never reached go back to
    what they said. `people` was missing from the list of what to remember,
    so those cells were left claiming the name."""
    js = w_pages.BROWSE_JS
    at = js.index("const before=todo.map(")
    snapshot = js[at:js.index("}));", at)]

    assert "people:c.dataset.people" in snapshot, snapshot
    assert "c.dataset.people=was.people" in js
    assert "repaint(c,'people')" in js


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
    assert ("person", "People") in w_vocab.CHIPS
    assert w_marks.ACT_MARKS["people"] == "person"
    assert w_marks.ACT_MARKS["access"] == "audience"
    assert w_marks.mark("person") != w_marks.mark("audience")


# --- getting back ---------------------------------------------------------

def test_every_page_carries_a_way_back(client: TestClient) -> None:
    """Installed on a phone the app is the whole window — no address bar, no
    back. Every filter, every grouping and every folder opened is a
    navigation, so the history was right there and nothing could reach it.

    On the pages with no script of their own as well: `/accounts` is exactly
    where somebody gets stranded.
    """
    for path in ("/browse", "/?date=2026&group=year", "/accounts", "/history"):
        html = client.get(path).text
        assert 'id="back"' in html, path


def test_the_way_back_is_part_of_the_app_everywhere(client: TestClient) -> None:
    """It was gated to the installed app, on the grounds that a second back
    button beside the browser's own is clutter. The answer is that this is an
    app on a desktop too, and an app is self-contained: the way out of where
    you are belongs in the same place every time, not outside the window on
    one platform and inside it on another."""
    css = w_shell.STYLE

    assert has_rule(css, '.back', 'display:inline-flex')
    assert "html[data-inapp]" not in css


def test_the_way_back_is_as_tall_as_what_stands_beside_it(
    client: TestClient
) -> None:
    """It holds a 19px drawing where every chip beside it holds a 21px line of
    text, so the same padding made it two pixels shorter — and the top row
    aligns to the top, which puts that difference at the bottom edge where it
    reads as a smaller button rather than a centred one. `--ctl` is the number
    they have to agree on."""
    css = w_shell.STYLE

    rule = css[css.index(".back {"):css.index("}", css.index(".back {"))]
    assert "min-height:var(--ctl)" in rule, rule
    # Two pixels taller than the drawing, so the drawing has to be told where
    # to sit in what is left.
    assert "justify-content:center" in rule, rule


def test_a_way_back_that_goes_nowhere_says_so(client: TestClient) -> None:
    """A fresh launch has nothing behind it, and a button that does nothing
    teaches you to stop believing the rest of them."""
    html = client.get("/browse").text

    assert "history.length <= 1" in html and "back.disabled = true" in html
    assert "history.back()" in html


def test_a_card_says_it_is_a_slice_of_something_longer(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """An event running from February into March is a card under each month.
    The count said *1 of 2 files* and the dates said only this card's, so one
    split was told twice over and the other not at all.

    The same `of` the count uses, so the two read as one sentence about one
    thing rather than two facts that happen to be adjacent.
    """
    share = app_env["share"]
    for name, when in (("m1.jpg", "2026:02:26 10:00:00"),
                       ("m2.jpg", "2026:03:03 10:00:00")):
        (writable / name).write_bytes(b"fake")
        (share / "meta" / "init_2026" / f"{name}.json").write_text(json.dumps({
            "file": name, "folder": "init_2026", "size": 30, "mtime_ns": 1,
            "exif": {"EXIF:DateTimeOriginal": when},
        }), encoding="utf-8")
    ix.build(app_env["db"], meta_dir=share / "meta",
             master_dir=share / "master")
    client.post("/api/decide/bulk", json={
        "event": "Sicily",
        "files": [{"folder": "init_2026", "name": "m1.jpg"},
                  {"folder": "init_2026", "name": "m2.jpg"}]})

    html = client.get("/?date=2026&group=month,event&stacks=firm").text
    card = next(c for c in re.findall(r'<a class="tile".*?</a>', html, re.S)
                if ">Sicily<" in c)

    assert 'class="when split"' in card, card
    assert "26 Feb" in card and "3 Mar" in card, card
    # And the count still says its half of the same thing.
    assert "<i>of</i> 2 files" in card, card


def test_a_card_that_is_all_of_its_group_says_nothing_of_the_sort(
    client: TestClient
) -> None:
    """Most cards are whole, and a card that announced it was not split would
    be a card describing the grouping rather than the library."""
    html = client.get("/?group=event").text
    card = next(iter(re.findall(r'<a class="tile".*?</a>', html, re.S)), "")

    assert 'class="when split"' not in card, card


def test_nothing_draws_a_progress_bar_any_more() -> None:
    """It said how much of a group a card was, which the count says in words,
    and how much of it was done, which the chip beside it says by being there.
    A third telling of two things nobody asked twice about."""
    assert ".tile .bar" not in w_shell.STYLE
    assert not hasattr(web, "_bar")


def test_a_heading_is_words_rather_than_a_toolbar() -> None:
    """Three of the things in a grouping heading are buttons, so giving
    buttons generally a border, a gradient and a hairline of light along the
    top gave the heading a line above its own text and a box around its plus
    sign.

    `box-shadow:none` is the part that is easy to forget: clearing the
    background and the border looks like it was enough, and the highlight is
    the one that draws on a transparent element.
    """
    for control in (".rmgrp {", ".grpname {", ".addgrp {"):
        # At the start of a line, or this finds the narrower rule that only
        # recolours one of them inside a shelf heading.
        found = re.search(r"^" + re.escape(control) + r"[^}]*}",
                          w_shell.STYLE, re.M)
        assert found is not None, control
        assert "box-shadow:none" in found.group(0), control
        assert "background:none" in found.group(0), control


def test_a_pill_says_what_kind_of_thing_it_names(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """A chip says `family`, and its colour says which kind of `family` that
    is — which works once the colours are learnt and not before.

    So the tooltip carries the kind and the count, not the value: the value is
    the word already under the pointer, and the click is already offered by
    the cursor and the hover.
    """
    burst(app_env, writable, "w.jpg", "x.jpg")
    every = client.get("/api/files?date=2026&stacks=firm").json()
    client.post("/api/decide/bulk", json={
        "add_audience": ["family"], "add_people": ["Mom"],
        "add_tags": ["beach"],
        "files": [{"folder": r["folder"], "name": r["name"]} for r in every]})

    card = card_of(client.get("/?date=2026&group=year&stacks=firm").text)

    assert 'title="Access — 3 files"' in card, card
    assert 'title="People — 3 files"' in card, card
    assert 'title="Tag — 3 files"' in card, card
    # Never the value, which is the word being pointed at.
    assert 'title="family' not in card and 'title="Mom' not in card, card


def test_what_is_left_says_what_being_left_means(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """*Undecided — 7 files* would spend the tooltip repeating the word under
    the pointer. What it can add is what the word means."""
    burst(app_env, writable, "w.jpg")

    card = card_of(client.get("/?date=2026&group=year&stacks=firm").text)

    assert 'title="No access yet' in card, card
    assert ">undecided</i>" in card, card


def test_one_file_is_not_one_files(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    burst(app_env, writable, "w.jpg")
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "w.jpg", "add_tags": ["beach"]})

    card = card_of(client.get("/?date=2026&group=year&stacks=firm").text)

    assert 'title="Tag — 1 file"' in card, card


def test_a_shelf_heading_leads_with_the_only_part_that_names_it() -> None:
    """*September 2026 › By event + 2* — and only the first of those says
    which shelf this is. The rest is the cut it was made by, a control for
    making another, and a count: true, and not what you are scanning a page
    of headings for.

    Faded rather than removed, so the row does not change width when they
    arrive — a heading that reflows under the pointer is one you cannot aim
    at.
    """
    block = media_block(w_shell.STYLE, "(hover: hover)")

    assert "h3.group.shelf .crumbsep" in block
    assert "opacity:0; pointer-events:none" in block
    # And back on a hover or a tab into it.
    assert "h3.group.shelf:hover .addgrp" in block
    assert "h3.group.shelf:focus-within .addgrp" in block


def test_the_grid_keeps_the_crumb_that_names_its_section() -> None:
    """A grid heading ends in a *value* — the name of the section under it —
    where a shelf heading ends in the name of the cut. Hiding the last crumb
    on both would hide what half of them are for."""
    block = media_block(w_shell.STYLE, "(hover: hover)")

    assert ".shelf" in block
    for line in block.splitlines():
        if "crumb:last-child" in line:
            assert "shelf" in line, line


def test_only_one_crumb_means_it_is_the_one_that_names_it() -> None:
    """Grouped one level deep the cut's name is the whole heading, and fading
    it leaves a blank row."""
    block = media_block(w_shell.STYLE, "(hover: hover)")

    assert ".crumb:last-child:not(:first-child)" in block


def test_nothing_hides_where_there_is_no_way_to_ask_for_it(
    client: TestClient
) -> None:
    """A control you cannot reveal is a control you do not have, so on a phone
    all of it stays on screen."""
    hover = media_block(w_shell.STYLE, "(hover: none), (any-pointer: coarse)")

    assert "addgrp { opacity:0" not in hover
    # What `(hover: hover)` fades out is put back, for a touchscreen that
    # also says it can hover.
    assert "h3.group.shelf .crumbsep" in hover
    assert "opacity:1; pointer-events:auto" in hover


def test_a_touchscreen_that_claims_hover_reveals_nothing_on_hover() -> None:
    """An iPad can report hover, and Safari takes a tap that reveals something
    as the hover alone — the folder card did not open. So the rules that keep
    controls on screen ask about any coarse pointer, not just about hover."""
    hover = media_block(w_shell.STYLE, "(hover: none), (any-pointer: coarse)")

    assert has_rule(hover, '.pick', 'opacity:.55')


# --- Display --------------------------------------------------------------

def test_display_offers_every_optional_fact_and_nothing_required(
    client: TestClient
) -> None:
    """Size first, then each fact a thumbnail can carry with its places. What
    cannot be turned off — the stack badge, the length, the circle — is not
    offered."""
    html = client.get("/browse").text
    menu = html[html.index('class="me disp"'):html.index('id="me"')]

    assert menu.index('data-size="small"') < menu.index('data-info=')
    for key in ("people", "access", "tags", "subevent"):
        for at in ("off", "top", "bot"):
            assert f'data-info="{key}" data-at="{at}"' in menu, (key, at)
    # A fact with a place of its own is only on or off.
    assert 'data-info="clip" data-at="on"' in menu
    assert 'data-info="clip" data-at="top"' not in menu
    for fixed in ("stack", "duration", "badge"):
        assert f'data-info="{fixed}"' not in menu


def test_a_household_member_is_not_offered_access(
    household: dict[str, object]
) -> None:
    kid = cast(TestClient, household["kid"])
    assert 'data-info="access"' not in kid.get("/browse").text


def test_a_fact_turned_off_is_hidden_before_the_page_paints(
    client: TestClient
) -> None:
    """The choice is put on `<html>` from the head, so the stylesheet hides a
    fact before it is ever drawn — on a thumbnail and on a folder card both."""
    html = client.get("/browse").text
    head = html[:html.index("</head>")]

    assert "pix2.info" in head and "data-info-" in head
    css = html[html.index("<style>"):html.index("</style>")]
    assert 'html[data-info-people="off"] .ov .folk' in css
    assert 'html[data-info-people="off"] .spread i.people' in css
    assert 'html[data-info-access="off"] .spread i.none' in css
    assert 'html[data-info-tags="off"] .spread i.tags' in css


def test_a_cell_is_drawn_in_the_default_lanes(
    client: TestClient, writable: Path
) -> None:
    """Tags along the top, people and access and the sub-event along the
    bottom — the arrangement before there was a choice — with the circle at
    the top-right and nothing of the photograph's own facts beside it."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg",
        "add_people": ["Mom"], "add_tags": ["beach"]})
    cell = cell_html(client.get("/browse").text, "a.jpg")
    top = cell[cell.index('class="ov top"'):cell.index('class="ov bot"')]
    bot = cell[cell.index('class="ov bot"'):]

    assert 'class="tags"' in top and 'class="folk"' in bot
    assert top.index('class="fix"') < top.index('class="pick"')


# --- the grid a page at a time --------------------------------------------

def test_the_grid_comes_a_page_at_a_time(
    client: TestClient, writable: Path, app_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first page with the grid, the rest from `/api/page` as it is
    scrolled towards; a heading counts its whole section either way."""
    monkeypatch.setattr(w_pages, "FIRST_PAGE", 1)
    html = client.get("/browse?group=none").text
    assert 'data-total="2" data-served="1"' in html
    assert 'id="more"' in html
    assert '<span class="dim">2</span>' in html, "the heading counts the page"

    nxt = client.get("/api/page?group=none&offset=1").json()
    assert nxt["served"] == 2 and nxt["total"] == 2
    assert nxt["html"].count('data-name=') == 1
    # The same section, so the page can join it to the one on screen.
    first = html[html.index('<section class="sect" data-key='):]
    key = first[:first.index(">")]
    assert key in nxt["html"]

    done = client.get("/api/page?group=none&offset=2").json()
    assert done["html"] == "" and done["served"] == 2


def test_a_page_is_the_filtered_view(client: TestClient, writable: Path) -> None:
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_tags": ["beach"]})
    got = client.get("/api/page?group=none&tag=beach&offset=0").json()
    assert got["total"] == 1 and 'data-name="a.jpg"' in got["html"]


def test_the_row_the_dot_is_about_carries_the_dot(client: TestClient) -> None:
    """A red mark on the name with nothing in the menu pointing back at it
    left you looking for what it meant."""
    assert ".memenu .bin-link::before" in w_shell.STYLE
    assert "background:var(--gone)" in w_shell.STYLE[
        w_shell.STYLE.index(".memenu .bin-link::before"):][:200]
