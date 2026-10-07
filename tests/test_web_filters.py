"""Filters and grouping: what each filter offers, how several values combine, partial dates, events and their parts, and the sections a grouping cuts the grid into.

Split out of the one web test module, by area.
"""

from __future__ import annotations

import pytest
import re
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient
from web_helpers import (
    event_named,
    evented,
    has_rule,
    media_block,
    quote,
    stamp_shape,
    three_files,
)

from pix.nas import decisions, history, index as ix, web
from pix.nas.webapp import (
    grid as w_grid,
    marks as w_marks,
    pages as w_pages,
    shell as w_shell,
    vocab as w_vocab,
)


# --- filtering ------------------------------------------------------------

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


# --- suggestions ----------------------------------------------------------

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


# --- partial dates --------------------------------------------------------

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


# --- grouping -------------------------------------------------------------

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


def test_one_column_is_not_grouped_on_twice(client: TestClient) -> None:
    """Event and sub-event read one field at two widths. Either inside the
    other cuts by a question the outer level has already answered — *Sicily*
    holding *Sicily › Taormina* is a heading and no new information, and the
    other way round is a group of one every time. The menu stops offering it;
    this is the same rule where the state actually lives, because the grouping
    comes out of a URL that people type and edit by hand."""
    assert w_vocab.groupings("event,subevent") == ["event"]
    assert w_vocab.groupings("subevent,event") == ["subevent"]
    # The outer one wins, and what asks something else is untouched.
    assert w_vocab.groupings("event,day,subevent") == ["event", "day"]
    # Day, month and year read one column too and are deliberately *not* in
    # this: a month inside a year is a real division, and it is the reason the
    # grouping is a list at all.
    assert w_vocab.groupings("year,month,day") == ["year", "month", "day"]


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


# --- sub-grouping ---------------------------------------------------------

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
    css = w_shell.STYLE
    # The whole rule, not the first hundred-odd characters of it: a property
    # is still declared when somebody adds one above it.
    rule = css[css.index(".addgrp {"):]
    rule = rule[:rule.index("}")]

    assert "visibility:hidden" not in rule, rule
    assert "opacity:.3" in rule, rule


def test_three_levels_is_the_limit(client: TestClient) -> None:
    """Past three the headings outnumber the photographs."""
    html = client.get("/browse?group=event,year,month,day").text

    assert 'data-level="2"' in html
    assert 'data-level="3"' not in html
    assert 'class="addgrp"' not in html


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
    stamp_shape(app_env["db"], ix.SCHEMA_VERSION + 1)

    r = client.get("/browse", follow_redirects=False)

    assert r.status_code == 503, r.status_code
    assert "Out of step" in r.text
    assert "needs updating" in r.text, "told the wrong side to move"
    assert "pix index" not in r.text, "rebuilding cannot fix a newer index"


def test_an_index_older_than_the_app_asks_for_a_rebuild(
    client: TestClient, app_env: dict[str, Path]
) -> None:
    """The opposite direction, and the opposite fix. Guessing wrong here is
    what costs the afternoon."""
    stamp_shape(app_env["db"], 1)

    r = client.get("/browse", follow_redirects=False)

    assert r.status_code == 503
    assert "pix index" in r.text
    assert "needs updating" not in r.text


def test_health_reports_the_disagreement_without_calling_itself_dead(
    client: TestClient, app_env: dict[str, Path]
) -> None:
    """`ok` stays true: the app is running and answering. Reporting it as dead
    would have Container Manager restart a container that works perfectly, and
    the restart would not fix it."""
    stamp_shape(app_env["db"], ix.SCHEMA_VERSION + 1)

    body = client.get("/healthz").json()

    assert body["ok"] is True
    assert body["index"] is False
    assert "newer pix" in body["says"]


def test_health_is_plain_when_the_two_agree(client: TestClient) -> None:
    body = client.get("/healthz").json()

    assert body == {"ok": True, "index": True}


# --- the filters are drawn, not spelled out -------------------------------

