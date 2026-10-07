"""Clean downloads (spec/metadata-cleanup.md).

A download carries the picture, its date, its orientation and its colours,
and an opaque `pix:SourceId` — nothing about where it was taken, on what, by
whom, or where the old library kept it. The photographs here are made with
Pillow and given the kind of metadata a seeded iPhone file carries.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageCms

from pix.nas import delivery, identity
from pix.nas import index as ix
from pix.nas import roundtrip, webroots

TAKEN = datetime(2026, 8, 30, 15, 34, 55)

#: What a seeded file carries that must not leave: the old pix's tags with a
#: person's name and a path, the camera, and where it was.
OLD_XMP = (
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf='
    b'"http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description '
    b'xmlns:pix="http://pix.local/" pix:EventOverride="Banff Skiing - Ballabans"'
    b' pix:OriginalPath="G:\\pix\\raw\\tmp\\mtp\\james\\IMG_1234.HEIC"/>'
    b'</rdf:RDF></x:xmpmeta>')
SECRETS = (b"Ballabans", b"james", b"Apple", b"iPhone 15", b"secret note")


def _segment(marker: int, body: bytes) -> bytes:
    return bytes((0xFF, marker)) + (len(body) + 2).to_bytes(2, "big") + body


def _icc() -> bytes:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _photo(*, progressive: bool = False, orientation: int = 6,
           trailing: bytes = b"") -> bytes:
    """A small JPEG with a seeded iPhone file's worth of metadata."""
    im = Image.new("RGB", (64, 48))
    im.putdata([((x * 4) % 256, (y * 5) % 256, (x * y) % 256)
                for y in range(48) for x in range(64)])
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = "Apple"
    exif[0x0110] = "iPhone 15"
    sub = exif.get_ifd(0x8769)
    sub[0x9003] = "2020:01:02 03:04:05"
    sub[0xA001] = 1
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (51.0, 10.0, 0.0)
    gps[3], gps[4] = "W", (115.0, 34.0, 0.0)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=90, exif=exif.tobytes(), icc_profile=_icc(),
            progressive=progressive)
    data = out.getvalue()
    extra = (_segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00" + OLD_XMP)
             + _segment(0xFE, b"secret note"))
    return data[:2] + extra + data[2:] + trailing


