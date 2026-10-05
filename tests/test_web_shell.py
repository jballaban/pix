"""The frame every page is drawn in: installing it on a phone, the bar, controls a thumb can reach, and moving from one page to another.

Split out of the one web test module, by area.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from web_helpers import contrast, has_rule, media_block, var

from pix import __version__ as PIX_VERSION
from pix.nas import web
from pix.nas.webapp import pwa as w_pwa, shell as w_shell


# --- installing it on a phone ---------------------------------------------

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
    assert "fetch(req, { cache: 'no-store' })" in body
    assert "caches.match('/offline')" in body


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
    explaining it the wrong way for as long as the install lasts.

    **This failing is the point.** It names the version on purpose, so that
    changing the worker breaks it and the bump is a thing somebody decided
    rather than a thing somebody forgot. Read the failure, bump the constant,
    change this line.
    """
    body = TestClient(web.app).get("/sw.js").text

    assert "pix2-shell-v3" in body, "shell cache not bumped"


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
    js = w_shell.INSTALL_JS

    assert "iPhone|iPad|iPod" in js and "/Android/" in js
    assert "if (!ios && !android) return;" in js
    # And never inside the thing it is offering.
    assert "display-mode: standalone" in js
    assert "navigator.standalone" in js


# --- reachable with a thumb -----------------------------------------------

def test_a_finger_gets_a_control_it_can_hit() -> None:
    """Apple asks for 44 points square; the bar was drawn to 31.

    `--ctl` is the one number: the height the action row reserves and the
    height a button measures both come off it, so a block that raised the
    buttons without raising it would put a 44px control in a 31px hole.
    """
    coarse = media_block(w_shell.STYLE, "(pointer: coarse)")

    assert "--ctl:44px" in coarse
    assert "min-height:44px" in coarse


def test_the_three_circles_are_left_out_of_it() -> None:
    """The stylesheet's own warning, and it has been paid for once: a
    `min-height` outranks their fixed `height` and would make an oval of every
    select circle in the grid. They get an invisible slug instead, so twenty
    pixels on screen is forty-four to a thumb."""
    coarse = media_block(w_shell.STYLE, "(pointer: coarse)")

    assert "button:not(.tick):not(.grppick):not(.pick)" in coarse
    assert ".pick::before, .tick::before, .grppick::before" in coarse
    assert "inset:-12px" in coarse
    # Positioned, or the slug is laid out against the page instead.
    assert has_rule(coarse, '.tick, .grppick', 'position:relative')


def test_hiding_and_sizing_are_asked_as_two_questions() -> None:
    """A touchscreen laptop has a pointer and wants nothing revealed; a phone
    on a trackpad has a coarse one and wants nothing enlarged. Answering both
    with one query gets one of them wrong."""
    hover = media_block(w_shell.STYLE, "(hover: none)")

    # What hover hides is unreachable here, and only that.
    assert has_rule(hover, '.choose', 'opacity:1')
    assert "--ctl" not in hover


def test_a_sign_in_field_does_not_zoom_the_page() -> None:
    """Sixteen pixels is the exact threshold below which the phone zooms in on
    a focused field and does not zoom back.

    In the login sheet rather than the main one because that sheet is served
    *after* it: `.gate input { font:inherit }` is the same weight and the last
    one counts, so the rule would have been written and quietly lost.
    """
    assert "font-size:16px" in media_block(w_shell.LOGIN_CSS, "(pointer: coarse)")
    assert "font-size:16px" in media_block(w_shell.STYLE, "(pointer: coarse)")


def test_every_control_in_the_bar_grows_together() -> None:
    """Two of the chips are spans rather than buttons — the one saying which
    operation you arrived from, and the one saying which stack you are in — so
    a rule about buttons alone leaves 31px chips in a 44px row, which is the
    misalignment `--ctl` exists to prevent."""
    coarse = media_block(w_shell.STYLE, "(pointer: coarse)")
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
    coarse = media_block(w_shell.STYLE, "(pointer: coarse)")
    rows = coarse[coarse.index(".memenu a,"):].split("{")[0]

    assert ".memenu button:not(.tick):not(.grppick):not(.pick)" in rows
    # Equal weight, so the later one counts — and it has to be the later one.
    assert coarse.index("button:not(.tick)") < coarse.index(".memenu button:not(")


