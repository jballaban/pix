"""Tests for `cleanup_empty_pix_workdirs` — reaping empty .pix workdirs."""

from __future__ import annotations

from pathlib import Path

from pix.cleanup import cleanup_empty_pix_workdirs


def _pix(root: Path) -> Path:
    p = root / ".pix"
    p.mkdir(parents=True)
    return p


def test_removes_empty_workdirs_including_nested_empty_subdirs(
    tmp_path: Path,
) -> None:
    pix = _pix(tmp_path)
    # errors with an empty mirrored subtree, plus empty staging + stash.
    (pix / "errors" / "G" / "pix" / "2020").mkdir(parents=True)
    (pix / "staging").mkdir()
    (pix / "stash").mkdir()

    removed = cleanup_empty_pix_workdirs(tmp_path)

    assert sorted(removed) == ["errors", "staging", "stash"]
    assert not (pix / "errors").exists()
    assert not (pix / "staging").exists()
    assert not (pix / "stash").exists()


def test_keeps_non_empty_workdir(tmp_path: Path) -> None:
    pix = _pix(tmp_path)
    held = pix / "errors" / "G" / "pix"
    held.mkdir(parents=True)
    (held / "still_here.jpg").write_bytes(b"x")
    (pix / "staging").mkdir()  # empty → should go

    removed = cleanup_empty_pix_workdirs(tmp_path)

    assert removed == ["staging"]
    assert (pix / "errors").exists()  # has a file → kept
    assert (held / "still_here.jpg").exists()
    assert not (pix / "staging").exists()


def test_missing_workdirs_are_a_noop(tmp_path: Path) -> None:
    _pix(tmp_path)  # .pix exists but no workdirs
    assert cleanup_empty_pix_workdirs(tmp_path) == []


def test_does_not_touch_runs_or_cache(tmp_path: Path) -> None:
    pix = _pix(tmp_path)
    (pix / "runs" / "2026-01-01_00-00-00").mkdir(parents=True)
    (pix / "cache").mkdir()
    (pix / "errors").mkdir()

    cleanup_empty_pix_workdirs(tmp_path)

    # Only the three workdirs are eligible; runs/ and cache/ are left alone
    # even when empty (runs holds rollback data; cache is rebuildable but
    # not our job to reap here).
    assert (pix / "runs").exists()
    assert (pix / "cache").exists()
    assert not (pix / "errors").exists()
