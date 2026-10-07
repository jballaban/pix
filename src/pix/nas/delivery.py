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

**A video is cleaned where it lies.** Its metadata sits in boxes inside `moov`
whose positions the rest of the file depends on, so nothing is removed: each
box is relabelled `free` and zeroed, at the same length, and the sample data
of every track that is not picture or sound is zeroed as it streams past. The
stamp is appended as a box of its own after everything else.

No web app in here, so it is testable on its own and usable by anything else
that hands files out.
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, NamedTuple, Protocol
from xml.sax.saxutils import quoteattr

from pix.nas.decisions import PIX_NS


class Uncleanable(ValueError):
    """A file that looks like a JPEG or an MP4 but cannot be walked.

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


# --- videos --------------------------------------------------------------

#: The extensions cleaned as ISO base-media. `.insv` is the same container
#: with a proprietary trailer Insta360's own tools need, and HEIC keeps its
#: picture in items rather than tracks; both go out unchanged for now
#: (spec/metadata-cleanup.md §8).
VIDEO_SUFFIXES: frozenset[str] = frozenset({".mp4", ".mov", ".m4v"})

#: Tracks a copy keeps. Everything else — GoPro telemetry and its GPS trace,
#: timecode, camera-data tracks, a phone's timed metadata — goes.
_KEPT_TRACKS: frozenset[bytes] = frozenset({b"vide", b"soun"})

#: Top-level boxes a copy keeps as they are. `free`, `skip` and `wide` keep
#: their name but lose whatever a writer left in them.
_TOP_KEPT: frozenset[bytes] = frozenset({b"ftyp", b"moov", b"mdat"})
_PADDING: frozenset[bytes] = frozenset({b"free", b"skip", b"wide"})

#: Inside `moov`: what playback needs. Anything else — `udta` (location, make,
#: model), `meta` (a phone's keys), `uuid` (XMP) — is freed.
_MOOV_KEPT: frozenset[bytes] = frozenset({b"mvhd", b"trak", b"iods"})
#: Inside a kept `trak`. `tref` goes because the tracks it points at may not
#: be there any more.
_TRAK_KEPT: frozenset[bytes] = frozenset({b"tkhd", b"edts", b"mdia"})

#: XMP's box, as ExifTool and Adobe write it at the top level of an MP4.
_XMP_UUID: bytes = bytes.fromhex("BE7ACFCB97A942E89C71999491E3AFAC")


class _Box(NamedTuple):
    kind: bytes
    at: int        # where the box starts, in whatever it was read from
    header: int    # 8, or 16 with a 64-bit size
    size: int

    @property
    def body(self) -> int:
        return self.at + self.header

    @property
    def end(self) -> int:
        return self.at + self.size


@dataclass(frozen=True)
class VideoPlan:
    """What to change in a video as it streams out.

    `edits` are `(start, end, bytes)` replacements of the same length, or
    `None` for zeros, in file order. `size` is how much of the source is sent
    — less than the file when junk follows the last box — and `tail` is
    appended after it. `mdat` are the coded data's ranges, for hashing what is
    sent; `zeroed` says whether any of it changed, which is when that hash is
    worth recording.
    """

    edits: tuple[tuple[int, int, bytes | None], ...]
    size: int
    tail: bytes
    mdat: tuple[tuple[int, int], ...]
    zeroed: bool

    @property
    def length(self) -> int:
        return self.size + len(self.tail)


class _Digest(Protocol):
    def update(self, data: memoryview, /) -> object: ...


def plan_video(fh: BinaryIO, size: int, *, shift: int = 0,
               source_id: str | None) -> VideoPlan:
    """How to clean the ISO base-media file open as `fh`.

    `shift` is seconds to add to the movie's own creation times — the
    difference a curator's date correction makes — so the copy files under
    the corrected day, as a photograph's copy does.
    """
    top: list[_Box] = []
    sent = size
    for box in _walk_file(fh, size):
        if box is None:
            # Something after the last box that is not one: junk, or a
            # trailer of somebody's. Not sent.
            sent = top[-1].end if top else 0
            break
        top.append(box)
    if any(b.kind in (b"moof", b"mfra") for b in top):
        raise Uncleanable("fragmented video")
    moovs = [b for b in top if b.kind == b"moov"]
    if len(moovs) != 1 or not any(b.kind == b"mdat" for b in top):
        raise Uncleanable("not a single-movie file")

    edits: list[tuple[int, int, bytes | None]] = []
    samples: list[tuple[int, int]] = []
    for box in top:
        if box.kind in _TOP_KEPT:
            continue
        if box.kind in _PADDING:
            edits.append((box.body, box.end, None))
        else:
            _free(edits, box, 0)

    moov = moovs[0]
    fh.seek(moov.at)
    data = fh.read(moov.size)
    if len(data) != moov.size:
        raise Uncleanable("movie header cut short")
    _clean_moov(data, moov.at, edits, samples, shift)

    for start, end in _merge(samples):
        end = min(end, sent)
        if start < end:
            edits.append((start, end, None))

    tail = b""
    if source_id:
        fixed = _true_size(fh, top[-1])
        if fixed is not None:
            edits.extend(fixed)
            tail = _uuid_box(source_id)

    mdat = tuple((b.body, min(b.end, sent)) for b in top if b.kind == b"mdat")
    return VideoPlan(edits=tuple(sorted(edits, key=lambda e: e[0])), size=sent,
                     tail=tail, mdat=mdat, zeroed=bool(samples))


def stream_video(fh: BinaryIO, plan: VideoPlan, digest: _Digest | None = None,
                 chunk: int = 1 << 20) -> Iterator[bytes]:
    """The cleaned video, a chunk at a time, `digest` fed its coded data.

    Nothing is held but the chunk in flight, so a multi-gigabyte video costs
    the NAS a buffer and not a copy.
    """
    edits = plan.edits
    # Edits never overlap and are in file order, so the first one that has
    # not yet ended only ever moves forward.
    first = 0
    fh.seek(0)
    pos = 0
    while pos < plan.size:
        block = bytearray(fh.read(min(chunk, plan.size - pos)))
        if not block:
            raise OSError("the file is shorter than it was")
        end = pos + len(block)
        while first < len(edits) and edits[first][1] <= pos:
            first += 1
        i = first
        while i < len(edits) and edits[i][0] < end:
            s, e, rep = edits[i]
            a, b = max(s, pos), min(e, end)
            if a < b:
                block[a - pos:b - pos] = (rep[a - s:b - s] if rep is not None
                                          else bytes(b - a))
            i += 1
        if digest is not None:
            view = memoryview(block)
            for s, e in plan.mdat:
                a, b = max(s, pos), min(e, end)
                if a < b:
                    digest.update(view[a - pos:b - pos])
        yield bytes(block)
        pos = end
    if plan.tail:
        yield plan.tail


def _walk_file(fh: BinaryIO, size: int) -> Iterator[_Box | None]:
    """Top-level boxes, then `None` if something after them is not one."""
    at = 0
    while at < size:
        fh.seek(at)
        head = fh.read(16)
        box = _parse(head, 0, len(head), size - at)
        # A name that is not text is junk that happened to have a plausible
        # size — what a trailer after the last box usually looks like.
        if box is None or not box.kind.isascii() or not box.kind.isalnum():
            yield None
            return
        yield box._replace(at=at)
        at += box.size


def _true_size(fh: BinaryIO,
               last: _Box) -> list[tuple[int, int, bytes | None]] | None:
    """Edits that make the last box's header say where it really ends, so a
    stamp appended after it is not read as part of it — or None where that
    cannot be said, and the copy goes unstamped.

    It already does, almost always. A box declaring *to the end of the file*
    (size zero) or more than the file holds (a cut-short recording) would
    swallow whatever follows.
    """
    fh.seek(last.at)
    head = fh.read(16)
    declared = int.from_bytes(head[:4], "big")
    if last.header == 16:
        if int.from_bytes(head[8:16], "big") == last.size:
            return []
        return [(last.at + 8, last.at + 16, last.size.to_bytes(8, "big"))]
    if declared == last.size:
        return []
    if last.size >= 1 << 32:
        return None
    return [(last.at, last.at + 4, last.size.to_bytes(4, "big"))]


def _parse(buf: bytes, at: int, end: int, room: int) -> _Box | None:
    """The box header at `buf[at:]`, or None if there is not a sound one.

    `room` is how far the box may run. Size zero means *to the end*; a box
    claiming more than the room is a truncated last box and taken to the end
    only if it is the coded data, which a cut-short recording leaves.
    """
    if end - at < 8:
        return None
    size, kind = struct.unpack_from(">I4s", buf, at)
    header = 8
    if size == 1:
        if end - at < 16:
            return None
        size = struct.unpack_from(">Q", buf, at + 8)[0]
        header = 16
    elif size == 0:
        size = room
    if size < header:
        return None
    if size > room:
        if kind != b"mdat":
            return None
        size = room
    return _Box(kind, at, header, size)


def _children(buf: bytes, start: int, end: int) -> Iterator[_Box]:
    at = start
    while at + 8 <= end:
        box = _parse(buf, at, end, end - at)
        if box is None:
            raise Uncleanable("malformed box")
        yield box
        at = box.end


def _free(edits: list[tuple[int, int, bytes | None]], box: _Box,
          base: int) -> None:
    """Relabel `box` as `free` and zero what it held."""
    edits.append((base + box.at + 4, base + box.at + 8, b"free"))
    edits.append((base + box.body, base + box.end, None))


def _clean_moov(data: bytes, base: int,
                edits: list[tuple[int, int, bytes | None]],
                samples: list[tuple[int, int]], shift: int) -> None:
    """Free what `moov` holds that playback does not need, and find the
    sample data of every track that is not picture or sound."""
    moov = _parse(data, 0, len(data), len(data))
    if moov is None:
        raise Uncleanable("malformed movie header")
    for box in _children(data, moov.body, moov.end):
        if box.kind == b"mvhd":
            _shift_times(data, box, base, edits, shift)
        elif box.kind == b"trak":
            handler = _handler(data, box)
            if handler in _KEPT_TRACKS:
                _clean_trak(data, box, base, edits, shift, handler)
            else:
                samples.extend(_sample_ranges(data, box))
                _free(edits, box, base)
        elif box.kind == b"cmov":
            raise Uncleanable("compressed movie header")
        elif box.kind not in _MOOV_KEPT:
            _free(edits, box, base)


def _clean_trak(data: bytes, trak: _Box, base: int,
                edits: list[tuple[int, int, bytes | None]], shift: int,
                handler: bytes) -> None:
    for box in _children(data, trak.body, trak.end):
        if box.kind == b"tkhd":
            _shift_times(data, box, base, edits, shift)
        elif box.kind == b"mdia":
            _clean_mdia(data, box, base, edits, shift, handler)
        elif box.kind not in _TRAK_KEPT:
            _free(edits, box, base)


def _clean_mdia(data: bytes, mdia: _Box, base: int,
                edits: list[tuple[int, int, bytes | None]], shift: int,
                handler: bytes) -> None:
    """Dates shifted; handler names — *GoPro AVC*, *Core Media Video* — and
    the encoder's name in the sample description blanked."""
    for box in _children(data, mdia.body, mdia.end):
        if box.kind == b"mdhd":
            _shift_times(data, box, base, edits, shift)
        elif box.kind == b"hdlr":
            _blank_name(box, base, edits)
        elif box.kind == b"minf":
            for inner in _children(data, box.body, box.end):
                if inner.kind == b"hdlr":
                    _blank_name(inner, base, edits)
                elif inner.kind == b"stbl" and handler == b"vide":
                    _blank_compressor(data, inner, base, edits)
                elif inner.kind in (b"udta", b"meta", b"uuid"):
                    _free(edits, inner, base)
        elif box.kind in (b"udta", b"meta", b"uuid"):
            _free(edits, box, base)