def _pixels(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        return im.convert("RGB").tobytes()


# --- what survives -------------------------------------------------------

def test_only_the_date_orientation_and_colours_survive() -> None:
    source = _photo()
    clean = delivery.clean_jpeg(source, taken=TAKEN, source_id="j:abc123")

    for secret in SECRETS:
        assert secret in source
        assert secret not in clean, secret
    with Image.open(io.BytesIO(clean)) as im:
        exif = im.getexif()
        assert dict(exif) == {0x0112: 6, 0x8769: exif[0x8769]}
        assert dict(exif.get_ifd(0x8769)) == {
            0x9000: b"0232", 0x9003: "2026:08:30 15:34:55",
            0x9004: "2026:08:30 15:34:55", 0xA001: 1}
        assert not exif.get_ifd(0x8825), "no location"
        assert im.info["icc_profile"] == _icc()
    assert b'pix:SourceId="j:abc123"' in clean


def test_the_picture_itself_is_untouched() -> None:
    """Not re-encoded: the same pixels, and the same content hash, which is
    what recognises the copy if it ever comes home."""
    source = _photo()
    clean = delivery.clean_jpeg(source, taken=TAKEN, source_id=None)

    assert _pixels(clean) == _pixels(source)
    assert identity.jpeg_hash(clean) == identity.jpeg_hash(source)


def test_a_progressive_photograph_keeps_every_scan() -> None:
    """Several scans, with tables between them; stopping at the first would
    hand over a blurred photograph."""
    source = _photo(progressive=True)
    clean = delivery.clean_jpeg(source, taken=TAKEN, source_id=None)

    assert source.count(b"\xff\xda") > 1
    assert _pixels(clean) == _pixels(source)
    assert identity.jpeg_hash(clean) == identity.jpeg_hash(source)


def test_what_follows_the_image_is_dropped() -> None:
    """Where an iPhone keeps its HDR gain map — a second image with metadata
    of its own. The copy is standard range, and hashes differently."""
    tail = _photo(orientation=1)
    source = _photo(trailing=tail)
    clean = delivery.clean_jpeg(source, taken=TAKEN, source_id=None)

    assert clean.endswith(b"\xff\xd9")
    assert clean.count(b"\xff\xd8") == 1
    assert _pixels(clean) == _pixels(source)
    assert identity.jpeg_hash(clean) != identity.jpeg_hash(source)


def test_cleaning_twice_changes_nothing() -> None:
    """The copy's own EXIF — big-endian, where Pillow writes little — reads
    back as what it says."""
    once = delivery.clean_jpeg(_photo(), taken=TAKEN, source_id="j:1")
    assert delivery.clean_jpeg(once, taken=TAKEN, source_id="j:1") == once


def test_nothing_to_say_writes_no_exif() -> None:
    source = _photo()
    stripped = source.replace(b"Exif\x00\x00", b"Junk\x00\x00", 1)
    clean = delivery.clean_jpeg(stripped, taken=None, source_id=None)

    assert b"Exif\x00\x00" not in clean
    assert b"xap/1.0" not in clean


def test_a_truncated_photograph_is_still_cleaned() -> None:
    """No end of image: decoders forgive it, and so does this."""
    source = _photo()
    clean = delivery.clean_jpeg(source[:-2], taken=TAKEN, source_id=None)

    assert clean.endswith(b"\xff\xd9")
    assert b"Ballabans" not in clean


@pytest.mark.parametrize("bad", [
    b"\xff\xd8\xff\xe1\x00",                       # a segment cut off
    b"\xff\xd8\xff\xe1\x00\x10Exif",               # length past the end
    b"\xff\xd8\xff\xdb\x00\x04\x00\x00\xff\xd9",   # tables, no image
    b"\xff\xd8\xff\xe0\x00\x04ab\x12\x34",         # not a marker
])
def test_a_jpeg_that_cannot_be_walked_is_refused(bad: bytes) -> None:
    with pytest.raises(delivery.Uncleanable):
        delivery.clean_jpeg(bad, taken=TAKEN, source_id=None)


def test_exiftool_finds_nothing_identifying(tmp_path: Path) -> None:
    """A second opinion from the tool that reads everything."""
    if shutil.which("exiftool") is None:
        pytest.skip("exiftool is not installed")
    out = tmp_path / "clean.jpg"
    out.write_bytes(delivery.clean_jpeg(_photo(), taken=TAKEN,
                                        source_id="j:abc"))

    tags = json.loads(subprocess.run(
        ["exiftool", "-j", "-a", "-G1", str(out)],
        capture_output=True, text=True, check=True).stdout)[0]

    # The colour profile reads as several `ICC…` groups, all of it colour.
    groups = {key.split(":")[0] for key in tags
              if ":" in key and not key.startswith("ICC")}
    assert groups <= {"System", "File", "ExifTool", "IFD0", "ExifIFD",
                      "XMP-x", "XMP-pix", "Composite", "JFIF"}, groups
    assert "ExifTool:Warning" not in tags, tags
    assert tags["IFD0:Orientation"] == "Rotate 90 CW"
    assert tags["XMP-pix:SourceId"] == "j:abc"


# --- names ---------------------------------------------------------------

def test_a_name_is_a_date() -> None:
    assert delivery.name_for(TAKEN, ".JPG", "j:abc") == "2026-08-30_153455.jpg"


def test_with_no_date_a_name_says_nothing_about_anyone() -> None:
    assert delivery.name_for(None, ".mp4", "m:0123456789ab") == "pix_01234567.mp4"
    assert delivery.name_for(None, ".mp4", None) == "pix.mp4"


def test_an_odd_extension_is_not_carried() -> None:
    assert delivery.name_for(TAKEN, ".a b", None) == "2026-08-30_153455"


def test_names_are_numbered_rather_than_overwritten() -> None:
    used: set[str] = set()
    got = [delivery.unique(n, used) for n in ("a.jpg", "a.jpg", "a.jpg", "b")]
    assert got == ["a.jpg", "a_2.jpg", "a_3.jpg", "b"]


# --- the download hash ---------------------------------------------------

def test_a_hash_is_recorded_once(tmp_path: Path) -> None:
    ledger = tmp_path / "app" / "delivered.jsonl"
    delivery.record(ledger, "j:1", "f", "a.jpg")
    delivery.record(ledger, "j:1", "f", "a.jpg")
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write('{"hash": "j:2", "fol\n')        # cut short by a crash

    assert ledger.read_text(encoding="utf-8").count("j:1") == 1
    assert delivery.recorded(ledger) == {"j:1": "f/a.jpg"}


def test_a_download_hash_is_known_to_import(tmp_path: Path) -> None:
    db = tmp_path / "index.db"
    (tmp_path / "meta").mkdir()
    (tmp_path / "master").mkdir()
    ix.build(db, meta_dir=tmp_path / "meta", master_dir=tmp_path / "master")
    ledger = tmp_path / "delivered.jsonl"
    delivery.record(ledger, "j:feed", "init_2026", "a.jpg")

    known = roundtrip.known_hashes(db, ledger)

    assert known == {"j:feed": "init_2026/a.jpg"}


def test_a_returning_download_is_recognised_by_its_stamp(tmp_path: Path) -> None:
    back = tmp_path / "IMG_0001.jpg"
    back.write_bytes(delivery.clean_jpeg(_photo(), taken=TAKEN,
                                         source_id="j:0123abcd"))

    assert roundtrip.returned(back) == "j:0123abcd"


# --- through the app -----------------------------------------------------

def _master(writable: Path, data: bytes) -> None:
    (writable / "a.jpg").write_bytes(data)


def test_a_photograph_downloads_clean(client: TestClient,
                                      writable: Path) -> None:
    _master(writable, _photo())

    r = client.get("/download/init_2026/a.jpg")

    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    for secret in SECRETS:
        assert secret not in r.content, secret
    with Image.open(io.BytesIO(r.content)) as im:
        # The index's date — the file's own says 2020.
        assert im.getexif().get_ifd(0x8769)[0x9003] == "2026:08:30 15:34:55"
    assert b"pix:SourceId=\"j:" in r.content


def test_the_date_is_the_corrected_one(client: TestClient,
                                      writable: Path) -> None:
    _master(writable, _photo())
    client.post("/api/decide", json={"folder": "init_2026", "name": "a.jpg",
                                     "date_override": "2019-*-*-*:*:*"})

    r = client.get("/download/init_2026/a.jpg")

    with Image.open(io.BytesIO(r.content)) as im:
        assert im.getexif().get_ifd(0x8769)[0x9003] == "2019:08:30 15:34:55"
    assert "2019-08-30_153455.jpg" in r.headers["content-disposition"]


def test_a_copy_that_hashes_differently_is_recorded(client: TestClient,
                                                    writable: Path) -> None:
    _master(writable, _photo(trailing=_photo(orientation=1)))

    first = client.get("/download/init_2026/a.jpg").content
    client.get("/download/init_2026/a.jpg")

    recorded = delivery.recorded(webroots.DELIVERED_FILE)
    assert recorded == {identity.jpeg_hash(first): "init_2026/a.jpg"}


def test_a_copy_that_hashes_the_same_is_not_recorded(client: TestClient,
                                                     writable: Path) -> None:
    _master(writable, _photo())
    client.get("/download/init_2026/a.jpg")

    assert delivery.recorded(webroots.DELIVERED_FILE) == {}


def test_a_photograph_that_cannot_be_cleaned_is_not_sent(
    client: TestClient, writable: Path
) -> None:
    _master(writable, b"\xff\xd8\xff\xe1\x00\x10Exif GPS 51N 115W")
    (writable / "b.mp4").write_bytes(b"a clip")

    assert client.get("/download/init_2026/a.jpg").status_code == 500
    r = client.post("/download.zip", data={"files": json.dumps([
        {"folder": "init_2026", "name": "a.jpg"},
        {"folder": "init_2026", "name": "b.mp4"}])})
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert zf.namelist() == ["pix_01234567.mp4"]


def test_a_zip_holds_clean_photographs(client: TestClient,
                                       writable: Path) -> None:
    _master(writable, _photo())

    r = client.post("/download.zip", data={"files": json.dumps([
        {"folder": "init_2026", "name": "a.jpg"}])})

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        got = zf.read("2026-08-30_153455.jpg")
    assert b"Ballabans" not in got
    assert _pixels(got) == _pixels(_photo())


# --- videos --------------------------------------------------------------

def _ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg is not installed")


#: A subtitle track stands in for telemetry: a track that is neither picture
#: nor sound, whose samples carry something that must not leave.
TELEMETRY = "GPS 51.1784N 115.5708W"


def _video(path: Path, *, telemetry: bool = True, faststart: bool = False,
           extra: tuple[str, ...] = ()) -> Path:
    """Two seconds of picture and sound, tagged the way a phone tags them."""
    _ffmpeg()
    args = ["ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=s=64x48:r=10:d=2",
            "-f", "lavfi", "-i", "sine=d=2"]
    if telemetry:
        srt = path.with_suffix(".srt")
        srt.write_text(f"1\n00:00:00,000 --> 00:00:02,000\n{TELEMETRY}\n",
                       encoding="utf-8")
        args += ["-i", str(srt)]
    args += ["-map", "0:v", "-map", "1:a"] + (["-map", "2:s"] if telemetry else [])
    args += ["-c:v", "libx264", "-c:a", "aac"] + (
        ["-c:s", "mov_text"] if telemetry else [])
    flags = "+use_metadata_tags" + ("+faststart" if faststart else "")
    args += ["-metadata", "location=+51.1784-115.5708/",
             "-metadata", "make=Apple", "-metadata", "model=iPhone 15",
             "-metadata", "comment=Banff Skiing - Ballabans",
             "-metadata", "creation_time=2026-08-30T15:34:55Z",
             "-movflags", flags, *extra, str(path)]
    subprocess.run(args, check=True, timeout=120)
    return path


def _clean_video(src: Path, dst: Path, *, shift: int = 0,
                 source_id: str | None = "m:abc") -> delivery.VideoPlan:
    from blake3 import blake3

    digest = blake3()
    with src.open("rb") as fh:
        plan = delivery.plan_video(fh, src.stat().st_size, shift=shift,
                                   source_id=source_id)
        dst.write_bytes(b"".join(delivery.stream_video(fh, plan, digest,
                                                       chunk=4096)))
    # What was hashed on the way out is what the file says it is.
    assert "m:" + digest.hexdigest() == identity.content_hash(dst)
    return plan


def _streams(path: Path) -> list[str]:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.split()


def _plays(path: Path) -> bool:
    got = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path),
                          "-f", "null", "-"], capture_output=True, text=True)
    return got.returncode == 0 and not got.stderr.strip()


