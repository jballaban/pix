"""The landing page's folders: one card per section of the library, what it
says about itself, and where clicking it leads.
"""

from __future__ import annotations

import sqlite3
from itertools import groupby

from pix import datestr
from pix.nas import decisions, index as ix
from pix.nas.webapp.app import Principal
from pix.nas.webapp.grid import browse_url, group_label, heading, section
from pix.nas.webapp.text import h
from pix.nas.webapp.vocab import (
    DRILL,
    GRID_GROUPS,
    KIND_HALVES,
    SPREAD_FILTER,
    SPREAD_LABEL,
)


def shelves(rows: list[sqlite3.Row], groups: list[str], view: ix.Filters,
             user: Principal,
             whole: dict[object, tuple[int, object, object]] | None = None,
             spread: dict[str, dict[tuple[object, ...],
                                    list[tuple[str, int]]]]
             | None = None) -> str:
    """The folders, under a heading for each level above them.

    `year › event` is a row of events under each year, not a flat list of
    cards each repeating which year it is in. The grid already reads that way
    and this is the same library, so it is the same shape: the outer levels
    are headings, and only the innermost is a folder.

    The heading is the grouping control here as it is there, and it carries
    one crumb per level — values for the levels above, and for the innermost
    the *name* of the cut, because that level has no single value: it is what
    the cards are. So `2025 › By event` says where you are and what you are
    looking at, and either half can be changed by clicking it.
    """
    inner = groups[-1] if groups else ""
    outer = groups[:-1]
    if not groups:
        return section(
            heading([], 0, len(rows), pick=False, cut=True),
            "".join(folder(r, groups, view, user, spread=spread)
                    for r in rows))
    out: list[str] = []
    for keys, run in groupby(rows, key=lambda r: tuple(
            r[f"grp{i}"] for i in range(len(outer)))):
        shelf = list(run)
        labels = [group_label(k, g, outer[:i], shelf[0])
                  for i, (k, g) in enumerate(zip(keys, outer))]
        labels.append(dict(GRID_GROUPS).get(inner, inner))
        out.append(section(
            heading(labels, len(groups), len(shelf), pick=False, cut=True),
            "".join(folder(r, groups, view, user, whole or {}, spread)
                    for r in shelf)))
    return "".join(out)


def spread_chips(kind: str, values: list[tuple[str, int]], n: int,
                  lead: tuple[str, int] | None = None) -> str:
    """What a section says about itself, one kind of value at a time.

    **The name and nothing else.** A share was printed beside each one while
    *undecided* was still a count in a sentence underneath, and the number was
    there to keep that reading alive. Once what is left became a chip of its
    own, `bob 5%` stopped answering anything anybody asks of a folder —
    *who is in here* and *what is left* are the questions, and neither of them
    is a percentage. It cost horizontal room on the one screen with none to
    spare, which is how it was noticed.

    The counts stay in the tooltip, where they cost nothing.

    **All of them, and the card grows.** The first version named three and
    finished with `+4`, which is the one thing a summary must not do: it says
    there is something else in there and refuses to say what, so the card
    stops being an answer and becomes a reason to open the folder — which
    is the errand it exists to save. A taller card is cheaper than that, and
    the ones with most in them are the ones worth reading.

    **One run, not three.** Each kind used to be its own flex box, so each
    started a fresh line however few chips were in it — three rows to say
    *family, Mom, beach*. They carry their own colour now, which is what lets
    them share one wrapping line and still be told apart: the order groups
    them by kind and the hue says which kind without a break to mark it.
    """
    if not n or not (values or lead):
        return ""
    # What is *not* decided, wearing the same clothes as what is. It was a
    # sentence of its own under the bar — *1 undecided* — which made the
    # one negative fact on the card the only one shaped differently from all
    # the positive ones. And *all decided* said nothing at all: a folder with
    # nothing left says it by having no such chip, the way a folder with no
    # tags says that by having no tags.
    col = SPREAD_FILTER.get(kind, "")

    def chip(value: str, count: int, cls: str = "", filter_on: str = "") -> str:
        # Opening the folder *and* narrowing it to this, which is the one
        # thing the card could say and the page could not then do. A chip
        # reading `undecided` is the part of an event still to work through,
        # and clicking it is how you get to exactly those.
        where = (f' data-col="{col}" data-val="{h(filter_on or value)}"'
                 if col else "")
        # **What kind, and how many.** It used to repeat the value, which is
        # the word already under the pointer, and then explain the click,
        # which the cursor and the hover already offer. What it could not say
        # without being asked is which of the four kinds this is — the
        # colour says that, once you know the colours.
        what = "No access yet" if cls == "none" else SPREAD_LABEL.get(kind, kind)
        files = f"{count:,} file" + ("" if count == 1 else "s")
        return (f'<i class="{cls or kind}"{where} '
                f'title="{what} — {files}">' + h(value) + "</i>")

    # `undecided` is not a value anything carries, it is the absence of one —
    # and the audience filter has a word for that, the same one its own chip
    # uses.
    first = ("" if lead is None or not lead[1] else
             chip(lead[0], lead[1], "none", ix.UNREVIEWED))
    return first + "".join(chip(v, c) for v, c in values)


