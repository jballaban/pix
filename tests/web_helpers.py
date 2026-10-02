"""Helpers the web app's tests share — the HTML a page is cut into, the
files a test arranges, the arithmetic of a contrast check."""

from __future__ import annotations

import json
import re
import urllib.parse

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from pix.nas import decisions
from pix.nas import index as ix
from pix.nas.webapp import shell as w_shell


#: What an action is a decision *about*, for the orderings below. The same
#: pairing the page script keeps in `ACT_COLUMN`, plus the three that never
#: became filters.
ACT_COLUMN: dict[str, str] = {
    "tags": "tag", "people": "person", "access": "audience",
    "stack": "stacks", "delete": "deleted", "restore": "deleted",
}


def as_columns(acts: list[str]) -> list[str]:
    """Each action as the filter column it edits, first mention only.

    `delete` and `restore` are both decisions about `deleted` — one per side
    of the line, only ever on screen one at a time — so the second is not a
    second position for that question.
    """
    out: list[str] = []
    for act in acts:
        col = ACT_COLUMN.get(act, act)
        if col not in out:
            out.append(col)
    return out


def relative(order: Sequence[str], among: set[str]) -> list[str]:
    return [x for x in order if x in among]


def two_files(writable: Path, app_env: dict[str, Path]) -> None:
    """A second photograph, in master *and* in the index.

    The fixture puts one file in master and a stack needs at least two — and
    two of a kind. A stack says *these are the same shot*, so the fixture's
    clip cannot be the other half of one, and the page and the API both
    refuse to make it so.
    """
    import json

    from pix.nas import index as ix

    (writable / "b.mp4").write_bytes(b"fake")
    (writable / "b.jpg").write_bytes(b"fake")
    share = app_env["share"]
    (share / "meta" / "init_2026" / "b.jpg.json").write_text(json.dumps({
        "file": "b.jpg", "folder": "init_2026", "size": 30, "mtime_ns": 1,
        "exif": {"EXIF:DateTimeOriginal": "2026:08:30 15:39:00",
                 "XMP:EventAuto": "Italy - Sicily"},
    }), encoding="utf-8")
    ix.build(app_env["db"], meta_dir=share / "meta",
             master_dir=share / "master")


def three_files(writable: Path, app_env: dict[str, Path]) -> None:
    """A third file, in master *and* in the index.

    The cascade that keeps a stack together reads the index to find what is
    behind a file — so a file only on disk is a file it cannot see. That is the
    architecture working as intended (the index is behind, never wrong), and it
    made the first version of these tests lie.
    """
    import json

    from pix.nas import index as ix

    (writable / "b.mp4").write_bytes(b"fake")
    share = app_env["share"]
    # Two more photographs rather than one, because a stack is one kind of
    # thing: these tests are about rings and cascades and promotion, and the
    # fixture's clip was only ever standing in for a second photograph.
    for i, name in enumerate(("c.jpg", "d.jpg")):
        (writable / name).write_bytes(b"fake")
        (share / "meta" / "init_2026" / f"{name}.json").write_text(json.dumps({
            "file": name, "folder": "init_2026", "size": 30, "mtime_ns": 1,
            "exif": {"EXIF:DateTimeOriginal": f"2026:08:30 15:4{i}:00",
                     "XMP:EventAuto": "Italy - Sicily"},
        }), encoding="utf-8")
    ix.build(app_env["db"], meta_dir=share / "meta",
             master_dir=share / "master")


def burst(app_env: dict[str, Path], writable: Path, *names: str,
           at: str = "11:00:0") -> None:
    """Files a second apart on one camera, in master and in the index.

    `at` moves them away from an earlier call's, for when what is wanted is a
    photograph the app proposes nothing about.
    """
    import json

    from pix.nas import index as ix

    share = app_env["share"]
    for i, name in enumerate(names):
        (writable / name).write_bytes(b"fake")
        (share / "meta" / "init_2026" / f"{name}.json").write_text(json.dumps({
            "file": name, "folder": "init_2026", "size": 30, "mtime_ns": 1,
            "exif": {"EXIF:DateTimeOriginal": f"2026:08:30 {at}{i}",
                     "EXIF:Model": "iPhone 17 Pro"},
        }), encoding="utf-8")
    ix.build(app_env["db"], meta_dir=share / "meta",
             master_dir=share / "master")