@pytest.mark.parametrize("faststart", [False, True])
def test_a_video_keeps_only_picture_and_sound(tmp_path: Path,
                                              faststart: bool) -> None:
    """Movie header before the data or after it, as phones and GoPros differ."""
    src = _video(tmp_path / "src.mp4", faststart=faststart)
    dst = tmp_path / "out.mp4"
    plan = _clean_video(src, dst)

    source, clean = src.read_bytes(), dst.read_bytes()
    for secret in (TELEMETRY.encode(), b"Ballabans", b"iPhone 15", b"Apple",
                   b"+51.1784", b"Lavf", b"VideoHandler"):
        assert secret in source, secret
        assert secret not in clean, secret
    assert _streams(src) == ["video", "audio", "subtitle"]
    assert _streams(dst) == ["video", "audio"]
    assert _plays(dst)
    assert len(clean) == len(source) + len(plan.tail)
    assert plan.zeroed
    assert identity.content_hash(dst) != identity.content_hash(src)


def test_a_plain_video_keeps_its_content_hash(tmp_path: Path) -> None:
    """Nothing in the coded data changed, so to the hash the copy is the
    master — and there is no download hash to record."""
    src = _video(tmp_path / "src.mp4", telemetry=False)
    dst = tmp_path / "out.mp4"
    plan = _clean_video(src, dst)

    assert not plan.zeroed
    assert identity.content_hash(dst) == identity.content_hash(src)
    assert b"Ballabans" not in dst.read_bytes()
    assert _plays(dst)