def _handler(data: bytes, trak: _Box) -> bytes | None:
    for box in _children(data, trak.body, trak.end):
        if box.kind == b"mdia":
            for inner in _children(data, box.body, box.end):
                if inner.kind == b"hdlr" and inner.end - inner.body >= 12:
                    return data[inner.body + 8:inner.body + 12]
    return None


def _blank_name(hdlr: _Box, base: int,
                edits: list[tuple[int, int, bytes | None]]) -> None:
    """Zero the name after the handler's fixed 24 bytes — an empty string
    whether it was written C-style or, as QuickTime does, with a length."""
    start = hdlr.body + 24
    if start < hdlr.end:
        edits.append((base + start, base + hdlr.end, None))


def _blank_compressor(data: bytes, stbl: _Box, base: int,
                      edits: list[tuple[int, int, bytes | None]]) -> None:
    """The 32-byte compressor name of each visual sample entry."""
    for box in _children(data, stbl.body, stbl.end):
        if box.kind != b"stsd":
            continue
        count = int.from_bytes(data[box.body + 4:box.body + 8], "big")
        at = box.body + 8
        for _ in range(count):
            entry = _parse(data, at, box.end, box.end - at)
            if entry is None:
                raise Uncleanable("malformed sample description")
            # Sample entry (8), then the visual fields up to the name (34).
            name = entry.body + 8 + 34
            if name + 32 <= entry.end:
                edits.append((base + name, base + name + 32, None))
            at = entry.end


