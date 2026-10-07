"""What a download hands over (spec/metadata-cleanup.md).

A download is somebody deliberately making a copy, so it carries nothing that
identifies anyone — no location, no camera, no people, none of the old pix's
paths — only what the file needs to display correctly and an opaque
`pix:SourceId` so a copy that comes home can be recognised. The archive itself
is left alone; this is applied on the way out, per request.

**Cleaning is copying, not converting.** A JPEG's metadata sits in segments
beside the coded image, so a clean copy is the image's own tables and scans
under a new header. Nothing is decoded, which is what makes it cheap enough to
do on the NAS for every download, and what keeps the content hash — which
covers exactly those tables and scans — the same as the source's.

Pure functions over bytes, with no web app in them, so they are testable on
their own and usable by anything else that hands files out.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import quoteattr

from pix.nas.decisions import PIX_NS


class Uncleanable(ValueError):
    """A file that looks like a JPEG but cannot be walked.

    Refused rather than sent as it is: an unparseable file is exactly the one
    whose metadata nobody has checked.
    """


#: Markers with no length after them: TEM and the restart markers.
_STANDALONE: frozenset[int] = frozenset({0x01, *range(0xD0, 0xD8)})

_SOI: bytes = b"\xff\xd8"
_EOI: bytes = b"\xff\xd9"

#: TIFF tags read from the source and written into the copy.
_ORIENTATION: int = 0x0112
_EXIF_IFD: int = 0x8769
_EXIF_VERSION: int = 0x9000
_DATE_ORIGINAL: int = 0x9003
_DATE_DIGITIZED: int = 0x9004
_COLOR_SPACE: int = 0xA001

_SHORT, _ASCII, _LONG, _UNDEFINED = 3, 2, 4, 7

_XMP_HEADER: bytes = b"http://ns.adobe.com/xap/1.0/\x00"


def is_jpeg(data: bytes) -> bool:
    """What the bytes say, not the name: the same test `identity` uses."""
    return data[:3] == b"\xff\xd8\xff"


def clean_jpeg(data: bytes, *, taken: datetime | None,
               source_id: str | None) -> bytes:
    """`data` with every metadata segment replaced by the few a copy needs.

    Kept from the source: orientation and EXIF `ColorSpace` (rewritten into a
    new minimal EXIF), the ICC profile, JFIF without its thumbnail, Adobe
    `APP14` — which says how to read the colour channels — and every table,
    frame header and scan, verbatim and in order, up to the first end of
    image. Added: `taken` as the capture date, and `source_id` as
    `pix:SourceId`. Everything else goes, including whatever follows the end
    of the image — which is where an iPhone keeps its HDR gain map.
    """
    if not is_jpeg(data):
        raise Uncleanable("not a JPEG")

    orientation: int | None = None
    colour: int | None = None
    jfif: bytes | None = None
    icc: list[bytes] = []
    adobe: bytes | None = None
    image: list[bytes] = []
    scanned = False

    end = len(data)
    at = 2
    while True:
        if at >= end:
            # No end-of-image after the last scan: truncated, which decoders
            # forgive, so this does too. The copy gets the EOI it lacked.
            break
        marker, at = _marker(data, at)
        if marker == 0xD9:
            break
        if marker in _STANDALONE:
            image.append(bytes((0xFF, marker)))
            continue
        if marker == 0xD8 or at + 2 > end:
            raise Uncleanable("malformed segment")
        length = int.from_bytes(data[at:at + 2], "big")
        if length < 2 or at + length > end:
            raise Uncleanable("segment runs past the end")
        segment = data[at - 2:at + length]
        body = data[at + 2:at + length]
        at += length

        if marker == 0xDA:
            # The scan's entropy-coded data runs to the next real marker. A
            # progressive file has several, with tables between them.
            stop = _scan_end(data, at)
            image.append(segment + data[at:stop])
            at = stop
            scanned = True
        elif 0xE0 <= marker <= 0xEF or marker == 0xFE:
            if (marker == 0xE0 and jfif is None and len(body) >= 14
                    and body.startswith(b"JFIF\x00")):
                # Version, units and density; the thumbnail's size zeroed and
                # the thumbnail itself not carried.
                jfif = body[:12] + b"\x00\x00"
            elif marker == 0xE1 and body.startswith(b"Exif\x00\x00"):
                found_o, found_c = _exif_readings(body[6:])
                orientation = orientation or found_o
                colour = colour if colour is not None else found_c
            elif marker == 0xE2 and body.startswith(b"ICC_PROFILE\x00"):
                icc.append(segment)
            elif marker == 0xEE and adobe is None and body.startswith(b"Adobe"):
                adobe = segment
        else:
            image.append(segment)

    if not scanned:
        raise Uncleanable("no image data")

    head = [_SOI]
    if jfif is not None:
        head.append(_segment(0xE0, jfif))
    exif = _exif(orientation, colour, taken)
    if exif is not None:
        head.append(_segment(0xE1, b"Exif\x00\x00" + exif))
    if source_id:
        head.append(_segment(0xE1, _XMP_HEADER + _xmp(source_id)))
    head.extend(icc)
    if adobe is not None:
        head.append(adobe)
    return b"".join([*head, *image, _EOI])


def _marker(data: bytes, at: int) -> tuple[int, int]:
    """The marker at `at` and where its segment begins, past any fill bytes."""
    end = len(data)
    if data[at] != 0xFF:
        raise Uncleanable("expected a marker")
    while at < end and data[at] == 0xFF:
        at += 1
    if at >= end:
        raise Uncleanable("ends in fill bytes")
    return data[at], at + 1


def _scan_end(data: bytes, at: int) -> int:
    """Where the entropy-coded data starting at `at` stops.

    Inside a scan `FF` is followed by `00` (a stuffed byte), a restart marker,
    or more fill — anything else is the next segment. Running off the end
    means a truncated file, and the scan is taken to its end.
    """
    end = len(data)
    i = at
    while True:
        j = data.find(b"\xff", i)
        if j < 0 or j + 1 >= end:
            return end
        following = data[j + 1]
        if following == 0x00 or 0xD0 <= following <= 0xD7:
            i = j + 2
        elif following == 0xFF:
            i = j + 1
        else:
            return j


def _segment(marker: int, body: bytes) -> bytes:
    return bytes((0xFF, marker)) + (len(body) + 2).to_bytes(2, "big") + body


def _exif_readings(tiff: bytes) -> tuple[int | None, int | None]:
    """Orientation and `ColorSpace` from an EXIF block, where they can be read.

    Anything malformed is simply not read: the copy then goes without, which
    is the source's own fault and costs no more than a sideways photograph.
    """
    if tiff[:4] == b"II*\x00":
        order = "little"
    elif tiff[:4] == b"MM\x00*":
        order = "big"
    else:
        return None, None

    def number(at: int, size: int) -> int | None:
        raw = tiff[at:at + size]
        return int.from_bytes(raw, order) if len(raw) == size else None

    def field(ifd: int | None, tag: int) -> int | None:
        count = number(ifd, 2) if ifd else None
        if count is None or ifd is None:
            return None
        for i in range(count):
            entry = ifd + 2 + 12 * i
            if number(entry, 2) == tag:
                kind = number(entry + 2, 2)
                if kind == _SHORT:
                    return number(entry + 8, 2)
                if kind == _LONG:
                    return number(entry + 8, 4)
                return None
        return None

    ifd0 = number(4, 4)
    orientation = field(ifd0, _ORIENTATION)
    if orientation is not None and not 1 <= orientation <= 8:
        orientation = None
    colour = field(field(ifd0, _EXIF_IFD), _COLOR_SPACE)
    return orientation, colour


def _exif(orientation: int | None, colour: int | None,
          taken: datetime | None) -> bytes | None:
    """A big-endian TIFF block holding only what a copy keeps, or None."""
    exif_entries: list[tuple[int, int, int, bytes]] = []
    if taken is not None:
        stamp = taken.strftime("%Y:%m:%d %H:%M:%S").encode("ascii") + b"\x00"
        exif_entries += [(_DATE_ORIGINAL, _ASCII, len(stamp), stamp),
                         (_DATE_DIGITIZED, _ASCII, len(stamp), stamp)]
    if colour is not None:
        exif_entries.append((_COLOR_SPACE, _SHORT, 1, colour.to_bytes(2, "big")))
    if exif_entries:
        exif_entries.append((_EXIF_VERSION, _UNDEFINED, 4, b"0232"))

    ifd0: list[tuple[int, int, int, bytes]] = []
    if orientation is not None:
        ifd0.append((_ORIENTATION, _SHORT, 1, orientation.to_bytes(2, "big")))
    if not ifd0 and not exif_entries:
        return None
    if exif_entries:
        # Where the EXIF IFD will start is IFD0's length, which does not depend
        # on the pointer's value — so a placeholder sizes it.
        placeholder = [*ifd0, (_EXIF_IFD, _LONG, 1, bytes(4))]
        at = 8 + len(_ifd(placeholder, 8))
        ifd0 = [*ifd0, (_EXIF_IFD, _LONG, 1, at.to_bytes(4, "big"))]
        first = _ifd(ifd0, 8)
        return b"MM\x00*" + (8).to_bytes(4, "big") + first + _ifd(exif_entries, at)
    return b"MM\x00*" + (8).to_bytes(4, "big") + _ifd(ifd0, 8)


def _ifd(entries: list[tuple[int, int, int, bytes]], start: int) -> bytes:
    """One IFD at offset `start`, its out-of-line values straight after it."""
    entries = sorted(entries)
    data_at = start + 2 + 12 * len(entries) + 4
    head = len(entries).to_bytes(2, "big")
    tail = b""
    for tag, kind, count, value in entries:
        if len(value) <= 4:
            slot = value.ljust(4, b"\x00")
        else:
            slot = (data_at + len(tail)).to_bytes(4, "big")
            tail += value + (b"\x00" if len(value) % 2 else b"")
        head += (tag.to_bytes(2, "big") + kind.to_bytes(2, "big")
                 + count.to_bytes(4, "big") + slot)
    return head + bytes(4) + tail


def _xmp(source_id: str) -> bytes:
    """An XMP packet saying only which master this came from."""
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        f'<rdf:Description rdf:about="" xmlns:pix="{PIX_NS}"'
        f' pix:SourceId={quoteattr(source_id)}/>'
        '</rdf:RDF></x:xmpmeta><?xpacket end="r"?>'
    ).encode("utf-8")


# --- names ---------------------------------------------------------------

_SUFFIX = re.compile(r"^\.[a-z0-9]{1,8}$")


def name_for(taken: datetime | None, suffix: str,
             source_id: str | None) -> str:
    """What a download is called: its date, never its path.

    Master names are the old library's paths — a folder, an event, sometimes
    a person — so none of a master's name survives into the copy. With no
    date, a few characters of the source id: still nothing about anyone, and
    still the same name every time the same file is taken.
    """
    ext = suffix.lower()
    if not _SUFFIX.match(ext):
        ext = ""
    if taken is not None:
        return taken.strftime("%Y-%m-%d_%H%M%S") + ext
    tail = (source_id or "").partition(":")[2][:8]
    return (f"pix_{tail}" if tail else "pix") + ext


def unique(name: str, taken: set[str]) -> str:
    """`name`, numbered `_2`, `_3`, … if already used — a zip is flat, and two
    photographs from the same second would otherwise be one file."""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    candidate, n = name, 1
    while candidate in taken:
        n += 1
        candidate = f"{stem}_{n}" + (f".{ext}" if dot else "")
    taken.add(candidate)
    return candidate


# --- the download hash ---------------------------------------------------

def record(ledger: Path, digest: str, folder: str, name: str) -> None:
    """Note that a copy hashing to `digest` was handed out for `folder/name`.

    Only called when the copy hashes differently from its source; once per
    hash, since the same file always cleans to the same bytes.
    """
    if digest in recorded(ledger):
        return
    ledger.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"hash": digest, "folder": folder, "name": name},
                      ensure_ascii=False)
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def recorded(ledger: Path) -> dict[str, str]:
    """Download hash -> `folder/name`, for everything recorded so far."""
    try:
        text = ledger.read_text(encoding="utf-8")
    except OSError:
        return {}
    found: dict[str, str] = {}
    for line in text.splitlines():
        try:
            raw: object = json.loads(line)
        except ValueError:
            # A line cut short by a crash costs that one record, not the file.
            continue
        if isinstance(raw, dict):
            entry: dict[str, object] = raw  # pyright: ignore[reportUnknownVariableType]
            digest, folder, name = (entry.get("hash"), entry.get("folder"),
                                    entry.get("name"))
            if (isinstance(digest, str) and isinstance(folder, str)
                    and isinstance(name, str)):
                found.setdefault(digest, f"{folder}/{name}")
    return found
