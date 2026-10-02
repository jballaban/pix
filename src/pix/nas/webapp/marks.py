"""The app's drawings: the glyph each filter and action is drawn with, the logo
and favicon, and the few chrome icons. Inline SVG, as strings, so a page
needs no icon font and no second request.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote


def folder_path(x: float, y: float, w: float = 16, h: float = 12.4,
                 r: float = 2.4) -> str:
    """One folder outline, drawn exactly where a card of the same size sits.

    Same box as the cards in `_FILES_MARK`, so the two marks are the same
    three shapes at the same three offsets and only their *kind* differs —
    which is the whole point of a toggle you read at a glance.
    """
    # `:g` throughout: plain arithmetic on these puts 20.400000000000002 into
    # the markup, which renders identically and reads like a mistake.
    return (f"M{x + r:g} {y:g}h5.2l1.4 1.8H{x + w - r:g}"
            f"a{r:g} {r:g} 0 0 1 {r:g} {r:g}V{y + h - r:g}"
            f"a{r:g} {r:g} 0 0 1 {-r:g} {r:g}H{x + r:g}"
            f"a{r:g} {r:g} 0 0 1 {-r:g} {-r:g}V{y + r:g}"
            f"a{r:g} {r:g} 0 0 1 {r:g} {-r:g}z")


#: The library as files: three photographs, the front one showing.
FILES_MARK = (
    '<svg viewBox="0 0 28 28" width="26" height="26" aria-hidden="true">'
    '<rect x="9" y="3.6" width="16" height="12.4" rx="2.4" fill="none" '
    'stroke="var(--dim)" stroke-width="1.4" opacity=".45"/>'
    '<rect x="6" y="7" width="16" height="12.4" rx="2.4" fill="none" '
    'stroke="var(--dim)" stroke-width="1.4" opacity=".75"/>'
    '<rect class="front" x="3" y="10.4" width="16" height="12.4" rx="2.4" '
    'fill="var(--panel)" stroke="var(--fg)" stroke-width="1.5"/>'
    '<circle cx="7.6" cy="14.4" r="1.5" fill="var(--top)"/>'
    '<path d="M4.4 20.6 L8.6 16.6 L11.4 19.2 L13.6 17.2 L17.6 20.8" '
    'fill="none" stroke="var(--accent)" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>')


#: The same library as folders: the same three shapes, with tabs.
FOLDERS_MARK = (
    '<svg viewBox="0 0 28 28" width="26" height="26" aria-hidden="true">'
    f'<path d="{folder_path(9, 3.6)}" fill="none" stroke="var(--dim)" '
    'stroke-width="1.4" opacity=".45"/>'
    f'<path d="{folder_path(6, 7)}" fill="none" stroke="var(--dim)" '
    'stroke-width="1.4" opacity=".75"/>'
    f'<path class="front" d="{folder_path(3, 10.4)}" fill="var(--panel)" '
    'stroke="var(--fg)" stroke-width="1.5" stroke-linejoin="round"/>'
    '<path d="M6.4 17.6H13.2" stroke="var(--accent)" stroke-width="1.5" '
    'stroke-linecap="round"/>'
    '<path d="M6.4 20.2H10.6" stroke="var(--dim)" stroke-width="1.5" '
    'stroke-linecap="round"/></svg>')


def logo_mark(size: int) -> str:
    """The logo: one photograph, on its own.

    Deliberately *not* the three-shape marks above. Those became a control the
    moment the corner started toggling between files and folders, and a control
    that changes under you cannot also be what the app is called. This is what
    stays still — the favicon and the sign-in page — and one card reads at 16
    pixels where a stack of three is mush.
    """
    return (f'<svg viewBox="0 0 28 28" width="{size}" height="{size}" '
            'aria-hidden="true">'
            '<rect x="4" y="6" width="20" height="16" rx="3" '
            'fill="var(--panel)" stroke="var(--fg)" stroke-width="1.6"/>'
            '<circle cx="9.2" cy="11" r="1.8" fill="var(--top)"/>'
            '<path d="M5.6 20.4 L11 14.6 L14.4 18.2 L17.2 15.4 L22.4 20.8" '
            'fill="none" stroke="var(--accent)" stroke-width="1.6" '
            'stroke-linecap="round" stroke-linejoin="round"/></svg>')


#: The same mark with the colours written out, because a favicon is a document
#: of its own and never sees this page's variables. On a tile, because that is
#: what a browser puts in a tab strip and a bookmark bar.
FAVICON = "data:image/svg+xml," + quote(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28 28">'
    '<rect width="28" height="28" rx="6" fill="#14161a"/>'
    '<rect x="4" y="6" width="20" height="16" rx="3" fill="#1b1e24" '
    'stroke="#e7e9ee" stroke-width="1.6"/>'
    '<circle cx="9.2" cy="11" r="1.8" fill="#e3b341"/>'
    '<path d="M5.6 20.4 L11 14.6 L14.4 18.2 L17.2 15.4 L22.4 20.8" '
    'fill="none" stroke="#6aa3ff" stroke-width="1.6" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>', safe="")


#: Back, on every page and every platform.
#:
#: Installed on a phone the app owns the whole window and there is no chrome
#: around it at all — every filter, every grouping and every folder opened is
#: a navigation, so the history is right there and nothing could reach it.
#: It is drawn in a tab as well, beside the browser's own: this is an app on a
#: desktop too, and the way out of where you are belongs inside it rather than
#: somewhere you reach for outside the window on one platform and inside it on
#: another.
BACK: str = (
    '<svg viewBox="0 0 24 24" width="19" height="19" aria-hidden="true" '
    'fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M15 5.5 8.5 12l6.5 6.5"/></svg>')


#: The gear, for when there is no room to spell any of it out.
GEAR: str = (
    '<svg viewBox="0 0 24 24" width="19" height="19" aria-hidden="true" '
    'fill="none" stroke="currentColor" stroke-width="1.7" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="3.1"/>'
    '<path d="M19.1 14.6a1.5 1.5 0 0 0 .3 1.7l.1.1a1.8 1.8 0 1 1-2.6 2.6l-.1-.1'
    'a1.5 1.5 0 0 0-1.7-.3 1.5 1.5 0 0 0-.9 1.4v.2a1.8 1.8 0 1 1-3.6 0v-.1'
    'a1.5 1.5 0 0 0-1-1.4 1.5 1.5 0 0 0-1.7.3l-.1.1a1.8 1.8 0 1 1-2.6-2.6l.1-.1'
    'a1.5 1.5 0 0 0 .3-1.7 1.5 1.5 0 0 0-1.4-.9h-.2a1.8 1.8 0 1 1 0-3.6h.1'
    'a1.5 1.5 0 0 0 1.4-1 1.5 1.5 0 0 0-.3-1.7l-.1-.1a1.8 1.8 0 1 1 2.6-2.6'
    'l.1.1a1.5 1.5 0 0 0 1.7.3h.1a1.5 1.5 0 0 0 .9-1.4v-.2a1.8 1.8 0 1 1 3.6 0'
    'v.1a1.5 1.5 0 0 0 .9 1.4 1.5 1.5 0 0 0 1.7-.3l.1-.1a1.8 1.8 0 1 1 2.6 2.6'
    'l-.1.1a1.5 1.5 0 0 0-.3 1.7v.1a1.5 1.5 0 0 0 1.4.9h.2a1.8 1.8 0 1 1 0 3.6'
    'h-.1a1.5 1.5 0 0 0-1.4.9z"/></svg>')


#: One drawing per filter, so the bar can say which question a chip asks
#: without spending a word on it.
#:
#: **Drawn here rather than taken from a set.** An icon font is a second
#: typeface to load for ten glyphs, and none of the general-purpose sets has a
#: mark for *stacks of near-identical photographs* or for *which import this
#: came off* — so the two that matter most in this app would have been the two
#: approximated. These are the same twenty-four unit grid, the same 1.7 stroke
#: and the same round ends as the bell in the bar, which is what makes them
#: read as one family rather than as clip art.
#:
#: Each is chosen against its neighbours as much as for itself: the set has to
#: be told apart at seventeen pixels, so no two share a silhouette.
MARKS: dict[str, str] = {
    # An occasion — planted somewhere and named. Not a calendar: that is the
    # date, and an event here is *which occasion*, not when.
    "event": '<path d="M6 21V3.6"/>'
             '<path d="M6 4.4h10.8l-2.6 3.6 2.6 3.6H6"/>',
    # The one shape nothing else uses, eyelet and all.
    "tag": '<path d="M3.6 11.9V5.3a1.7 1.7 0 0 1 1.7-1.7h6.6a1.7 1.7 0 0 1 '
           '1.2.5l7.1 7.1a1.7 1.7 0 0 1 0 2.4l-6.6 6.6a1.7 1.7 0 0 1-2.4 '
           '0l-7.1-7.1a1.7 1.7 0 0 1-.5-1.2z"/>'
           '<circle cx="8.1" cy="8.1" r="1.4"/>',
    "date": '<rect x="3.4" y="5" width="17.2" height="15.6" rx="2.2"/>'
            '<path d="M3.4 10h17.2"/><path d="M8 3.2v3.5"/>'
            '<path d="M16 3.2v3.5"/>',
    # Who is **in** the photograph. A head and shoulders, because that is
    # what a person is, and inside a frame because the question is who is in
    # *this* — which is also where a detected face will one day be drawn.
    "person": '<rect x="3.3" y="3.3" width="17.4" height="17.4" rx="3"/>'
              '<circle cx="12" cy="10" r="2.9"/>'
              '<path d="M6.9 19.4a5.6 5.6 0 0 1 10.2 0"/>',
    # Who may **see** it, which is the opposite question and had the person
    # shape until People needed it more. An eye: the thing this decides is
    # whether somebody can look, and nothing else in the set is round.
    "audience": '<path d="M2.2 12s3.6-6.4 9.8-6.4S21.8 12 21.8 12s-3.6 6.4-9.8 '
                '6.4S2.2 12 2.2 12z"/><circle cx="12" cy="12" r="2.9"/>',
    # Photographs, video, and whatever else — so, kinds of thing. A play
    # triangle would have named one of the three values rather than the
    # question.
    "kind": '<rect x="3.4" y="3.4" width="9.4" height="9.4" rx="1.8"/>'
            '<circle cx="15.8" cy="15.8" r="4.8"/>',
    # Its three values are small, medium and large, and this is that sentence
    # with no words in it.
    "band": '<path d="M5 19.8v-3.4"/><path d="M12 19.8v-7.6"/>'
            '<path d="M19 19.8v-11.6"/>',
    # Which import it came off. A card rather than a phone, because the camera
    # filter next to it is already a device and two devices side by side is
    # two silhouettes to tell apart at seventeen pixels.
    "source": '<path d="M6 3.5h8.6L19 7.9V20a1.6 1.6 0 0 1-1.6 1.6H6A1.6 1.6 '
              '0 0 1 4.4 20V5.1A1.6 1.6 0 0 1 6 3.5z"/>'
              '<path d="M8.4 3.6v3.2"/><path d="M11.4 3.6v3.2"/>'
              '<path d="M14.4 4.2v2.6"/>',
    "camera": '<path d="M3.5 8.6a1.8 1.8 0 0 1 1.8-1.8h2.5l1.5-2.3h5.4l1.5 '
              '2.3h2.5a1.8 1.8 0 0 1 1.8 1.8v9a1.8 1.8 0 0 1-1.8 '
              '1.8H5.3a1.8 1.8 0 0 1-1.8-1.8z"/>'
              '<circle cx="12" cy="13" r="3.5"/>',
    # A card with cards behind it — the same depth the stack badge on a
    # thumbnail is drawn with, so the filter and the thing it filters on look
    # like the same idea.
    "stacks": '<rect x="3.4" y="9.2" width="12.6" height="11.4" rx="2"/>'
              '<path d="M6.9 6.4h9.5a2 2 0 0 1 2 2v9.2"/>'
              '<path d="M10.4 3.6h8.2a2 2 0 0 1 2 2v8.4"/>',
    # The bin, because that is what every other part of the app calls it.
    "deleted": '<path d="M4 6.4h16"/>'
               '<path d="M6.6 6.4l.9 12a1.8 1.8 0 0 0 1.8 1.7h5.4a1.8 1.8 0 0 '
               '0 1.8-1.7l.9-12"/>'
               '<path d="M9.6 6.4V4.7a1.3 1.3 0 0 1 1.3-1.3h2.2a1.3 1.3 0 0 1 '
               '1.3 1.3v1.7"/>',
    # --- and the things you *do*, which are not filters ----------------
    # The edit bar asks the same questions in the same order as the filter bar
    # — that is deliberate, and it is why most of these reuse a drawing from
    # above rather than get one of their own. These five are the actions with
    # no question above them to borrow from.

    # Taking a copy away is the same gesture on every platform and has the
    # same mark everywhere: into something, downwards.
    "get": '<path d="M12 3.6v11"/><path d="M7.8 10.4 12 14.6l4.2-4.2"/>'
           '<path d="M4.4 16.2v2.4a1.8 1.8 0 0 0 1.8 1.8h11.6a1.8 1.8 0 0 0 '
           '1.8-1.8v-2.4"/>',
    # Which one of a stack speaks for the rest: raise this to the top, drawn
    # as an arrow meeting a ceiling it cannot go past.
    "top": '<path d="M4.6 3.9h14.8"/><path d="M12 20.4V8.4"/>'
           '<path d="M7.2 13.2 12 8.4l4.8 4.8"/>',
    # Cards side by side and not touching — the same two shapes the stack mark
    # overlaps, which is the whole of what the action does to them.
    "unstack": '<rect x="2.9" y="7.5" width="8.2" height="10.6" rx="1.8"/>'
               '<rect x="12.9" y="7.5" width="8.2" height="10.6" rx="1.8"/>',
    # Refusing the app's guess, so: a stack, struck through. Two cards rather
    # than the filter's three, because a slash across three is mush at
    # seventeen pixels.
    "nostack": '<rect x="3.2" y="9" width="11.6" height="11.6" rx="2"/>'
               '<path d="M7 6.2h9a2 2 0 0 1 2 2v9"/>'
               '<path d="M3.6 20.6 20.6 3.6"/>',
    # Cutting a video into clips: scissors, which is what it is called
    # everywhere else too.
    "splice": '<circle cx="6.3" cy="6.8" r="2.7"/>'
              '<circle cx="6.3" cy="17.2" r="2.7"/>'
              '<path d="M8.4 8.5 20 17.6"/><path d="M8.4 15.5 20 6.4"/>',
    # The splice page's controls. Transport in the shapes every player uses;
    # a keyframe is the diamond the timeline draws a cut as, so stepping to
    # one is a chevron at a diamond.
    # Play and pause solid, as every player draws them — the two controls
    # read by shape alone, before anything else on the bar.
    "sp_play": '<path d="M8 5.2v13.6L18.8 12z" fill="currentColor"/>',
    "sp_pause": '<rect x="6.8" y="5.2" width="3.8" height="13.6" rx="1" '
                'fill="currentColor"/><rect x="13.4" y="5.2" width="3.8" '
                'height="13.6" rx="1" fill="currentColor"/>',
    "sp_prevf": '<path d="M7 6v12"/><path d="M17 6.5 10.5 12l6.5 5.5"/>',
    "sp_nextf": '<path d="M17 6v12"/><path d="M7 6.5l6.5 5.5L7 17.5"/>',
    "sp_prevk": '<path d="M3.5 12 7 8.5l3.5 3.5L7 15.5z"/>'
                '<path d="M19.5 6.5 14 12l5.5 5.5"/>',
    "sp_nextk": '<path d="M20.5 12 17 8.5l-3.5 3.5 3.5 3.5z"/>'
                '<path d="M4.5 6.5 10 12l-5.5 5.5"/>',
    # A keyframe against a wall: as far as it goes, that way.
    "sp_firstk": '<path d="M4 5.5v13"/>'
                 '<path d="M11 12l3.5-3.5L18 12l-3.5 3.5z"/>',
    "sp_lastk": '<path d="M20 5.5v13"/>'
                '<path d="M13 12 9.5 8.5 6 12l3.5 3.5z"/>',
    # A marker as the timeline draws one: a knob on a line.
    "sp_marker": '<circle cx="12" cy="6.5" r="3"/><path d="M12 9.5V21"/>',
    "sp_photo": '<rect x="3" y="7" width="18" height="13" rx="2.2"/>'
                '<path d="M8.5 7l1.6-2.6h3.8L15.5 7"/>'
                '<circle cx="12" cy="13.5" r="3.6"/>',
    "sp_eye": '<path d="M2.8 12s3.4-6 9.2-6 9.2 6 9.2 6-3.4 6-9.2 6-9.2-6-9.2-6z"/>'
              '<circle cx="12" cy="12" r="2.8"/>',
    "sp_eyeoff": '<path d="M2.8 12s3.4-6 9.2-6 9.2 6 9.2 6-3.4 6-9.2 6-9.2-6-9.2-6z"/>'
                 '<circle cx="12" cy="12" r="2.8"/><path d="M4 20 20 4"/>',
    "sp_zin": '<circle cx="10.5" cy="10.5" r="6.3"/><path d="M15.2 15.2 20.5 20.5"/>'
              '<path d="M10.5 7.8v5.4"/><path d="M7.8 10.5h5.4"/>',
    "sp_zout": '<circle cx="10.5" cy="10.5" r="6.3"/><path d="M15.2 15.2 20.5 20.5"/>'
               '<path d="M7.8 10.5h5.4"/>',
    # Back out of the bin. A circle turned the other way is *undo* everywhere.
    "restore": '<path d="M3.5 12a8.5 8.5 0 1 0 2.5-6"/>'
               '<path d="M3.4 4.3v5.4h5.4"/>',
    # The end of the file rather than a decision about it. The bin it shares
    # with Delete, and the cross that says this one is not coming back.
    "purge": '<path d="M4 6.4h16"/>'
             '<path d="M6.6 6.4l.9 12a1.8 1.8 0 0 0 1.8 1.7h5.4a1.8 1.8 0 0 0 '
             '1.8-1.7l.9-12"/>'
             '<path d="M9.6 6.4V4.7a1.3 1.3 0 0 1 1.3-1.3h2.2a1.3 1.3 0 0 1 '
             '1.3 1.3v1.7"/>'
             '<path d="M10.4 11.5 13.6 15.3"/><path d="M13.6 11.5 10.4 15.3"/>',
}


#: Which drawing each action wears.
#:
#: Most of them point back into the filter marks above, and that is the point:
#: *Event* the filter and *Event* the action are the same question asked twice,
#: once about what you are looking at and once about what it should become.
#: Two bars that read the same way — a control that changes its face between
#: them is a control you have to learn twice.
ACT_MARKS: dict[str, str] = {
    "event": "event", "tags": "tag", "people": "person", "date": "date",
    "access": "audience",
    "stack": "stacks", "top": "top", "unstack": "unstack",
    "nostack": "nostack", "download": "get", "delete": "deleted",
    "restore": "restore", "purge": "purge", "splice": "splice",
}


def mark(name: str, size: int = 17) -> str:
    """One glyph, as the bar draws it.

    Same attributes as the bell beside it: `currentColor`, so a mark takes the
    colour of whatever state its chip is in and nothing has to be drawn twice.
    """
    body = MARKS.get(name)
    if not body:
        return ""
    return (f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" '
            'aria-hidden="true" fill="none" stroke="currentColor" '
            'stroke-width="1.7" stroke-linecap="round" '
            f'stroke-linejoin="round">{body}</svg>')


#: Pre-rendered, and committed, because the container has no Pillow in it: the
#: app never decodes anything, which is what keeps it viable on the Atom.
#: `tools/make_icons.py` re-emits them from the same geometry as `_logo_mark`.
# Beside the package, not inside it: `pix/nas/icons`, where the deploy and
# the image both put them.
ICONS: Path = Path(__file__).resolve().parents[1] / "icons"
