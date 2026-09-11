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
    for name in ("thumb", "preview", "meta", "render"):
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
    for tier in ("thumb", "preview"):
        d = tiers["master"].parent / tier / "init_2026"
        d.mkdir(parents=True, exist_ok=True)
        (d / "a.mp4.jpg").write_bytes(b"x")

    assert [p.name for p in derive.pending_files()] == ["a.mp4"]


def test_scan_leaves_a_finished_h264_clip_alone(tiers: dict[str, Path]) -> None:
    _clip(tiers, "a.mp4", "avc1")
    for tier in ("thumb", "preview"):
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
