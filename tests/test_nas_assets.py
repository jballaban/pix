"""The app's scripts and stylesheets, as the files they now are.

They were strings in `web.py`, where an escape could go wrong twice — once in
Python, once in the language inside it. As files they are checked by the tools
made for them, and this is where that happens.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from pix.nas import assets
from pix.nas.webapp import shell as w_shell
from pix.nas.webapp import pages as w_pages

JS = sorted(assets.STATIC.glob("js/**/*.js"))
CSS = sorted(assets.STATIC.glob("css/*.css"))


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed")
@pytest.mark.parametrize("script", JS,
                         ids=[p.relative_to(assets.STATIC).as_posix() for p in JS])
def test_every_script_parses(script: Path) -> None:
    result = subprocess.run(["node", "--check", str(script)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]


@pytest.mark.parametrize("sheet", CSS, ids=[p.name for p in CSS])
def test_every_stylesheet_balances_its_braces(sheet: Path) -> None:
    """The cheapest check that catches the commonest slip: a rule left
    open swallows every rule after it, silently."""
    text = sheet.read_text(encoding="utf-8")
    depth = 0
    for ch in text:
        depth += (ch == "{") - (ch == "}")
        assert depth >= 0, f"{sheet.name}: a closing brace with no opening"
    assert depth == 0, f"{sheet.name}: {depth} rule(s) left open"


def test_the_page_is_handed_exactly_what_is_on_disk() -> None:
    """Inlined as read, nothing between the file and the page."""
    assert w_shell.STYLE == assets.asset("css/app.css")  # pyright: ignore[reportPrivateUsage]
    assert w_pages.BROWSE_JS == "".join(  # pyright: ignore[reportPrivateUsage]
        assets.asset(f"js/browse/{p}")
        for p in w_pages.BROWSE_PARTS)  # pyright: ignore[reportPrivateUsage]


def test_every_part_of_the_grid_script_is_read() -> None:
    """A part on disk and missing from the list is code that silently does
    not run; one in the list and not on disk fails to load at all."""
    on_disk = sorted(p.name for p in (assets.STATIC / "js/browse").glob("*.js"))
    assert sorted(w_pages.BROWSE_PARTS) == on_disk  # pyright: ignore[reportPrivateUsage]
    # Read in the order they are numbered, which is the order they were cut.
    assert list(w_pages.BROWSE_PARTS) == on_disk  # pyright: ignore[reportPrivateUsage]


def test_no_asset_has_carriage_returns() -> None:
    """Written and read without newline translation, so a Windows checkout
    cannot slip `\r` into a script that is then served to a phone."""
    for path in [*JS, *CSS, *assets.STATIC.glob("html/*.html")]:
        assert b"\r" not in path.read_bytes(), path.name


def test_the_web_apps_roots_are_never_the_real_share() -> None:
    """The web app looks its roots up in one module, and the test guard has
    to have redirected every one of them — or a test writes to the archive."""
    import os

    from pix.nas import webroots

    real = Path(os.environ.get("PIX2_SHARE") or r"\nas\pix2")
    for name in ("DB_PATH", "MASTER_DIR", "META_DIR", "THUMB_DIR",
                 "PREVIEW_DIR", "LARGE_DIR", "RENDER_DIR", "STRIP_DIR"):
        value: Path = getattr(webroots, name)
        assert real not in (value, *value.parents), name


def test_the_script_unpacks_exactly_what_the_page_is_handed() -> None:
    """`window.PIX` is the one contract between the server and the page
    script: a key the server stops sending is an `undefined` somewhere in
    three thousand lines, and one the script stops reading is dead weight."""
    import re

    from pix.nas.webapp import pages

    first = assets.asset("js/browse/00-page.js")
    unpacked = first[first.index("const {"):first.index("} = window.PIX")]
    names = {re.sub(r"=.*", "", n).strip()
             for n in unpacked[len("const {"):].split(",") if n.strip()}
    assert names == set(pages.PageConfig.__annotations__)


def test_has_rule_reads_rules_not_text() -> None:
    """The stylesheet assertions stand on this, so it is checked both ways:
    a `not has_rule(...)` that could never find anything would pass forever."""
    from web_helpers import css_rules, has_rule

    sheet = """/* a } in a comment */
.a, .b  {  color:red ;display : none }
@media (max-width: 720px) { .c { flex-wrap:nowrap; gap:3px } }"""
    assert len(css_rules(sheet)) == 2       # one at the top, one in the block
    assert has_rule(sheet, ".b, .a", "display:none; color:red")
    assert has_rule(sheet, ".a, .b", "color")            # set, any value
    assert not has_rule(sheet, ".a", "color:red")         # not the same list
    assert not has_rule(sheet, ".a, .b", "color:blue")
    assert has_rule(sheet, ".c", "flex-wrap:now")         # a prefix, like a cut literal
    assert has_rule(sheet, ".c", "gap:3px", media="(max-width: 720px)")
    assert not has_rule(sheet, ".c", "gap:3px", media="(hover: none)")
    assert has_rule("<html><style>.d { x:1 }", ".d", "x:1"), "an unclosed page"
    # And on the real thing: it finds hundreds, and a rule that is there.
    from pix.nas.webapp import shell
    assert len(css_rules(shell.STYLE)) > 300
    assert has_rule(shell.STYLE, ".ov .fix", "flex:none")
