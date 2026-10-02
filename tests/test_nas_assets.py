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

from pix.nas import assets, web

JS = sorted(assets.STATIC.glob("js/*.js"))
CSS = sorted(assets.STATIC.glob("css/*.css"))


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed")
@pytest.mark.parametrize("script", JS, ids=[p.name for p in JS])
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
    assert web._STYLE == assets.asset("css/app.css")  # pyright: ignore[reportPrivateUsage]
    assert web._BROWSE_JS == assets.asset("js/browse.js")  # pyright: ignore[reportPrivateUsage]


def test_no_asset_has_carriage_returns() -> None:
    """Written and read without newline translation, so a Windows checkout
    cannot slip `\r` into a script that is then served to a phone."""
    for path in [*JS, *CSS, *assets.STATIC.glob("html/*.html")]:
        assert b"\r" not in path.read_bytes(), path.name