def _shift_times(data: bytes, box: _Box, base: int,
                 edits: list[tuple[int, int, bytes | None]], shift: int) -> None:
    """Move a header's creation and modification times by `shift` seconds."""
    if not shift:
        return
    width = 8 if data[box.body] == 1 else 4
    for slot in (box.body + 4, box.body + 4 + width):
        if slot + width > box.end:
            return
        value = int.from_bytes(data[slot:slot + width], "big")
        if value == 0:
            continue                 # never set; a shifted zero is a lie
        moved = min(max(value + shift, 1), (1 << (8 * width)) - 1)
        edits.append((base + slot, base + slot + width,
                      moved.to_bytes(width, "big")))


def _sample_ranges(data: bytes, trak: _Box) -> list[tuple[int, int]]:
    """Where in the file a track's samples are, one range per chunk.

    From the sample tables: `stco`/`co64` say where each chunk starts, `stsc`
    how many samples each chunk holds, `stsz`/`stz2` how big each sample is.
    A track whose tables cannot be read is refused rather than guessed at: its
    data is the part of the file this exists to remove.
    """
    stbl = _find(data, trak, (b"mdia", b"minf", b"stbl"))
    if stbl is None:
        return []
    tables = {box.kind: box for box in _children(data, stbl.body, stbl.end)}
    try:
        offsets = _chunk_offsets(data, tables)
        sizes = _sample_sizes(data, tables)
        runs = _chunk_runs(data, tables)
    except (IndexError, struct.error) as e:
        raise Uncleanable("unreadable sample tables") from e
    if not offsets:
        return []
    if runs is None or sizes is None:
        raise Uncleanable("incomplete sample tables")

    ranges: list[tuple[int, int]] = []
    sample = 0
    run = -1
    for i, offset in enumerate(offsets, start=1):
        # Each run holds from its first chunk until the next run's.
        while run + 1 < len(runs) and runs[run + 1][0] <= i:
            run += 1
        per = runs[run][1] if run >= 0 else 0
        length = sum(sizes(sample + k) for k in range(per))
        sample += per
        if length:
            ranges.append((offset, offset + length))
    return ranges


