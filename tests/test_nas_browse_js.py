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

import shutil
import subprocess
from pathlib import Path

import pytest

from pix.nas import web

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
def test_the_browse_script_parses(tmp_path: Path) -> None:
    """A syntax error is silent in a browser and total in its effect."""
    script = tmp_path / "browse.js"
    script.write_text(web._BROWSE_JS, encoding="utf-8")

    result = subprocess.run(["node", "--check", str(script)],
                            capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr[-2000:]
