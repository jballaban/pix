"""The app's stylesheets, scripts and page fragments, as files.

They were Python strings in `web.py` — more than half of it — and every one
was a place for an escape to go wrong twice: a `\2715` in a plain string
became a superscript one and a five, and a `'\n'` a line break in the middle
of a script. As files they are what they say, can be checked by the tools
made for them (`node --check`), and an edit to the grid's script no longer
means loading eleven thousand lines of Python to make it.

**Still inlined into each page** (`web._page`), deliberately: a page and its
script are always the same build, which a separately cached file is not —
see the `_page` docstring. And no build step: the deploy copies `src` to the
share, and these travel with it like the icons do.

Read once, when the module loads. A change to one needs the server
restarted, the same as a change to the Python beside it.
"""

from __future__ import annotations

from pathlib import Path

#: Where they live: beside this module, inside the package.
STATIC: Path = Path(__file__).parent / "static"


def asset(name: str) -> str:
    """One file's text, exactly as written — `name` is relative to `STATIC`."""
    with open(STATIC / name, encoding="utf-8", newline="") as f:
        return f.read()