def test_exiftool_finds_nothing_in_a_video(tmp_path: Path) -> None:
    if shutil.which("exiftool") is None:
        pytest.skip("exiftool is not installed")
    src = _video(tmp_path / "src.mp4")
    dst = tmp_path / "out.mp4"
    _clean_video(src, dst, source_id="m:abc")

    tags = json.loads(subprocess.run(
        ["exiftool", "-j", "-a", "-G1", "-ee", str(dst)],
        capture_output=True, text=True, check=True).stdout)[0]

    # What kind of track and codec is fine to say; what made it is not.
    found = {k: v for k, v in tags.items()
             if any(w in k for w in ("GPS", "Location", "Make", "Model",
                                     "Comment", "Encoder", "HandlerDescription",
                                     "CompressorName", "Text"))
             and v not in ("", 0)}
    assert not found, found
    assert tags["XMP-pix:SourceId"] == "m:abc"


def test_a_corrected_date_moves_the_video_own_date(tmp_path: Path) -> None:
    src = _video(tmp_path / "src.mp4")
    dst = tmp_path / "out.mp4"
    _clean_video(src, dst, shift=-365 * 86400)

    when = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time",
         "-of", "csv=p=0", str(dst)],
        capture_output=True, text=True, check=True).stdout.strip()
    assert when.startswith("2025-08-30T15:34:55"), when