def _find(data: bytes, box: _Box, path: tuple[bytes, ...]) -> _Box | None:
    for kind in path:
        found = next((b for b in _children(data, box.body, box.end)
                      if b.kind == kind), None)
        if found is None:
            return None
        box = found
    return box


def _chunk_offsets(data: bytes, tables: dict[bytes, _Box]) -> list[int]:
    for kind, width in ((b"stco", 4), (b"co64", 8)):
        box = tables.get(kind)
        if box is not None:
            count = int.from_bytes(data[box.body + 4:box.body + 8], "big")
            at = box.body + 8
            if at + count * width > box.end:
                raise IndexError("chunk offsets run past their box")
            return [int.from_bytes(data[at + i * width:at + (i + 1) * width],
                                   "big") for i in range(count)]
    return []


def _chunk_runs(data: bytes, tables: dict[bytes, _Box]) -> list[tuple[int, int]] | None:
    box = tables.get(b"stsc")
    if box is None:
        return None
    count = int.from_bytes(data[box.body + 4:box.body + 8], "big")
    at = box.body + 8
    if at + count * 12 > box.end:
        raise IndexError("sample-to-chunk runs past its box")
    return [struct.unpack_from(">II", data, at + i * 12)
            for i in range(count)]


def _sample_sizes(data: bytes, tables: dict[bytes, _Box]
                  ) -> Callable[[int], int] | None:
    """A function from sample number (0-based) to its size, or None."""
    box = tables.get(b"stsz")
    if box is not None:
        uniform, count = struct.unpack_from(">II", data, box.body + 4)
        if uniform:
            return lambda i: uniform if i < count else 0
        at = box.body + 12
        if at + count * 4 > box.end:
            raise IndexError("sample sizes run past their box")
        table = struct.unpack_from(f">{count}I", data, at)
        return lambda i: table[i] if i < count else 0
    box = tables.get(b"stz2")
    if box is not None:
        field = data[box.body + 7]
        count = int.from_bytes(data[box.body + 8:box.body + 12], "big")
        at = box.body + 12
        if field == 16:
            table = struct.unpack_from(f">{count}H", data, at)
        elif field == 8:
            table = tuple(data[at:at + count])
        elif field == 4:
            packed = data[at:at + (count + 1) // 2]
            table = tuple((packed[i // 2] >> (4 if i % 2 == 0 else 0)) & 0xF
                          for i in range(count))
        else:
            raise IndexError("unknown compact sample size")
        if len(table) != count:
            raise IndexError("compact sample sizes run past their box")
        return lambda i: table[i] if i < count else 0
    return None


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _uuid_box(source_id: str) -> bytes:
    body = _XMP_UUID + _xmp(source_id)
    return (len(body) + 8).to_bytes(4, "big") + b"uuid" + body


def date_shift(captured: datetime | None, effective: datetime | None) -> int:
    """Seconds a curator's correction moved a file's date — what a video's own
    times are moved by, since those are UTC and the dates here are local."""
    if captured is None or effective is None:
        return 0
    return int((effective - captured).total_seconds())


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
