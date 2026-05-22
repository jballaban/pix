"""Tests for `pix.stash` — purist sidecar + opaque-filename move."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from pix.stash import (
    SIDECAR_SUFFIX,
    StashSidecar,
    read_sidecar,
    sidecar_path_for,
    stash_file,
    stash_filename,
    write_sidecar,
)


# --- Sidecar serialization --------------------------------------------------


def test_sidecar_round_trip() -> None:
    s = StashSidecar(
        origin="F:/source/IMG_001.dng", stashed_at="2026-05-22T15:30:00"
    )
    parsed = StashSidecar.from_yaml(s.to_yaml())
    assert parsed == s


def test_sidecar_rejects_missing_origin() -> None:
    with pytest.raises(ValueError, match="origin"):
        StashSidecar.from_yaml("stashed_at: 2026-05-22T15:30:00\n")


def test_sidecar_rejects_missing_stashed_at() -> None:
    with pytest.raises(ValueError, match="stashed_at"):
        StashSidecar.from_yaml("origin: F:/x\n")


# --- Sidecar file ops -------------------------------------------------------


def test_write_and_read_sidecar(tmp_path: Path) -> None:
    stash_path = tmp_path / "2026-05-22_15-30-00_L001.dng"
    stash_path.write_bytes(b"")
    sidecar = StashSidecar(origin="F:/src.dng", stashed_at="2026-05-22T15:30:00")
    write_sidecar(stash_path, sidecar)
    assert sidecar_path_for(stash_path).exists()
    assert read_sidecar(stash_path) == sidecar


def test_read_sidecar_returns_none_when_missing(tmp_path: Path) -> None:
    assert read_sidecar(tmp_path / "no-such.dng") is None


def test_read_sidecar_returns_none_on_malformed_yaml(tmp_path: Path) -> None:
    stash_path = tmp_path / "x.dng"
    stash_path.write_bytes(b"")
    sidecar_path_for(stash_path).write_text(
        "not: valid: yaml: tokens", encoding="utf-8"
    )
    assert read_sidecar(stash_path) is None


# --- Opaque filename ---------------------------------------------------------


def test_stash_filename_uses_run_id_line_id_and_ext() -> None:
    src = Path("F:/source/IMG_001.dng")
    assert (
        stash_filename("2026-05-22_15-30-00", "L042", src)
        == "2026-05-22_15-30-00_L042.dng"
    )


def test_stash_filename_lowercases_extension() -> None:
    src = Path("F:/source/IMG.DNG")
    assert stash_filename("r", "L001", src) == "r_L001.dng"


def test_stash_filename_handles_no_extension() -> None:
    src = Path("F:/source/raw_blob")
    assert stash_filename("r", "L001", src) == "r_L001"


# --- stash_file --------------------------------------------------------------


def test_stash_file_moves_source_and_writes_sidecar(tmp_path: Path) -> None:
    source = tmp_path / "src" / "IMG_001.dng"
    source.parent.mkdir()
    source.write_bytes(b"raw-bytes")

    stash_dir = tmp_path / ".pix" / "stash"
    target = stash_dir / "2026-05-22_15-30-00_L001.dng"

    stash_file(
        source=source,
        target_path=target,
        stashed_at=datetime(2026, 5, 22, 15, 30, 0),
    )

    assert not source.exists()
    assert target.exists()
    assert target.read_bytes() == b"raw-bytes"

    sidecar = read_sidecar(target)
    assert sidecar is not None
    assert sidecar.origin == str(source)
    assert sidecar.stashed_at == "2026-05-22T15:30:00"


def test_stash_file_creates_target_dir(tmp_path: Path) -> None:
    """Stash dir doesn't have to exist yet."""
    source = tmp_path / "src.dng"
    source.write_bytes(b"")
    target = tmp_path / "deeply" / "nested" / "stash" / "x.dng"
    stash_file(source=source, target_path=target)
    assert target.exists()


def test_stash_file_two_different_sources_no_collision(tmp_path: Path) -> None:
    """With opaque filenames, two stash ops never collide."""
    src1 = tmp_path / "a" / "IMG_001.dng"
    src2 = tmp_path / "b" / "IMG_001.dng"  # same basename!
    for s in (src1, src2):
        s.parent.mkdir()
        s.write_bytes(b"")
    stash_dir = tmp_path / "stash"

    stash_file(source=src1, target_path=stash_dir / "r_L001.dng")
    stash_file(source=src2, target_path=stash_dir / "r_L002.dng")

    assert (stash_dir / "r_L001.dng").exists()
    assert (stash_dir / "r_L002.dng").exists()


def test_stash_file_same_content_lands_twice_no_dedup(tmp_path: Path) -> None:
    """Purist stash doesn't dedup — same content from different sources
    yields two copies in stash. Future dedupe-stash operation can collapse."""
    src1 = tmp_path / "a" / "x.dng"
    src2 = tmp_path / "b" / "y.dng"
    for s in (src1, src2):
        s.parent.mkdir()
        s.write_bytes(b"same-content")
    stash_dir = tmp_path / "stash"

    stash_file(source=src1, target_path=stash_dir / "r_L001.dng")
    stash_file(source=src2, target_path=stash_dir / "r_L002.dng")

    assert (stash_dir / "r_L001.dng").read_bytes() == b"same-content"
    assert (stash_dir / "r_L002.dng").read_bytes() == b"same-content"
