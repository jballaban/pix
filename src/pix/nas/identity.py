"""What makes two files the same photograph (spec/nas-app.md §15).

Two hashes, both facts about a file rather than judgements about it, both
written into the meta tier by `process` because that is the one moment the whole
library is being read anyway.

**The content hash covers the coded image and nothing else.** Every metadata
segment is skipped — EXIF, XMP, ICC, embedded thumbnails — so a photograph
AirDropped to a second phone and imported from there matches the copy already in
master, despite the receiving device having re-wrapped it, re-tagged it and
renamed it. A hash of the *file* says different; a person says obviously the
same. This is what lets the machine agree with the person without a threshold to
tune: two files either carry the same coded image or they do not.

**Deliberately not a hash of the decoded pixels**, which is equally free here
since `process` decodes anyway. Two versions of libjpeg can differ in the last
bit of an IDCT, so a decoder upgrade would silently change every hash in the
archive with nothing to tell that from a real change. Reading the coded stream
needs no decoder at all, and is therefore stable for as long as the archive is.

The hash is prefixed by how it was taken — `j:` from a JPEG's segments, `m:`
from an ISO base-media file's `mdat`, `f:` from the whole file where the format
is not one this knows. The prefix keeps the three from ever colliding, and says
in the value itself how much metadata blindness it is promising: `f:` promises
none, so two copies matching under it is a stronger claim, not a weaker one.
"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from blake3 import blake3

#: Read size for the streaming paths. Large enough that a multi-gigabyte `mdat`
#: is not ten thousand round trips over SMB, small enough that eight workers
#: hashing at once do not each hold a video in memory.
_CHUNK: int = 1 << 20

#: Markers carrying no payload, so no length follows them.
_STANDALONE: frozenset[int] = frozenset({0x01, 0xD8, 0xD9, *range(0xD0, 0xD8)})


def content_hash(path: Path) -> str | None:
    """The coded image data of `path`, hashed. `None` if it cannot be read.

    Dispatches on what the bytes say rather than on the extension: a name is a
    claim about a file and the first four bytes are a fact about it, and this is
    the one place in the app where being wrong means calling two photographs the
    same.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(12)
            if head[:3] == b"\xff\xd8\xff":
                return _jpeg(fh)
            if head[4:8] == b"ftyp":
                digest = _isobmff(fh)
                # A container with no `mdat` is either fragmented or something
                # this does not understand; the whole file is then the honest
                # answer rather than a hash of nothing.
                return digest if digest is not None else _whole(fh)
            return _whole(fh)
    except OSError:
        return None


def perceptual_hash(path: Path) -> str | None:
    """A 64-bit dHash of `path` as 16 hex characters, or `None`.

    Difference hashing: reduce to a 9×8 grey image and record, for each row,
    which way the brightness goes between neighbouring pixels. That comparison
    is what makes it survive re-compression and rescaling — a messaging app's
    1600px copy of a 4032px original lands on the same value — and it is why
    this is the tool for derivatives and the wrong tool for duplicates, which
    are a question with an exact answer.

    Stored now and read by nothing yet. `process` holds the decoded pixels
    exactly once in a file's life; wanting this later means reading 2.3TB back
    off the array to get them again.

    Pillow is imported here rather than at module scope. The app serves the
    derived tiers and never decodes, so its container ships without Pillow — and
    a module-level import would make merely importing this fail there.
    """
    from PIL import Image, ImageOps

    try:
        with Image.open(path) as im:
            # Orientation applied, not carried: the same photograph rotated by a
            # tag is the same photograph, and a copy that had the tag baked in
            # during re-encoding must not read as a different one.
            grey = ImageOps.exif_transpose(im).convert("L").resize(
                (9, 8), Image.Resampling.LANCZOS)
            # `tobytes` rather than `getdata`: one row-major buffer, and the
            # deprecation notice on the other one says it leaves in Pillow 14.
            px = grey.tobytes()
    except Exception:                                      # noqa: BLE001
        # Every failure is the same failure here — unreadable, unsupported,
        # truncated — and none of them is a reason to fail the file's metadata.
        return None

    bits = 0
    for row in range(8):
        for col in range(8):
            bits <<= 1
            if px[row * 9 + col] > px[row * 9 + col + 1]:
                bits |= 1
    return f"{bits:016x}"


