"""Tests for perceptual video grouping + keeper ranking in pix.dedupe."""

from __future__ import annotations

from pathlib import Path

from pix.dedupe import group_by_fingerprint, select_video_keeper
from pix.metadata import FileMetadata
from pix.video_fingerprint import VideoFingerprint


def _meta(p: Path) -> FileMetadata:
    return FileMetadata(path=p, raw={"SourceFile": str(p)})


def _fp(frames: tuple[int, ...], w: int = 1920, h: int = 1080,
        dur: float = 10.0) -> VideoFingerprint:
    return VideoFingerprint(frames=frames, width=w, height=h, duration=dur)


def _mk(root: Path, name: str, nbytes: int = 100) -> Path:
    p = (root / name).resolve()
    p.write_bytes(b"\x00" * nbytes)
    return p


Z = (0, 0, 0, 0, 0, 0)


def test_groups_identical_fingerprints(tmp_path: Path) -> None:
    a, b = _mk(tmp_path, "a.mp4"), _mk(tmp_path, "b.mp4")
    cache = {a: _meta(a), b: _meta(b)}
    fps = {a: _fp(Z), b: _fp(Z)}
    groups = group_by_fingerprint(tmp_path, cache, fps, 0, 30)
    assert len(groups) == 1
    g = groups[0]
    assert {g.keeper, *g.losers} == {a, b}
    assert g.kind == "perceptual" and g.distance == 0


def test_not_grouped_above_max_distance(tmp_path: Path) -> None:
    a, b = _mk(tmp_path, "a.mp4"), _mk(tmp_path, "b.mp4")
    cache = {a: _meta(a), b: _meta(b)}
    fps = {a: _fp(Z), b: _fp(((1 << 31) - 1, 0, 0, 0, 0, 0))}  # 31 bits > 30
    assert group_by_fingerprint(tmp_path, cache, fps, 0, 30) == []


def test_grouped_just_under_max(tmp_path: Path) -> None:
    a, b = _mk(tmp_path, "a.mp4"), _mk(tmp_path, "b.mp4")
    cache = {a: _meta(a), b: _meta(b)}
    fps = {a: _fp(Z), b: _fp(((1 << 20) - 1, 0, 0, 0, 0, 0))}  # 20 bits <= 30
    groups = group_by_fingerprint(tmp_path, cache, fps, 0, 30)
    assert len(groups) == 1 and groups[0].distance == 20


def test_min_distance_excludes_exact(tmp_path: Path) -> None:
    """Band curation: --min 10 skips identical (distance 0) pairs."""
    a, b = _mk(tmp_path, "a.mp4"), _mk(tmp_path, "b.mp4")
    cache = {a: _meta(a), b: _meta(b)}
    fps = {a: _fp(Z), b: _fp(Z)}
    assert group_by_fingerprint(tmp_path, cache, fps, 10, 40) == []


def test_not_grouped_different_resolution(tmp_path: Path) -> None:
    a, b = _mk(tmp_path, "a.mp4"), _mk(tmp_path, "b.mp4")
    cache = {a: _meta(a), b: _meta(b)}
    fps = {a: _fp(Z, w=1920, h=1080), b: _fp(Z, w=1280, h=720)}
    assert group_by_fingerprint(tmp_path, cache, fps, 0, 30) == []


def test_not_grouped_duration_beyond_tolerance(tmp_path: Path) -> None:
    a, b = _mk(tmp_path, "a.mp4"), _mk(tmp_path, "b.mp4")
    cache = {a: _meta(a), b: _meta(b)}
    fps = {a: _fp(Z, dur=10.0), b: _fp(Z, dur=12.0)}  # 2s apart > 0.75
    assert group_by_fingerprint(tmp_path, cache, fps, 0, 30) == []


def test_keeper_is_higher_bitrate(tmp_path: Path) -> None:
    """Within a same-resolution group, the larger file (higher bitrate)
    is the keeper; the smaller is a loser."""
    big = _mk(tmp_path, "big.mp4", nbytes=5000)
    small = _mk(tmp_path, "small.mp4", nbytes=500)
    fps = {big: _fp(Z), small: _fp(Z)}
    assert select_video_keeper(tmp_path, [big, small], fps) == big
    cache = {big: _meta(big), small: _meta(small)}
    groups = group_by_fingerprint(tmp_path, cache, fps, 0, 30)
    assert len(groups) == 1
    assert groups[0].keeper == big and groups[0].losers == (small,)


def test_failed_fingerprint_never_groups(tmp_path: Path) -> None:
    a, b = _mk(tmp_path, "a.mp4"), _mk(tmp_path, "b.mp4")
    cache = {a: _meta(a), b: _meta(b)}
    fps = {a: _fp((-1, -1, -1, -1, -1, -1)), b: _fp(Z)}
    assert group_by_fingerprint(tmp_path, cache, fps, 0, 30) == []
