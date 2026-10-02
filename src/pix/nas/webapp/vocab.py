"""The web app's vocabulary: which filters there are and what each offers, how
the grid can be grouped, the facts a thumbnail can show, the media types it
serves, and the limits on a page, a bulk edit and a download.
"""

from __future__ import annotations

from pathlib import Path
from pix.nas import accounts
from pix.nas import decisions
from pix.nas import index as ix
from pix.nas import paths
from pix.nas.webapp.app import Principal
from pix.nas.webapp.app import store


#: Thumbnail size: three values, all three on show, the one you are in
#: pressed.
#:
#: It was one button that cycled, carrying a single letter which said what you
#: would get *next* — so the size you were in was written nowhere, the third
#: setting was only reachable by pressing twice to find out it existed, and
#: going back to the one you liked meant going round. Three values is exactly
#: the number a segmented control is for.
#:
#: **Rendered twice, and that is deliberate.** Where there is room it stands
#: in the bar, because it is the one view control that is worth a glance while
#: you are looking rather than a trip into a menu; where there is not it is a
#: row of the menu, which is where it has lived since the bar was four rows
#: deep on a phone. Both are always in the markup, because which one applies
#: changes while the page is open — by turning the phone over — and the page
#: script drives every copy it finds rather than the first.
SIZES: tuple[tuple[str, str, str], ...] = (
    ("small", "S", "Small thumbnails"),
    ("medium", "M", "Medium thumbnails"),
    ("large", "L", "Large thumbnails"))


#: What a thumbnail can say about itself, in the order the Display menu lists
#: it and a lane draws it: `(key, label, where it goes by default)`.
#:
#: **Which of them, and where, is the viewer's.** A grid being culled wants
#: access and nothing else; one being browsed wants none of it. A lane is
#: `top` or `bot`; `on` is a fact with a place of its own — the clip mark sits
#: by the stack badge, because it is the same kind of fact about the file.
#:
#: What is *not* here cannot be turned off: the stack badge, the duration, the
#: select circle and the deleted cross. Each is either a control or the one
#: thing that says the photograph is not what it looks like.
#:
#: The defaults are the arrangement before there was a choice, so nothing
#: moves for anybody who never opens the menu.
INFO: tuple[tuple[str, str, str], ...] = (
    ("people", "People", "bot"),
    ("access", "Access", "bot"),
    ("tags", "Tags", "top"),
    ("subevent", "Sub-event", "bot"),
    ("clip", "Clip / Still", "on"),
)


#: A file's `kind` column, as the Type boxes that make it up — what a folder
#: of a grouping by type stands for: a By-type folder of videos holds the
#: clips too.
KIND_HALVES: dict[str, tuple[str, ...]] = {
    "image": ("photo", "still"), "video": ("video", "clip")}


#: The words an address used to say for a type and no longer does. Only
#: `image`: `video` is a box of its own now — videos that are not clips — and
#: reading it as both halves was why ticking Videos alone came back as
#: Videos and Clips.
OLD_KINDS: dict[str, tuple[str, ...]] = {"image": KIND_HALVES["image"]}


def pick(values: list[str] | None) -> ix.Pick:
    """Repeated parameters as a filter: none, one, or several.

    One stays a plain string — *the view is about exactly this* — and
    repeats collapse, so `?tag=a&tag=a` is the same question as `?tag=a`.
    """
    got = tuple(dict.fromkeys(v for v in values or [] if v != ""))
    if not got:
        return None
    return got[0] if len(got) == 1 else got


#: What the front door opens on: this year, by month and then by event.
#: Applied as a **redirect from a bare `/`** rather than as a default inside
#: the page, so that everything after it is in the URL where the rest of the
#: view already lives. A default applied invisibly could not be cleared —
#: taking the year off would put the year straight back on.
HOME_GROUPING: str = "month,event"


#: Which filter each kind of chip narrows by, so a chip on a card can open
#: the folder already cut down to itself.
SPREAD_FILTER: dict[str, str] = {
    "audience": "audience", "people": "person", "tags": "tag",
}


#: What kind of thing a chip names, in the words the filter bar uses for it.
#:
#: The chip says `family` and its colour says which kind of `family` that is
#: — which works once the colours are learnt and not before. The name of the
#: kind is the thing a tooltip should carry, not the value, which is already
#: the word being pointed at.
SPREAD_LABEL: dict[str, str] = {
    "audience": "Access", "people": "People", "tags": "Tag",
}


#: What each grouping means as a filter, which is what makes a folder openable.
#: `camera` is here because of this page: it could cut the library by camera
#: and then had nowhere to send you.
DRILL: dict[str, str] = {
    "day": "date", "month": "date", "year": "date", "event": "event",
    "subevent": "event",
    "camera": "camera", "source": "source", "kind": "kind",
    "stack": "within",
}