def spread(app_env: dict[str, Path], writable: Path,
            dates: dict[str, str]) -> None:
    """Files on given days, in master and in the index."""
    import json

    from pix.nas import index as ix

    share = app_env["share"]
    for name, when in dates.items():
        (writable / name).write_bytes(b"fake")
        (share / "meta" / "init_2026" / f"{name}.json").write_text(json.dumps({
            "file": name, "folder": "init_2026", "size": 30, "mtime_ns": 1,
            "exif": {"EXIF:DateTimeOriginal": when},
        }), encoding="utf-8")
    ix.build(app_env["db"], meta_dir=share / "meta",
             master_dir=share / "master")


def folders_of(html: str) -> str:
    """Just the folders. The page script is inlined below them and mentions
    `/thumb/` and half the words on the card."""
    return html[html.index('id="grid"'):html.index("</div></main>")]


def undated_clip(app_env: dict[str, Path], writable: Path,
                  name: str) -> None:
    """Another video with no date of its own, in master and in the index."""
    (writable / name).write_bytes(b"fake")
    share = app_env["share"]
    (share / "meta" / "init_2026" / f"{name}.json").write_text(json.dumps({
        "file": name, "folder": "init_2026", "size": 20, "mtime_ns": 1,
        "exif": {"QuickTime:Duration": "5 s",
                 "XMP:EventAuto": "Italy - Sicily"}}), encoding="utf-8")
    ix.build(app_env["db"], meta_dir=share / "meta",
             master_dir=share / "master")


def dated(client: TestClient, name: str, override: str) -> None:
    r = client.post("/api/decide", json={
        "folder": "init_2026", "name": name, "date_override": override})
    assert r.status_code == 200, r.text


def targets(*names: str) -> list[dict[str, str]]:
    return [{"folder": "init_2026", "name": n} for n in names]


def render_for(name: str) -> Path:
    """The playable copy of one master file, made to exist."""
    from pix.nas import derive

    at = derive.render_path(derive.MASTER_DIR / "init_2026" / name)
    at.parent.mkdir(parents=True, exist_ok=True)
    at.write_bytes(b"h264 copy")
    return at


def shaped(app_env: dict[str, Path], writable: Path, name: str,
            w: int, h: int) -> None:
    """A file of known proportions, in master and in the index."""
    import json

    share = app_env["share"]
    (writable / name).write_bytes(b"fake")
    (share / "meta" / "init_2026" / f"{name}.json").write_text(json.dumps({
        "file": name, "folder": "init_2026", "size": 30, "mtime_ns": 1,
        "exif": {"EXIF:DateTimeOriginal": "2026:08:30 11:00:00",
                 "File:ImageWidth": w, "File:ImageHeight": h},
    }), encoding="utf-8")
    ix.build(app_env["db"], meta_dir=share / "meta",
             master_dir=share / "master")


def corner(html: str) -> str:
    """The mark in the top-left, with whatever it is wrapped in."""
    at = html.index('class="brand"')
    return html[html.index("<a", at - 40):html.index("</a>", at) + 4]


def stamp_shape(db: Path, shape: int) -> None:
    """Write a shape number into an index, as a build of that age would."""
    import sqlite3

    conn = sqlite3.connect(db)
    with conn:
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)",
                     (str(shape),))
    conn.close()


def pix(html: str) -> dict[str, Any]:
    """What the page script is told about the page — `window.PIX`, parsed."""
    at = html.index("window.PIX=") + len("window.PIX=")
    return cast("dict[str, Any]",
                json.loads(html[at:html.index(";</script>", at)]))


def cell_html(html: str, name: str) -> str:
    """One thumbnail's markup, whole — from its name to the next cell.

    Not to the first `</div>`: a cell holds its two lanes, and that is where
    the top one ends.
    """
    cell = html[html.index(f'data-name="{name}"'):]
    ends = [i for i in (cell.find('<div class="cell', 1),
                        cell.find("</section>")) if i > 0]
    return cell[:min(ends)] if ends else cell


def media_block(sheet: str, query: str) -> str:
    """One `@media` block's body, braces balanced.

    A rule's presence in the stylesheet says nothing; which block it is in is
    the whole of what these tests are about, and a nested block means the
    first closing brace is not the end.
    """
    at = sheet.index("@media " + query)
    depth, start = 0, sheet.index("{", at)
    for i in range(start, len(sheet)):
        depth += (sheet[i] == "{") - (sheet[i] == "}")
        if depth == 0:
            return sheet[start + 1:i]
    raise AssertionError(f"unclosed @media {query}")


def stacked(client: TestClient, app_env: dict[str, Path],
             writable: Path) -> None:
    """`y.jpg` behind `x.jpg`, as a stack somebody actually made."""
    burst(app_env, writable, "x.jpg", "y.jpg")
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "y.jpg",
        "stacked_under": "init_2026/x.jpg"})


