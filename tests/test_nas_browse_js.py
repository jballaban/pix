"""The browse page's script, actually executed (spec/nas-app.md §8).

Every other test here asserts on the HTML the server *sends*. That leaves the
part that has broken repeatedly completely uncovered: the script itself. A
runtime error there takes every handler with it and leaves a grid that silently
ignores clicks — which is exactly what happened when the count and message line
moved into the footer, below the script that looks them up.

So this runs the real script against a small DOM stub and drives it: select a
photo, open Access, tick a name, and check the request that comes out. Node is
not a dependency of the app or of the test suite; where it is absent the test
skips rather than failing, because the browser it stands in for is not a
dependency either.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pix.nas import web

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

JS_DIR = Path(__file__).parent / "js"


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed")
def test_the_browse_script_runs_and_does_the_right_thing(
    tmp_path: Path
) -> None:
    """Select, open the Access menu, tick a name, and see what is sent."""
    script = tmp_path / "browse.js"
    script.write_text(web._BROWSE_JS, encoding="utf-8")

    result = subprocess.run(
        ["node", str(JS_DIR / "drive.js"), str(script)],
        capture_output=True, text=True, timeout=120, cwd=JS_DIR)

    assert result.returncode == 0, (result.stdout + result.stderr)[-2000:]


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed")
def test_the_same_script_folds_and_refuses_the_apps_own_guesses(
    tmp_path: Path
) -> None:
    """A grid holding a guessed stack, a decided one and a photograph that is
    neither: only the first can be refused, and refusing it puts back what it
    was hiding.

    Worth driving rather than reading, because every part of it is a
    consequence rather than a string — which button is on screen, what order
    the two requests go out in, and where the photographs land afterwards.
    """
    script = tmp_path / "browse.js"
    script.write_text(web._BROWSE_JS, encoding="utf-8")

    result = subprocess.run(
        ["node", str(JS_DIR / "review.js"), str(script)],
        capture_output=True, text=True, timeout=120, cwd=JS_DIR)

    assert result.returncode == 0, (result.stdout + result.stderr)[-2000:]


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed")
def test_the_same_script_says_which_stack_you_are_in_and_lets_you_leave(
    tmp_path: Path
) -> None:
    """A grid standing inside an opened stack: it says so, it offers the way
    out, and the badge that got you here does not also open the viewer.

    Driven rather than read, because the bug it locks down was invisible in the
    markup. Opening a stack left the viewer open behind the navigation, so the
    browser's back button — the only way out there was — restored a page with
    a photograph over the grid that nobody had asked to see.
    """
    script = tmp_path / "browse.js"
    script.write_text(web._BROWSE_JS, encoding="utf-8")

    result = subprocess.run(
        ["node", str(JS_DIR / "stack.js"), str(script)],
        capture_output=True, text=True, timeout=120, cwd=JS_DIR)

    assert result.returncode == 0, (result.stdout + result.stderr)[-2000:]


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed")
def _element_ids(html: str) -> list[str]:
    """The ids of the page's real elements.

    The script is inlined into the page, so its own `getElementById('menu')`
    strings are in the HTML too — and a stage built from those would contain
    every element the script *wants* rather than every element the page
    *has*, which is the one difference worth knowing.
    """
    markup = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    return sorted(set(re.findall(r'id="([^"]+)"', markup)))


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed")
def test_the_same_script_runs_the_landing_page(
    tmp_path: Path, client: "TestClient"
) -> None:
    """The landing page is the same two controls over folders instead of
    files, so it is the same script — on a page with no selection, no viewer
    and no actions to wire.

    **Staged from the page the server actually sends.** The element list used
    to be written out by hand here, and it held a `#menu` the landing page did
    not render: the script threw reaching for it on load, which takes every
    handler on the page with it, and this test went on passing because its own
    stage had one. A page whose chips never draw and whose grouping never
    wires looks exactly like a page whose controls were never built.
    """
    script = tmp_path / "browse.js"
    script.write_text(web._BROWSE_JS, encoding="utf-8")
    ids = tmp_path / "ids.json"
    ids.write_text(json.dumps(_element_ids(client.get("/").text)),
                   encoding="utf-8")

    result = subprocess.run(
        ["node", str(JS_DIR / "home.js"), str(script), str(ids)],
        capture_output=True, text=True, timeout=120, cwd=JS_DIR)

    assert result.returncode == 0, (result.stdout + result.stderr)[-2000:]


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed")
def test_the_browse_script_parses(tmp_path: Path) -> None:
    """A syntax error is silent in a browser and total in its effect."""
    script = tmp_path / "browse.js"
    script.write_text(web._BROWSE_JS, encoding="utf-8")

    result = subprocess.run(["node", "--check", str(script)],
                            capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr[-2000:]
