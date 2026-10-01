"""The pending-work scan and the sweep count (spec/nas-app.md §9).

Two bugs this pins. The scan used three `stat`s per file, so a 6,400-file folder
cost ~19,000 SMB round trips and took longer than the work itself. And the sweep
counted routine scratch reaping as swept partials, so every run claimed to have
recovered from an interruption that never happened.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from pix.markers import EXPORT_TMP_SUFFIX
from pix.nas import derive
from pix.nas import ledger


@pytest.fixture(autouse=True)
def tiers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    share = tmp_path / "nas"
    master = share / "master"
    master.mkdir(parents=True)
    monkeypatch.setattr(derive, "MASTER_DIR", master)
    monkeypatch.setattr(derive, "THUMB_DIR", share / "thumb")
    monkeypatch.setattr(derive, "PREVIEW_DIR", share / "preview")
    monkeypatch.setattr(derive, "META_DIR", share / "meta")
    monkeypatch.setattr(derive, "_scratch", lambda: tmp_path / "scratch")
    monkeypatch.setattr(ledger, "MASTER_SHARE", share)
    monkeypatch.setattr(ledger, "MASTER_DIR", master)
    return {"master": master, "thumb": share / "thumb",
            "preview": share / "preview", "meta": share / "meta"}


def _photos(folder: Path, n: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        Image.new("RGB", (60, 40)).save(folder / f"{i:03d}.jpg", "JPEG")


def test_scan_finds_everything_when_nothing_is_derived(
    tiers: dict[str, Path]
) -> None:
    _photos(tiers["master"] / "legacy_2026", 5)

    assert len(derive.pending_files()) == 5


def test_scan_finds_nothing_when_all_tiers_are_present(
    tiers: dict[str, Path]
) -> None:
    _photos(tiers["master"] / "legacy_2026", 3)
    derive.run_process()

    assert derive.pending_files() == []


def test_a_single_missing_tier_makes_a_file_pending(
    tiers: dict[str, Path]
) -> None:
    """All three tiers count — metadata missing is work, same as a thumbnail."""
    _photos(tiers["master"] / "legacy_2026", 2)
    derive.run_process()
    (tiers["meta"] / "legacy_2026" / "000.jpg.json").unlink()

    pending = derive.pending_files()

    assert [p.name for p in pending] == ["000.jpg"]


def test_scan_uses_listings_not_a_stat_per_file(
    tiers: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: ~19k SMB round trips for one folder.

    Three `stat`s per file is fine locally and ruinous over a network share, so
    the scan must ask each tier directory once instead.
    """
    _photos(tiers["master"] / "legacy_2026", 40)

    calls = {"n": 0}
    real = Path.is_file

    def counting(self: Path) -> bool:
        calls["n"] += 1
        return real(self)

    monkeypatch.setattr(Path, "is_file", counting)
    derive.pending_files()

    # One per master file to classify it, and nothing per derived tier.
    assert calls["n"] < 40 * 2


def test_ledgers_and_sidecars_are_not_pending_work(
    tiers: dict[str, Path]
) -> None:
    folder = tiers["master"] / "legacy_2026"
    _photos(folder, 1)
    (folder / ".import.jsonl").write_text("{}\n", encoding="utf-8")
    (folder / "000.jpg.xmp").write_text("<x/>", encoding="utf-8")

    assert [p.name for p in derive.pending_files()] == ["000.jpg"]


def test_empty_master_scans_to_nothing(tiers: dict[str, Path]) -> None:
    assert derive.pending_files() == []


# --- the sweep count ---------------------------------------------------------

def test_reaping_old_scratch_is_not_a_swept_partial(
    tiers: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every run left a dead scratch dir, so every run claimed to sweep a partial."""
    scratch_root = derive._scratch().parent
    dead = scratch_root / "999999999"
    dead.mkdir(parents=True)

    swept = derive.sweep_partials()

    assert swept == 0                 # housekeeping, not a recovered partial
    assert not dead.exists()          # still cleaned up


def test_real_partials_are_still_counted(tiers: dict[str, Path]) -> None:
    stale = tiers["thumb"] / "legacy_2026" / ("x.jpg" + EXPORT_TMP_SUFFIX)
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"partial")

    assert derive.sweep_partials() == 1
    assert not stale.exists()


# --- filmstrips (spec/clips.md §9) --------------------------------------------

def _video(path: Path, seconds: int = 12) -> None:
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=160x90:rate=10", "-t", str(seconds),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                   check=True, timeout=60)


def test_a_video_gets_a_filmstrip(tmp_path: Path,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """Frames evenly across it, side by side in one picture, with what the
    page needs to find each of them."""
    import json

    from pix.nas import paths

    monkeypatch.setattr(derive, "STRIP_DIR", tmp_path / "strip")
    clip = tmp_path / "master" / "f" / "a.mp4"
    _video(clip)

    assert derive.wants_strip(clip)
    assert derive.make_strip(clip)
    info = json.loads(paths.strip_info_path(clip, tmp_path / "strip")
                      .read_text(encoding="utf-8"))
    assert info["n"] == 4 and info["h"] == derive.STRIP_PX
    with Image.open(paths.strip_path(clip, tmp_path / "strip")) as sprite:
        assert sprite.size == (info["w"] * 4, derive.STRIP_PX)
    assert not derive.wants_strip(clip)


def test_a_photograph_has_no_filmstrip(tmp_path: Path) -> None:
    assert not derive.wants_strip(tmp_path / "a.jpg")
    assert not derive.wants_strip(tmp_path / "a.insv")


def test_a_video_with_no_filmstrip_is_work_to_do(
    tiers: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Everything else made already — the scan still has to find it, or no
    video processed before filmstrips existed would ever get one."""
    monkeypatch.setattr(derive, "STRIP_DIR", tmp_path / "nas" / "strip")
    folder = tiers["master"] / "legacy_2026"
    folder.mkdir(parents=True)
    (folder / "a.mp4").write_bytes(b"fake")
    for tier in ("thumb", "preview"):
        (tiers[tier] / "legacy_2026").mkdir(parents=True)
        (tiers[tier] / "legacy_2026" / "a.mp4.jpg").write_bytes(b"x")
    (tiers["meta"] / "legacy_2026").mkdir(parents=True)
    (tiers["meta"] / "legacy_2026" / "a.mp4.json").write_text("{}")
    large = tmp_path / "nas" / "large" / "legacy_2026"
    large.mkdir(parents=True)
    (large / "a.mp4.jpg").write_bytes(b"x")
    monkeypatch.setattr(derive, "LARGE_DIR", tmp_path / "nas" / "large")
    monkeypatch.setattr(derive, "wants_render", lambda media: False)

    assert [p.name for p in derive.pending_files()] == ["a.mp4"]


def test_a_clip_whose_picture_ends_before_its_sound_gets_a_filmstrip(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The strip is spaced over the container's length, and in a short phone
    clip the audio runs on past the last picture — so the last frame asked
    for is after the video has ended. A player holds the last picture there,
    and so does the strip, rather than giving up on the whole thing and
    trying again every run."""
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    monkeypatch.setattr(derive, "STRIP_DIR", tmp_path / "strip")
    clip = tmp_path / "master" / "f" / "short.mp4"
    clip.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=160x90:rate=30:d=0.2",
                    "-f", "lavfi", "-i", "sine=d=0.5",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(clip)], check=True, timeout=60)

    assert derive.make_strip(clip)
    assert not derive.wants_strip(clip)
