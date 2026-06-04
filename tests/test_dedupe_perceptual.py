"""Tests for perceptual video grouping + keeper ranking in pix.dedupe."""

from __future__ import annotations

from pathlib import Path

from pix.dedupe import (
    generate_plan,
    group_by_fingerprint,
    select_video_keeper,
)
from pix.metadata import FileMetadata
from pix.plan import PIX_ORIGINAL_PATH
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


def test_transitive_group_reports_worst_pair_distance(tmp_path: Path) -> None:
    """A~B (5) and B~C (30) chain A,B,C into one group even though A~C (35)
    exceeds the band; the reported distance is the worst pair (35), not the
    largest linking edge (30)."""
    a, b, c = _mk(tmp_path, "a.mp4"), _mk(tmp_path, "b.mp4"), _mk(tmp_path, "c.mp4")
    cache = {a: _meta(a), b: _meta(b), c: _meta(c)}
    fa = (0, 0, 0, 0, 0, 0)
    fb = ((1 << 5) - 1, 0, 0, 0, 0, 0)    # 5 bits from A
    fc = ((1 << 35) - 1, 0, 0, 0, 0, 0)   # 35 bits from A; 30 bits from B
    fps = {a: _fp(fa), b: _fp(fb), c: _fp(fc)}
    groups = group_by_fingerprint(tmp_path, cache, fps, 0, 30)
    assert len(groups) == 1
    g = groups[0]
    assert {g.keeper, *g.losers} == {a, b, c}
    assert g.distance == 35   # worst pair (A~C), above the band — flagged


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


def _migrated(p: Path) -> FileMetadata:
    return FileMetadata(
        path=p, raw={"SourceFile": str(p), PIX_ORIGINAL_PATH: f"F:/src/{p.name}"}
    )


def test_videos_only_excludes_exact_image_groups(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    i1, i2 = _mk(root, "a.jpg"), _mk(root, "b.jpg")        # exact image dups
    v1, v2 = _mk(root, "v1.mp4", 200), _mk(root, "v2.mp4", 100)  # video dups
    cache = {p: _migrated(p) for p in (i1, i2, v1, v2)}
    hashes: dict[Path, str | None] = {i1: "H", i2: "H", v1: "vh1", v2: "vh2"}
    fps: dict[Path, VideoFingerprint | None] = {v1: _fp(Z), v2: _fp(Z)}
    run_dir = root / ".pix" / "runs" / "r"
    run_dir.mkdir(parents=True)

    full = generate_plan(
        library_root=root, cache=cache, hashes=hashes, run_id="r",
        run_dir=run_dir, fingerprints=fps, min_distance=0, max_distance=30,
        videos_only=False,
    )
    assert sorted(g.kind for g in full.groups) == ["exact", "perceptual"]

    vids = generate_plan(
        library_root=root, cache=cache, hashes=hashes, run_id="r",
        run_dir=run_dir, fingerprints=fps, min_distance=0, max_distance=30,
        videos_only=True,
    )
    assert [g.kind for g in vids.groups] == ["perceptual"]