def spread_row(chips: str) -> str:
    """All of a card's chips, in one run that wraps where it runs out."""
    return f'<span class="spread">{chips}</span>' if chips else ""


def folder(row: sqlite3.Row, groups: list[str], view: ix.Filters,
            user: Principal,
            whole: dict[object, tuple[int, object, object]] | None = None,
            spread: dict[str, dict[tuple[object, ...], list[tuple[str, int]]]]
            | None = None) -> str:
    """One section of the grid, drawn as what is worth knowing about it.

    Not a photograph. A cover was whichever file happened to be first, which
    told you what one picture in there looks like and nothing about the
    section — and a wall of unrelated pictures is harder to read than a wall
    of text, not easier. What you want before opening a folder is what is in
    it: how much, when, and how much of it you have not dealt with.
    """
    # Only what this card *is*. Which year it falls in is the heading above
    # it, and a card repeating its own shelf's name is a card saying one thing
    # and looking like it says two.
    last = len(groups) - 1
    name = (group_label(row[f"grp{last}"], groups[last], groups[:last], row)
            if groups else "Everything")
    href = drill(row, groups, view)
    n = int(row["n"])
    # An event that runs from February into March is a card under each, and a
    # folder saying *312 files* with nothing to say it is part of eleven
    # hundred is a folder describing the grouping rather than the library.
    # The count says both, which is also the shortest way to say it is split.
    outer = (whole or {}).get(row[f"grp{last}"]) if groups else None
    entire = outer[0] if outer else n
    videos = int(row["videos"] or 0)
    left = int(row["unreviewed"] or 0) if user.is_admin else 0
    # Not under a heading that already says it: grouped by day, the name *is*
    # the date, and printing it twice is a card that looks like it is telling
    # you two things.
    dated = not (groups and groups[-1] == "day")
    when = span(row["first_seen"], row["last_seen"]) if dated else ""
    # **And the dates say it too.** The count already said *312 of 1,100*, so
    # a card that was a fortnight of a three-week event said so about its
    # files and not about its days — two facts about the same split, one of
    # them told and one of them not. Same shape as the count, so the two read
    # as one sentence about one thing.
    across = span(outer[1], outer[2]) if (outer and dated) else ""
    if across and across != when:
        when = f'{when} <i>of</i> {across}'
        split_when = " split"
    else:
        split_when = ""
    inner = (
        f'<b class="name">{h(name)}</b>'
        # `when` may already carry its own markup, so it is not escaped
        # again here; every value inside it came through `_span`, which builds
        # from parsed dates and never from anything a person typed.
        + (f'<span class="when{split_when}">{when}</span>' if when else
           '<span class="when dim">no dates</span>' if dated else "")
        + ('<span class="n split" title="Split by the grouping above it — '
           f'{entire:,} files in all">{n:,} <i>of</i> {entire:,} files'
           if entire > n else
           f'<span class="n">{n:,} file{"" if n == 1 else "s"}')
        + (f'<i class="kinds">{videos:,} video{"" if videos == 1 else "s"}</i>'
           if videos else "")
        + "</span>"
        # Two readings in one bar, because they nest: the whole track is the
        # group this card is part of, the filled part is how much of it is
        # here, and inside that sits how much of *this* has been decided. A
        # card holding all of an event is a full bar; one holding a fortnight
        # of it is a quarter of one, and the green grows inside either as the
        # work gets done.
        # What is *in* it, not just how much of it is done. A thumbnail says
        # which tags and which audience it carries; this is the same sentence
        # for a folder, with the share that carries each — and what is
        # undecided is the first of them rather than a line of its own.
        + spread_row(
            "".join(
                spread_chips(kind, (spread or {}).get(kind, {}).get(
                    tuple(row[f"grp{i}"] for i in range(len(groups))), []), n,
                    lead=("undecided", left) if kind == "audience" else None)
                for kind in (("audience", "people", "tags") if user.is_admin
                             else ("people", "tags")))))
    if href is None:
        # Nothing to link to, rather than a link somewhere else. *No day* is
        # every file whose date stops at the month, and there is no filter that
        # says so — offering the month itself would open a folder holding files
        # this one does not.
        return (f'<div class="tile dead" title="There is no filter for this '
                f'one, so it cannot be opened on its own">{inner}</div>')
    # The same circle the thumbnails carry, for the same gesture one zoom
    # out: a folder is a set of files, and a decision about it is a decision
    # about them. Only on a folder that can be opened — one with no address
    # has no set to name either, so there is nothing to select.
    return (f'<a class="tile" href="{h(href)}">'
            f'<button class="pick" aria-label="Select this folder"></button>'
            f'{inner}</a>')