# --- the bar on a phone ---------------------------------------------------

def test_three_rarely_used_controls_became_one() -> None:
    """The account, the thumbnail size and what is waiting were three separate
    things standing permanently in the bar. Each is small, each is reached
    once in a while, and together with the filters they had the top of a phone
    at four rows and nearly half the screen.

    Only on a phone, now. The size control is worth ninety pixels of a desktop
    bar — it is the one view control you reach for *while* looking at what it
    changes, and a trip into a menu to do that is a trip each way."""
    coarse = media_block(w_shell.STYLE, "(max-width: 720px)")

    # The name gives way to a gear; the gear is the only thing left.
    assert has_rule(coarse, '.me .name', 'position:absolute')
    assert has_rule(coarse, '.me .gearbtn', 'display:inline-flex')
    # And the size control goes back inside it — the whole group, so that a
    # section divider and its padding are not left standing over nothing.
    assert has_rule(coarse, '.memenu .viewrow', 'display:flex')
    assert has_rule(coarse, '.right .sizeset, .right > .disp', 'display:none')
    # Which is the off position everywhere else.
    assert has_rule(w_shell.STYLE, '.memenu .viewrow', 'display:none')


def test_the_size_control_stands_in_the_bar_where_there_is_room(
    client: TestClient
) -> None:
    """Both copies are always rendered, because which one applies changes
    while the page is open — by turning the phone over. So the markup carries
    two and the media query picks, the same arrangement as the name and the
    gear."""
    html = client.get("/browse?event=Italy%20-%20Sicily").text
    row = html[html.index('class="row"'):html.index("</div><main")]

    assert row.count('data-size="small"') == 2, row
    # One in the bar's Display menu, beside the account rather than inside
    # it; one inside, on a row that says what it is on — three letters in a box say nothing.
    outside, inside = row.split('id="me"')
    assert 'data-size="small"' in outside
    assert 'class="rowlab">Thumbnail size' in inside
    assert 'class="sizeset"' in inside


def test_the_bar_is_not_two_copies_of_anything(client: TestClient) -> None:
    """The narrow rules hide rather than move, which only works while nothing
    that is hidden is a second copy of something *identified*. The size
    control is rendered twice on purpose and carries no id for exactly that
    reason: the script drives every copy it finds rather than the first."""
    html = client.get("/browse?event=Italy%20-%20Sicily").text

    for once in ('id="me"', 'id="bincount"', 'class="whoami"'):
        assert html.count(once) == 1, f"{once} appears {html.count(once)} times"
    assert "id=\"sizepick\"" not in html, "an id on a control rendered twice"


def test_what_is_waiting_rides_on_the_control_you_can_see(
    client: TestClient, writable: Path
) -> None:
    """A notification that needs opening to be seen is not one."""
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "a.jpg", "deleted": True})
    html = client.get("/browse").text
    bar = html[html.index('class="topbar"'):html.index("</div><main")]

    assert '<details class="me" id="me" data-any="1">' in bar
    assert 'class="dot"' in bar


def test_the_corner_stays_where_it_can_be_read(client: TestClient) -> None:
    """It went to the bottom while the bar was four rows deep. The bar is the
    filters and a gear now, and a picture of the grid under it earns the
    thirty pixels — which is the whole reason the corner is a picture."""
    html = client.get("/browse?event=Italy%20-%20Sicily").text
    coarse = media_block(w_shell.STYLE, "(max-width: 720px)")

    assert 'class="brand"' in html
    assert not has_rule(coarse, '.topbar .brand', 'display:none')


def test_a_filter_doing_nothing_is_not_on_a_phones_bar() -> None:
    """Ten glyphs do not fit across a phone. Which fit applies is measured
    (see `test_the_unused_filters_fold_away_where_they_do_not_fit`); this is
    that the fold-away still outweighs the thumb-sized buttons' rule."""
    assert has_rule(w_shell.STYLE, '.chips[data-fit="spare"] .spare, '
                                   '.chips[data-fit="icons"] .spare',
                    'display:none')


