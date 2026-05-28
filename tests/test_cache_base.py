"""Tests for `pix.cache_base` — shared cache plumbing.

The path-mirror, atomic-write, and parallel-read helpers are exercised
indirectly by the per-cache test files (test_metadata_cache,
test_hash_cache, test_video_cache). This module focuses on what's
unique to cache_base: orphan pruning and legacy-suffix sweeping.
"""

from __future__ import annotations

from pathlib import Path

from pix.cache_base import (
    PruneStats,
    cache_path_for,
    cache_root_for,
    prune_orphans,
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

    meta = _make_cache_file(library_root, gone, ".meta")
    hash_ = _make_cache_file(library_root, gone, ".hash")
    video = _make_cache_file(library_root, gone, ".video")

    stats = prune_orphans(library_root, expected_paths=set())
    assert stats.orphans_removed == 3
    assert stats.legacy_removed == 0
    assert not meta.exists()
    assert not hash_.exists()
    assert not video.exists()


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
