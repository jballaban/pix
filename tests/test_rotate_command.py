"""Tests for `pix tag rotate` — helpers and validation (no ffmpeg needed)."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from pix.commands import rotate as rot


@pytest.mark.parametrize(
    "deg, expected",
    [(0, 0), (-90, -90), (270, -90), (-270, 90), (180, -180), (-180, -180), (360, 0)],
)
def test_normalize(deg: int, expected: int) -> None:
    assert rot._normalize(deg) == expected


@pytest.mark.parametrize(
    "current, clockwise, stored",
    [
        (0, 90, -90),    # rotate right from upright
        (0, 270, 90),    # rotate left
        (0, 180, -180),  # half turn
        (-90, 90, -180),  # compose with an existing rotation
    ],
)
def test_composed_stored_rotation(current: int, clockwise: int, stored: int) -> None:
    # The command stores normalize(current - clockwise).
    assert rot._normalize(current - clockwise) == stored


def test_rejects_bad_degrees(tmp_path: Path) -> None:
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    with pytest.raises(typer.Exit):
        rot.rotate_videos(45, [f], no_prompt=True)


def test_expand_videos_filters_and_recurses(tmp_path: Path) -> None:
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "b.txt").write_bytes(b"x")
    (tmp_path / "c.jpg").write_bytes(b"x")  # image: not rotatable here
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "d.mov").write_bytes(b"x")
    (sub / "e.png").write_bytes(b"x")

    out = rot._expand_videos([tmp_path / "a.mp4", tmp_path / "b.txt", sub])
    assert sorted(p.name for p in out) == ["a.mp4", "d.mov"]


def test_expand_videos_dedupes_file_and_folder(tmp_path: Path) -> None:
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    out = rot._expand_videos([f, tmp_path])
    assert [p.name for p in out] == ["a.mp4"]
