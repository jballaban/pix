"""How the grid is drawn: a thumbnail and everything it carries, the sections
and their headings, the stack and clip badges, the action bar and the
viewer's own row, and the addresses they link to.
"""

from __future__ import annotations

import sqlite3
from itertools import groupby
from typing import cast, Sequence

from pix import datestr
from pix.nas import clips, decisions, delivery, index as ix, paths, webroots
from pix.nas.webapp.app import Principal, usual
from pix.nas.webapp.marks import mark
from pix.nas.webapp.permissions import may
from pix.nas.webapp.shell import act
from pix.nas.webapp.text import dur, h, js, q, split


def stack_url(key: str, back: str = "") -> str:
    """One stack's page, and the way out of it.

    `back` is the address it was opened from, carried rather than guessed.
    The browser's own history would do it on the way out and does not on the
    way in: a stack reached from a bookmark, a shared link or the operation
    log has nothing behind it, and *leave this stack* still has to mean
    something. It is also what makes the way out survive a write — going back
    to a page that has just stopped being true is how somebody ends up
    reloading by hand.
    """
    folder, _, name = key.partition("/")
    out = f"/stack/{q(folder)}/{q(name)}"
    return out + (f"?back={q(back)}" if back else "")


def browse_url(view: ix.Filters,
                patch: dict[str, str | list[str] | None]) -> str:
    """The grid, at this view plus `patch`. The page's own `url()` in Python."""
    query = {**view_dict(view), **patch}
    # Joined with a bare `&`: this is a URL, and the one place it becomes
    # markup escapes it. Building it pre-escaped produced `&amp;amp;` and a
    # link that carried its second filter as part of the first one's value.
    pairs = [(k, x) for k, v in query.items() if v
             for x in (v if isinstance(v, list) else [v])]
    return "/browse" + (
        "?" + "&".join(f"{k}={q(str(x))}" for k, x in pairs) if pairs else "")


def totals(conn: sqlite3.Connection, view: ix.Filters,
            groups: list[str]) -> dict[tuple[object, ...], int]:
    """Every section's whole count, by its key — for headings that say how
    many are in the section, not how many of them have arrived."""
    if not groups:
        return {}
    n = len(groups)
    return {tuple(r[f"grp{i}"] for i in range(n)): int(r["n"])
            for r in ix.sections(conn, view, groups=groups, limit=100_000)}


def cuts_badge(row: sqlite3.Row) -> str:
    """*5 clips · 1 still* on a video that has them, opening just those.

    Beside the stack badge, because it is the same kind of fact: there are
    more of these, somewhere else, and this is the way to them. Only what
    this viewer would see when it opens (`ix._cuts_cols`).
    """
    keys = row.keys()
    clips_n = int(row["cut_clips"] or 0) if "cut_clips" in keys else 0
    stills_n = int(row["cut_stills"] or 0) if "cut_stills" in keys else 0
    if not clips_n and not stills_n:
        return ""
    words = [f"{n} {word}{'' if n == 1 else 's'}"
             for n, word in ((clips_n, "clip"), (stills_n, "still")) if n]
    key = f'{row["folder"]}/{row["name"]}'
    href = f"/browse?cuts={q(key)}&group=none"
    return (f'<a class="cuts" href="{h(href)}" '
            f'title="The clips and stills cut from this video">'
            f'{h(" · ".join(words))}</a>')


