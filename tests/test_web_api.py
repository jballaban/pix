"""The media routes and the JSON API the page script talks to.

Split out of the one web test module, by area.
"""

from __future__ import annotations

import json
import pytest
import re
import time
from pathlib import Path

from fastapi.testclient import TestClient
from web_helpers import burst, dated, folders_of, pix, spread, undated_clip

from pix.nas import index as ix, web
from pix.nas.webapp import pages as w_pages


# --- media ----------------------------------------------------------------

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


# --- api ------------------------------------------------------------------

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


def test_the_outer_groupings_are_shelves_not_prefixes(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """`year › event` is a row of events under each year, not a flat list of
    cards each repeating which year it is in. The grid reads that way and this
    is the same library."""
    burst(app_env, writable, "x.jpg", "y.jpg")
    client.post("/api/decide/bulk", json={
        "event": "Sports Day",
        "files": [{"folder": "init_2026", "name": "x.jpg"}]})

    folders = folders_of(client.get("/?group=year,event").text)

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
    folders = folders_of(client.get("/?group=year").text)

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
    folders = folders_of(client.get("/?group=event").text)

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
    by_day = folders_of(client.get("/?group=day").text)
    assert 'class="when"' not in by_day, "the day is printed twice"

    # Where the name is not the date, when it happened is worth saying.
    by_event = folders_of(client.get("/?group=event").text)
    assert 'class="when"' in by_event


def test_a_folder_split_by_the_grouping_says_so(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """An event running from one month into the next is a card under each. A
    folder saying *2 files* with nothing to say it is part of five is a folder
    describing the grouping rather than the library."""
    spread(app_env, writable, {"jan.jpg": "2026:01:20 10:00:00",
                                "feb1.jpg": "2026:02:02 10:00:00",
                                "feb2.jpg": "2026:02:03 10:00:00"})
    client.post("/api/decide/bulk", json={
        "event": "Ski Trip",
        "files": [{"folder": "init_2026", "name": n}
                  for n in ("jan.jpg", "feb1.jpg", "feb2.jpg")]})

    folders = folders_of(client.get("/?date=2026&group=month,event").text)

    assert "1 <i>of</i> 3 files" in folders, folders
    assert "2 <i>of</i> 3 files" in folders, folders
    assert "3 files in all" in folders, "no explanation of what it is part of"


def test_a_folder_that_is_whole_says_nothing_about_being_split(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """Most cards are slices the moment you group by month and event. The ones
    that are not have to read as plainly as they ever did."""
    spread(app_env, writable, {"feb1.jpg": "2026:02:02 10:00:00",
                                "feb2.jpg": "2026:02:03 10:00:00"})
    client.post("/api/decide/bulk", json={
        "event": "One Weekend",
        "files": [{"folder": "init_2026", "name": n}
                  for n in ("feb1.jpg", "feb2.jpg")]})

    folders = folders_of(client.get("/?date=2026&group=month,event").text)
    card = folders[folders.index("One Weekend"):]

    assert "2 files" in card[:200], card[:200]
    assert "<i>of</i>" not in card[:200], "a whole folder claiming to be a part"


def test_nothing_is_split_when_nothing_is_above_it(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """One level of grouping cuts nothing up, so there is nothing to warn
    about and no second query to run finding that out."""
    spread(app_env, writable, {"jan.jpg": "2026:01:20 10:00:00",
                                "feb1.jpg": "2026:02:02 10:00:00"})

    folders = folders_of(client.get("/?date=2026&group=event").text)

    assert "<i>of</i>" not in folders


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
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """*No day* holding one file dated to the year and one to the month is
    two sets of files, and no one filter opens exactly both. Sending it to
    the year would open a folder holding more than the one that was
    clicked."""
    (writable / "b.mp4").write_bytes(b"fake")
    undated_clip(app_env, writable, "c.mp4")
    dated(client, "b.mp4", "2026-*-*-*:*:*")
    dated(client, "c.mp4", "2026-08-*-*:*:*")
    html = client.get("/?group=day").text

    assert 'class="tile dead"' in html, "offered a door to somewhere else"
    assert "No day" in html


def test_a_folder_dated_only_to_its_year_opens_as_exactly_that(
    client: TestClient, writable: Path
) -> None:
    """*No month* under 2026 is the files dated to 2026 and no finer — a set
    of its own, which neither `2026` nor `undated` is."""
    (writable / "b.mp4").write_bytes(b"fake")
    dated(client, "b.mp4", "2026-*-*-*:*:*")
    html = client.get("/?date=2026&group=month,event").text

    assert 'class="tile dead"' not in html
    assert "date=2026-%2A" in html or "date=2026-*" in html, html[:0]
    names = [f["name"] for f in client.get("/api/files?date=2026-*").json()]
    assert names == ["b.mp4"], "a file dated to the day is not dated to 2026"


def test_a_month_known_and_no_day_is_its_own_filter(
    client: TestClient, writable: Path
) -> None:
    (writable / "b.mp4").write_bytes(b"fake")
    dated(client, "b.mp4", "2026-08-*-*:*:*")
    names = [f["name"] for f in client.get("/api/files?date=2026-08-*").json()]
    assert names == ["b.mp4"]
    assert client.get("/api/files?date=2026-*").json() == []


def test_the_chip_says_what_is_missing() -> None:
    js = w_pages.BROWSE_JS
    assert "', no month'" in js and "', no day'" in js


def test_the_event_pages_undated_folder_opens(client: TestClient) -> None:
    """The Event page is cut by month, and a file with no date has no month —
    so its folder was the one on the page that would not open, pills and all.
    Nothing in it has a date, so *undated* is exactly what it holds."""
    html = client.get("/?group=month,event").text

    assert 'class="tile dead"' not in html
    assert "date=%28undated%29" in html or "date=(undated)" in html, html[:0]


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
        chips = json.dumps(pix(page)["CHIPS"])
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
    burst(app_env, writable, "x.jpg", "y.jpg")
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