def test_every_filter_has_a_drawing() -> None:
    """The bar says which question a chip asks by drawing it, so a filter
    added to `_CHIPS` without a mark is a button with nothing in it. The page
    falls back to the name rather than rendering an empty control — this is
    what stops that fallback from being the thing anybody actually sees."""
    missing = [col for col, _ in w_vocab.CHIPS if col not in w_marks.MARKS]

    assert not missing, f"no drawing for: {', '.join(missing)}"


def test_no_two_filters_are_drawn_the_same() -> None:
    """They are told apart at seventeen pixels and only by their shape."""
    marks = [w_marks.MARKS[col] for col, _ in w_vocab.CHIPS]

    assert len(set(marks)) == len(marks)


def test_a_drawing_takes_the_colour_of_whatever_it_is_in() -> None:
    """`currentColor` throughout, so a chip that is doing something and one
    that is not are the same drawing and not two of them — and the dim state,
    the hover and the accent all come free."""
    svg = w_marks.mark("event")

    assert 'stroke="currentColor"' in svg and "fill=\"none\"" in svg
    # The same grid and weight as the bell in the bar beside them, which is
    # what makes the set read as one family.
    assert 'viewBox="0 0 24 24"' in svg and 'stroke-width="1.7"' in svg


def test_an_unknown_name_draws_nothing_rather_than_a_broken_shape() -> None:
    assert w_marks.mark("nonesuch") == ""


def test_the_page_carries_no_instructions(client: TestClient) -> None:
    """A standing sentence about clicking and holding is read once and then
    occupies a fixed strip at the bottom of every screen for as long as the
    app exists — which on a phone was three lines of it."""
    html = client.get("/browse").text

    assert "circle to select" not in html
    # The one thing that was down there and had to stay: how every write says
    # whether it happened.
    assert 'id="note"' in html


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
    narrow = media_block(w_shell.STYLE, "(max-width: 720px)")

    assert has_rule(narrow, '#viewer:not(.norail) .stage', 'display:none')
    # And the rail takes the whole of it rather than a slice.
    assert "max-height:none" in narrow


def test_the_unused_filters_fold_away_where_they_do_not_fit() -> None:
    """The bar is one line, and what is on it is what fits — measured, not
    guessed from the width of the screen, since two filters fit across a
    phone and six long ones do not fit across a laptop. Every control is
    always rendered; the script names the fit and the stylesheet picks."""
    css = w_shell.STYLE
    js = w_pages.BROWSE_JS

    assert "const FITS=['all','spare','icons'];" in js
    assert "chips.dataset.fit=f" in js and "oneLine(chips)" in js
    # Everything at the widest; the `+` and the chevron are only for the
    # fits that need them.
    assert has_rule(css, '.chips .addchip, .chips .more', 'display:none')
    assert has_rule(css, '.chips[data-fit="spare"] .spare, '
                         '.chips[data-fit="icons"] .spare', 'display:none')
    assert has_rule(css, '.chips[data-fit="icons"] .more',
                    'display:inline-flex')
    # At the narrowest the values fold away until the chevron opens them.
    assert has_rule(css, '.chips[data-fit="icons"]:not(.open) .chip.on .val, '
                         '.chips[data-fit="icons"]:not(.open) .chip.on .x',
                    'display:none')
    # Not a breakpoint any more: a width rule would fight the measurement.
    narrow = media_block(css, "(max-width: 720px)")
    assert ".chips .spare" not in narrow and ".chips .addchip" not in narrow


def test_the_two_bars_wear_the_same_drawings() -> None:
    """*Event* the filter and *Event* the action are one question asked twice
    — once about what you are looking at, once about what it should become.
    The bars already ask them in the same order; a control that changes its
    face between them is a control you have to learn twice."""
    assert w_marks.ACT_MARKS["event"] == "event"
    assert w_marks.ACT_MARKS["tags"] == "tag"
    assert w_marks.ACT_MARKS["access"] == "audience"
    assert w_marks.ACT_MARKS["stack"] == "stacks"
    assert w_marks.ACT_MARKS["delete"] == "deleted"
    # Which is the same pairing the script uses to decide what a menu writes,
    # and the two must not disagree about what an action is about.
    for act, col in (("tags", "tag"), ("access", "audience"),
                     ("event", "event")):
        assert f"{act}:'{col}'" in w_pages.BROWSE_JS


