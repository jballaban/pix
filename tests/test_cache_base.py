"""Tests for `pix.cache_base` — shared cache plumbing.

The path-mirror, atomic-write, and parallel-read helpers are exercised
indirectly by the per-cache test files (test_metadata_cache,
test_hash_cache, test_vfp_cache). This module focuses on what's
unique to cache_base: orphan pruning and legacy-suffix sweeping.
"""

from __future__ import annotations

from pathlib import Path

from pix.cache_base import (
    LIVE_SUFFIXES,
    PruneStats,
    cache_path_for,
    cache_root_for,
    prune_orphans,
    relocate_all,
    remove_all,
)


def _make_cache_file(
    library_root: Path,
    media_path: Path,
    suffix: str,
    content: bytes = b"{}",
) -> Path:
    """Create a sidecar at the mirrored path for `media_path`."""
    cache_path = cache_path_for(library_root, media_path, suffix)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(content)
    return cache_path


def test_keeps_sidecars_for_files_still_in_library(tmp_path: Path) -> None:
    """A cache entry whose media file still exists is left alone."""
    library_root = tmp_path / "lib"
    media = tmp_path / "lib" / "subdir" / "photo.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"x")

    meta = _make_cache_file(library_root, media, ".meta")
    hash_ = _make_cache_file(library_root, media, ".hash")

    stats = prune_orphans(library_root, {media})
    assert stats == PruneStats(orphans_removed=0, legacy_removed=0)
    assert meta.is_file()
    assert hash_.is_file()


def test_removes_orphan_sidecars(tmp_path: Path) -> None:
    """Sidecars for files NOT in expected_paths get unlinked."""
    library_root = tmp_path / "lib"
    library_root.mkdir()
    gone = tmp_path / "lib" / "old_dir" / "deleted.jpg"

    sidecars = [_make_cache_file(library_root, gone, s) for s in LIVE_SUFFIXES]

    stats = prune_orphans(library_root, expected_paths=set())
    assert stats.orphans_removed == len(LIVE_SUFFIXES)
    assert stats.legacy_removed == 0
    assert all(not s.exists() for s in sidecars)


def test_sweeps_legacy_cache_suffix(tmp_path: Path) -> None:
    """`.cache` files (pre-v0.1.88 metadata sidecars) are always removed."""
    library_root = tmp_path / "lib"
    library_root.mkdir()
    media = tmp_path / "lib" / "alive.jpg"
    media.write_bytes(b"x")

    # Place a legacy .cache sidecar alongside its mirrored path.
    legacy = _make_cache_file(library_root, media, ".cache")
    # And a .meta one for the same file (the new cache).
    new_meta = _make_cache_file(library_root, media, ".meta")

    stats = prune_orphans(library_root, {media})
    assert stats.legacy_removed == 1
    assert stats.orphans_removed == 0
    assert not legacy.exists()
    assert new_meta.is_file()  # current sidecar untouched


def test_sweeps_legacy_video_suffix(tmp_path: Path) -> None:
    """`.video` files (codec cache, dead since video handling went
    remux-only) are swept as legacy even though the media is still live."""
    library_root = tmp_path / "lib"
    library_root.mkdir()
    media = tmp_path / "lib" / "clip.mp4"
    media.write_bytes(b"x")

    legacy = _make_cache_file(library_root, media, ".video")
    new_meta = _make_cache_file(library_root, media, ".meta")

    stats = prune_orphans(library_root, {media})
    assert stats.legacy_removed == 1
    assert not legacy.exists()
    assert new_meta.is_file()  # current sidecar untouched


def test_allowed_prefix_scopes_orphan_removal(tmp_path: Path) -> None:
    """With allowed_prefix set, orphans outside the prefix are left alone.

    Migrate uses this: a migrate of `<library>/sub_a/` walks only that
    subfolder, so it shouldn't prune cache entries that mirror paths
    under `<library>/sub_b/`.
    """
    library_root = tmp_path / "lib"
    library_root.mkdir()
    in_scope_gone = tmp_path / "lib" / "sub_a" / "gone.jpg"
    out_of_scope_gone = tmp_path / "lib" / "sub_b" / "gone.jpg"

    inside = _make_cache_file(library_root, in_scope_gone, ".meta")
    outside = _make_cache_file(library_root, out_of_scope_gone, ".meta")

    stats = prune_orphans(
        library_root,
        expected_paths=set(),
        allowed_prefix=tmp_path / "lib" / "sub_a",
    )
    assert stats.orphans_removed == 1
    assert not inside.exists()
    assert outside.is_file()


