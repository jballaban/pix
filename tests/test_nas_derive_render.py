"""H.264 renders (spec/nas-app.md §5, §9).

Whether a video needs a render is a **codec** question, not an extension one: an
`.mp4` containing HEVC still needs one. Measured across the seeded year, 421 of
724 clips are HEVC against 303 H.264, so most of the video library is unplayable
in a browser until this runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pix.markers import EXPORT_TMP_SUFFIX
from pix.nas import derive
from pix.nas import ledger


@pytest.fixture(autouse=True)
def tiers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    share = tmp_path / "nas"
    master = share / "master"
    master.mkdir(parents=True)
    for name in ("thumb", "large", "preview", "meta", "render"):
        monkeypatch.setattr(derive, f"{name.upper()}_DIR", share / name)
    monkeypatch.setattr(derive, "MASTER_DIR", master)
    monkeypatch.setattr(derive, "_scratch", lambda: tmp_path / "scratch")
    monkeypatch.setattr(ledger, "MASTER_SHARE", share)
    monkeypatch.setattr(ledger, "MASTER_DIR", master)
    return {"master": master, "render": share / "render", "meta": share / "meta"}


def _clip(tiers: dict[str, Path], name: str, codec: str | None) -> Path:
    folder = tiers["master"] / "init_2026"
    folder.mkdir(parents=True, exist_ok=True)
    media = folder / name
    media.write_bytes(b"not really a video")

    meta = tiers["meta"] / "init_2026"
    meta.mkdir(parents=True, exist_ok=True)
    exif: dict[str, object] = {}
    if codec:
        exif["QuickTime:CompressorID"] = codec
    (meta / f"{name}.json").write_text(
        json.dumps({"file": name, "folder": "init_2026", "exif": exif}),
        encoding="utf-8")
    return media


def _fake_ffmpeg(monkeypatch: pytest.MonkeyPatch,
                 fail_on: str | None = None) -> list[list[str]]:
    """Stand in for ffmpeg, recording what it was asked to do.

    Writes the output file the real one would, so the caller's *did it work*
    check sees what it expects — the thing under test is the command, and a
    run that "succeeded" without producing a file would be a different test
    passing for a different reason.
    """
    seen: list[list[str]] = []
    monkeypatch.setattr(derive.shutil, "which", lambda name: name)

    class Done:
        def __init__(self, code: int) -> None:
            self.returncode = code
            self.stdout = ""
            self.stderr = ""

    def run(cmd: list[str], **_: object) -> Done:
        # Only the encodes. `ffprobe` is asked the length of the clip on the
        # way past, and counting that as a command under test would make every
        # assertion here off by one.
        if cmd and "ffmpeg" in cmd[0]:
            seen.append(list(cmd))
        joined = " ".join(cmd)
        if fail_on and fail_on in joined:
            return Done(1)
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x")
        return Done(0)

    monkeypatch.setattr(derive.subprocess, "run", run)
    return seen


# --- when a render is needed -------------------------------------------------

def test_hevc_needs_a_render(tiers: dict[str, Path]) -> None:
    """The case that matters: an .mp4 a browser cannot play."""
    media = _clip(tiers, "a.mp4", "hvc1")
    assert derive.needs_render(media, derive.video_codec(media)) is True


def test_h264_does_not(tiers: dict[str, Path]) -> None:
    media = _clip(tiers, "a.mp4", "avc1")
    assert derive.needs_render(media, derive.video_codec(media)) is False


def test_images_never_need_one(tiers: dict[str, Path]) -> None:
    media = _clip(tiers, "a.jpg", None)
    assert derive.needs_render(media, None) is False


def test_insv_never_gets_one(tiers: dict[str, Path]) -> None:
    """360 footage has no meaningful flat rendition — a transcode gives
    dual-fisheye that nothing displays usefully (§5)."""
    media = _clip(tiers, "a.insv", "hvc1")
    assert derive.needs_render(media, "hvc1") is False


def test_an_unknown_codec_is_rendered(tiers: dict[str, Path]) -> None:
    """Better to render something playable than to assume and serve nothing."""
    media = _clip(tiers, "a.mp4", None)
    assert derive.needs_render(media, None) is True


def test_an_existing_render_is_not_redone(tiers: dict[str, Path]) -> None:
    media = _clip(tiers, "a.mp4", "hvc1")
    dest = derive.render_path(media)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"already rendered")

    assert derive.needs_render(media, "hvc1") is False


# --- codec lookup ------------------------------------------------------------

def test_codec_comes_from_the_meta_tier(tiers: dict[str, Path]) -> None:
    """Cheaper than opening a multi-gigabyte file over SMB."""
    media = _clip(tiers, "a.mp4", "hvc1")
    assert derive.video_codec(media) == "hvc1"


def test_codec_is_none_when_nothing_knows(tiers: dict[str, Path],
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(derive.shutil, "which", lambda _n: None)
    media = _clip(tiers, "a.mp4", None)
    assert derive.video_codec(media) is None


# --- the scan ----------------------------------------------------------------

def test_scan_finds_a_clip_needing_only_a_render(
    tiers: dict[str, Path]
) -> None:
    """Every image tier present, yet still work to do — the codec question."""
    _clip(tiers, "a.mp4", "hvc1")
    for tier in ("thumb", "large", "preview"):
        d = tiers["master"].parent / tier / "init_2026"
        d.mkdir(parents=True, exist_ok=True)
        (d / "a.mp4.jpg").write_bytes(b"x")

    assert [p.name for p in derive.pending_files()] == ["a.mp4"]


def test_scan_leaves_a_finished_h264_clip_alone(tiers: dict[str, Path]) -> None:
    _clip(tiers, "a.mp4", "avc1")
    for tier in ("thumb", "large", "preview"):
        d = tiers["master"].parent / tier / "init_2026"
        d.mkdir(parents=True, exist_ok=True)
        (d / "a.mp4.jpg").write_bytes(b"x")

    assert derive.pending_files() == []


# --- encoding ----------------------------------------------------------------

def test_a_failed_encode_leaves_no_partial(tiers: dict[str, Path]) -> None:
    """The input here is not a video, so both encoders must fail cleanly."""
    media = _clip(tiers, "a.mp4", "hvc1")

    assert derive.render_video(media, timeout=30) is False
    assert not derive.render_path(media).exists()
    leftovers = list(tiers["render"].rglob(f"*{EXPORT_TMP_SUFFIX}*"))
    assert leftovers == []


def test_a_failed_render_is_reported_not_silent(tiers: dict[str, Path]) -> None:
    media = _clip(tiers, "a.mp4", "hvc1")
    summary = derive.ProcessSummary()

    derive._derive_one(media, summary, derive.threading.Lock(),
                       derive._ExifPool(), {"cancelling": 0})

    assert any("render" in f for f in summary.failed)


def test_no_render_is_attempted_while_cancelling(tiers: dict[str, Path]) -> None:
    """Encoding is the expensive item; a cancel must not start one."""
    media = _clip(tiers, "a.mp4", "hvc1")
    summary = derive.ProcessSummary()

    derive._derive_one(media, summary, derive.threading.Lock(),
                       derive._ExifPool(), {"cancelling": 1})

    assert summary.renders == 0
    assert summary.failed == []


# --- the gap between the scan and the worker ---------------------------------

def test_a_clip_needing_only_a_render_is_not_skipped(
    tiers: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: `pending_files` found all 421, `_derive_one` skipped them.

    `_derive_one` returned early once the image tiers and meta were
    complete — before it ever reached the render block — so every HEVC clip was
    counted as "already done" and nothing was encoded.
    """
    media = _clip(tiers, "a.mp4", "hvc1")
    for tier in ("thumb", "large", "preview"):
        d = tiers["master"].parent / tier / "init_2026"
        d.mkdir(parents=True, exist_ok=True)
        (d / "a.mp4.jpg").write_bytes(b"x")

    called: list[Path] = []
    monkeypatch.setattr(derive, "render_video",
                        lambda m, **k: called.append(m) or True)

    summary = derive.ProcessSummary()
    derive._derive_one(media, summary, derive.threading.Lock(),
                       derive._ExifPool(), {"cancelling": 0})

    assert called == [media]
    assert summary.renders == 1
    assert summary.skipped == 0


