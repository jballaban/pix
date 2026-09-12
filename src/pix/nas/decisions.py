"""Curation decisions — the `.xmp` sidecar beside each master file (spec §4).

Four fields: **`audience`, `event`, `tags`, and a date override**. Nothing
recomputable goes here, because master is the one tier backed up forever and
caching a probed fact in it duplicates recomputable data into permanent
storage. Everything that is here is a human judgement about the file.

**The date override has holes in it.** It is a `YYYY-MM-DD-HH:MM:SS` pattern
where any component may be `*` (`pix.datestr`), so *this scan is from
1987* can be recorded without inventing a month, a day and a time nobody
knows. Fabricating that precision is not a harmless convenience — a made-up
`1987-01-01 00:00:00` is indistinguishable from a real one a year later.

The sidecar is named **full filename plus `.xmp`** (`IMG_4471.HEIC.xmp`, not
`IMG_4471.xmp`). Lightroom's basename-replacing convention would collide on the
most common device in the library: an iPhone Live Photo lands as `IMG_4471.HEIC`
and `IMG_4471.MOV` in the same import.

**A stack is one file speaking for several.** Eight takes of the same
photograph, one shown and the rest folded behind it. Each of the others records
which file it defers to; the top records nothing, because being spoken for is
the decision and speaking is just what is left. That is [§15]'s open question
about where a judgement concerning a *set* lives, answered: it was never about
the set.

**`audience` is who may see the file**, and it replaced a `tier` of
`none`/`photo`/`top`. Those were two questions wearing one name — *has this
been reviewed* and *how good is it* — and neither was the question actually
being asked, which is **who is this for**. A household has photographs the
children should not see and photographs that belong on the television, and
that is one axis, not a quality ranking. "The best ones" is simply an
audience that happens to be small.

An audience is a **name**, and some names happen to have a login: a role like
`family` has none, and grants to it are what a household actually runs on.
Nothing is special-cased — *keep this but show it to nobody* is a role with no
members, created like any other. The owner is never in the list: an
administrator sees everything by definition.

**No sidecar means undecided.** That is the whole review-state model — there
is no separate "reviewed" flag, and clearing every field deletes the file
rather than leaving an empty one, so the two states stay distinguishable. An
audience with no members is still a decision and does create a sidecar:
choosing to keep something to yourself *is* a judgement.

### Serialization

XMP packet, RDF attribute form, in the `pix` namespace already registered for
ExifTool in `exiftool_config.cfg` (`http://pix.local/`). Reusing it is what makes
the read-through in `index` symmetric: `pix:EventOverride` means the same thing
whether it was read from a sidecar here or from the tags embedded in a legacy
file, so the index treats the two as one cascade.

Standard fields stay standard, so other tools see the decisions they can
understand:

| decision | pix property | also written as |
|---|---|---|
| event | `pix:EventOverride` | `Iptc4xmpExt:Event` |
| tags | — | `dc:subject`, the standard keywords bag |
| audience | `pix:Audience` | — nothing standard expresses *who may see this* |
| stacked under | `pix:StackedUnder` | — Lightroom keeps stacks in its catalogue, not the file |
| date override | `pix:DateOverride` | `photoshop:DateCreated`, when the
  override pins a whole timestamp — a partial one has no standard form |

Tags live **only** in `dc:subject` rather than getting a `pix:` twin. It is
the industry keyword field, every tool round-trips it, and nothing in pix
used it before — so there is no legacy vocabulary to reconcile and no reason
to invent a second home that could disagree with the first.

Writes are temp-then-rename, so a kill mid-write cannot leave a half-written
sidecar that parses as a decision nobody made.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence
from xml.sax.saxutils import escape, quoteattr

from pix import datestr
from pix.datestr import PIX_DATETIME_FORMAT
from pix.markers import SIDECAR_TMP_SUFFIX

#: The pix XMP namespace, as registered in `exiftool_config.cfg`.
PIX_NS: str = "http://pix.local/"
_PHOTOSHOP_NS: str = "http://ns.adobe.com/photoshop/1.0/"
_IPTC_EXT_NS: str = "http://iptc.org/std/Iptc4xmpExt/2008-02-29/"
_RDF_NS: str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_DC_NS: str = "http://purl.org/dc/elements/1.1/"


class Unset:
    """Sentinel: this field is not being changed.

    Distinct from `None`, which means *clear this decision*. Without the
    distinction, setting a tier would silently erase an event.
    """


UNSET: Unset = Unset()


@dataclass(frozen=True)
class Decision:
    """What a human decided about one master file.

    `tags` is sorted and de-duplicated on construction, so two sidecars
    recording the same judgement are the same bytes — which keeps a
    re-write from looking like a change.
    """

    event: str | None = None
    date_override: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    audience: tuple[str, ...] = field(default_factory=tuple)
    #: Soft-deleted — *this should go*, which is a judgement like any other and
    #: so lives here rather than in a list off to the side. Beside the file, a
    #: deletion survives losing the index, travels with the folder, and is
    #: undone by the ordinary revert.
    deleted: bool = False
    #: `folder/name` of the file this one is **stacked behind** — eight takes
    #: of one photograph, with one of them shown and the rest folded under it.
    #: Empty on the top of a stack, which records nothing: a stack is *these
    #: files defer to that one*, and only the deferring is a decision.
    #:
    #: This answers §15's open question about where a judgement concerning a
    #: *set* lives. It lives per file after all, because it is not really about
    #: the set — it is each file saying which one speaks for it. Losing a folder
    #: costs those files and their deference together, which is the same rule
    #: as everything else here.
    stacked_under: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", normalize_tags(self.tags))
        object.__setattr__(self, "audience",
                           normalize_names(self.audience))

    def is_empty(self) -> bool:
        """True when nothing has been decided, so no sidecar should exist."""
        return not (self.event or self.date_override or self.tags
                    or self.audience or self.deleted
                    or self.stacked_under)


def normalize_tags(values: Iterable[str]) -> tuple[str, ...]:
    """Trim, drop blanks, de-duplicate, sort — **keeping case**.

    A tag is a phrase someone wrote, so `Beach` and `beach` stay distinct:
    folding here would silently rewrite what they typed into the permanent
    record, and the fix for the duplicate is the UI offering the tags that
    already exist so people pick instead of retyping.
    """
    return tuple(sorted({v.strip() for v in values if v and v.strip()}))


def normalize_names(values: Iterable[str]) -> tuple[str, ...]:
    """The same, but **case-folded** — for names of people and roles.

    An audience is an identifier, not a phrase, and two spellings of one
    person are not two audiences. It has to fold here rather than only at
    the login, because the access check compares a grant against a name:
    signing in as `Kid` while a photograph is shared with `kid` would
    otherwise read as *not shared*, which is a failure that looks exactly
    like a correctly-kept secret.
    """
    return tuple(sorted({v.strip().casefold()
                         for v in values if v and v.strip()}))


class DecisionError(ValueError):
    """A decision that cannot be stored as written."""


def sidecar_path(media: Path) -> Path:
    """The `.xmp` beside `media` — full filename plus the suffix."""
    return media.with_name(media.name + ".xmp")


def read(media: Path) -> Decision | None:
    """The decision recorded for `media`, or None if it has no sidecar.

    A sidecar that exists but cannot be parsed also reads as None: the index is
    a projection, and guessing at a damaged record would put invented values
    into it. The file stays on disk for inspection.
    """
    path = sidecar_path(media)
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    return _from_xml(root)


def write(media: Path, decision: Decision) -> None:
    """Record `decision` for `media`, or delete the sidecar if it is empty.

    Temp-then-rename: `os.replace` is atomic, so a reader either sees the old
    sidecar or the new one, never a partial packet.
    """
    _validate(decision)
    path = sidecar_path(media)

    if decision.is_empty():
        path.unlink(missing_ok=True)
        return

    tmp = path.with_name(path.name + SIDECAR_TMP_SUFFIX)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_text(_to_xml(decision), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def change(media: Path, *,
           event: str | None | Unset = UNSET,
           date_override: str | None | Unset = UNSET,
           tags: Sequence[str] | None | Unset = UNSET,
           add_tags: Sequence[str] = (),
           remove_tags: Sequence[str] = (),
           audience: Sequence[str] | None | Unset = UNSET,
           add_audience: Sequence[str] = (),
           remove_audience: Sequence[str] = (),
           deleted: bool | Unset = UNSET,
           stacked_under: str | None | Unset = UNSET,
           ) -> tuple[Decision | None, Decision]:
    """Change some fields of `media`'s decision, leaving the rest alone.

    Read-modify-write rather than replace, because the UI changes one field at a
    time: tiering a photo must not discard the event someone else assigned it.
    Last-write-wins is the conflict policy (spec §8) and is sufficient — a
    single process serializes the writes, so concurrency is a policy question
    here, never an integrity one.

    Tags and audience take `add_`/`remove_` as well as a wholesale replace,
    and the difference matters at scale: sharing 200 files with the kids must
    add to whatever each is already shared with, not flatten them all to one
    list.

    Returns **both** the previous decision and the new one. The caller needs the
    previous value to be able to undo it, and it has already been read here —
    asking for it again would double the SMB reads of every bulk edit.
    """
    was = read(media)
    current = was or Decision()
    updated = Decision(
        event=current.event if isinstance(event, Unset) else event,
        date_override=(current.date_override
                       if isinstance(date_override, Unset)
                       else date_override),
        tags=_merge(current.tags, tags, add_tags, remove_tags),
        audience=_merge(current.audience, audience,
                        add_audience, remove_audience, fold=True),
        deleted=current.deleted if isinstance(deleted, Unset) else deleted,
        stacked_under=(current.stacked_under
                       if isinstance(stacked_under, Unset) else stacked_under),
    )
    write(media, updated)
    return was, updated


def apply(media: Path, **fields: Any) -> Decision:
    """`change`, for callers with no use for the previous value."""
    return change(media, **fields)[1]


def _merge(current: tuple[str, ...], replace: Sequence[str] | None | Unset,
           add: Sequence[str], remove: Sequence[str], *,
           fold: bool = False) -> tuple[str, ...]:
    """Apply a replace-or-add-or-remove edit to one multi-valued field.

    `fold` picks the normaliser, and it matters most for *remove*:
    unsharing `Kid` has to match a stored `kid`, or access could be
    granted and then not taken back.
    """
    norm = normalize_names if fold else normalize_tags
    kept = current if isinstance(replace, Unset) else norm(replace or ())
    if add or remove:
        dropped = set(norm(remove))
        kept = norm([v for v in [*kept, *norm(add)] if v not in dropped])
    return kept


# --- serialization -----------------------------------------------------------

def _validate(decision: Decision) -> None:
    if decision.date_override:
        if not datestr.valid(decision.date_override):
            raise DecisionError(
                f"date override {decision.date_override!r} is not "
                f"{PIX_DATETIME_FORMAT} with `*` for unknown components")
        if not datestr.pins_anything(decision.date_override):
            raise DecisionError(
                "date override pins nothing — clear it instead of storing "
                "all-`*`, which would record a decision nobody made")
    for value in (*decision.tags, *decision.audience):
        if len(value) > 120:
            raise DecisionError(f"{value[:40]!r}… is too long")


_TEMPLATE: str = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="pix">
 <rdf:RDF xmlns:rdf="{rdf}">
  <rdf:Description rdf:about=""
    xmlns:pix="{pix}"
    xmlns:dc="{dc}"
    xmlns:photoshop="{photoshop}"
    xmlns:Iptc4xmpExt="{iptc}"
{props}  >{children}</rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


def _to_xml(decision: Decision) -> str:
    props: list[tuple[str, str]] = []
    if decision.event:
        props.append(("pix:EventOverride", decision.event))
        props.append(("Iptc4xmpExt:Event", decision.event))
    if decision.date_override:
        props.append(("pix:DateOverride", decision.date_override))
        # Only a fully-pinned override has a standard equivalent: a partial
        # date has no ISO 8601 form, and filling the holes to produce one
        # would publish a precision the curator explicitly did not claim.
        moment = datestr.alone(decision.date_override)
        if moment is not None and '*' not in decision.date_override:
            props.append(("photoshop:DateCreated", moment.isoformat()))
    if decision.stacked_under:
        props.append(("pix:StackedUnder", decision.stacked_under))
    if decision.deleted:
        # No standard equivalent, deliberately. Expressing it as a rating or a
        # keyword would tell another tool this file is deleted in *its* terms,
        # and Lightroom acting on that is not what a soft delete means here.
        props.append(("pix:Deleted", "true"))

    body = "".join(f"    {key}={quoteattr(value)}\n" for key, value in props)
    children = (_bag("dc:subject", decision.tags)
                + _bag("pix:Audience", decision.audience))
    return _TEMPLATE.format(rdf=_RDF_NS, pix=PIX_NS, dc=_DC_NS,
                            photoshop=_PHOTOSHOP_NS, iptc=_IPTC_EXT_NS,
                            props=body, children=children)


def _bag(prop: str, values: tuple[str, ...]) -> str:
    """A multi-valued property as an RDF Bag — the shape XMP readers expect."""
    if not values:
        return ""
    items = "".join(f"\n     <rdf:li>{escape(v)}</rdf:li>" for v in values)
    return (f"\n   <{prop}>\n    <rdf:Bag>{items}\n    </rdf:Bag>\n   </{prop}>\n  ")


def _from_xml(root: ET.Element) -> Decision | None:
    """Read the pix properties out of a parsed XMP packet.

    Properties are read from both attributes and child elements: the attribute
    form is what this module writes, and the element form is what most other
    XMP writers produce, so a sidecar edited elsewhere still reads.
    """
    description = _description(root)
    if description is None:
        return None

    values: dict[str, str] = {}
    for name, value in description.attrib.items():
        if name.startswith(f"{{{PIX_NS}}}") and value:
            values[name.rpartition("}")[2]] = value
    for child in description:
        if child.tag.startswith(f"{{{PIX_NS}}}"):
            text = (child.text or "").strip()
            if text:
                values[child.tag.rpartition("}")[2]] = text

    decision = Decision(event=values.get("EventOverride"),
                        date_override=values.get("DateOverride"),
                        tags=_read_bag(description, _DC_NS, "subject"),
                        audience=normalize_names(
                            _read_bag(description, PIX_NS, "Audience")),
                        deleted=_truth(values.get("Deleted")),
                        stacked_under=values.get("StackedUnder") or None)
    return None if decision.is_empty() else decision


def _truth(value: str | None) -> bool:
    """Lenient on the way in, exact on the way out.

    This writes `true` and nothing else, but a sidecar edited by hand or by
    another tool may say `True` or `1`, and reading that as *not deleted*
    would quietly resurrect a file somebody meant to be rid of.
    """
    return (value or "").strip().lower() in ("true", "1", "yes")


def _read_bag(description: ET.Element, namespace: str,
              local: str) -> tuple[str, ...]:
    """A multi-valued property, whether bagged or written bare.

    A Bag is what this writes and what Lightroom writes, but a single value is
    sometimes written as plain text, and a sidecar edited elsewhere still has
    to read.
    """
    found: list[str] = []
    for node in description.iter(f"{{{namespace}}}{local}"):
        items = list(node.iter(f"{{{_RDF_NS}}}li"))
        if items:
            found.extend((i.text or "").strip() for i in items)
        else:
            found.append((node.text or "").strip())
    return normalize_tags(found)


def _description(root: ET.Element) -> ET.Element | None:
    """The `rdf:Description` carrying the properties, wherever it sits.

    Written packets nest it under `x:xmpmeta/rdf:RDF`, but a bare `rdf:RDF` root
    is also valid XMP, so this searches rather than walking a fixed path.
    """
    tag = f"{{{_RDF_NS}}}Description"
    if root.tag == tag:
        return root
    return root.find(f".//{tag}")
