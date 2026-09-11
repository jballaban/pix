"""`pix2 process` — thumbnails and previews (spec/nas-app.md §6, §9)."""

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
    monkeypatch.setattr(ledger, "MASTER_SHARE", share)
    monkeypatch.setattr(ledger, "MASTER_DIR", master)
    return {"master": master, "thumb": thumb, "preview": preview}


def _photo(folder: Path, name: str, size: tuple[int, int] = (3000, 2000)) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    Image.new("RGB", size, (120, 60, 30)).save(p, "JPEG")
    return p


def test_makes_both_tiers(tiers: dict[str, Path]) -> None:
    _photo(tiers["master"] / "legacy_2026", "a_one.jpg")

    s = derive.run_process()

    assert s.thumbs == 1 and s.previews == 1
    assert (tiers["thumb"] / "legacy_2026" / "a_one.jpg.jpg").is_file()
    assert (tiers["preview"] / "legacy_2026" / "a_one.jpg.jpg").is_file()


def test_sizes_are_capped_on_the_long_edge(tiers: dict[str, Path]) -> None:
    """A thumbnail you cannot judge from is pointless; one too big is slow."""
    _photo(tiers["master"] / "legacy_2026", "wide.jpg", size=(4000, 1000))

    derive.run_process()

    with Image.open(tiers["thumb"] / "legacy_2026" / "wide.jpg.jpg") as t:
        assert max(t.size) == derive.THUMB_PX
    with Image.open(tiers["preview"] / "legacy_2026" / "wide.jpg.jpg") as p:
        assert max(p.size) == derive.PREVIEW_PX


def test_small_images_are_not_upscaled(tiers: dict[str, Path]) -> None:
    _photo(tiers["master"] / "legacy_2026", "tiny.jpg", size=(100, 80))

    derive.run_process()

    with Image.open(tiers["preview"] / "legacy_2026" / "tiny.jpg.jpg") as p:
        assert p.size == (100, 80)


def test_derived_layout_mirrors_master(tiers: dict[str, Path]) -> None:
    """One master folder per upload; the derived tiers keep that shape."""
    _photo(tiers["master"] / "legacy_a", "one.jpg")
    _photo(tiers["master"] / "legacy_b", "one.jpg")

    derive.run_process()

    assert (tiers["thumb"] / "legacy_a" / "one.jpg.jpg").is_file()
    assert (tiers["thumb"] / "legacy_b" / "one.jpg.jpg").is_file()


def test_rerun_does_nothing(tiers: dict[str, Path]) -> None:
    """Resumable by construction: missing is recomputed every run."""
    _photo(tiers["master"] / "legacy_2026", "a.jpg")
    derive.run_process()

    again = derive.run_process()

    assert again.made == 0
    assert again.skipped == 0          # nothing was even pending


def test_only_the_missing_tier_is_regenerated(tiers: dict[str, Path]) -> None:
    _photo(tiers["master"] / "legacy_2026", "a.jpg")
    derive.run_process()
    (tiers["thumb"] / "legacy_2026" / "a.jpg.jpg").unlink()

    again = derive.run_process()

    assert again.thumbs == 1
    assert again.previews == 0


def test_ledgers_and_xmp_are_not_media(tiers: dict[str, Path]) -> None:
    """Master holds a ledger and decision sidecars; neither has a thumbnail."""
    folder = tiers["master"] / "legacy_2026"
    _photo(folder, "a.jpg")
    (folder / ".import.jsonl").write_text("{}\n", encoding="utf-8")
    (folder / "a.jpg.xmp").write_text("<x/>", encoding="utf-8")

    s = derive.run_process()

    assert s.thumbs == 1
    assert not (tiers["thumb"] / "legacy_2026" / ".import.jsonl.jpg").exists()
    assert not (tiers["thumb"] / "legacy_2026" / "a.jpg.xmp.jpg").exists()


def test_one_bad_file_does_not_stop_the_rest(tiers: dict[str, Path]) -> None:
    """62k files must not be held hostage by one corrupt JPEG."""
    folder = tiers["master"] / "legacy_2026"
    _photo(folder, "good.jpg")
    (folder / "bad.jpg").write_bytes(b"not an image at all")

    s = derive.run_process()

    assert s.thumbs == 1
    assert len(s.failed) == 1
    assert "bad.jpg" in s.failed[0]


def test_unknown_extensions_are_reported_not_failed(tiers: dict[str, Path]) -> None:
    folder = tiers["master"] / "legacy_2026"
    folder.mkdir(parents=True)
    (folder / "notes.txt").write_bytes(b"hello")

    s = derive.run_process()

    assert s.unsupported == 1
    assert s.failed == []


def test_no_partials_are_left_behind(tiers: dict[str, Path]) -> None:
    """Derived files are temp-then-renamed, like every other write here."""
    _photo(tiers["master"] / "legacy_2026", "a.jpg")

    derive.run_process()

    leftover = list(tiers["thumb"].rglob(f"*{EXPORT_TMP_SUFFIX}*"))
    assert leftover == []


def test_orientation_is_applied_not_carried(tiers: dict[str, Path]) -> None:
    """A sideways thumbnail is worse than useless in a grid you are scanning."""
    folder = tiers["master"] / "legacy_2026"
    folder.mkdir(parents=True)
    img = Image.new("RGB", (400, 200), (10, 200, 10))
    exif = img.getexif()
    exif[274] = 6                       # rotate 90 CW
    img.save(folder / "rot.jpg", "JPEG", exif=exif)

    derive.run_process()

    with Image.open(tiers["thumb"] / "legacy_2026" / "rot.jpg.jpg") as t:
        assert t.height > t.width       # transposed, not left on its side


def test_empty_master_is_not_an_error(tiers: dict[str, Path]) -> None:
    assert derive.run_process().made == 0