def actions(user: Principal, *, folders: bool = False) -> str:
    """The edit bar — as much of it as this person gets.

    **The family curates** (§8): tagging and naming are the whole point of the
    app, and the whole bar being an administrator's meant a household member
    logged in, saw a circle on every thumbnail, selected things, and watched
    nothing happen. Which of the two readings that is — *no permission* or
    *broken* — was not on screen anywhere.

    What they do not get is `access` and `purge`, and the reason is the same
    for both: they are the two that somebody else noticing cannot undo. Access
    is the only control that can show a photograph to a person who should not
    see it, and purge ends the file. Delete is theirs, because it is soft, and
    restore is not, because `/history` is not — see `HOUSEHOLD`.

    Not merely hidden: the endpoints refuse a field this person may not write,
    whatever their browser was showing them. This is so the page does not
    offer a control that would fail, which reads as brokenness rather than as
    policy — and so that hiding the control is never what is holding the line.

    **Two sets, shown by what is selected rather than by what is filtered.**
    A deleted file cannot be deleted again and a living one cannot be
    restored, so offering either would be offering a button that does nothing.
    Which of them is on screen follows the selection, not the `deleted` chip:
    with *Including deleted* on, a selection can hold both kinds, and then
    both sets are offered and each acts only on the files it means.

    The row itself is always here. It carries the count and the tick, so it
    has something to say with nothing selected — and a row that came and went
    would move the whole grid under the pointer on the first click.

    **One rule separates them, not several.** What a file *is* — its event, its
    tags, its date, who may see it, and which of them speaks for the rest —
    runs together in the order those questions get asked. What happens *to* it
    is the cluster after the bar. Bars between every pair said there were four
    groups when there are two.

    The stack actions join the first cluster rather than earning a bar of their
    own, and not only for tidiness: they are usually hidden, and a separator is
    not, so a bar around them would hang there beside nothing.

    **A folder bar is four of these and no more.** One zoom out, the selection
    is sets rather than photographs, and the question each control asks has to
    survive that. *Event*, *Tags*, *Access* and *Download* are statements about
    a set and read the same either way — naming a run of days as one event
    is the gesture the whole backlog turns on (§8). The four stack actions
    are not: every one of them needs *a photograph* — which of these takes
    speaks for the others, whether they are one moment — and across an
    event there is no such question to ask.
    """
    if folders:
        return f"""<div class="row" id="actions">
  <button id="selall" class="tick" title="Select all"
          aria-label="Select all"></button>
  <span class="count" id="selcount" style="margin:0"></span>
  <span class="grp" data-side="live" hidden>
    {act("event", "Event&hellip;", user=user)}
    {act("tags", "Tags&hellip;", user=user)}
    {act("access", "Access&hellip;", user=user)}
    {act("download", "Download", user=user)}
  </span>
</div>"""
    return f"""<div class="row" id="actions">
  <button id="selall" class="tick" title="Select all" aria-label="Select all"></button>
  <span class="count" id="selcount" style="margin:0"></span>
  <span class="grp" data-side="live" hidden>
    {act("event", "Event&hellip;", user=user)}
    {act("tags", "Tags&hellip;", user=user)}
    {act("people", "People&hellip;", user=user)}
    {act("date", "Date&hellip;", user=user)}
    {act("access", "Access&hellip;", user=user)}
    {act("stack", "Stack", user=user)}
    {act("top", "Make top", user=user)}
    {act("unstack", "Unstack", user=user)}
    {act("nostack", "Not a stack", user=user)}
    {act("splice", "Splice", user=user)}
    {act("download", "Download", user=user)}
    <span class="sep"></span>
    {act("delete", "Delete", "danger", user=user)}
  </span>
  <span class="grp" data-side="gone" hidden>
    {act("restore", "Restore", user=user)}
    {act("purge", "Purge&hellip;", "danger", user=user)}
  </span>
  <span class="grp" data-side="choose" hidden>
    <b>Click the one to show</b>
    <button id="choosecancel">Cancel</button>
  </span>
</div>"""


def chips_html(cls: str, values: list[str]) -> str:
    """One chip per value, each truncated with the whole thing as a tooltip.

    Individually rather than as a run of text, because a thumbnail is 150px
    and three role names are not: a single line just gets cut off mid-word
    with no way to find out what it said. The container carries the full list
    too, for when there are more chips than fit.
    """
    if not values:
        return ""
    chips = "".join(f'<i title="{h(v)}">{h(v)}</i>' for v in values)
    return f'<span class="{cls}" title="{h(", ".join(values))}">{chips}</span>'