def test_a_finished_h264_clip_is_still_skipped(
    tiers: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The early return must still fire for work that is genuinely done."""
    media = _clip(tiers, "a.mp4", "avc1")
    for tier in ("thumb", "large", "preview"):
        d = tiers["master"].parent / tier / "init_2026"
        d.mkdir(parents=True, exist_ok=True)
        (d / "a.mp4.jpg").write_bytes(b"x")

    called: list[Path] = []
    monkeypatch.setattr(derive, "render_video",
                        lambda m, **k: called.append(m) or True)

    summary = derive.ProcessSummary()
    derive._derive_one(media, summary, derive.threading.Lock(),
                       derive._ExifPool(), {"cancelling": 0})

    assert called == []
    assert summary.skipped == 1


def test_images_never_cost_a_codec_lookup(
    tiers: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """5,685 JPEGs must not each pay a meta-tier read to be told they are images."""
    media = _clip(tiers, "a.jpg", None)
    monkeypatch.setattr(derive, "video_codec",
                        lambda m: pytest.fail("probed an image"))

    assert derive.wants_render(media) is False


# --- HDR ---------------------------------------------------------------------
#
# 196 of this library's 1,069 clips are HLG, nearly all from one phone. A frame
# pulled out of one of those and written as an ordinary JPEG is read as though
# the curve were BT.709, which lifts the midtones and flattens everything: the
# washed-out grey thumbnail this exists to stop.


def _hdr_clip(tiers: dict[str, Path], name: str, transfer: str | None) -> Path:
    folder = tiers["master"] / "init_2026"
    folder.mkdir(parents=True, exist_ok=True)
    media = folder / name
    media.write_bytes(b"not really a video")
    meta = tiers["meta"] / "init_2026"
    meta.mkdir(parents=True, exist_ok=True)
    exif: dict[str, object] = {"QuickTime:CompressorID": "hvc1"}
    if transfer:
        exif["QuickTime:TransferCharacteristics"] = transfer
    (meta / f"{name}.json").write_text(
        json.dumps({"file": name, "folder": "init_2026", "exif": exif}),
        encoding="utf-8")
    return media


def test_hlg_is_read_off_the_meta_tier(tiers: dict[str, Path]) -> None:
    """Cheaper than opening a multi-gigabyte file over SMB, and the same place
    the codec is read from."""
    assert derive.is_hdr(
        _hdr_clip(tiers, "a.mp4", "BT.2100 HLG, ARIB STD-B67")) is True


def test_an_ordinary_clip_is_not_hdr(tiers: dict[str, Path]) -> None:
    """Tone mapping something already display-referred would wash it out in
    the other direction, so this has to be a question and not a habit."""
    assert derive.is_hdr(_hdr_clip(tiers, "a.mp4", "BT.709")) is False


def test_a_clip_nothing_knows_about_is_not_assumed_to_be_hdr(
    tiers: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(derive.shutil, "which", lambda _: None)
    assert derive.is_hdr(_hdr_clip(tiers, "a.mp4", None)) is False


def test_the_poster_frame_of_an_hdr_clip_is_tone_mapped(
    tiers: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: brought down to the range a JPEG can hold, rather than
    read as though it were already in it."""
    media = _hdr_clip(tiers, "a.mp4", "BT.2100 HLG, ARIB STD-B67")
    seen = _fake_ffmpeg(monkeypatch)

    derive._poster_frame(media)

    assert seen, "nothing was run"
    assert "-vf" in seen[0], seen[0]
    assert "tonemap" in seen[0][seen[0].index("-vf") + 1], seen[0]


def test_the_poster_frame_of_an_ordinary_clip_is_left_alone(
    tiers: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    media = _hdr_clip(tiers, "a.mp4", "BT.709")
    seen = _fake_ffmpeg(monkeypatch)

    derive._poster_frame(media)

    assert seen and all("-vf" not in cmd for cmd in seen), seen


def test_a_build_without_the_filter_still_gets_a_frame(
    tiers: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`zscale` needs an ffmpeg built with zimg. A washed-out thumbnail beats
    no thumbnail at all, so the plain command follows rather than replaces."""
    media = _hdr_clip(tiers, "a.mp4", "BT.2100 HLG, ARIB STD-B67")
    seen = _fake_ffmpeg(monkeypatch, fail_on="tonemap")

    got = derive._poster_frame(media)

    assert got is not None, "gave up instead of falling back"
    assert len(seen) == 2, seen


def test_an_hdr_render_is_tone_mapped_too(
    tiers: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """H.264 in a browser is shown as ordinary picture, so the render has to
    be brought down before it is encoded — otherwise playing it back
    disagrees with the original on every screen."""
    media = _hdr_clip(tiers, "a.mp4", "BT.2100 HLG, ARIB STD-B67")
    seen = _fake_ffmpeg(monkeypatch)

    assert derive.render_video(media) is True
    assert "-vf" in seen[0] and "tonemap" in seen[0][seen[0].index("-vf") + 1]


def test_an_ordinary_render_is_left_alone(
    tiers: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    media = _hdr_clip(tiers, "a.mp4", "BT.709")
    seen = _fake_ffmpeg(monkeypatch)

    assert derive.render_video(media) is True
    assert all("-vf" not in cmd for cmd in seen), seen