def test_every_action_is_drawn() -> None:
    """Eleven buttons in a row and four of them illustrated is not a style,
    it is an unfinished edit."""
    html = w_grid.actions(web.Principal(name="admin", is_admin=True))
    acts = set(re.findall(r'data-act="(\w+)"', html))

    assert acts
    undrawn = [a for a in acts if not w_marks.mark(w_marks.ACT_MARKS.get(a, ""))]
    assert not undrawn, f"no drawing for: {', '.join(sorted(undrawn))}"


def test_no_two_actions_are_drawn_the_same() -> None:
    """Delete and Purge are the nearest pair — both the bin — and the cross
    inside one of them is the whole difference between recoverable and not."""
    marks = [w_marks.mark(name) for name in w_marks.ACT_MARKS.values()]

    assert len(set(marks)) == len(marks)


def test_an_action_keeps_its_word_where_the_script_can_find_it() -> None:
    """The takeover names an action by reading it off its own button rather
    than keeping a second vocabulary for the same four words. With a drawing
    in there too, the word has to be its own element — `textContent` on the
    button would take the drawing with it, and one control already sets it."""
    html = w_grid.actions(web.Principal(name="admin", is_admin=True))

    assert '<span class="word">Event&hellip;</span>' in html
    assert "[data-act=\"'+act+'\"] .word" in w_pages.BROWSE_JS


def test_an_action_is_its_drawing_alone_on_a_phone() -> None:
    """Eleven drawings and eleven words is two rows of bar on a screen with
    none to give, and the drawing is the half that survives being small."""
    narrow = media_block(w_shell.STYLE, "(max-width: 720px)")

    assert has_rule(narrow, '#actions .grp button .word', 'display:none')
    assert "min-width:44px" in narrow


def test_an_action_keeps_a_name_where_the_word_is_hidden() -> None:
    """A control whose only name is switched off by a media query has no name
    at all — not to a screen reader, and not to anyone hovering it on a
    desktop either. So the word is carried three times over."""
    html = w_grid.actions(web.Principal(name="admin", is_admin=True))

    assert 'title="Make top" aria-label="Make top"' in html
    # The ellipsis says *this one asks something next*, which is a fact about
    # the button rather than part of what it is called.
    assert 'title="Event" aria-label="Event"' in html
    assert '<span class="word">Event&hellip;</span>' in html


# --- an event, and one level inside it ------------------------------------

