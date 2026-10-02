"""Small text helpers the web app's pages and APIs share: escaping for HTML,
JSON for an inline script, URL quoting, and the formatters for an age, a
duration and a moment.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import quote


def split(value: object) -> list[str]:
    """A `group_concat` column back into a list."""
    return str(value).split(chr(10)) if value else []


def under(root: Path, target: Path) -> bool:
    """Whether `target` really sits inside `root`, after resolving `..`."""
    return root.resolve() in target.resolve().parents


def h(text: object) -> str:
    """Escape for HTML text and attributes."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def js(value: object) -> str:
    """Embed a value in a <script> block.

    `<` is escaped because an event named with a literal `</script>` would
    otherwise close the block and run whatever followed as markup — and event
    names are typed by whoever is curating.
    """
    return json.dumps(value).replace("<", "\\u003c")


def q(text: object) -> str:
    return quote(str(text), safe="")


def age(timestamp: float | None) -> str:
    """How long ago the index was built, in words.

    Nothing watches the share, so an index predating the last `process` run
    simply does not know about the files it made. Showing the age is how that
    gets noticed, rather than experienced as photos mysteriously missing.
    """
    if timestamp is None:
        return "at an unknown time"
    seconds = max(time.time() - timestamp, 0)
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 36:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"


def dur(seconds: object) -> str:
    try:
        total = int(float(str(seconds)))
    except (TypeError, ValueError):
        return "video"
    return f"{total // 60}:{total % 60:02d}"


def when(moment: float) -> str:
    """A timestamp as a person reads it."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(moment))
