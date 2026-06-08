"""Unit tests for `pix.commands.migrate._post_apply_cache_update`.

The post-apply cache reflector has to mirror file mutations into the
persistent cache. Each plan action has its own cache mutation shape; the
trickiest is RENAME+TAG, where apply runs TAG then RENAME, so by the time
this function is called the file is at `target_path` with a *new* size
(the tag write added bytes). Calls keyed off `abs_path` will silently
fail to stat the file and leave the cache entry with a pre-tag size.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pix.commands.migrate import _post_apply_cache_update
from pix.metadata_cache import PerFileCache
from pix.plan import Action, Plan, PlanLine


def _line(
    *,
    line_id: str,
    action: Action,
    abs_path: Path,
    target_path: Path | None = None,
    pix_writes: dict[str, str] | None = None,
) -> PlanLine:
    return PlanLine(
        line_id=line_id,
        action=action,
        rel_path=abs_path.name,
        details="",
        abs_path=abs_path,
        target_path=target_path,
        pix_writes=pix_writes or {},
    )


def _make_plan(lines: list[PlanLine]) -> Plan:
    return Plan(
        source=Path("."),
        run_id="test",
        generated_at=datetime(2026, 1, 1),
        lines=lines,
    )


def test_rename_tag_updates_cache_at_target_path_with_post_tag_size(
    tmp_path: Path,
) -> None:
    """Regression: RENAME+TAG must leave the cache reflecting the
    file's *current* location and *current* size.

    Apply does TAG → RENAME, so by post-apply time the file is at
    target_path with a larger size. The cache must end up with an
    entry at the new path whose recorded size matches — otherwise the
    next migrate sees a size mismatch and re-reads via ExifTool.
    """
    library_root = tmp_path / "lib"
    library_root.mkdir()
    cache = PerFileCache.for_library(library_root)

    abs_path = library_root / "IMG_1234.jpg"
    target_path = library_root / "2023-08-15_143205.jpg"

    # Simulate the state mid-migrate, *before* apply:
    # the source file has been read by ExifTool and cached.
    abs_path.write_bytes(b"x" * 100)
    cache.add(abs_path, {"EXIF:DateTimeOriginal": "2023:08:15 14:32:05"})

    # Simulate apply: TAG adds bytes (file grows), then RENAME moves it.
    abs_path.write_bytes(b"x" * 110)  # tag write added 10 bytes
    abs_path.rename(target_path)

    line = _line(
        line_id="L001",
        action=Action.RENAME_TAG,
        abs_path=abs_path,
        target_path=target_path,
        pix_writes={"XMP:DateAuto": "2023-08-15-14:32:05"},
    )
    _post_apply_cache_update(cache, _make_plan([line]), {"L001"})

    # Next migrate would see target_path with size 110. The cache lookup
    # must hit.
    cached = cache.get(target_path, expected_size=110)
    assert cached is not None, "cache entry missing or size mismatch"
    assert cached.get("XMP:DateAuto") == "2023-08-15-14:32:05"
    assert cached.get("EXIF:DateTimeOriginal") == "2023:08:15 14:32:05"

    # And the entry at abs_path should be gone — the sidecar moved.
    assert cache.get(abs_path) is None


def test_pure_tag_updates_cache_in_place(tmp_path: Path) -> None:
    """TAG (no rename) updates the cache at abs_path with new size."""
    library_root = tmp_path / "lib"
    library_root.mkdir()
    cache = PerFileCache.for_library(library_root)

    f = library_root / "2023-08-15_143205.jpg"
    f.write_bytes(b"y" * 200)
    cache.add(f, {"XMP:OriginalPath": "Canon"})

    f.write_bytes(b"y" * 215)  # tag write grew file by 15 bytes

    line = _line(
        line_id="L001",
        action=Action.TAG,
        abs_path=f,
        pix_writes={"XMP:EventAuto": "birthday"},
    )
    _post_apply_cache_update(cache, _make_plan([line]), {"L001"})

    cached = cache.get(f, expected_size=215)
    assert cached is not None
    assert cached.get("XMP:EventAuto") == "birthday"
    assert cached.get("XMP:OriginalPath") == "Canon"


def test_pure_rename_moves_cache_intact(tmp_path: Path) -> None:
    """RENAME (no tag) moves the sidecar; size stays the same so the
    cache entry remains valid at the new path."""
    library_root = tmp_path / "lib"
    library_root.mkdir()
    cache = PerFileCache.for_library(library_root)

    abs_path = library_root / "DSC_0042.JPG"
    target_path = library_root / "2023-08-15_143205.jpg"
    abs_path.write_bytes(b"z" * 300)
    cache.add(abs_path, {"XMP:OriginalPath": "Nikon"})
    abs_path.rename(target_path)

    line = _line(
        line_id="L001",
        action=Action.RENAME,
        abs_path=abs_path,
        target_path=target_path,
    )
    _post_apply_cache_update(cache, _make_plan([line]), {"L001"})

    cached = cache.get(target_path, expected_size=300)
    assert cached is not None
    assert cached.get("XMP:OriginalPath") == "Nikon"
    assert cache.get(abs_path) is None


def test_delete_removes_cache_entry(tmp_path: Path) -> None:
    library_root = tmp_path / "lib"
    library_root.mkdir()
    cache = PerFileCache.for_library(library_root)

    f = library_root / "thumbs.db"
    f.write_bytes(b"")
    cache.add(f, {"File:FileName": "thumbs.db"})

    line = _line(line_id="L001", action=Action.DELETE, abs_path=f)
    _post_apply_cache_update(cache, _make_plan([line]), {"L001"})

    assert cache.get(f) is None


def test_skipped_lines_dont_mutate_cache(tmp_path: Path) -> None:
    """Lines not in kept_line_ids must not touch the cache."""
    library_root = tmp_path / "lib"
    library_root.mkdir()
    cache = PerFileCache.for_library(library_root)

    f = library_root / "keep_me.jpg"
    f.write_bytes(b"hello")
    cache.add(f, {"XMP:OriginalPath": "preserved"})

    line = _line(line_id="L001", action=Action.DELETE, abs_path=f)
    _post_apply_cache_update(cache, _make_plan([line]), set())  # nothing kept

    assert cache.get(f) is not None