def _jpeg(fh: BinaryIO) -> str | None:
    """Every JPEG segment that defines the image, plus the scan itself.

    Skipped: `APPn`, which is where EXIF, XMP, ICC profiles and embedded
    thumbnails live, and `COM`. Kept: the quantisation and Huffman tables, the
    frame header, the restart interval and the entropy-coded data — everything a
    decoder needs and nothing a tagger touches.
    """
    fh.seek(0)
    data = fh.read()
    if not data.startswith(b"\xff\xd8"):
        return None

    digest = blake3()
    at = 2
    end = len(data)
    while at < end - 1:
        if data[at] != 0xFF:
            return None
        # Fill bytes are legal padding before a marker and carry no meaning.
        while at < end and data[at] == 0xFF:
            at += 1
        if at >= end:
            break
        marker = data[at]
        at += 1
        if marker in _STANDALONE:
            continue
        if at + 2 > end:
            break
        length = int.from_bytes(data[at:at + 2], "big")
        if length < 2:
            return None
        body = data[at + 2:at + length]
        if marker == 0xDA:
            # The scan header, then the entropy-coded data to the end of image.
            # Trailing bytes after EOI are somebody else's — a phone's appended
            # payload, a broken transfer — and are not part of the photograph.
            digest.update(bytes([marker]))
            digest.update(body)
            rest = data[at + length:]
            cut = rest.rfind(b"\xff\xd9")
            digest.update(rest if cut < 0 else rest[:cut + 2])
            return "j:" + digest.hexdigest()
        if not (0xE0 <= marker <= 0xEF or marker == 0xFE):
            digest.update(bytes([marker]))
            digest.update(body)
        at += length
    return None


def _isobmff(fh: BinaryIO) -> str | None:
    """The `mdat` payloads of an ISO base-media file, hashed in order.

    One walk covers MP4, MOV, HEIC and `.insv`, because they are the same
    container: the coded samples of a video and the coded extents of a HEIC's
    primary image both live in `mdat`, and everything a tagger writes lives in
    `moov`, `meta` or `uuid` boxes that this never reads.
    """
    fh.seek(0)
    digest = blake3()
    found = False
    at = 0
    while True:
        fh.seek(at)
        header = fh.read(8)
        if len(header) < 8:
            break
        size = int.from_bytes(header[:4], "big")
        kind = header[4:8]
        body = at + 8
        if size == 1:
            wide = fh.read(8)
            if len(wide) < 8:
                break
            size = int.from_bytes(wide, "big")
            body = at + 16
        elif size == 0:
            # Runs to the end of the file, which is legal for the last box.
            size = _remaining(fh, at)
        if size < 8:
            break
        if kind == b"mdat":
            found = True
            _stream(fh, body, at + size, digest)
        at += size
    return ("m:" + digest.hexdigest()) if found else None


def _remaining(fh: BinaryIO, at: int) -> int:
    """How many bytes are left from `at`, for a box declaring size zero."""
    here = fh.tell()
    try:
        fh.seek(0, 2)
        return fh.tell() - at
    finally:
        fh.seek(here)


def _stream(fh: BinaryIO, start: int, stop: int, digest: object) -> None:
    """Feed `[start, stop)` into `digest` a chunk at a time."""
    fh.seek(start)
    left = stop - start
    update = getattr(digest, "update")
    while left > 0:
        block = fh.read(min(_CHUNK, left))
        if not block:
            return
        update(block)
        left -= len(block)


def _whole(fh: BinaryIO) -> str:
    """The entire file — the fallback when the format is not one we parse."""
    fh.seek(0)
    digest = blake3()
    while True:
        block = fh.read(_CHUNK)
        if not block:
            break
        digest.update(block)
    return "f:" + digest.hexdigest()
