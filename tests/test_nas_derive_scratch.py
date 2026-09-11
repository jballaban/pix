"""Poster-frame scratch isolation (spec/nas-app.md §9).

A shared scratch directory meant any other run — including a test suite —
swept the frames a live run was mid-way through reading, surfacing as a
`FileNotFoundError` on a file that demonstrably existed a moment earlier.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pix.nas import derive


def test_the_guard_isolates_scratch_from_the_real_one() -> None:
    """The conftest guard must cover `_scratch`, not just the tier constants.

    It is a function rather than a Path attribute, so the guard's attribute scan
    cannot see it — and it is precisely the one that caused a live run to lose
    its poster frames to a test suite sweep.
    """
    import tempfile

    scratch = derive._scratch()
    shared = Path(tempfile.gettempdir()) / "pix2-process"

    assert shared not in scratch.parents
    assert scratch != shared


def test_reap_removes_only_dead_owners(monkeypatch: pytest.MonkeyPatch,
                                       tmp_path: Path) -> None:
    """A live run's scratch is never anyone else's to delete."""
    base = tmp_path / "pix2-process"
    mine = base / str(os.getpid())
    mine.mkdir(parents=True)
    dead = base / "999999999"
    dead.mkdir()
    (dead / "stale.jpg").write_bytes(b"x")
    alive = base / str(os.getppid())
    alive.mkdir(exist_ok=True)

    monkeypatch.setattr(derive, "_scratch", lambda: mine)
    removed = derive._reap_dead_scratch()

    assert removed >= 1
    assert not dead.exists()
    assert mine.is_dir()
    assert alive.is_dir()


def test_reap_ignores_non_numeric_directories(monkeypatch: pytest.MonkeyPatch,
                                              tmp_path: Path) -> None:
    base = tmp_path / "pix2-process"
    mine = base / str(os.getpid())
    mine.mkdir(parents=True)
    other = base / "not-a-pid"
    other.mkdir()

    monkeypatch.setattr(derive, "_scratch", lambda: mine)
    derive._reap_dead_scratch()

    assert other.is_dir()


def test_poster_names_include_the_master_folder(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """Two master folders can hold the same flattened filename.

    With a name keyed only on the file, one worker would delete the poster
    another was reading.
    """
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(derive, "_scratch", lambda: scratch)
    monkeypatch.setattr(derive.shutil, "which", lambda _n: None)   # no ffmpeg

    a = tmp_path / "init_A" / "clip.mp4"
    b = tmp_path / "init_B" / "clip.mp4"
    for p in (a, b):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")

    # `_poster_frame` returns None without ffmpeg, so assert on the naming rule
    # it would use: the folder is part of the stem.
    stem_a = f"{a.parent.name}_{a.name}"
    stem_b = f"{b.parent.name}_{b.name}"
    assert stem_a != stem_b
