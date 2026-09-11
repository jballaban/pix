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
