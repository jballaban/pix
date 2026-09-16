"""The content and perceptual hashes (spec/nas-app.md §15).

The one property worth testing hardest is the one the whole duplicate design
rests on: **rewriting a file's metadata must not move its content hash.** That
is what makes a photograph AirDropped to a second phone and imported from there
match the copy already in master, and it is invisible in any test that only
checks that two identical files agree.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from pix.nas import identity


def _jpeg(path: Path, *, colour: tuple[int, int, int] = (120, 60, 30),
          size: tuple[int, int] = (64, 48)) -> Path:
    Image.new("RGB", size, colour).save(path, "JPEG", quality=90)
    return path


def _with_app1(src: Path, dest: Path, payload: bytes) -> Path:
    """`src` with an APP1 segment spliced in — the same image, newly tagged.

    Byte surgery rather than a real EXIF write, because it guarantees what the
    test is about: not one bit of the coded image changes, so any difference in
    the hash is the hash's fault.
    """
    raw = src.read_bytes()
    seg = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    dest.write_bytes(raw[:2] + seg + raw[2:])
    return dest


def _box(kind: bytes, body: bytes) -> bytes:
    return (len(body) + 8).to_bytes(4, "big") + kind + body


def _mp4(path: Path, *, mdat: bytes, moov: bytes) -> Path:
    path.write_bytes(_box(b"ftyp", b"isom\x00\x00\x02\x00")
                     + _box(b"moov", moov) + _box(b"mdat", mdat))
    return path


def test_retagging_a_photograph_does_not_change_what_it_is(
    tmp_path: Path
) -> None:
    """The AirDrop case: a second device rewrites the metadata and renames the
    file, and changes nothing about the image the camera encoded."""
    original = _jpeg(tmp_path / "IMG_4471.JPG")
    retagged = _with_app1(original, tmp_path / "IMG_0001.JPG",
                          b"Exif\x00\x00" + b"\x01" * 200)

    assert original.read_bytes() != retagged.read_bytes(), "not a real test"
    assert identity.content_hash(original) == identity.content_hash(retagged)


def test_a_real_exif_write_is_the_same_photograph(tmp_path: Path) -> None:
    """The same thing through Pillow rather than by hand: one save carries EXIF
    and the other does not, and the encoder output between them is identical."""
    im = Image.new("RGB", (80, 60), (20, 90, 160))
    bare, tagged = tmp_path / "bare.jpg", tmp_path / "tagged.jpg"
    im.save(bare, "JPEG", quality=88)
    im.save(tagged, "JPEG", quality=88, exif=Image.Exif().tobytes(),
            comment=b"a comment nobody should hash")

    assert bare.stat().st_size != tagged.stat().st_size
    assert identity.content_hash(bare) == identity.content_hash(tagged)


def test_a_different_photograph_is_a_different_hash(tmp_path: Path) -> None:
    a = _jpeg(tmp_path / "a.jpg", colour=(10, 10, 10))
    b = _jpeg(tmp_path / "b.jpg", colour=(200, 40, 40))

    assert identity.content_hash(a) != identity.content_hash(b)


def test_bytes_appended_after_the_image_are_somebody_elses(
    tmp_path: Path
) -> None:
    """A trailer bolted on after EOI — a phone's payload, a broken transfer —
    is not part of the photograph and must not make it a different one."""
    clean = _jpeg(tmp_path / "clean.jpg")
    trailing = tmp_path / "trailing.jpg"
    trailing.write_bytes(clean.read_bytes() + b"\x00INSTA360 TRAILER" * 40)

    assert identity.content_hash(clean) == identity.content_hash(trailing)


def test_a_clip_is_its_samples_and_not_its_headers(tmp_path: Path) -> None:
    """Same `mdat`, different `moov`: tags, timestamps and titles live in the
    headers, and none of them is the footage."""
    one = _mp4(tmp_path / "one.mp4", mdat=b"\x11\x22\x33" * 500, moov=b"A" * 64)
    two = _mp4(tmp_path / "two.mp4", mdat=b"\x11\x22\x33" * 500,
               moov=b"B" * 512)

    assert identity.content_hash(one) == identity.content_hash(two)
    assert str(identity.content_hash(one)).startswith("m:")


def test_different_footage_is_a_different_clip(tmp_path: Path) -> None:
    one = _mp4(tmp_path / "one.mp4", mdat=b"\x11" * 300, moov=b"A" * 64)
    two = _mp4(tmp_path / "two.mp4", mdat=b"\x22" * 300, moov=b"A" * 64)

    assert identity.content_hash(one) != identity.content_hash(two)


def test_an_unknown_format_falls_back_to_the_whole_file(
    tmp_path: Path
) -> None:
    """Honest rather than clever: nothing is skipped, and the prefix says so."""
    odd = tmp_path / "notes.txt"
    odd.write_bytes(b"not a photograph at all")

    got = identity.content_hash(odd)

    assert str(got).startswith("f:")
    assert got == identity.content_hash(odd), "not stable"


def test_a_file_that_cannot_be_read_is_not_a_crash(tmp_path: Path) -> None:
    assert identity.content_hash(tmp_path / "nothing-here.jpg") is None


def test_a_truncated_jpeg_still_answers(tmp_path: Path) -> None:
    """Half a file is not a reason to fail the rest of its metadata."""
    cut = tmp_path / "half.jpg"
    cut.write_bytes(_jpeg(tmp_path / "whole.jpg").read_bytes()[:40])

    identity.content_hash(cut)  # must not raise; either answer is acceptable


# --- the perceptual half ------------------------------------------------------

def _distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _photo(path: Path, size: tuple[int, int]) -> Path:
    """Something shaped like a photograph: broad gradients and one bright
    subject.

    Not a fine repeating pattern, which would be testing the wrong thing. Detail
    finer than the grid a dHash reduces to aliases when the image is downscaled
    — that is sampling, not the hash failing — and photographs do not consist of
    one-pixel stripes. A flat colour is the other extreme, with no gradient to
    compare at all, and hashes to the same value as every other flat colour.
    """
    w, h = size
    im = Image.new("RGB", size)
    for x in range(w):
        for y in range(h):
            im.putpixel((x, y), (x * 255 // w, y * 255 // h,
                                 (x + y) * 255 // (w + h)))
    # The subject, off-centre so the left and right halves differ.
    cx, cy, r = w // 3, h // 2, min(w, h) // 5
    for x in range(max(0, cx - r), min(w, cx + r)):
        for y in range(max(0, cy - r), min(h, cy + r)):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                im.putpixel((x, y), (250, 245, 230))
    im.save(path, "JPEG", quality=92)
    return path


def test_a_messaging_copy_looks_like_what_it_came_from(tmp_path: Path) -> None:
    """Downscaled and re-compressed, which is what a text message does to a
    photograph. The whole point of a perceptual hash is that this survives."""
    full = _photo(tmp_path / "full.jpg", (800, 600))
    with Image.open(full) as im:
        small = im.resize((320, 240), Image.Resampling.LANCZOS)
        small.save(tmp_path / "sent.jpg", "JPEG", quality=70)

    a = identity.perceptual_hash(full)
    b = identity.perceptual_hash(tmp_path / "sent.jpg")

    assert a is not None and b is not None
    assert _distance(a, b) <= 6, f"{a} vs {b}"


def test_a_different_photograph_does_not(tmp_path: Path) -> None:
    """Mirrored, which is a different picture of the same scene and the
    sharpest case a difference hash can be asked about.

    Not *this photograph versus a flat colour*, which looks like the obvious
    counterexample and is a bad one: a dHash records which way the brightness
    goes between neighbours, so a flat image and any smooth left-to-right
    gradient both answer "the same way everywhere" and land within a few bits
    of each other. Two images being far apart in *content* does not make them
    far apart in derivative, and a test that pretended otherwise would be
    asserting something this hash never promised.
    """
    from PIL import ImageOps

    source = _photo(tmp_path / "a.jpg", (400, 300))
    with Image.open(source) as im:
        ImageOps.mirror(im).save(tmp_path / "b.jpg", "JPEG", quality=92)

    a = identity.perceptual_hash(source)
    b = identity.perceptual_hash(tmp_path / "b.jpg")

    assert a is not None and b is not None
    assert _distance(a, b) > 10, f"{a} vs {b}"


def test_something_that_is_not_an_image_has_no_perceptual_hash(
    tmp_path: Path
) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00\x00\x00\x14ftypisom" + b"\x00" * 64)

    assert identity.perceptual_hash(clip) is None


def test_the_hash_is_a_fixed_width_regardless_of_the_picture(
    tmp_path: Path
) -> None:
    """Sixteen hex characters always — a leading zero dropped would make two
    hashes of different lengths, and comparison by string is what the index
    does."""
    dark = _jpeg(tmp_path / "dark.jpg", colour=(0, 0, 0))
    got = identity.perceptual_hash(dark)

    assert got is not None and len(got) == 16


def test_a_readable_image_in_memory_is_not_required(tmp_path: Path) -> None:
    """Pillow is imported inside the function, because the app's container
    ships without it — importing the module must not need it."""
    source = Path(identity.__file__).read_text(encoding="utf-8")
    header = source.split("def content_hash")[0]

    assert "from PIL" not in header, "Pillow imported at module scope"
    assert io  # the stdlib import above is what a reader expects to see used
