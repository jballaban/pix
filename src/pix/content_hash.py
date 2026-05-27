"""Format-aware content hashing for `pix:ContentHash`.

Per spec/tags.md → System fields: pix stores a BLAKE3-256 hex digest of
each file's content, with metadata sections skipped so that TAG writes
(which change bytes inside EXIF/XMP/IPTC regions) don't invalidate the
hash. The hash is computed on first migrate and re-computed only when
content changes (i.e. CONVERT).

Format-specific framing:

- **JPEG** — scan the marker stream; skip every APPn segment (markers
  `0xFF 0xE0` through `0xFF 0xEF`, which is where EXIF/XMP/IPTC/Photoshop
  metadata lives); hash all other bytes including the entropy-coded scan
  data. Correctly handles `FF 00` escapes and restart markers (`FF D0` -
  `FF D7`) within scan data.

- **MP4 / ISO BMFF** (`.mp4`, `.mov`, `.m4v`, `.3gp`) — walk the box tree
  and hash the payload of every `mdat` box (the actual encoded media
  bytes); everything else (`ftyp`, `moov`, `udta`, `meta`, `uuid`, `free`,
  ...) is structure or metadata and excluded. Robust against any metadata
  write regardless of where ExifTool places it.
"""

from __future__ import annotations

from pathlib import Path

from blake3 import blake3


_VIDEO_EXTS: frozenset[str] = frozenset(
    {"mp4", "mov", "m4v", "3gp"}
)


def compute_content_hash(path: Path) -> str:
    """Return the format-aware BLAKE3 hex digest for `path`.

    Format is detected by file extension. Unknown formats are hashed as
    a raw byte stream (no metadata stripping) — better than nothing for
    files that slip through, but the canonical formats (JPEG / MP4) are
    the ones that get the stable metadata-invariant treatment.
    """
    ext = path.suffix.lower().lstrip(".")
    if ext == "jpg" or ext == "jpeg":
        return hash_jpeg(path)
    if ext in _VIDEO_EXTS:
        return hash_mp4(path)
    # Fallback: raw bytes. Should be rare — most extensions in the
    # default policy are JPEG or MP4 by this point in the pipeline.
    return _hash_raw(path)


def hash_jpeg(path: Path) -> str:
    """BLAKE3 of a JPEG with all APPn segments stripped.

    APPn markers (`0xFF 0xE0` through `0xFF 0xEF`) carry EXIF, XMP, IPTC,
    Photoshop metadata, ICC profiles, JFIF identification, and various
    vendor extensions. All of these are excluded from the hash so that
    metadata-only edits don't invalidate it.
    """
    hasher = blake3()
    with path.open("rb") as f:
        data = f.read()

    n = len(data)
    i = 0
    while i < n:
        # Most of the file is entropy-coded scan data with long runs of
        # non-`FF` bytes. `bytes.find` jumps to the next marker candidate
        # in C, and we feed the intervening run to BLAKE3 in one update.
        # Equivalent to byte-at-a-time but orders of magnitude faster.
        next_ff = data.find(b"\xff", i)
        if next_ff < 0:
            hasher.update(data[i:n])
            break
        if next_ff > i:
            hasher.update(data[i:next_ff])
            i = next_ff

        if i + 1 >= n:
            # Trailing stray FF; hash and bail.
            hasher.update(data[i:n])
            break

        marker = data[i + 1]

        # `FF 00` is an escape in scan data (literal FF byte). Hash the
        # pair and move on.
        if marker == 0x00:
            hasher.update(data[i : i + 2])
            i += 2
            continue

        # Fill bytes: a chain of `FF` bytes can precede a real marker;
        # hash one and re-check.
        if marker == 0xFF:
            hasher.update(data[i : i + 1])
            i += 1
            continue

        # Markers without a length field:
        #   SOI=D8, EOI=D9, TEM=01, RST0-7=D0-D7
        if (
            marker in (0x01, 0xD8, 0xD9)
            or 0xD0 <= marker <= 0xD7
        ):
            hasher.update(data[i : i + 2])
            i += 2
            continue

        # All other segments have a 2-byte big-endian length following
        # the marker (length includes its own 2 bytes).
        if i + 4 > n:
            hasher.update(data[i:n])
            break
        length = (data[i + 2] << 8) | data[i + 3]
        seg_end = i + 2 + length
        if seg_end > n:
            seg_end = n

        if 0xE0 <= marker <= 0xEF:
            # APPn — skip the entire segment.
            i = seg_end
            continue

        # Other length-prefixed segments (SOF, DHT, DQT, SOS, DRI, COM, ...).
        # Hash the marker, length, and segment data; then continue.
        hasher.update(data[i:seg_end])
        i = seg_end

    return hasher.hexdigest()


def hash_mp4(path: Path) -> str:
    """BLAKE3 of the concatenated `mdat` payloads in an ISO BMFF file."""
    hasher = blake3()
    with path.open("rb") as f:
        data = f.read()

    n = len(data)
    i = 0
    while i + 8 <= n:
        size = int.from_bytes(data[i : i + 4], "big")
        type_ = data[i + 4 : i + 8]

        if size == 1:
            # Large box — next 8 bytes are extended 64-bit size.
            if i + 16 > n:
                break
            size = int.from_bytes(data[i + 8 : i + 16], "big")
            header_len = 16
        elif size == 0:
            # Box extends to end of file.
            size = n - i
            header_len = 8
        else:
            header_len = 8

        if size < header_len or i + size > n:
            break  # malformed

        if type_ == b"mdat":
            hasher.update(data[i + header_len : i + size])

        i += size

    return hasher.hexdigest()


def _hash_raw(path: Path) -> str:
    """Fallback: hash raw bytes (for formats we don't have framing for)."""
    hasher = blake3()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()