def test_an_event_filter_reaches_inside_itself(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The way a date filter answers a year with every day in it. `Sicily` is
    the trip; picking `Sicily > Taormina` narrows to the afternoon, and no
    second filter had to be invented for it."""
    evented(client, writable, app_env)

    trip = {r["name"] for r in
            client.get("/api/files?event=Sicily&stacks=firm").json()}
    inside = {r["name"] for r in client.get(
        "/api/files?event=Sicily%20%3E%20Taormina&stacks=firm").json()}

    assert trip == {"e1.jpg", "e2.jpg"}
    assert inside == {"e2.jpg"}


def test_grouping_by_event_puts_a_sub_event_under_it(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Which is what makes a sub-event a subdivision rather than a second
    event sitting beside the first."""
    evented(client, writable, app_env)
    conn = ix.open_ro(app_env["db"])
    try:
        by_event = {r["grp0"]: r["n"] for r in
                    ix.sections(conn, ix.Filters(apart=True),
                                groups=["event"])}
        by_sub = {r["grp0"]: r["n"] for r in
                  ix.sections(conn, ix.Filters(apart=True),
                              groups=["subevent"])}
    finally:
        conn.close()

    assert by_event["Sicily"] == 2
    assert by_sub["Sicily"] == 1 and by_sub["Sicily > Taormina"] == 1


def test_renaming_an_event_over_a_selection_keeps_each_sub_event(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """One request, a different answer per file — which is the whole reason
    the halves are written separately rather than composed by the caller."""
    evented(client, writable, app_env)

    client.post("/api/decide/bulk", json={
        "event_head": "Family Trip",
        "files": [{"folder": "init_2026", "name": "e1.jpg"},
                  {"folder": "init_2026", "name": "e2.jpg"}]})

    assert event_named(writable, "e1.jpg") == "Family Trip"
    assert event_named(writable, "e2.jpg") == "Family Trip > Taormina"


def test_reverting_a_rename_puts_each_name_back(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """A half-name write resolves differently per file, so the log records the
    name each one ended up with rather than the half that was asked for —
    reverting compares what it recorded against what the file says now, and a
    batch-wide *event_head* would match neither."""
    evented(client, writable, app_env)
    client.post("/api/decide/bulk", json={
        "event_head": "Family Trip",
        "files": [{"folder": "init_2026", "name": "e1.jpg"},
                  {"folder": "init_2026", "name": "e2.jpg"}]})

    client.post("/history/revert", data={"id": history.recent()[0].id})

    assert event_named(writable, "e1.jpg") == "Sicily"
    assert event_named(writable, "e2.jpg") == "Sicily > Taormina"


def test_an_event_can_be_asked_for_without_its_parts(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """*Sicily* is the trip and *Sicily, no sub-event* is the part of it
    nobody has divided up yet — two different sets of files, and the sub-event
    grouping makes a folder of each. Spelled as the event with `(none)` where
    a part would go: the separator and the empty-column sentinel, which are
    the two conventions the grouping itself is built out of."""
    evented(client, writable, app_env)
    exact = f"Sicily{decisions.EVENT_SEP}{ix.NO_EVENT}"

    trip = {r["name"] for r in
            client.get("/api/files?event=Sicily&stacks=firm").json()}
    itself = {r["name"] for r in client.get(
        f"/api/files?event={quote(exact)}&stacks=firm").json()}

    assert trip == {"e1.jpg", "e2.jpg"}, trip
    assert itself == {"e1.jpg"}, itself


def test_the_folder_of_an_event_itself_opens_on_what_it_counted(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The one place a folder's link and a folder's count could disagree
    about what is in it. Grouped by event and sub-event there is a folder for
    *Sicily* beside one for *Sicily > Taormina*, and the first counts only
    the files directly in the event — so asking for the event plainly would
    open it on the whole trip, one more file than the folder said."""
    evented(client, writable, app_env)

    page = client.get("/?group=subevent&stacks=firm").text
    want = quote(f"Sicily{decisions.EVENT_SEP}{ix.NO_EVENT}")

    assert f"event={want}" in page, "the folder asks for the whole trip"
    opened = {r["name"] for r in client.get(
        f"/api/files?event={want}&stacks=firm").json()}
    assert opened == {"e1.jpg"}, opened


def test_dividing_an_event_up_takes_the_file_out_of_the_undivided_folder(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The folder of an event *itself* asks for the files in no part of it, so
    giving one a part is the file leaving that folder — which looks like the
    edit undoing itself if the grid empties and nothing says why.

    It is the honest answer and it stays: the way out is the cross on the
    chip, which widens to the whole event. Pinned because it is the one edit
    that empties the view it was made in, and a future change that quietly
    stopped reporting it would leave the grid claiming files it no longer
    holds."""
    evented(client, writable, app_env)
    exact = quote(f"Sicily{decisions.EVENT_SEP}{ix.NO_EVENT}")

    # `firm`, so the guessed stack the two of them make does not cascade the
    # write onto the other one — this is about one file leaving one view.
    out = client.post(f"/api/decide/bulk?event={exact}&stacks=firm", json={
        "event_leaf": "Beach day",
        "files": [{"folder": "init_2026", "name": "e1.jpg"}]}).json()

    assert decisions.read(writable / "e1.jpg") is not None
    assert event_named(writable, "e1.jpg") == "Sicily > Beach day"
    assert [f["name"] for f in out["dropped"]] == ["e1.jpg"], out


def test_a_sub_event_can_be_named_on_an_event_nobody_wrote_down(
    client: TestClient, writable: Path
) -> None:
    """Most of the library's events live in the files' own tags rather than in
    a decision — seeding skipped writing ~62k sidecars on exactly that — so a
    write that keeps the half it is not replacing has to read that half from
    where the file actually keeps it.

    It read the sidecar alone, found nothing, joined a leaf onto no head and
    wrote nothing at all: naming a sub-event of the event on screen looked
    like the app ignoring the press."""
    assert decisions.read(writable / "a.jpg") is None, "the premise: no sidecar"
    assert {r["name"] for r in client.get(
        "/api/files?event=Italy - Sicily&stacks=firm").json()} >= {"a.jpg"}

    client.post("/api/decide/bulk?stacks=firm", json={
        "event_leaf": "Boat",
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    assert event_named(writable, "a.jpg") == "Italy - Sicily > Boat"
    assert {r["name"] for r in client.get(
        "/api/files?event=Italy - Sicily %3E Boat&stacks=firm").json()}         == {"a.jpg"}


def test_renaming_an_event_nobody_wrote_down_keeps_its_sub_event(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The other half of the same write. A whole name can be inherited too —
    the tags hold whatever was written into the file — and renaming the event
    has to carry the part across from wherever the part is kept.

    And it is a fallback only: a decision that names an event outranks the
    tags, which is what a decision is for."""
    import json

    share = app_env["share"]
    (share / "meta" / "init_2026" / "a.jpg.json").write_text(json.dumps({
        "file": "a.jpg", "folder": "init_2026", "size": 10, "mtime_ns": 1,
        "exif": {"EXIF:DateTimeOriginal": "2026:08:30 15:34:55",
                 "XMP:EventAuto": "Italy - Sicily > Boat"},
    }), encoding="utf-8")
    ix.build(app_env["db"], meta_dir=share / "meta",
             master_dir=share / "master")

    client.post("/api/decide/bulk?stacks=firm", json={
        "event_head": "Sicily",
        "files": [{"folder": "init_2026", "name": "a.jpg"}]})

    assert event_named(writable, "a.jpg") == "Sicily > Boat"


def test_a_thumbnail_says_which_part_of_its_event_it_is(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """The same lozenge a tag wears, in the event's own colour: it is the same
    kind of fact about the file and not the same kind of thing. The part
    alone, because *Taormina* is the news on a 150px thumbnail — the whole
    name is the tooltip."""
    evented(client, writable, app_env)

    page = client.get("/browse?event=Sicily&stacks=firm&group=none").text

    assert 'class="part"' in page, page[:200]
    assert ">Taormina</span>" in page
    assert "Sub-event &mdash; Sicily &gt; Taormina" in page
    # One chip, on the one file that is in a part of the event. The other is
    # in the event itself and there is nothing to say about it.
    assert page.count('class="part"') == 1


def test_it_does_not_say_what_the_view_has_already_said(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Grouped by event and sub-event every heading names one, and filtered to
    a whole name every thumbnail under it carries the same one. A chip that is
    true of everything on screen is furniture.

    Grouping by *event* is not that, and is the case worth keeping: it names
    the trip, and which part of the trip is exactly what still differs from
    one thumbnail to the next."""
    evented(client, writable, app_env)
    whole = quote(f"Sicily{decisions.EVENT_SEP}Taormina")

    grouped = client.get("/browse?event=Sicily&stacks=firm&group=subevent").text
    by_event = client.get("/browse?event=Sicily&stacks=firm&group=event").text
    pinned = client.get(f"/browse?event={whole}&stacks=firm&group=none").text

    assert 'class="part"' not in grouped, "said twice under its own heading"
    assert 'class="part"' in by_event, "the trip is named, the part is not"
    assert 'class="part"' not in pinned, "said on every thumbnail in the view"


def test_an_event_with_no_part_says_nothing(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """There is no part to name, and an empty chip would be a mark meaning
    *this one is plain* — which is most of the library."""
    evented(client, writable, app_env)
    whole = quote(f"Sicily{decisions.EVENT_SEP}{ix.NO_EVENT}")

    page = client.get(f"/browse?event={whole}&stacks=firm&group=none").text

    assert "e1.jpg" in page
    assert 'class="part"' not in page


def test_a_sub_event_is_not_a_filter_of_its_own() -> None:
    """One field at two widths means one filter at two widths. A second
    parameter would be a second thing that could disagree with the first about
    which files are in an event."""
    assert "subevent" not in dict(w_vocab.CHIPS)
    assert "subevent" not in ix.Filters.NAMES
    # But it is a way to cut the library up, and drilling one sets the event.
    # And it is named for grouping on the whole name — events with no
    # sub-event stand as themselves rather than dropping out of the view.
    assert ("subevent", "By event and sub-event") in w_vocab.GRID_GROUPS
    assert w_vocab.DRILL["subevent"] == "event"


def test_a_household_member_may_name_one_but_not_smuggle_a_share(
    household: dict[str, object], writable: Path
) -> None:
    """Half-name writes are not in the log's vocabulary, so they had to be
    named in the permission check or they would have reached the archive
    unchecked."""
    kid = cast(TestClient, household["kid"])

    assert kid.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "event_head": "Sicily"
    }).status_code == 200
    assert kid.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_audience": ["kid"]
    }).status_code == 403


# --- several values in one filter, by address -----------------------------

def test_a_filter_repeated_in_the_address_is_any_of_them(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    three_files(writable, app_env)
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "add_tags": ["beach"]})
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "c.jpg", "add_tags": ["sunset"]})

    html = client.get("/browse?tag=beach&tag=sunset").text
    assert 'data-name="a.jpg"' in html and 'data-name="c.jpg"' in html
    # And the page is told both, as a list.
    view = html[html.index('"VIEW": '):]
    assert '"tag": ["beach", "sunset"]' in view[:400], view[:400]


def test_the_old_stacks_words_still_open_the_same_view(
    client: TestClient
) -> None:
    """Links from before the checkboxes keep working."""
    def view(url: str) -> str:
        html = client.get(url).text
        return html[html.index('"VIEW": '):html.index('"VIEW": ') + 400]

    assert '"stacks": ["stacked", "suggested"]' in view("/browse?stacks=only")
    assert '"stacks": "suggested"' in view("/browse?stacks=guesses")
    # *No suggestions* was never a set of files; it is shown apart now.
    firm = view("/browse?stacks=firm")
    assert '"stacks": null' in firm and '"apart": "1"' in firm


def test_both_sides_of_the_bin_are_two_boxes(client: TestClient) -> None:
    def deleted(url: str) -> str:
        html = client.get(url).text
        at = html.index('"deleted": ')
        return html[at:at + 30]

    assert deleted("/browse?deleted=gone").startswith('"deleted": "gone"')
    assert deleted("/browse?deleted=gone&deleted=live").startswith(
        '"deleted": ["gone", "live"]')
    assert deleted("/browse?deleted=with").startswith(
        '"deleted": ["gone", "live"]')
    assert deleted("/browse?deleted=live").startswith('"deleted": null')


def test_suggestions_apart_is_offered_in_display(client: TestClient) -> None:
    html = client.get("/browse").text
    menu = html[html.index('class="me disp"'):html.index('id="me"')]
    assert 'class="showopt apartopt" data-at="apart"' in menu


def test_a_fixed_filter_offers_only_what_is_there(client: TestClient) -> None:
    """Type offered *Other* to a library with none, and picking it was an
    empty grid. The list is fixed; what is worth offering from it is not."""
    got = {s["value"]: s["n"]
           for s in client.get("/api/suggest?column=kind").json()}
    assert got["photo"] == 1 and got["video"] == 1
    assert got["other"] == 0 and got["clip"] == 0
    # The bin, counted the same way: nothing is in it.
    gone = {s["value"]: s["n"]
            for s in client.get("/api/suggest?column=deleted").json()}
    assert gone == {"gone": 0, "live": 2}