def sections(rows: list[sqlite3.Row], groups: list[str],
              view: ix.Filters | None = None,
              totals: dict[tuple[object, ...], int] | None = None,
              total: int | None = None) -> str:
    """The cells, with one heading wherever the section changes.

    **One heading, not one per level.** Nested headings meant an indent for
    every level and a row of chrome for each, and the deeper ones said less and
    less. A section's identity is the whole path — *2026 › July › Sports Day* —
    so the heading says that, once, and the levels in it are the controls.

    Headings are grid items spanning every column, so one flow holds headings
    and thumbnails; arrow-key movement walks straight through rather than having
    to know the sections are there.

    There is always at least one heading, even ungrouped: the heading *is* the
    control, so a grid without one would offer no way to start.
    """
    said = "subevent" in groups
    if not groups:
        return section(heading([], 0, total if total is not None
                                 else len(rows)),
                        "".join(cell(r, view, groups=groups) for r in rows),
                        key=())

    out: list[str] = []
    for keys, run in groupby(rows, key=lambda r: tuple(
            r[f"grp{i}"] for i in range(len(groups)))):
        batch = list(run)
        labels = [group_label(k, g, groups[:i], batch[0])
                  for i, (k, g) in enumerate(zip(keys, groups))]
        # The whole section's count, not the part of it on this page: the
        # grid arrives a page at a time, and a heading saying 240 over a day
        # of 1,100 photographs would be describing the page, not the day.
        out.append(section(
            heading(labels, len(groups),
                     (totals or {}).get(keys, len(batch))),
            "".join(cell(r, view, said=said, groups=groups)
                    for r in batch), key=keys))
    return "".join(out)


def section(heading: str, items: str,
             key: tuple[object, ...] = ()) -> str:
    """One heading and everything under it, in a box of their own.

    The heading used to be a grid item spanning every column, and the cells
    its siblings — one flow, which is tidier markup and is why it was
    written that way. It cannot stick, though: a grid item is confined to its
    own grid area for the purpose of `position: sticky`, and a heading's grid
    area is the single row it sits in, so it has nowhere to travel.

    Given a box it has the section to travel through, and the next heading
    arrives and pushes it off — which is what *stays at the top while you are
    in it* means, done by the browser rather than by a scroll handler.

    Nothing else about the page moved. The columns still line up across
    sections, because every `.cells` resolves the same `auto-fill` over the
    same width; a heading already started a new row, because it spanned them
    all; and the cells are still in document order, so arrow-key movement
    walks straight through the sections without knowing they are there.
    """
    # The section's key, so a page arriving later can tell whether it
    # carries on the section already at the bottom of the grid or starts the
    # next one — the same day must not get a second heading.
    return (f'<section class="sect" data-key="{h(js(list(key)))}">'
            f'{heading}<div class="cells">{items}</div></section>')


def heading(labels: list[str], levels: int, count: int, *,
             pick: bool = True, cut: bool = False) -> str:
    """One section heading, which is also how grouping is changed.

    Each crumb is two controls: the name changes that level, the `Ã—` drops it.
    Removal lives here rather than inside the menu because *take this away* is
    a thing you should be able to see, not something to go and find.
    """
    if not labels:
        crumbs = ('<span class="crumb" data-level="0">'
                  '<button class="grpname">Ungrouped</button></span>')
    else:
        crumbs = '<span class="crumbsep">&rsaquo;</span>'.join(
            f'<span class="crumb" data-level="{i}">'
            f'<button class="grpname">{h(label)}</button>'
            f'<button class="rmgrp" title="Remove this grouping">&times;</button>'
            f'</span>'
            for i, label in enumerate(labels))
    # On a shelf the last crumb names the *cut* — "By event" — and it is the
    # same two words over every shelf on the page. What says where you are is
    # the value in front of it, so the emphasis runs the other way round.
    shelf = " shelf" if cut else ""
    add = ("" if levels >= 3 else
           '<button class="addgrp" title="Add a grouping inside this one">'
           "+</button>")
    return (f'<h3 class="group{shelf}">'
            + ('<button class="grppick" title="Select this group" '
               'aria-label="Select this group"></button>'
               if pick else "")
            + f'<span class="crumbs">{crumbs}</span>{add}'
              f'<span class="dim">{count:,}</span>'
              f'</h3>')


