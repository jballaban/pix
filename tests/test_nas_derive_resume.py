"""Cancelling and restarting `pix2 process` (spec/nas-app.md §9).

Resumable by construction: it makes what is missing, and missing is recomputed
every run — so there is no state to corrupt and nothing to resume *from*.
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
    master, thumb, preview = share / "master", share / "thumb", share / "preview"
    master.mkdir(parents=True)
    monkeypatch.setattr(derive, "MASTER_DIR", master)
    monkeypatch.setattr(derive, "THUMB_DIR", thumb)
    monkeypatch.setattr(derive, "PREVIEW_DIR", preview)
    monkeypatch.setattr(derive, "META_DIR", share / "meta")
    monkeypatch.setattr(derive, "_scratch", lambda: tmp_path / "scratch")
    monkeypatch.setattr(ledger, "MASTER_SHARE", share)
    monkeypatch.setattr(ledger, "MASTER_DIR", master)
    return {"master": master, "thumb": thumb, "preview": preview, "tmp": tmp_path}


def _photos(folder: Path, n: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        Image.new("RGB", (800, 600), (i % 255, 60, 30)).save(
            folder / f"{i:03d}.jpg", "JPEG")


def test_cancel_then_restart_completes_the_rest(
    tiers: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _photos(tiers["master"] / "legacy_2026", 12)

    real = derive._resize
    seen = {"n": 0}

    def interrupt_partway(source: Path, dest: Path, long_edge: int) -> None:
        seen["n"] += 1
        if seen["n"] == 5:
            raise KeyboardInterrupt
        real(source, dest, long_edge)

    monkeypatch.setattr(derive, "_resize", interrupt_partway)
    first = derive.run_process()
    assert first.cancelled is True
    assert first.made < 36                      # 12 files x 3 tiers

    monkeypatch.setattr(derive, "_resize", real)
    second = derive.run_process()

    assert second.cancelled is False
    assert len(list((tiers["thumb"] / "legacy_2026").iterdir())) == 12
    assert len(list((tiers["preview"] / "legacy_2026").iterdir())) == 12


def test_restart_does_not_redo_finished_work(
    tiers: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second run must only touch what the first did not finish."""
    _photos(tiers["master"] / "legacy_2026", 6)

    real = derive._resize
    seen = {"n": 0}

    def interrupt_partway(source: Path, dest: Path, long_edge: int) -> None:
        seen["n"] += 1
        if seen["n"] == 4:
            raise KeyboardInterrupt
        real(source, dest, long_edge)

    monkeypatch.setattr(derive, "_resize", interrupt_partway)
    first = derive.run_process()

    monkeypatch.setattr(derive, "_resize", real)
    second = derive.run_process()

    assert first.made + second.made == 18       # 6 files x 3 tiers, each once


def test_partials_are_swept_on_the_next_run(tiers: dict[str, Path]) -> None:
    """A kill leaves marker temps; nothing else removes them."""
    _photos(tiers["master"] / "legacy_2026", 1)
    stale = tiers["thumb"] / "legacy_2026" / ("x.jpg" + EXPORT_TMP_SUFFIX)
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"partial")

    derive.run_process()

    assert not stale.exists()


def test_a_partial_is_never_mistaken_for_finished_work(
    tiers: dict[str, Path]
) -> None:
    """Temp-then-rename is what makes restart safe rather than lossy."""
    _photos(tiers["master"] / "legacy_2026", 1)
    partial = tiers["thumb"] / "legacy_2026" / ("000.jpg.jpg" + EXPORT_TMP_SUFFIX)
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"truncated")

    derive.run_process()

    real = tiers["thumb"] / "legacy_2026" / "000.jpg.jpg"
    assert real.is_file()
    with Image.open(real) as img:                # a real image, not the stub
        assert max(img.size) == derive.THUMB_PX


def test_status_reports_progress_and_eta() -> None:
    s = derive.ProcessSummary(thumbs=3, previews=2)
    body = derive._status(s, 10, 0.0, {"done": 5, "inflight": 2, "cancelling": 0})

    assert body.startswith("5/10")
    assert "3 thumb" in body and "2 preview" in body
    assert "ETA" in body


def test_status_reports_the_drain_when_cancelling() -> None:
    s = derive.ProcessSummary()
    body = derive._status(s, 10, 0.0, {"done": 5, "inflight": 4, "cancelling": 1})

    assert "CANCELLING" in body
    assert "4" in body


def test_status_surfaces_failures() -> None:
    """A run quietly failing on every file should not look healthy."""
    s = derive.ProcessSummary(failed=["a: boom"])
    body = derive._status(s, 10, 0.0, {"done": 1, "inflight": 0, "cancelling": 0})

    assert "1 failed" in body