def span(first: object, last: object) -> str:
    """When a section happened, in as few words as it takes to say it.

    Written the way a person would: one day is a day, a fortnight in one month
    drops the month from the first end, and a year that appears twice is
    printed once.
    """
    start = datestr.parse_pix(str(first)) if first else None
    end = datestr.parse_pix(str(last)) if last else None
    if start is None or end is None:
        return ""
    if start.date() == end.date():
        return f"{start.day} {start:%B %Y}"
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day} – {end.day} {end:%B %Y}"
    if start.year == end.year:
        return f"{start.day} {start:%b} – {end.day} {end:%b %Y}"
    return f"{start.day} {start:%b %Y} – {end.day} {end:%b %Y}"


def imprecise(row: sqlite3.Row, name: str) -> str | None:
    """The date filter holding exactly a section with no `name` key, or None.

    Read off how precisely the section's files are dated: all undated is
    `undated`; all dated to one year, or one month, and no finer is
    `2025-*` or `2025-08-*`. A mix of the two has no filter.
    """
    if "pmin" not in row.keys():
        # A section without the reading: only a year nobody knows can still
        # be answered, because that is undated by definition.
        return ix.UNDATED if name == "year" else None
    low, high = row["pmin"], row["pmax"]
    if low is None or low != high:
        return None
    width = int(low)
    if width == datestr.NOTHING:
        return ix.UNDATED
    first, last = str(row["elo"] or ""), str(row["ehi"] or "")
    if width not in (datestr.YEAR, datestr.MONTH) or not first \
            or first[:width] != last[:width]:
        return None
    return f"{first[:width]}-*"


def drill(row: sqlite3.Row, groups: list[str],
           view: ix.Filters) -> str | None:
    """Where one folder leads: this view, plus what the folder is.

    `None` where the section cannot be said as a filter. *No day* and *no
    month* mean *dated less precisely than that*, and they open as the one
    date filter that says so (`_imprecise`) — or, where their files are dated
    to different widths, not at all. Sending those to the year would open a
    folder with more in it than the one that was clicked, which is worse than
    a folder that does not open.
    """
    patch: dict[str, str | list[str] | None] = {}
    for i, name in enumerate(groups):
        key = row[f"grp{i}"]
        column = DRILL.get(name)
        if column is None:
            return None
        if key is None:
            # No year, no month or no day: the files are dated less
            # precisely than this level cuts. That is one filter when they
            # are all dated alike — `undated`, or `2025-*` for the year and
            # no month — and otherwise none, because a folder that opened
            # onto more than it counted would be worse than one that does
            # not open.
            exact = imprecise(row, name)
            if exact is None:
                return None
            patch["date"] = exact
            continue
        patch[column] = str(key)
        # A type folder is every box its kind is made of.
        if name == "kind":
            patch[column] = list(KIND_HALVES.get(str(key), (str(key),)))
        # A folder of an event *itself*, made beside one folder per part of
        # it: the files directly in the event and no others. Asking for the
        # event plainly would open the whole trip, which is more than the
        # folder counted — the one place a folder's link and a folder's count
        # could disagree about what is in it.
        if (name == "subevent" and key != ix.NO_EVENT
                and decisions.EVENT_SEP not in str(key)):
            patch[column] = f"{key}{decisions.EVENT_SEP}{ix.NO_EVENT}"
    return browse_url(view, patch)