def group_label(key: object, group: str, outer: Sequence[str] = (),
                 first: sqlite3.Row | None = None) -> str:
    """A heading a person reads, not a sort key.

    `outer` is the coarser levels already shown to the left, so a crumb does
    not repeat what the path has said: under *2025*, the month is **January**
    rather than *January 2025*, and under that the day is **Saturday 4**.

    `first` is the section's first file, for the one grouping whose key is not
    something anybody would want to read. A stack is identified by the
    photograph that speaks for it, and a generated name carries the date, the
    camera and the extension — so the heading was 40 characters of machinery
    where the useful part is *when*.
    """
    if key is None or key == "":
        # Named for what is missing rather than for the date as a whole: a file
        # dated to its month lands here when the grid is cut by day, and it is
        # not undated â€” it has no *day*. Calling that "No date" would deny what
        # is actually known about it.
        return {"day": "No day", "month": "No month", "year": "No date",
                "stack": "Not in a stack"}.get(group, "None")
    text = str(key)
    if group == "stack":
        # The moment, because that is what a stack is: one photograph taken
        # several times. The name of the file that speaks for it is machinery.
        moment = (datestr.parse_pix(str(first["effective_date"]))
                  if first is not None and first["precision"] >= datestr.FULL
                  else None)
        if moment is None:
            # No usable clock, so the only honest label left is the file that
            # speaks for the section. The folder is the path the heading
            # already sits in, and repeating it would push the one part that
            # differs off the end of the line.
            return text.rpartition("/")[2]
        if "day" in outer:
            return f"{moment:%H:%M}"
        return (f"{moment:%A} {moment.day} {moment:%B %Y}, "
                f"{moment:%H:%M}")
    if group == "day":
        moment = datestr.parse_pix(text + "-00:00:00")
        if not moment:
            return text
        # Composed rather than one strftime: `%-d` drops the leading zero on
        # Linux and is simply invalid on Windows, and this runs on both.
        if "month" in outer:
            return f"{moment:%A} {moment.day}"
        if "year" in outer:
            return f"{moment:%A} {moment.day} {moment:%B}"
        return f"{moment:%A} {moment.day} {moment:%B %Y}"
    if group == "month":
        moment = datestr.parse_pix(text + "-01-00:00:00")
        if not moment:
            return text
        return moment.strftime("%B" if "year" in outer else "%B %Y")
    return text


def access_html(shared: list[str]) -> str:
    """What a thumbnail says about who can see it.

    Three states, and only two of them are visible:

    - **nobody** â€” a small mark, because that is the work still to do;
    - **the usual audience, exactly** â€” nothing at all. If nine files in ten
      say `family`, printing `family` on nine thumbnails in ten is noise
      that tells you nothing you did not already assume;
    - **anything else** â€” named, because that is the exception and the whole
      reason to look.

    The usual audience is a setting rather than a hard-coded name: which one
    is usual is a fact about a household, not about the software.
    """
    if not shared:
        return ('<span class="unshared" title="Nobody has access yet">'
                '</span>')
    ordinary = usual()
    unusual = [a for a in shared if a != ordinary]
    return chips_html("who", unusual)


def people_html(people: list[str], view: ix.Filters) -> str:
    """Who is **in** the photograph, on the thumbnail.

    It was the one fact a cell carried and never showed. The name was in
    `data-people` for the script, on the folder card, in the viewer rail, in
    its own filter and its own bulk action — everywhere except the thing you
    are actually looking at while you decide.

    Never collapsed into access, and drawn on its own line above it. *Pictures
    of Mum* and *pictures Mum may see* are opposite questions that take the
    same kind of word, and on a 150px tile the colour should not have to carry
    the whole difference.

    **The name the view is already filtered to is left off.** Filtered to
    `person:Ana` every thumbnail on screen says Ana, and a chip that is true
    of everything is furniture — the same rule `_access_html` applies to the
    usual audience and `_part_html` to an event the heading already names.
    """
    return chips_html("folk", [p for p in people if p != view.person])


def part_html(row: sqlite3.Row, view: ix.Filters, said: bool) -> str:
    """Which part of its event this file is, on the thumbnail.

    Not where the view has already said it: grouped by event and sub-event
    every heading names one, and filtered to a whole name every thumbnail
    under it carries the same one. A chip that is true of everything on
    screen is furniture.

    Grouping by *event* is not that — it names the trip, and which part of
    the trip is exactly what still differs from cell to cell.
    """
    whole = row["event"]
    leaf = decisions.split_event(whole)[1]
    if said or not leaf or view.event == whole:
        return ""
    return (f'<span class="part" title="Sub-event &mdash; {h(str(whole))}">'
            f'{h(leaf)}</span>')


