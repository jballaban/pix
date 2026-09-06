"""Tests for the import-ingest pre-pass (see spec/import.md → Ingestion)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pix import ingest
from pix.ingest import (
    committed_import_ids,
    incoming_dir,
    run_ingest,
    should_ingest,
)


def _verified(friendly_dir: Path, rel: str, data: bytes = b"media",
              *, serial: str = "SER1", puid: str = "{P1}",
              device_name: str = "james", imported_at: str = "20260720") -> Path:
    """Create a landed VERIFIED file (media + `.manifest/`-housed .importinfo)."""
    media = friendly_dir / rel
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(data)
    sidecar = media.parent / ".manifest" / (media.name + ".importinfo")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    dev_path = f"Internal Storage/{rel}"
    sidecar.write_text(
        f"serial: {serial}\npuid: '{puid}'\ndevice_name: {device_name}\n"
        f"imported_at: '{imported_at}'\ndevice_path: {dev_path}\n",
        encoding="utf-8",
    )
    return media


def _import_dir(root: Path, friendly: str = "james") -> Path:
    d = root / ".pix" / "local" / "import" / friendly
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- scope -------------------------------------------------------------------
def test_should_ingest_when_incoming_under_folder(tmp_path: Path) -> None:
    assert should_ingest(tmp_path, tmp_path) is True            # root
    assert should_ingest(tmp_path, incoming_dir(tmp_path)) is True  # incoming itself


def test_should_not_ingest_unrelated_folder(tmp_path: Path) -> None:
    assert should_ingest(tmp_path, tmp_path / "2014") is False


# --- drain / flatten ---------------------------------------------------------
def test_run_ingest_flattens_into_incoming(tmp_path: Path) -> None:
    fdir = _import_dir(tmp_path)
    _verified(fdir, "202605_a/IMG_1.HEIC")
    _verified(fdir, "202605_a/IMG_2.JPG")
    runs = tmp_path / ".pix" / "runs" / "r1"
    runs.mkdir(parents=True)

    summary = run_ingest(tmp_path, tmp_path, runs)

    assert summary.ingested == 2
    inc = incoming_dir(tmp_path)
    assert (inc / "IMG_1.HEIC").is_file()
    assert (inc / "IMG_2.JPG").is_file()
    # Device month-bucket folders dropped (flat landing).
    assert not (inc / "202605_a").exists()
    # Sidecars ride along.
    assert (inc / "IMG_1.HEIC.importinfo").is_file()


def test_run_ingest_legacy_beside_media_sidecar_still_ingests(
    tmp_path: Path,
) -> None:
    """Transitional: a landing written by a pre-`.manifest/` build (sidecar beside
    the media) still ingests instead of stranding."""
    fdir = _import_dir(tmp_path)
    media = fdir / "202605_a" / "IMG_9.HEIC"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"media")
    (media.parent / (media.name + ".importinfo")).write_text(  # beside, legacy
        "serial: SER1\npuid: '{P9}'\ndevice_name: james\n"
        "imported_at: '20260720'\ndevice_path: Internal Storage/202605_a/IMG_9.HEIC\n",
        encoding="utf-8",
    )
    runs = tmp_path / ".pix" / "runs" / "r1"; runs.mkdir(parents=True)

    summary = run_ingest(tmp_path, tmp_path, runs)
    assert summary.ingested == 1
    inc = incoming_dir(tmp_path)
    assert (inc / "IMG_9.HEIC").is_file()
    assert (inc / "IMG_9.HEIC.importinfo").is_file()  # rides along beside media


def test_run_ingest_skips_unverified_and_issue_files(tmp_path: Path) -> None:
    fdir = _import_dir(tmp_path)
    _verified(fdir, "IMG_OK.JPG")
    # Unprobed straggler: media, no sidecar.
    (fdir / "IMG_RAW.JPG").write_bytes(b"x")
    # Problem marker: media + .importissue (not .importinfo).
    (fdir / "IMG_BAD.JPG").write_bytes(b"x")
    (fdir / "IMG_BAD.JPG.importissue").write_text("state: failed\n", encoding="utf-8")
    runs = tmp_path / ".pix" / "runs" / "r1"; runs.mkdir(parents=True)

    summary = run_ingest(tmp_path, tmp_path, runs)

    assert summary.ingested == 1
    inc = incoming_dir(tmp_path)
    assert (inc / "IMG_OK.JPG").is_file()
    assert not (inc / "IMG_RAW.JPG").exists()   # left in place (not VERIFIED)
    assert not (inc / "IMG_BAD.JPG").exists()   # left in place (.importissue)


def test_run_ingest_collision_suffix(tmp_path: Path) -> None:
    fdir = _import_dir(tmp_path)
    _verified(fdir, "202605_a/IMG_1.HEIC", data=b"one", puid="{A}")
    _verified(fdir, "202606_a/IMG_1.HEIC", data=b"two", puid="{B}")
    runs = tmp_path / ".pix" / "runs" / "r1"; runs.mkdir(parents=True)

    summary = run_ingest(tmp_path, tmp_path, runs)

    assert summary.ingested == 2
    inc = incoming_dir(tmp_path)
    names = sorted(p.name for p in inc.iterdir() if p.suffix.upper() == ".HEIC")
    assert names == ["IMG_1.HEIC", "IMG_1_2.HEIC"]  # second collided → suffixed


def test_run_ingest_reaps_drained_folders(tmp_path: Path) -> None:
    fdir = _import_dir(tmp_path)  # .pix/local/import/james
    _verified(fdir, "202605_a/IMG_1.HEIC")
    _verified(fdir, "202606_b/IMG_2.HEIC")
    runs = tmp_path / ".pix" / "runs" / "r1"; runs.mkdir(parents=True)

    summary = run_ingest(tmp_path, tmp_path, runs)

    assert summary.ingested == 2
    # Drained device folders — and the now-empty friendly folder — are gone.
    assert not (fdir / "202605_a").exists()
    assert not (fdir / "202606_b").exists()
    assert not fdir.exists()
    assert summary.folders_reaped >= 2
    # The import/ container itself is left in place.
    assert (tmp_path / ".pix" / "local" / "import").is_dir()


def test_run_ingest_keeps_folder_with_unprocessed_files(tmp_path: Path) -> None:
    fdir = _import_dir(tmp_path)
    _verified(fdir, "202605_a/IMG_1.HEIC")            # ingested
    (fdir / "202605_a" / "IMG_BAD.JPG").write_bytes(b"x")   # not VERIFIED
    (fdir / "202605_a" / "IMG_BAD.JPG.importissue").write_text(
        "state: failed\n", encoding="utf-8"
    )
    runs = tmp_path / ".pix" / "runs" / "r1"; runs.mkdir(parents=True)

    run_ingest(tmp_path, tmp_path, runs)

    # Folder still holds the failed file → NOT reaped.
    assert (fdir / "202605_a").is_dir()
    assert (fdir / "202605_a" / "IMG_BAD.JPG").exists()


def test_run_ingest_records_committed_ids(tmp_path: Path) -> None:
    fdir = _import_dir(tmp_path)
    _verified(fdir, "IMG_1.HEIC", serial="SER1", puid="{P1}")
    runs = tmp_path / ".pix" / "runs" / "r1"; runs.mkdir(parents=True)

    run_ingest(tmp_path, tmp_path, runs)

    assert committed_import_ids(tmp_path) == {"SER1:{P1}"}


def test_run_ingest_noop_out_of_scope(tmp_path: Path) -> None:
    fdir = _import_dir(tmp_path)
    _verified(fdir, "IMG_1.HEIC")
    runs = tmp_path / ".pix" / "runs" / "r1"; runs.mkdir(parents=True)
    # Migrating an unrelated subfolder → ingest is a no-op.
    summary = run_ingest(tmp_path, tmp_path / "2014", runs)
    assert summary.ingested == 0
    assert not incoming_dir(tmp_path).exists()


# --- Live Photo drop ---------------------------------------------------------
def test_live_photo_mov_dropped(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    fdir = _import_dir(tmp_path)
    _verified(fdir, "202605_a/IMG_1.HEIC")            # the photo
    _verified(fdir, "202605_a/IMG_1.MOV")             # its Live Photo clip
    monkeypatch.setattr(ingest, "_duration_seconds", lambda _p: 2.5)  # short
    runs = tmp_path / ".pix" / "runs" / "r1"; runs.mkdir(parents=True)

    summary = run_ingest(tmp_path, tmp_path, runs)

    assert summary.ingested == 1              # only the HEIC
    assert summary.live_photos_dropped == 1
    inc = incoming_dir(tmp_path)
    assert (inc / "IMG_1.HEIC").is_file()
    assert not (inc / "IMG_1.MOV").exists()   # dropped
    assert (runs / "dropped-live-photos" / "IMG_1.MOV").is_file()  # soft, recoverable


def test_long_paired_mov_kept(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    fdir = _import_dir(tmp_path)
    _verified(fdir, "202605_a/IMG_1.HEIC")
    _verified(fdir, "202605_a/IMG_1.MOV")
    monkeypatch.setattr(ingest, "_duration_seconds", lambda _p: 30.0)  # real clip
    runs = tmp_path / ".pix" / "runs" / "r1"; runs.mkdir(parents=True)

    summary = run_ingest(tmp_path, tmp_path, runs)

    assert summary.ingested == 2 and summary.live_photos_dropped == 0
    assert (incoming_dir(tmp_path) / "IMG_1.MOV").is_file()


def test_standalone_mov_kept(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    fdir = _import_dir(tmp_path)
    _verified(fdir, "202605_a/CLIP.MOV")  # no sibling image
    monkeypatch.setattr(ingest, "_duration_seconds", lambda _p: 2.0)
    runs = tmp_path / ".pix" / "runs" / "r1"; runs.mkdir(parents=True)

    summary = run_ingest(tmp_path, tmp_path, runs)

    assert summary.ingested == 1 and summary.live_photos_dropped == 0


# --- culled landing entries (media deleted, sidecar kept) ---------------------
def test_run_ingest_retires_culled_sidecar_and_reaps_folder(tmp_path: Path) -> None:
    """A culled entry's skip record moves to the durable ledger and its folder
    stops being pinned alive (spec/import.md → Delete semantics)."""
    fdir = _import_dir(tmp_path)
    media = _verified(fdir, "202605_a/IMG_1.HEIC", puid="{P1}")
    media.unlink()  # the user culls the photo, keeping .manifest/
    runs = tmp_path / ".pix" / "runs" / "r1"; runs.mkdir(parents=True)

    summary = run_ingest(tmp_path, tmp_path, runs)

    assert summary.ingested == 0
    assert summary.culled_recorded == 1
    assert committed_import_ids(tmp_path) == {"SER1:{P1}"}
    assert not (fdir / "202605_a").exists()
    assert not fdir.exists()


def test_run_ingest_retires_culled_beside_media_sidecar(tmp_path: Path) -> None:
    """Legacy pre-`.manifest/` layout: an orphan sidecar beside the (deleted)
    media retires the same way."""
    fdir = _import_dir(tmp_path)
    (fdir / "202605_a").mkdir(parents=True)
    (fdir / "202605_a" / "IMG_1.HEIC.importinfo").write_text(
        "serial: SER1\npuid: '{P9}'\n", encoding="utf-8"
    )
    runs = tmp_path / ".pix" / "runs" / "r1"; runs.mkdir(parents=True)

    summary = run_ingest(tmp_path, tmp_path, runs)

    assert summary.culled_recorded == 1
    assert committed_import_ids(tmp_path) == {"SER1:{P9}"}
    assert not fdir.exists()


def test_run_ingest_keeps_culled_sidecar_without_import_id(tmp_path: Path) -> None:
    """No `<serial>:<puid>` → the sidecar is the only skip record, so it stays
    (and keeps its folder) rather than silently re-pulling the object."""
    fdir = _import_dir(tmp_path)
    media = _verified(fdir, "202605_a/IMG_1.HEIC", puid="")
    media.unlink()
    runs = tmp_path / ".pix" / "runs" / "r1"; runs.mkdir(parents=True)

    summary = run_ingest(tmp_path, tmp_path, runs)

    assert summary.culled_recorded == 0
    assert (fdir / "202605_a" / ".manifest" / "IMG_1.HEIC.importinfo").is_file()
    assert committed_import_ids(tmp_path) == set()


def test_run_ingest_does_not_retire_ingested_files_sidecars(tmp_path: Path) -> None:
    """A sidecar that just rode its media into incoming/ is gone from the landing,
    so it is never miscounted as culled."""
    fdir = _import_dir(tmp_path)
    _verified(fdir, "202605_a/IMG_1.HEIC", puid="{P1}")
    runs = tmp_path / ".pix" / "runs" / "r1"; runs.mkdir(parents=True)

    summary = run_ingest(tmp_path, tmp_path, runs)

    assert summary.ingested == 1
    assert summary.culled_recorded == 0
    assert committed_import_ids(tmp_path) == {"SER1:{P1}"}


def test_run_ingest_leaves_orphan_importissue_alone(tmp_path: Path) -> None:
    """An `.importissue` with no media is the operator's terminal record — it is
    not retired, and it still keeps its folder."""
    fdir = _import_dir(tmp_path)
    (fdir / "202605_a" / ".manifest").mkdir(parents=True)
    issue = fdir / "202605_a" / ".manifest" / "IMG_BAD.JPG.importissue"
    issue.write_text("state: failed\nserial: SER1\npuid: '{PB}'\n", encoding="utf-8")
    runs = tmp_path / ".pix" / "runs" / "r1"; runs.mkdir(parents=True)

    summary = run_ingest(tmp_path, tmp_path, runs)

    assert summary.culled_recorded == 0
    assert issue.is_file()
    assert (fdir / "202605_a").is_dir()
