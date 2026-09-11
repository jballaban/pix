"""Curation decisions — the `.xmp` sidecar beside each master file (spec §4).

Four fields: **`tier`, `event`, `tags`, and a date override**. Nothing
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

**No sidecar means unreviewed.** That is the whole review-state model — there is
no separate "reviewed" flag, and clearing every field deletes the file rather
than leaving an empty one, so the two states stay distinguishable.
`tier="none"` is a decision and does create a sidecar: rejection *is* a
judgement.

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
| date override | `pix:DateOverride` | `photoshop:DateCreated`, when the
  override pins a whole timestamp — a partial one has no standard form |
| tier | `pix:Tier` | — no standard equivalent; rating was dropped |

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
from typing import Iterable, Sequence
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

#: Delivery selector (spec §7). Absent means unreviewed; `none` means reviewed
#: and rejected. `rating` was deliberately dropped — a 1-5 scale asked people to
#: grade photos when the only question that matters is where a photo goes.
TIERS: frozenset[str] = frozenset({"none", "photo", "top"})


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

    tier: str | None = None
    event: str | None = None
    date_override: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", normalize_tags(self.tags))

    def is_empty(self) -> bool:
        """True when nothing has been decided, so no sidecar should exist."""
        return not (self.tier or self.event or self.date_override
                    or self.tags)


def normalize_tags(values: Iterable[str]) -> tuple[str, ...]:
    """Trim, drop blanks, de-duplicate, sort.

    Case is preserved rather than folded: `Beach` and `beach` stay distinct,
    because the fix for that is the UI offering the tags that already exist
    so people pick instead of retyping. Folding here would silently rewrite
    what someone typed into the permanent record.
    """
    return tuple(sorted({v.strip() for v in values if v and v.strip()}))


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


def apply(media: Path, *,
          tier: str | None | Unset = UNSET,
          event: str | None | Unset = UNSET,
          date_override: str | None | Unset = UNSET,
          tags: Sequence[str] | None | Unset = UNSET,
          add_tags: Sequence[str] = (),
          remove_tags: Sequence[str] = ()) -> Decision:
    """Change some fields of `media`'s decision, leaving the rest alone.

    Read-modify-write rather than replace, because the UI changes one field at a
    time: tiering a photo must not discard the event someone else assigned it.
    Last-write-wins is the conflict policy (spec §8) and is sufficient — a
    single process serializes the writes, so concurrency is a policy question
    here, never an integrity one.

    Tags take `add_tags`/`remove_tags` as well as a wholesale `tags`, and the
    difference matters at scale: tagging a selection of 200 files must add to
    what each already carries, not flatten them all to one list.
    """
    current = read(media) or Decision()
    if isinstance(tags, Unset):
        kept = current.tags
    else:
        kept = normalize_tags(tags or ())
    if add_tags or remove_tags:
        dropped = set(normalize_tags(remove_tags))
        kept = normalize_tags(
            [t for t in [*kept, *normalize_tags(add_tags)] if t not in dropped])
    updated = Decision(
        tier=current.tier if isinstance(tier, Unset) else tier,
        event=current.event if isinstance(event, Unset) else event,
        date_override=(current.date_override
                       if isinstance(date_override, Unset)
                       else date_override),
        tags=kept,
    )
    write(media, updated)
    return updated


# --- serialization -----------------------------------------------------------

def _validate(decision: Decision) -> None:
    if decision.tier is not None and decision.tier not in TIERS:
        raise DecisionError(
            f"unknown tier {decision.tier!r} — expected one of "
            f"{', '.join(sorted(TIERS))}")
    if decision.date_override:
        if not datestr.valid(decision.date_override):
            raise DecisionError(
                f"date override {decision.date_override!r} is not "
                f"{PIX_DATETIME_FORMAT} with `*` for unknown components")
        if not datestr.pins_anything(decision.date_override):
            raise DecisionError(
                "date override pins nothing — clear it instead of storing "
                "all-`*`, which would record a decision nobody made")
    for tag in decision.tags:
        if len(tag) > 120:
            raise DecisionError(f"tag {tag[:40]!r}… is too long")


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
    if decision.tier:
        props.append(("pix:Tier", decision.tier))
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

    body = "".join(f"    {key}={quoteattr(value)}\n" for key, value in props)
    children = _tag_bag(decision.tags)
    return _TEMPLATE.format(rdf=_RDF_NS, pix=PIX_NS, dc=_DC_NS,
                            photoshop=_PHOTOSHOP_NS, iptc=_IPTC_EXT_NS,
                            props=body, children=children)


def _tag_bag(tags: tuple[str, ...]) -> str:
    """Tags as a `dc:subject` RDF Bag — the shape every XMP reader expects."""
    if not tags:
        return ""
    items = "".join(f"\n     <rdf:li>{escape(t)}</rdf:li>" for t in tags)
    return (f"\n   <dc:subject>\n    <rdf:Bag>{items}\n    </rdf:Bag>\n   </dc:subject>\n  ")


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

    decision = Decision(tier=values.get("Tier"),
                        event=values.get("EventOverride"),
                        date_override=values.get("DateOverride"),
                        tags=_read_tags(description))
    return None if decision.is_empty() else decision


def _read_tags(description: ET.Element) -> tuple[str, ...]:
    """Keywords from `dc:subject`, whether bagged or written bare.

    A Bag is what this writes and what Lightroom writes, but a single-keyword
    `dc:subject` is sometimes written as plain text, and a sidecar edited
    elsewhere still has to read.
    """
    found: list[str] = []
    for subject in description.iter(f"{{{_DC_NS}}}subject"):
        for item in subject.iter(f"{{{_RDF_NS}}}li"):
            found.append((item.text or "").strip())
        if not list(subject.iter(f"{{{_RDF_NS}}}li")):
            found.append((subject.text or "").strip())
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