def cell(row: sqlite3.Row, view: ix.Filters | None = None, *,
          said: bool = False, groups: Sequence[str] = ()) -> str:
    mark = stack_badge(row, view or ix.Filters(), groups)
    tags = split(row["tags"])
    shared = split(row["audience"])
    # Newline-joined, matching what the client splits on. A stray control byte
    # had crept in here from a shell heredoc, so multiple tags arrived at the
    # page as one unsplittable blob.
    nl = chr(10)
    return (
        f'<div class="cell{" gone" if row["deleted"] else ""}'
        f'{" marked" if mark else ""}" '
        f'data-folder="{h(row["folder"])}" '
        f'data-name="{h(row["name"])}" data-kind="{h(row["kind"])}" '
        f'data-audience="{h(nl.join(shared))}" '
        f'data-event="{h(row["event"] or "")}" '
        f'data-tags="{h(nl.join(tags))}" '
        f'data-people="{h(nl.join(split(row["people"])))}" '
        f'data-date="{h(str(row["effective_date"] or "no date"))}" '
        f'data-deleted="{"1" if row["deleted"] else ""}" '
        f'data-under="{h(row["stacked_under"] or "")}" '
        # What it is only *proposed* to be behind, which is a different fact
        # and was missing: opened, a guessed stack listed its members through
        # this column and the page could not see it, so nothing in there was
        # in a stack as far as the bar was concerned and there was no way to
        # say which of them should be the one that shows.
        #
        # Only while the view folds guesses, the same rule the count follows:
        # with them off these are ordinary photographs sitting in the grid on
        # their own, and nothing is behind anything.
        f'data-proposed-under="'
        f'{h(row["suggested_under"] or "") if (view or ix.Filters()).folds_guesses else ""}" '
        f'data-ar="{squareness(row)}" '
        f'data-clip="{clip_attr(row)}" '
        f'data-splice="{h(splices(row))}" '
        f'data-clips="{row["clips"] if "clips" in row.keys() else 0}" '
        f'data-copy="{"1" if has_render(row) else ""}" '
        # A video `process` has not been over yet cannot be downloaded
        # (`delivery.ready`); the page leaves it out of a selection and says so.
        f'data-unready="{"" if delivery.ready(row["kind"], row["content_hash"]) else "1"}" '
        f'data-behind="{row["behind"] or 0}" '
        f'data-proposed="{guessed(row, view or ix.Filters())}">'
        f'<img loading="lazy" src="/thumb/{q(row["folder"])}/{q(row["name"])}">'
        # Two lanes, each a run of optional facts that wraps from its own
        # edge inward, and the right-hand end of each held by what is always
        # there. Drawn in the default arrangement (`_INFO`); the page script
        # moves each fact to the lane the viewer chose (`placeInfo`).
        + lane("top", chips_html("tags", tags),
                cuts_badge(row) + clip_badge(row, view or ix.Filters())
                + mark
                + '<button class="pick" aria-label="select"></button>')
        + lane("bot",
                people_html(split(row["people"]), view or ix.Filters())
                + access_html(shared)
                + part_html(row, view or ix.Filters(), said),
                f'<span class="badge">{dur(row["duration"])}</span>'
                if row["kind"] == "video" else "")
        + "</div>"
    )


def lane(where: str, flow: str, fixed: str) -> str:
    """One edge of a thumbnail: the facts that flow, then the ones that stay."""
    return (f'<div class="ov {where}"><span class="lane">{flow}</span>'
            f'<span class="fix">{fixed}</span></div>')


def clip_attr(row: sqlite3.Row) -> str:
    """A clip's range as the page reads it — `start,end` in seconds — or
    nothing for a file that is not a clip."""
    if "clip_of" not in row.keys() or row["clip_of"] is None:
        return ""
    # With a file of its own it plays as itself, from its first frame — a
    # range applied to that would seek past what it is.
    if row["size"] is not None:
        return ""
    return f'{float(row["clip_in"] or 0):g},{float(row["clip_out"] or 0):g}'


def splices(row: sqlite3.Row) -> str:
    """The video *Splice* on this cell opens — its own name, or for a clip
    its source's — or nothing where there is nothing to cut."""
    of = row["clip_of"] if "clip_of" in row.keys() else None
    if of is not None:
        return str(of)
    name = str(row["name"])
    return name if clips.can_splice(name, str(row["kind"])) is None else ""


