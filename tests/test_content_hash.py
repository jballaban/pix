from __future__ import annotations

import struct
from pathlib import Path

from pix.content_hash import compute_content_hash, hash_jpeg, hash_mp4


def _make_jpeg(tmp_path: Path, name: str, *, exif: bytes | None = None) -> Path:
    """Synthesize a minimal valid JPEG with an optional APP1 (EXIF) segment.

    Layout:
      SOI  FF D8
      [APP1 FF E1 + length + EXIF payload]      (optional)
      DQT  FF DB + length + 1 table (65 bytes)
      SOF0 FF C0 + length + frame params
      DHT  FF C4 + length + huffman tables
      SOS  FF DA + length + scan params
      <scan data>
      EOI  FF D9
    """
    out = bytearray()
    out += b"\xff\xd8"  # SOI

    if exif is not None:
        seg = exif
        out += b"\xff\xe1"
        out += struct.pack(">H", len(seg) + 2)
        out += seg

    # DQT: 1 quant table, 64 bytes of value + 1 byte header
    out += b"\xff\xdb"
    out += struct.pack(">H", 67)  # length includes itself
    out += bytes([0]) + bytes(64)

    # SOF0: baseline, 1 sample component, 1x1 image
    out += b"\xff\xc0"
    out += struct.pack(">H", 11)
    out += bytes([8, 0, 1, 0, 1, 1, 0x11, 0])

    # DHT: minimal
    out += b"\xff\xc4"
    out += struct.pack(">H", 21)
    out += bytes([0] * 17 + [0, 0])  # 19 bytes of tables

    # SOS: 1 component
    out += b"\xff\xda"
    out += struct.pack(">H", 8)
    out += bytes([1, 0, 0, 0, 0x3f, 0])

    # Scan data + EOI
    out += b"\x00"
    out += b"\xff\xd9"

    path = tmp_path / name
    path.write_bytes(bytes(out))
    return path


def test_jpeg_hash_is_stable_for_byte_identical_files(tmp_path: Path) -> None:
    a = _make_jpeg(tmp_path, "a.jpg")
    b = _make_jpeg(tmp_path, "b.jpg")
    assert hash_jpeg(a) == hash_jpeg(b)


def test_jpeg_hash_ignores_app1_exif_changes(tmp_path: Path) -> None:
    """Same image data + different EXIF must hash the same."""
    a = _make_jpeg(tmp_path, "a.jpg", exif=b"Exif\x00\x00" + b"AAAA" * 4)
    b = _make_jpeg(tmp_path, "b.jpg", exif=b"Exif\x00\x00" + b"ZZZZ" * 4)
    c = _make_jpeg(tmp_path, "c.jpg")  # no APP1 at all
    h_a = hash_jpeg(a)
    h_b = hash_jpeg(b)
    h_c = hash_jpeg(c)
    assert h_a == h_b == h_c


def test_jpeg_hash_changes_when_scan_data_changes(tmp_path: Path) -> None:
    """Modifying actual image bytes (scan data) must change the hash."""
    a = _make_jpeg(tmp_path, "a.jpg")
    b_bytes = bytearray(a.read_bytes())
    # The scan data byte we wrote is right before EOI. Flip it.
    eoi_pos = b_bytes.rfind(b"\xff\xd9")
    b_bytes[eoi_pos - 1] = 0x42
    b = tmp_path / "b.jpg"
    b.write_bytes(bytes(b_bytes))
    assert hash_jpeg(a) != hash_jpeg(b)


def _make_mp4(tmp_path: Path, name: str, *, mdat: bytes, udta: bytes | None = None) -> Path:
    """Synthesize a minimal MP4-ish file: ftyp + mdat (+ optional udta).

    Not a strictly valid MP4 (no moov), but the box framing is real, which
    is all our hash cares about.
    """
    out = bytearray()
    # ftyp box: size + 'ftyp' + brand + minor + compat brands
    ftyp_payload = b"isom" + struct.pack(">I", 512) + b"isom" + b"mp42"
    out += struct.pack(">I", 8 + len(ftyp_payload)) + b"ftyp" + ftyp_payload

    # mdat box
    out += struct.pack(">I", 8 + len(mdat)) + b"mdat" + mdat

    if udta is not None:
        out += struct.pack(">I", 8 + len(udta)) + b"udta" + udta

    path = tmp_path / name
    path.write_bytes(bytes(out))
    return path


def test_mp4_hash_is_stable_for_identical_mdat(tmp_path: Path) -> None:
    a = _make_mp4(tmp_path, "a.mp4", mdat=b"\x01\x02\x03\x04" * 16)
    b = _make_mp4(tmp_path, "b.mp4", mdat=b"\x01\x02\x03\x04" * 16)
    assert hash_mp4(a) == hash_mp4(b)


def test_mp4_hash_ignores_udta_changes(tmp_path: Path) -> None:
    """Adding/changing udta metadata must not change the mdat-only hash."""
    payload = b"\xde\xad\xbe\xef" * 32
    a = _make_mp4(tmp_path, "a.mp4", mdat=payload)
    b = _make_mp4(tmp_path, "b.mp4", mdat=payload, udta=b"random metadata")
    c = _make_mp4(tmp_path, "c.mp4", mdat=payload, udta=b"completely different metadata")
    h_a = hash_mp4(a)
    h_b = hash_mp4(b)
    h_c = hash_mp4(c)
    assert h_a == h_b == h_c


def test_mp4_hash_changes_when_mdat_changes(tmp_path: Path) -> None:
    a = _make_mp4(tmp_path, "a.mp4", mdat=b"AAAAAAAA" * 16)
    b = _make_mp4(tmp_path, "b.mp4", mdat=b"BBBBBBBB" * 16)
    assert hash_mp4(a) != hash_mp4(b)


def test_compute_dispatcher_picks_format_by_extension(tmp_path: Path) -> None:
    jpg = _make_jpeg(tmp_path, "x.jpg")
    mp4 = _make_mp4(tmp_path, "y.mp4", mdat=b"abc" * 32)

    # The dispatcher should call hash_jpeg / hash_mp4 respectively.
    assert compute_content_hash(jpg) == hash_jpeg(jpg)
    assert compute_content_hash(mp4) == hash_mp4(mp4)


def test_compute_dispatcher_handles_jpeg_extension_alias(tmp_path: Path) -> None:
    jpg = _make_jpeg(tmp_path, "x.jpeg")
    # `.jpeg` should hash the same way `.jpg` does (format-aware).
    assert compute_content_hash(jpg) == hash_jpeg(jpg)


def test_hash_is_hex_blake3(tmp_path: Path) -> None:
    """Result is 64 lowercase hex chars (BLAKE3-256)."""
    p = _make_jpeg(tmp_path, "x.jpg")
    h = hash_jpeg(p)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)