#: The derived tiers a thumbnail can be drawn from, smallest first, with what
#: each one is capped at. Sent to the page so that *which tier is big enough*
#: is arithmetic there rather than a second copy of these numbers — they are
#: `derive`'s to choose and have already changed once.
TIERS: tuple[tuple[str, int], ...] = (
    ("/thumb/", paths.THUMB_PX),
    ("/large/", paths.LARGE_PX),
    ("/preview/", paths.PREVIEW_PX),
)


#: How many files one grid renders. Enough to hold the largest seeded event
#: (1,766) in a single page, because paging through a cull loses your place.
PAGE_LIMIT: int = 2000


#: How many thumbnails come with the grid, and how many each later page
#: brings. Enough to fill a large screen at the small size twice over, so
#: the first scroll has something under it; small enough that a view of the
#: whole library answers in a quarter of a second rather than in ten. The
#: rest arrive as they are scrolled towards — a file nobody scrolls to is
#: never queried, drawn or sent.
FIRST_PAGE: int = 240


NEXT_PAGE: int = 240


def groupings(raw: str) -> list[str]:
    """The grouping levels, outermost first.

    A list rather than one key, so a day inside an event is expressible.
    Unknown and repeated names are dropped rather than refused: this comes
    out of a URL, which people type and edit by hand.
    """
    if raw.strip() == "none":
        return []
    out: list[str] = []
    for name in raw.split(","):
        name = name.strip()
        if name in ix.GROUPINGS and name != "none" and name not in out                 and not any(same_field(name, had) for had in out):
            out.append(name)
    # Nothing recognisable is a typo, not a request to stop grouping — `none`
    # says that, and says it on purpose.
    return out[:3] or ["day"]


def chips(user: Principal) -> tuple[tuple[str, str], ...]:
    """The filters this person gets.

    Access is an administrator's control. Everyone else sees only what has
    been shared with them, so filtering by who else can see it offers a
    choice between their whole world and nothing.

    The bin is an administrator's too, and for a household member it is not
    merely hidden but empty by definition: the deleted are in nobody else's
    view to be found, which is what makes deleting safe to hand over and
    restoring not.

    **Stacks are everyone's**, because the stack actions are. A thousand
    suggestions is shared work, and *Not a stack* cannot be reached without
    the filter that makes a suggestion fold into one — see `_stacks`, which
    still leaves the default the safe way round for a household member.
    """
    return tuple((col, label) for col, label in CHIPS
                 if col not in ("audience", "deleted")
                 or user.is_admin)


def group_names() -> list[str]:
    """The groups, so the access menu can put them before the individuals."""
    book = store()
    return sorted(set(book.groups) - accounts.RESERVED)


def audience_names() -> list[str]:
    """Who access can be given to: **groups first, then people.**

    In that order because a group is almost always the right answer — sharing
    with `family` keeps working as the family changes, where naming four
    people does not. Both are offered, because sometimes one person really is
    the audience.

    Listed at all because sharing has to be possible on the very first file,
    before any decision exists to draw a suggestion from. The administrator is
    never here — it sees everything already, so granting it access is a no-op
    dressed as a decision.
    """
    book = store()
    groups = sorted(set(book.groups) - accounts.RESERVED)
    people = sorted(set(book.users) - accounts.RESERVED - set(groups))
    return [*groups, *people]


#: Labels for the filter chips and the fixed vocabularies. Kept server-side so
#: the tier and band words are defined once, next to the columns they describe.
CHIPS: tuple[tuple[str, str], ...] = (
    # The same order as the actions, because they are the same questions:
    # what it is, then what it is for. `kind`, `band` and `deleted` come last
    # as a group of their own — they are facts about the file rather than
    # judgements about it, and nobody reaches for them mid-cull.
    ("event", "Event"), ("tag", "Tag"), ("person", "People"),
    ("date", "Date"), ("audience", "Access"),
    ("kind", "Type"), ("band", "Size"), ("source", "Source"),
    ("camera", "Camera"),
    ("stacks", "Stacks"), ("deleted", "Deleted"),
)


#: Complete vocabularies — these columns cannot hold anything else.
FIXED: dict[str, tuple[tuple[str, str], ...]] = {
    "kind": (("photo", "Photos"), ("still", "Stills"), ("video", "Videos"),
             ("clip", "Clips"), ("other", "Other")),
    "band": (("small", "Small / short"), ("medium", "Medium"),
             ("large", "Large / long")),
    # Off is the third value and has no entry: clearing the chip is what says
    # *the living*, the same gesture as clearing any other filter.
    "deleted": (("gone", "Deleted"), ("live", "Not deleted")),
    # No entry for the ordinary view, the same as every other chip: *not
    # filtering on this* is what the cross says, and a value that only clears
    # the filter is a second way to say it — which is one more thing to read
    # in the list of the ones that do something. It was named while off meant
    # something of its own; folding is the default now, so it does not.
    "stacks": (("stacked", "Stacked"), ("suggested", "Suggested"),
               ("single", "Not in a stack")),
}