def clip_badge(row: sqlite3.Row, view: ix.Filters) -> str:
    """Says that this was cut from a video, and which kind of cut — to
    someone who may see that video, and to nobody else (`ix._source_seen`).
    To anyone else a clip is simply a video."""
    if "clip_of" not in row.keys() or row["clip_of"] is None:
        return ""
    if view.viewer is not None and not (
            "source_seen" in row.keys() and row["source_seen"]):
        return ""
    still = row["clip_in"] == row["clip_out"]
    word = "Still" if still else "Clip"
    return (f'<span class="clip-mark" title="{word} from '
            f'{h(row["clip_of"])}">{word}</span>')


def stack_badge(row: sqlite3.Row, view: ix.Filters,
                 groups: Sequence[str] = ()) -> str:
    """What a thumbnail says about the stack it is part of.

    Two different marks for two different questions. Where the others are out
    of sight, on the file that speaks for them: **how many**, as a link,
    because opening a stack is a view of the library like any other. Only when
    it has anything behind it — a count of one is a photograph, not a stack.

    Where the stack is open — one of them by address, or all of them by
    grouping — on the file that is doing the speaking: **which one**. Everything
    in there looks alike, which is the whole reason they were stacked, so
    without this there is nothing to tell you which one the grid outside will
    show. Not the count again: a depth badge beside the very files it counts
    reads as a stack within a stack, and it would link to where you already
    are.

    A guess counts only where the view is folding guesses. With that off the
    others are on screen as themselves, and a badge saying two photographs are
    here would be the app describing a stack it has not been allowed to make.
    """
    key = f'{row["folder"]}/{row["name"]}'
    # How the library is being read, which the view does not carry: it is not
    # a filter, it is the shape of the page, and a stack opened out of a grid
    # cut by event and day should come back to one.
    grouped = ",".join(groups) or "none"
    behind = row["behind"] or 0
    n_guessed = guessed(row, view)
    if view.within or view.unfold:
        speaks = (key == view.within if view.within else
                  not row["stacked_under"] and not row["suggested_under"])
        return ('<span class="top-mark" title="This is the one shown '
                'outside the stack">Top</span>'
                if speaks and behind + n_guessed else "")
    n = behind + n_guessed
    if not n:
        return ""
    # Drawn differently when any of it is the app's own guess, because the two
    # numbers answer different questions. A decided count says *somebody put
    # these together*; a guessed one says *these look alike, and nobody has
    # said yet* — and a curator deciding what to trust needs to see which is
    # which without opening it.
    # **A page, not a filter.** This was `/browse?within=…` — the grid with one
    # more thing narrowing it — and the takes of one photograph are not a view
    # of the library. Everything the grid brings with it was wrong in there:
    # eleven filters over eight frames of one moment, a grouping control for a
    # section that is the whole page, and a *Stack* button offering to stack
    # what is already a stack. See `stack_page`.
    #
    # It carries where it was opened from, because the way out has to be the
    # way in reversed: the grid it came from, filters and grouping and all.
    return (f'<a class="stack{" guessed" if n_guessed else ""}" '
            f'href="{h(stack_url(key, browse_url(view, {"group": grouped})))}" '
            f'title="{n + 1} photographs '
            f'{"that look alike — nobody has said yet" if n_guessed else ""}'
            f'{"" if n_guessed else "stacked here"}">'
            f'{n + 1}</a>')


def has_render(row: sqlite3.Row) -> bool:
    """Whether a playable copy of this file exists and differs from it.

    Only for the clips a browser will not play as they are. For every
    photograph here, and the third of the clips that were already H.264, the
    original *is* the playable file — so *original or copy* is not a question
    to ask about them, and the page only asks it where there is an answer.
    """
    if row["kind"] != "video":
        return False
    try:
        return paths.render_path(
            webroots.MASTER_DIR / str(row["folder"]) / str(row["name"]),
            webroots.RENDER_DIR).is_file()
    except OSError:
        return False


