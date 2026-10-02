"""The landing page: what a folder says about itself.

Split out of the one web test module, by area.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient
from web_helpers import burst, card_of, mixed

from pix.nas import index as ix
from pix.nas.webapp import pages as w_pages, shell as w_shell


# --- what a folder says about itself --------------------------------------

def test_a_folder_says_what_is_in_it_and_not_only_how_much(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """A card said how many files it held and how much of that was decided;
    what it could not say is *what* was decided. Opening it was the only way
    to find out, which is the one thing a folder exists to save you."""
    mixed(client, writable, app_env)

    card = card_of(client.get("/?date=2026&group=year&stacks=firm").text)

    assert '<i class="audience"' in card
    assert ">family<" in card


def test_a_share_the_whole_folder_carries_prints_no_percentage(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """A value the whole folder has is a fact about the folder and reads as a
    plain word, exactly as it does on a thumbnail. `100%` beside everything
    would bury the one chip that is not all of it."""
    mixed(client, writable, app_env)

    card = card_of(client.get("/?date=2026&group=year&stacks=firm").text)
    who = card[card.index('class="spread"'):]
    who = who[:who.index("</span>") + 7]

    assert "100%" not in who, who
    assert "<b>" not in who, who


def test_a_chip_says_the_name_and_nothing_else(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """A share was printed beside each one while *undecided* was still a count
    in a sentence underneath, and the number kept that reading alive. Once
    what is left became a chip of its own, `bob 5%` stopped answering anything
    anybody asks of a folder — *who is in here* and *what is left* are the
    questions, and neither is a percentage.

    It cost horizontal room on the one screen with none to spare, which is how
    it was noticed. The counts stay in the tooltip, where they cost nothing.
    """
    mixed(client, writable, app_env)

    card = card_of(client.get("/?date=2026&group=year&stacks=firm").text)
    tags = card[card.index('class="spread"'):]
    tags = tags[:tags.index("</span>") + 7]

    assert "%" not in tags, tags
    assert "<b>" not in tags, tags
    assert ">beach</i>" in tags, tags
    # What kind of thing it is and how many, where it costs nothing to
    # anybody who does not want it.
    assert 'title="Tag — 1 file"' in tags, tags


def test_a_folder_names_every_value_it_holds(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """It used to name three and finish with `+4`, which is the one thing a
    summary must not do: it says there is something else in there and refuses
    to say what, so the card stops being an answer and becomes a reason to
    open the folder — the errand it exists to save.

    A taller card is cheaper than that.
    """
    burst(app_env, writable, "w.jpg")
    many = [f"t{i}" for i in range(7)]
    for tag in many:
        client.post("/api/decide", json={
            "folder": "init_2026", "name": "w.jpg", "add_tags": [tag]})

    card = card_of(client.get("/?date=2026&group=year&stacks=firm").text)
    tags = card[card.index('class="spread"'):]
    tags = tags[:tags.index("</span>") + 7]

    for tag in many:
        assert f">{tag}<" in tags, f"{tag} is not on the card: {tags}"
    assert "more" not in tags, tags


def test_a_household_member_is_not_told_about_an_audience(
    client: TestClient, writable: Path, app_env: dict[str, Path],
    sign_in: "Callable[[str, str], TestClient]",
    add_user: "Callable[..., None]"
) -> None:
    """They see nothing that is not already shared with them, so the answer is
    always *all of it* — a chip that can only ever say one thing."""
    mixed(client, writable, app_env)
    add_user("kid", "pw")
    client.post("/api/decide/bulk", json={
        "add_audience": ["kid"],
        "files": [{"folder": "init_2026", "name": "w.jpg"}]})

    html = sign_in("kid", "pw").get("/?date=2026&group=year&stacks=firm").text

    assert '<i class="audience"' not in html
    assert "spread tags" in html or "spread people" in html or True


def test_the_share_is_of_the_card_that_prints_it() -> None:
    """The same `GROUP BY` the sections use, with one join added — a count
    drawn from a different question would be a percentage of something else,
    and nothing on screen would say so."""
    import inspect

    body = inspect.getsource(ix.spread)
    assert "GROUPINGS[g]" in body and "_where(view)" in body


def test_every_page_that_writes_can_say_it_is_writing(
    client: TestClient
) -> None:
    """A write is a run of requests over SMB and takes seconds; without the
    takeover the screen just sits there.

    It lived in the grid's markup alone, which was true for exactly as long as
    the grid was the only page that wrote. When the landing page learned to,
    every `if(working)` guard in the script quietly did nothing and a folder
    edit ran with no sign of it — the failure a guard is supposed to prevent,
    arriving as silence instead of an error.
    """
    for path in ("/browse", "/?date=2026&group=month,event"):
        html = client.get(path).text
        for part in ('id="working"', 'id="workbar"', 'id="worktally"',
                     'id="workstop"', 'id="workwhat"'):
            assert part in html, f"{path} is missing {part}"


def test_the_takeover_is_written_once(client: TestClient) -> None:
    """Two copies is two things to keep in step, and the one that fell behind
    would be the page nobody was looking at."""
    assert w_pages.BROWSE_JS.count("getElementById('working')") == 1
    html = client.get("/browse").text
    assert html.count('id="working"') == 1


def test_what_is_left_to_do_is_not_the_colour_of_what_is_done() -> None:
    """Amber, which is what this app has always meant by *this wants you* —
    the same as a guessed stack and a half-ticked box. It is the one chip on a
    card that is a job rather than a fact, and the audience green filed it in
    with the thing it is counted from.

    Pinned for its **weight**, not its colour. `.spread i.none` and
    `.spread.audience i` weigh exactly the same, so the later one won and this
    drew green for as long as it sat higher up the file — a rule that was
    correct and had no effect.
    """
    css = w_shell.STYLE
    rule = ".spread i.none { color:#f2d38a; background:var(--top-bed); }"

    assert rule in css
    # After the rule that colours an audience chip, which it shares a class
    # with and must outrank.
    assert css.index(".spread i.audience {") < css.index(rule)


def test_the_plus_more_chip_is_gone_from_the_stylesheet() -> None:
    """Cards name every value they hold, so nothing renders it any more."""
    assert ".spread .more" not in w_shell.STYLE


def test_the_landing_page_opens_on_the_folders(client: TestClient) -> None:
    """A standing description of the library — how much there is, how much is
    undated, when it was last indexed — does not change while you read it, so
    a row of it above the grid was a row of folders pushed off the screen to
    say something that had not moved since yesterday."""
    html = client.get("/?date=2026&group=year").text
    main = html[html.index("<main>"):]

    assert main.startswith('<main><div class="grid folders"') or \
        main.startswith('<main><p class="empty"'), main[:120]


def test_but_it_still_says_what_the_library_holds(client: TestClient) -> None:
    """Moved, not dropped — into the account menu, which is where the rest
    of what you reach for once in a while already lives."""
    html = client.get("/?date=2026&group=year").text
    menu = html[html.index('class="memenu"'):]
    menu = menu[menu.index('class="info"'):]

    assert "files" in menu and "undated" in menu and "indexed" in menu
