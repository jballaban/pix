"""Tests for migrate's remux-repair salvage of damaged video containers
(`pix.apply._repair_video_container`). `remux_repair` is monkeypatched so
these don't shell out to ffmpeg."""

from __future__ import annotations

from pathlib import Path

import pytest

import pix.apply as apply_mod
from pix.convert import ConvertFailed
from pix.exiftool_session import ExifToolSession
from pix.plan import Action, PlanLine

_repair_video_container = apply_mod._repair_video_container  # pyright: ignore[reportPrivateUsage]
_repair_image = apply_mod._repair_image  # pyright: ignore[reportPrivateUsage]


def _line(src: Path) -> PlanLine:
    return PlanLine(
        line_id="L001",
        action=Action.TAG,
        rel_path=src.name,
        details="",
        abs_path=src,
    )


def test_repair_remuxes_and_swaps_conserving_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    staging = tmp_path / "staging"
    src = tmp_path / "lib" / "v.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"DAMAGED")

    def fake_remux(s: Path, d: Path) -> None:
        d.write_bytes(b"CLEAN")

    monkeypatch.setattr(apply_mod, "remux_repair", fake_remux)

    assert _repair_video_container(_line(src), run_dir, staging) is True
    # Clean container swapped into place; damaged original conserved.
    assert src.read_bytes() == b"CLEAN"
    captured = run_dir / "data" / "L001_v.mp4.damaged"
    assert captured.exists() and captured.read_bytes() == b"DAMAGED"


def test_repair_skips_non_video(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    src = tmp_path / "lib" / "photo.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"img")

    # No monkeypatch needed — it must bail before touching ffmpeg.
    assert _repair_video_container(_line(src), run_dir, tmp_path / "s") is False
    assert src.read_bytes() == b"img"  # untouched


def test_repair_returns_false_and_leaves_original_when_ffmpeg_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    src = tmp_path / "lib" / "v.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"DAMAGED")

    def boom(s: Path, d: Path) -> None:
        raise ConvertFailed("too damaged to salvage")

    monkeypatch.setattr(apply_mod, "remux_repair", boom)

    assert _repair_video_container(_line(src), run_dir, tmp_path / "s") is False
    # Original left in place for the caller to quarantine.
    assert src.exists() and src.read_bytes() == b"DAMAGED"
    assert not (run_dir / "data" / "L001_v.mp4.damaged").exists()


class _StubExif(ExifToolSession):
    """Records metadata-copy calls; spawns no subprocess."""

    def __init__(self) -> None:
        self.copied: list[tuple[Path, Path]] = []

    def copy_metadata_and_write_tags(
        self, source: Path, dest: Path, tags: dict[str, str]
    ) -> None:
        self.copied.append((source, dest))


def test_repair_image_reencodes_copies_metadata_and_swaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    staging = tmp_path / "staging"
    src = tmp_path / "lib" / "photo.jpg"  # content is actually PNG/odd
    src.parent.mkdir(parents=True)
    src.write_bytes(b"NOT-REALLY-JPEG")

    def fake_to_jpg(s: Path, d: Path) -> None:
        d.write_bytes(b"CLEANJPG")

    monkeypatch.setattr(apply_mod, "convert_to_jpg", fake_to_jpg)
    stub = _StubExif()

    ln = PlanLine(
        line_id="L001", action=Action.TAG, rel_path="photo.jpg",
        details="", abs_path=src,
    )
    assert _repair_image(ln, run_dir, staging, stub) is True
    # Clean JPEG swapped in; EXIF copied onto it; original conserved.
    assert src.read_bytes() == b"CLEANJPG"
    assert stub.copied and stub.copied[0][0] == src
    captured = run_dir / "data" / "L001_photo.jpg.original"
    assert captured.exists() and captured.read_bytes() == b"NOT-REALLY-JPEG"