def test_legacy_sweep_ignores_allowed_prefix(tmp_path: Path) -> None:
    """Legacy `.cache` sidecars are always swept regardless of prefix —
    they're dead bytes in any scope."""
    library_root = tmp_path / "lib"
    library_root.mkdir()
    media_inside = tmp_path / "lib" / "sub_a" / "x.jpg"
    media_outside = tmp_path / "lib" / "sub_b" / "y.jpg"

    legacy_in = _make_cache_file(library_root, media_inside, ".cache")
    legacy_out = _make_cache_file(library_root, media_outside, ".cache")

    stats = prune_orphans(
        library_root,
        expected_paths=set(),
        allowed_prefix=tmp_path / "lib" / "sub_a",
    )
    assert stats.legacy_removed == 2
    assert not legacy_in.exists()
    assert not legacy_out.exists()


def test_unknown_suffixes_left_alone(tmp_path: Path) -> None:
    """Files in `.pix/cache/` with unrecognized suffixes aren't touched."""
    library_root = tmp_path / "lib"
    cache_root = cache_root_for(library_root)
    cache_root.mkdir(parents=True)
    stray = cache_root / "random.txt"
    stray.write_text("not a sidecar")

    stats = prune_orphans(library_root, expected_paths=set())
    assert stats == PruneStats(orphans_removed=0, legacy_removed=0)
    assert stray.is_file()


def test_no_cache_dir_is_a_noop(tmp_path: Path) -> None:
    """If there's no .pix/cache/ at all (first run), prune is a no-op."""
    library_root = tmp_path / "lib"
    library_root.mkdir()
    stats = prune_orphans(library_root, expected_paths=set())
    assert stats == PruneStats(orphans_removed=0, legacy_removed=0)


def test_relocate_all_moves_every_sidecar(tmp_path: Path) -> None:
    """A media move must carry every LIVE_SUFFIXES sidecar to the new mirror.

    Regression for the organize cache-loss bug: relocating only some
    suffixes orphaned the rest, which the next walk pruned — forcing a
    needless re-`pix hash` (and, for the omitted `.vfp`, a re-fingerprint
    of the whole library) after every organize.
    """
    library_root = tmp_path / "lib"
    library_root.mkdir()
    old_media = tmp_path / "lib" / "raw" / "2023-08-15_143205.jpg"
    new_media = tmp_path / "lib" / "2023" / "Hawaii" / "2023-08-15_143205.jpg"

    for suffix in LIVE_SUFFIXES:
        _make_cache_file(library_root, old_media, suffix, b'{"k":1}')

    relocate_all(library_root, old_media, new_media)

    for suffix in LIVE_SUFFIXES:
        assert not cache_path_for(library_root, old_media, suffix).exists()
        new_side = cache_path_for(library_root, new_media, suffix)
        assert new_side.is_file()
        assert new_side.read_bytes() == b'{"k":1}'

    # Orphan prune at the new location keeps them (media is "expected").
    stats = prune_orphans(library_root, {new_media})
    assert stats.orphans_removed == 0


def test_remove_all_deletes_every_sidecar(tmp_path: Path) -> None:
    library_root = tmp_path / "lib"
    library_root.mkdir()
    media = tmp_path / "lib" / "gone.jpg"
    for suffix in LIVE_SUFFIXES:
        _make_cache_file(library_root, media, suffix)

    remove_all(library_root, media)

    for suffix in LIVE_SUFFIXES:
        assert not cache_path_for(library_root, media, suffix).exists()


def test_relocate_all_best_effort_when_sidecars_absent(tmp_path: Path) -> None:
    """Relocating a file that was never cached is a silent no-op."""
    library_root = tmp_path / "lib"
    library_root.mkdir()
    relocate_all(
        library_root,
        tmp_path / "lib" / "a.jpg",
        tmp_path / "lib" / "b.jpg",
    )  # no raise