def test_a_returning_video_is_recognised_by_its_stamp(tmp_path: Path) -> None:
    """The stamp is at the end, past the head the check used to read."""
    src = _video(tmp_path / "src.mp4")
    dst = tmp_path / "out.mp4"
    _clean_video(src, dst, source_id="m:0123abcd")
    padded = tmp_path / "big.mp4"
    padded.write_bytes(bytes(roundtrip.HEAD) + dst.read_bytes())

    assert roundtrip.returned(dst) == "m:0123abcd"
    assert roundtrip.returned(padded) == "m:0123abcd"


def test_junk_after_the_last_box_is_not_sent(tmp_path: Path) -> None:
    src = _video(tmp_path / "src.mp4")
    junky = tmp_path / "junky.mp4"
    junky.write_bytes(src.read_bytes() + b"\x01\x02serial 12345 trailer")
    dst = tmp_path / "out.mp4"
    _clean_video(junky, dst)

    assert b"serial 12345" not in dst.read_bytes()
    assert _plays(dst)


def test_a_fragmented_video_is_refused(tmp_path: Path) -> None:
    """Its samples are described fragment by fragment; until that is read,
    nothing can say which bytes are telemetry."""
    src = _video(tmp_path / "src.mp4",
                 extra=("-movflags", "+frag_keyframe+empty_moov"))
    with src.open("rb") as fh, pytest.raises(delivery.Uncleanable):
        delivery.plan_video(fh, src.stat().st_size, source_id=None)


def test_something_that_is_not_a_movie_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00junk")
    with bad.open("rb") as fh, pytest.raises(delivery.Uncleanable):
        delivery.plan_video(fh, bad.stat().st_size, source_id=None)