def squareness(row: sqlite3.Row) -> str:
    """The short edge over the long one, or `1` where nothing is known.

    The grid shows squares, so a thumbnail is cropped to its short edge — but
    the tiers are capped on the **long** one. A 16:9 frame in the 1000px tier
    is 1000x563, and the square taken out of it is 563 across: a bit over half
    the number the tier is named for. That is why a video thumbnail went soft
    a size before a photograph did, and why the page cannot pick a tier from
    the cap alone.
    """
    w, h = row["width"] or 0, row["height"] or 0
    if not w or not h:
        return "1"
    return f"{min(w, h) / max(w, h):.3f}"


def guessed(row: sqlite3.Row, view: ix.Filters) -> int:
    """How many files this one speaks for **in this view**.

    Nothing, unless the view is folding the app's guesses. A guess changes
    nothing about the library until somebody turns it on — so with it off the
    others are on screen as themselves, and this file has nobody behind it.

    Read by the badge *and* by the cell the page is built from, because the
    two disagreeing is the whole of a bug worth not having again: the badge
    showed nothing and the grid treated the file as a stack, so *Stack* opened
    it and more photographs came back than had been selected.
    """
    return count(row, "proposed") if view.folds_guesses else 0


def count(row: sqlite3.Row, column: str) -> int:
    """A counted column, where the query that produced the row had one."""
    try:
        return int(row[column] or 0)
    except IndexError:
        return 0


def view_dict(view: ix.Filters) -> dict[str, str | list[str] | None]:
    """The view as the address spells it — and as the page script reads it.

    Several values are a list; one is a string, as it always was. `deleted`
    is turned back into the boxes the bar offers rather than the two modes
    the index works in, so the page and the address say the same thing.
    """
    out: dict[str, str | list[str] | None] = {}
    for name in ix.Filters.NAMES:
        got = ix.picks(cast("ix.Pick", getattr(view, name)))
        out[name] = (None if not got else got[0] if len(got) == 1
                     else list(got))
    sides: dict[str, str | list[str]] = {"only": "gone",
                                         "with": ["gone", "live"]}
    out["deleted"] = sides.get(str(view.deleted))
    out["apart"] = "1" if view.apart else None
    return out


def viewacts(user: Principal, *, stack: bool = False) -> str:
    """What can be done to the one photograph filling the screen.

    Everything the bar does that makes sense of a single file, so that seeing
    something worth acting on is not a reason to leave it: name its event,
    tag it, say who is in it, fix its date, decide who sees it, cut it, save
    it, bin it. The stack actions are not here — each is a question about
    several photographs — except *Show this one* on a stack's own page, which
    is the question that page is for.

    The same buttons as the bar, by the same rules (`_act`, `may`), marked
    `data-vact` rather than `data-act` so the bar's wiring cannot pick them
    up. The page script points the bar's own actions at the file on show
    (`viewAct`), so a menu here is the same menu, with the same suggestions,
    the same confirmation and the same History line.

    Two sets, like the bar's: a binned file is restored or purged, never
    tagged.
    """
    live = "".join(act(a, w, c, user=user, attr="data-vact") for a, w, c in (
        ("event", "Event&hellip;", ""), ("tags", "Tags&hellip;", ""),
        ("people", "People&hellip;", ""), ("date", "Date&hellip;", ""),
        ("access", "Access&hellip;", "")))
    gone = "".join(act(a, w, c, user=user, attr="data-vact") for a, w, c in (
        ("restore", "Restore", ""), ("purge", "Purge&hellip;", "danger")))
    bin_ = act("delete", "Delete", "danger", user=user, attr="data-vact")
    return ('<div id="viewacts">'
            + ('<button id="viewtop" hidden></button>' if stack else "")
            + f'<span class="vgrp" data-side="live">{live}</span>'
            + viewsplice(user)
            + f'<a id="viewget" class="who-link" download>{mark("get", 19)}</a>'
            + f'<span class="vgrp" data-side="live">{bin_}</span>'
            + f'<span class="vgrp" data-side="gone" hidden>{gone}</span>'
            + '</div>')


def viewsplice(user: Principal) -> str:
    """Splice, from the preview: an administrator's, like the action."""
    if not may(user, "splice"):
        return ""
    return (f'<a id="viewsplice" class="who-link" hidden title="Splice" '
            f'aria-label="Splice">{mark("splice", 19)}</a>')