def test_a_page_is_never_served_from_a_cache(client: TestClient) -> None:
    """Every page carries its own script inlined, so a cached page is a cached
    *build*.

    Nothing used to say how old one was — no `Cache-Control`, no `ETag`, no
    `Last-Modified` — which leaves a browser free to decide for itself, and
    Safari in an installed app decides yes. A deploy would land, the container
    restart, and the phone go on showing last week's app with nothing on
    screen to say so.
    """
    for path in ("/browse", "/?date=2026&group=year", "/login"):
        assert client.get(path).headers.get("cache-control") == "no-store", path


def test_the_worker_fetches_a_page_rather_than_the_cached_one() -> None:
    """It claims navigations go to the network every time, and a plain `fetch`
    reads the HTTP cache — so it was handing back the very copy it thought it
    was avoiding."""
    assert "fetch(req, { cache: 'no-store' })" in w_pwa.SERVICE_WORKER


def test_making_a_control_bigger_does_not_make_it_visible() -> None:
    """The rule that gives every button a thumb-sized target carries three
    `:not()`s, which weigh three classes — so it outranks nearly anything that
    tries to hide one of those buttons later.

    The filters a phone folds away are buttons. A `display` in that rule put
    every unused filter back on the bar, four rows of them, while
    `.chips .spare { display:none }` sat two blocks above it being outweighed.
    The fourth specificity tie this stylesheet has paid for, and the first
    that was visible from across the room.
    """
    coarse = media_block(w_shell.STYLE, "(pointer: coarse)")
    rule = coarse[coarse.index("button:not(.tick)"):]
    rule = rule[:rule.index("}")]

    assert "display" not in rule, rule
    assert "min-height:44px" in rule


# --- one surface from another ---------------------------------------------

def test_a_card_reads_as_a_thing_on_the_page() -> None:
    """They were a shade apart and measured it: a tile against the page was
    1.08:1 and its own border 1.04:1 against the tile — which is not an edge,
    it is a rumour of one. Forty of them read as a single grey field with text
    in it."""
    assert contrast(var("--panel"), var("--bg")) > 1.18
    assert contrast(var("--line"), var("--panel")) > 1.35
    assert contrast(var("--chrome"), var("--bg")) > 1.25


def test_the_quiet_text_is_still_readable_on_it() -> None:
    """Lifting the surfaces moves the floor under everything written on them,
    and the half of the app that is dim text is the half that notices."""
    assert contrast(var("--dim"), var("--panel")) >= 4.5
    assert contrast(var("--fg"), var("--panel")) >= 7


def test_a_card_is_lifted_as_well_as_outlined() -> None:
    """An edge and a shadow say *this is on top of that* twice, which is what
    a card needs to say when there are forty of them."""
    tile = w_shell.STYLE[w_shell.STYLE.index(".tile { display:flex"):]
    tile = tile[:tile.index("}")]

    assert "box-shadow" in tile and "border:1px solid var(--line)" in tile


def test_every_page_says_which_build_it_is(client: TestClient) -> None:
    """The script is inlined into each page, so an open tab keeps the build it
    was served with — and a stale tab and a broken build look identical from
    the outside.

    In the `<head>`, because the visible one moved into the account menu and
    went off every signed-out page with it. The login screen is the one page a
    deploy check can reach without a session, and it had no version on it at
    all.
    """
    for path in ("/browse", "/?date=2026&group=year", "/login"):
        html = client.get(path).text
        assert f'<meta name="pix-version" content="{PIX_VERSION}">' in html, path


def test_and_says_it_where_somebody_can_read_it(client: TestClient) -> None:
    """Under the account where there is one, beside Sign in where there is
    not."""
    assert f"v{PIX_VERSION}" in client.get("/browse").text
    assert f"v{PIX_VERSION}" in client.get("/login").text
