"""End-to-end checkout → commit orchestration for perceptual `pix dedupe`.

Drives `dedupe_library` with seeded caches (.meta/.hash/.vfp) and a stubbed
montage renderer, so it needs neither ffmpeg nor ExifTool — exercising the
real path that *deletes* files: build → manifest → survivor match → apply.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pix import dedupe_review
from pix.commands.dedupe import dedupe_library
from pix.dedupe_review import montage_name, read_manifest
from pix.hash_cache import write_cached_hash
from pix.metadata_cache import PerFileCache
from pix.plan import PIX_ORIGINAL_PATH
from pix.vfp_cache import write_cached_fingerprint
from pix.video_fingerprint import VideoFingerprint


def _stub_render(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace montage rendering with a touch of the montage file (no ffmpeg)."""
    def fake(review_dir: Path, group_id: str, distance: int,
             members: list[Path], durations: dict[Path, float]) -> bool:
        (review_dir / montage_name(group_id, distance)).write_bytes(b"montage")
        return True
    monkeypatch.setattr(dedupe_review, "render_montage", fake)


def _seed_lib(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A library with two perceptual-duplicate videos (identical seeded
    fingerprint), keeper = the larger file. Returns (root, keeper, loser)."""
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    keep = (root / "keep.mp4").resolve()
    dup = (root / "dup.mp4").resolve()
    keep.write_bytes(b"\x00" * 4000)   # larger → higher bitrate → keeper
    dup.write_bytes(b"\x00" * 800)

    cache = PerFileCache.for_library(root)
    for f in (keep, dup):
        cache.add(f, {PIX_ORIGINAL_PATH: f"F:/src/{f.name}"})
        st = f.stat()
        write_cached_hash(
            root, f, hash_hex=f"h_{f.name}",
            size=st.st_size, mtime_ns=st.st_mtime_ns,
        )
        write_cached_fingerprint(
            root, f,
            fingerprint=VideoFingerprint(
                frames=(0, 0, 0, 0, 0, 0), width=1920, height=1080, duration=10.0
            ),
            size=st.st_size, mtime_ns=st.st_mtime_ns,
        )
    return root, keep, dup


def test_checkout_writes_review_and_deletes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_render(monkeypatch)
    root, keep, dup = _seed_lib(tmp_path)
    review = tmp_path / "review"

    dedupe_library(path=root, checkout=review)

    # one perceptual group → one montage + a manifest; nothing removed
    m = read_manifest(review)
    assert len(m["groups"]) == 1
    g = m["groups"][0]
    assert g["keeper"] == "keep.mp4"
    assert (review / montage_name(g["id"], g["distance"])).is_file()
    assert keep.exists() and dup.exists()


def test_commit_removes_loser_keeps_keeper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_render(monkeypatch)
    root, keep, dup = _seed_lib(tmp_path)
    review = tmp_path / "review"

    dedupe_library(path=root, checkout=review)
    dedupe_library(commit=review)

    assert keep.exists()          # keeper survives
    assert not dup.exists()       # loser removed (conserved to run folder)


def test_commit_skips_group_whose_montage_was_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_render(monkeypatch)
    root, keep, dup = _seed_lib(tmp_path)
    review = tmp_path / "review"

    dedupe_library(path=root, checkout=review)
    # user deletes the only montage → group is skipped on commit
    g = read_manifest(review)["groups"][0]
    (review / montage_name(g["id"], g["distance"])).unlink()

    dedupe_library(commit=review)

    assert keep.exists() and dup.exists()   # nothing removed