def mixed(client: TestClient, writable: Path,
           app_env: dict[str, Path]) -> None:
    """A section shared with everybody, and one tag on part of it.

    *Everybody* is read off the listing rather than the four files added here:
    the fixture puts another in the same year, and a helper that shared only
    what it created would be testing a folder that is 80% shared while calling
    it whole — which is a test that fails for the one reason it must not, an
    untruth in its own setup.
    """
    burst(app_env, writable, "w.jpg", "x.jpg", "y.jpg", "z.jpg")
    every = client.get("/api/files?date=2026&stacks=firm").json()
    client.post("/api/decide/bulk", json={
        "add_audience": ["family"],
        "files": [{"folder": r["folder"], "name": r["name"]} for r in every]})
    client.post("/api/decide", json={
        "folder": "init_2026", "name": "z.jpg", "add_tags": ["beach"]})


def card_of(html: str) -> str:
    card = html[html.index('<a class="tile"'):]
    return card[:card.index("</a>") + 4]


def luminance(colour: str) -> float:
    raw = colour.lstrip("#")
    parts = [int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lit = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
           for c in parts]
    return 0.2126 * lit[0] + 0.7152 * lit[1] + 0.0722 * lit[2]


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def var(name: str) -> str:
    found = re.search(re.escape(name) + r":\s*(#[0-9a-f]+)", w_shell.STYLE)
    assert found is not None, name
    return found.group(1)


def event_named(writable: Path, name: str) -> str | None:
    """What one file calls its event, whole."""
    decision = decisions.read(writable / name)
    assert decision is not None, name
    return decision.event


def quote(value: str) -> str:
    """One filter value, as it appears in a URL."""
    return urllib.parse.quote(value, safe="")


def evented(client: TestClient, writable: Path,
             app_env: dict[str, Path]) -> None:
    """Two files under one event, one of them in a sub-event of it."""
    burst(app_env, writable, "e1.jpg", "e2.jpg")
    for name, event in (("e1.jpg", "Sicily"),
                        ("e2.jpg", "Sicily > Taormina")):
        client.post("/api/decide", json={
            "folder": "init_2026", "name": name, "event": event})


# --- reading a stylesheet as rules, not as text --------------------------------

def css_rules(sheet: str) -> list[tuple[str, str, dict[str, str]]]:
    """Every rule in a stylesheet as `(media, selector, declarations)`.

    Comments out, `@media` blocks followed one level down. A whole page is
    read for its `<style>` blocks, so a test can hand over either.
    """
    import re as _re

    if "<style>" in sheet:
        # Every block after an opening tag, to its closing one where there is
        # one — a test often hands over the page cut off before `</style>`.
        sheet = "\n".join(chunk.split("</style>")[0]
                          for chunk in sheet.split("<style>")[1:])
    sheet = _re.sub(r"/\*[\s\S]*?\*/", "", sheet)
    out: list[tuple[str, str, dict[str, str]]] = []

    def walk(text: str, media: str) -> None:
        i = 0
        while i < len(text):
            open_at = text.find("{", i)
            if open_at < 0:
                return
            head = text[i:open_at].strip()
            depth, j = 0, open_at
            while j < len(text):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            inner = text[open_at + 1:j]
            if head.startswith("@media"):
                walk(inner, head[len("@media"):].strip())
            elif head and not head.startswith("@"):
                decls: dict[str, str] = {}
                for part in inner.split(";"):
                    if ":" in part:
                        k, _, v = part.partition(":")
                        decls[k.strip()] = " ".join(v.split())
                    elif part.strip():
                        decls[part.strip()] = ""
                out.append((media, " ".join(head.split()), decls))
            i = j + 1

    walk(sheet, "")
    return out


def has_rule(sheet: str, selector: str, decls: str = "",
             media: str | None = None) -> bool:
    """Whether a rule for `selector` says everything `decls` says.

    Spacing and the order of a selector list do not count, nor does the
    order of the declarations. A declaration given without a value only asks
    that the property be set; the last value given may be the start of the
    actual one (`animation:menuopen`), the way the literal it replaces was.
    """
    def norm(sel: str) -> frozenset[str]:
        return frozenset(" ".join(s.split()) for s in sel.split(","))

    want: list[tuple[str, str]] = []
    for part in decls.split(";"):
        if not part.strip():
            continue
        k, _, v = part.partition(":")
        want.append((k.strip(), " ".join(v.split())))
    target = norm(selector)
    for m, sel, have in css_rules(sheet):
        if media is not None and m != media:
            continue
        if norm(sel) != target:
            continue
        if all(k in have and (not v or have[k] == v or have[k].startswith(v))
               for k, v in want):
            return True
    return False