def _index_video(app_env: dict[str, Path], src: Path) -> None:
    """Give `b.mp4` the date and content hash `process` would have."""
    meta = app_env["share"] / "meta" / "init_2026" / "b.mp4.json"
    record = json.loads(meta.read_text(encoding="utf-8"))
    record["exif"]["QuickTime:CreateDate"] = "2026:08:30 15:34:55"
    record["content_hash"] = identity.content_hash(src)
    meta.write_text(json.dumps(record), encoding="utf-8")
    ix.build(app_env["db"], meta_dir=app_env["share"] / "meta",
             master_dir=src.parent.parent)


def test_a_video_downloads_clean(client: TestClient, writable: Path,
                                 app_env: dict[str, Path],
                                 tmp_path: Path) -> None:
    src = _video(writable / "b.mp4")
    _index_video(app_env, src)

    r = client.get("/download/init_2026/b.mp4")

    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert int(r.headers["content-length"]) == len(r.content)
    assert b"Ballabans" not in r.content
    assert TELEMETRY.encode() not in r.content
    assert b'pix:SourceId="m:' in r.content
    assert "2026-08-30_153455.mp4" in r.headers["content-disposition"]
    # Telemetry zeroed, so the copy hashes differently, and that is recorded.
    got = tmp_path / "got.mp4"
    got.write_bytes(r.content)
    assert delivery.recorded(webroots.DELIVERED_FILE) == {
        identity.content_hash(got): "init_2026/b.mp4"}


def test_a_zip_holds_clean_videos(client: TestClient, writable: Path,
                                  app_env: dict[str, Path]) -> None:
    _index_video(app_env, _video(writable / "b.mp4"))

    r = client.post("/download.zip", data={"files": json.dumps([
        {"folder": "init_2026", "name": "b.mp4"}])})

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        got = zf.read("2026-08-30_153455.mp4")
    assert b"Ballabans" not in got
    assert TELEMETRY.encode() not in got


# --- not processed yet ---------------------------------------------------

def _unprocessed(app_env: dict[str, Path], writable: Path) -> None:
    """`b.mp4` as a video `process` has not been over: no content hash."""
    (writable / "b.mp4").write_bytes(b"a clip")
    meta = app_env["share"] / "meta" / "init_2026" / "b.mp4.json"
    record = json.loads(meta.read_text(encoding="utf-8"))
    record.pop("content_hash", None)
    meta.write_text(json.dumps(record), encoding="utf-8")
    ix.build(app_env["db"], meta_dir=app_env["share"] / "meta",
             master_dir=writable.parent)


def test_a_video_not_processed_yet_is_not_downloaded(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """No hash to stamp it with, so a copy of it could not be recognised if
    it came home."""
    _unprocessed(app_env, writable)

    r = client.get("/download/init_2026/b.mp4")

    assert r.status_code == 409
    assert "not ready to download yet" in r.text


def test_a_zip_leaves_out_what_is_not_ready(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    _unprocessed(app_env, writable)

    r = client.post("/download.zip", data={"files": json.dumps([
        {"folder": "init_2026", "name": "a.jpg"},
        {"folder": "init_2026", "name": "b.mp4"}])})

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert zf.namelist() == ["2026-08-30_153455.jpg"]


def test_a_zip_of_nothing_ready_says_so(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    _unprocessed(app_env, writable)

    r = client.post("/download.zip", data={"files": json.dumps([
        {"folder": "init_2026", "name": "b.mp4"}])})

    assert r.status_code == 409
    assert "ready to download yet" in r.text


def test_the_grid_marks_what_is_not_ready(
    client: TestClient, writable: Path, app_env: dict[str, Path]
) -> None:
    """So the page can leave it out of a selection, and say so, before
    asking for anything."""
    _unprocessed(app_env, writable)

    page = client.get("/browse").text

    marks = dict(re.findall(r'data-name="([^"]+)"[^>]*?data-unready="(1?)"',
                            page))
    assert marks == {"a.jpg": "", "b.mp4": "1"}, marks