#: Headings in a fixed filter's checklist, each standing for the values
#: under it: ticking one ticks them all. Not a submenu — a row like any
#: other, which is what lets a finger use it.
FIXED_GROUPS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "kind": (("All photos", ("photo", "still")),
             ("All videos", ("video", "clip"))),
}


#: How the grid can be cut up, and what to call each choice.
GRID_GROUPS: tuple[tuple[str, str], ...] = (
    # Day, month and year lead because grouping by time is how the library is
    # mostly read and `day` is the default — an ordering by use, which is
    # allowed to win. Everything after them follows the filter bar, because
    # there it is the same set of questions and there is no reason for it to
    # be a second arrangement to learn: `kind` sat after `camera` here and
    # before `source` there, for no reason anybody chose.
    ("day", "By day"), ("month", "By month"), ("year", "By year"),
    # Named for what it actually does. It groups on the whole event name, so
    # an event with no sub-event stands as itself and one with a sub-event
    # stands under its own full name — *By sub-event* read as though it were
    # about the sub-events alone and left you wondering where the rest had
    # gone, and it is the only choice here that has to say two things.
    ("event", "By event"), ("subevent", "By event and sub-event"),
    ("kind", "By type"), ("source", "By source"),
    ("camera", "By camera"),
    ("stack", "By stack"), ("none", "Ungrouped"),
)


#: Which column each grouping reads, for the ones that share.
#:
#: *Event* and *sub-event* are one field at two widths — the head of the name
#: and the whole of it. Nesting either inside the other cuts by a question the
#: outer level has already answered: *Sicily* holding *Sicily › Taormina* is a
#: heading and no new information, and *Sicily › Taormina* holding *Sicily* is
#: a group of one, every time. So picking one takes the other off the menu.
#:
#: Day, month and year are deliberately **not** in here. They read the same
#: column too, but a month inside a year is a real division and the reason the
#: grouping is a list in the first place.
ONE_FIELD: dict[str, str] = {"event": "event", "subevent": "event"}


def same_field(a: str, b: str) -> bool:
    """Two groupings that read one column, so only one of them can be on."""
    return a != b and ONE_FIELD.get(a, a) == ONE_FIELD.get(b, b)


#: Offered *in addition* to whatever already exists. Audience names are free
#: text, but "nobody yet" is a state rather than a name, and it is the single
#: most useful thing to filter on — it is the pile of work.
#: What `archived` is called wherever it is offered. Said as what it does
#: as well as what it is, because it is the one value in the access menu that
#: is not somebody: it takes the file out of every view, the curator's own
#: included.
ARCHIVED_LABEL: str = "Archived — out of every view"


EXTRA: dict[str, tuple[tuple[str, str], ...]] = {
    "audience": ((ix.UNREVIEWED, "Nobody — not shared yet"),
                 (decisions.ARCHIVED, ARCHIVED_LABEL)),
}


#: What a file is, by the only thing a URL knows about it.
#:
#: Not a guess the app acts on — it serves the same bytes either way. It is what
#: lets a phone put a photograph in Photos: iOS will only offer *Save Image* for
#: something it has been told is an image, and a file handed over as
#: `application/octet-stream` is a file the share sheet can only put in Files.
MIME: dict[str, str] = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic",
    ".heif": "image/heif", ".avif": "image/avif", ".tif": "image/tiff",
    ".tiff": "image/tiff", ".dng": "image/x-adobe-dng",
    ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mov": "video/quicktime",
    ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
    ".webm": "video/webm", ".mts": "video/mp2t", ".m2ts": "video/mp2t",
    ".3gp": "video/3gpp",
}


def mime(name: str) -> str:
    """What to call this kind of file, or nothing useful if we do not know.

    Octet-stream for anything unlisted — an `.insv` is not a type any phone has
    an opinion about, and claiming one would be worse than admitting it.
    """
    return MIME.get(Path(name).suffix.lower(), "application/octet-stream")


#: How many meta records to fetch at once.
#:
#: Each is 5KB of JSON behind an 11ms round trip to the NAS, and the wait is
#: latency rather than bandwidth — so the cure is having several in flight,
#: not asking for less. Eight because the archive runs on a four-core Atom and
#: the point is to keep its network busy, not its processor.
META_READERS: int = 8


#: What each kind is called where a refusal has to name it.
KIND_WORDS: dict[str, str] = {
    "image": "photographs", "video": "video", "other": "other files"}


#: How many files one zip will hold. Not a technical limit — the stream is
#: constant-memory whatever goes through it — but a selection can run to
#: thousands, and a download nobody meant to start is a download nobody can
#: stop without noticing it is running.
ZIP_LIMIT: int = 500


#: Bounds one request rather than the whole gesture. Finishing a 1,766-file
#: event is chunked by the client, which keeps each request short enough not to
#: hold the single worker and gives a progress reading for free.
BULK_LIMIT: int = 500
