from __future__ import annotations

from pathlib import Path

from pix.cleanup import (
    cleanup_exiftool_tmp,
    cleanup_rename_orphans,
    scan_cleanup_markers,
    wipe_staging,
)


def test_cleanup_reverts_orphan_intermediate(tmp_path: Path) -> None:
    """An orphan `.__pixrename__` from a crashed rename is reverted to original."""
    src = tmp_path / "src"
    src.mkdir()
    orphan = src / "FOO.JPG.__pixrename__"
    orphan.write_bytes(b"img")

    resolved = cleanup_rename_orphans(scan_cleanup_markers(src).rename_orphans)

    assert len(resolved) == 1
    assert not orphan.exists()
    assert (src / "FOO.JPG").exists()
    assert (src / "FOO.JPG").read_bytes() == b"img"


def test_cleanup_deletes_stale_intermediate_if_original_exists(
    tmp_path: Path,
) -> None:
    """If the original name is occupied, the orphan is stale — delete it."""
    src = tmp_path / "src"
    src.mkdir()
    original = src / "FOO.JPG"
    original.write_bytes(b"original")
    orphan = src / "FOO.JPG.__pixrename__"
    orphan.write_bytes(b"stale-orphan-bytes")

    cleanup_rename_orphans(scan_cleanup_markers(src).rename_orphans)

    assert original.exists()
    assert original.read_bytes() == b"original"
    assert not orphan.exists()


def test_cleanup_returns_empty_when_no_orphans(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "normal.jpg").write_bytes(b"")
    assert cleanup_rename_orphans(scan_cleanup_markers(src).rename_orphans) == []


def test_cleanup_skips_pix_state_directory(tmp_path: Path) -> None:
    """Same `.pix/` skip semantics as walk_source_files."""
    src = tmp_path / "src"
    src.mkdir()
    (src / ".pix").mkdir()
    (src / ".pix" / "weird.__pixrename__").write_bytes(b"")

    resolved = cleanup_rename_orphans(scan_cleanup_markers(src).rename_orphans)
    assert resolved == []
    # The pix-state file is untouched.
    assert (src / ".pix" / "weird.__pixrename__").exists()


def test_cleanup_handles_nested_subdirectories(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "deep" / "nested").mkdir(parents=True)
    orphan = src / "deep" / "nested" / "FILE.JPG.__pixrename__"
    orphan.write_bytes(b"img")

    cleanup_rename_orphans(scan_cleanup_markers(src).rename_orphans)
    assert not orphan.exists()
    assert (src / "deep" / "nested" / "FILE.JPG").exists()


def test_cleanup_exiftool_tmp_deletes_leftovers(tmp_path: Path) -> None:
    """A `*_exiftool_tmp` orphan from an interrupted TAG write is deleted."""
    src = tmp_path / "src"
    src.mkdir()
    original = src / "photo.jpg"
    original.write_bytes(b"img")
    tmp = src / "photo.jpg_exiftool_tmp"
    tmp.write_bytes(b"half-written-tmp")

    deleted = cleanup_exiftool_tmp(scan_cleanup_markers(src).exiftool_tmps)

    assert len(deleted) == 1
    assert not tmp.exists()
    # Original is untouched (per ExifTool's atomic-write protocol).
    assert original.exists()
    assert original.read_bytes() == b"img"


def test_cleanup_exiftool_tmp_skips_pix_dir(tmp_path: Path) -> None:
    """Files under `.pix/` aren't part of the source-folder sweep."""
    src = tmp_path / "src"
    src.mkdir()
    (src / ".pix").mkdir()
    in_pix = src / ".pix" / "weird.jpg_exiftool_tmp"
    in_pix.write_bytes(b"")

    assert cleanup_exiftool_tmp(scan_cleanup_markers(src).exiftool_tmps) == []
    assert in_pix.exists()


def test_cleanup_exiftool_tmp_nested(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "a" / "b").mkdir(parents=True)
    tmp = src / "a" / "b" / "deep.jpg_exiftool_tmp"
    tmp.write_bytes(b"")

    deleted = cleanup_exiftool_tmp(scan_cleanup_markers(src).exiftool_tmps)
    assert deleted == [tmp]
    assert not tmp.exists()


def test_cleanup_exiftool_tmp_returns_empty_when_none(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "photo.jpg").write_bytes(b"")
    assert cleanup_exiftool_tmp(scan_cleanup_markers(src).exiftool_tmps) == []


def test_scan_classifies_all_kinds_and_skips_pix(tmp_path: Path) -> None:
    """One walk buckets the three marker kinds and ignores `.pix/` + normals."""
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / ".pix").mkdir()
    rename = src / "FOO.JPG.__pixrename__"
    marker = src / "sub" / "bar.jpg.__migrate__.mp4"
    tmp = src / "baz.jpg_exiftool_tmp"
    for p in (rename, marker, tmp):
        p.write_bytes(b"")
    (src / "normal.jpg").write_bytes(b"")  # not a marker
    (src / ".pix" / "x.jpg_exiftool_tmp").write_bytes(b"")  # skipped

    found = scan_cleanup_markers(src)
    assert found.rename_orphans == [rename]
    assert found.migrate_markers == [marker]
    assert found.exiftool_tmps == [tmp]


def test_wipe_staging_removes_contents(tmp_path: Path) -> None:
    """Wipe deletes everything inside staging, leaves the directory itself."""
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "leftover.jpg").write_bytes(b"data")
    (staging / "subdir").mkdir()
    (staging / "subdir" / "deep.tmp").write_bytes(b"more")

    count = wipe_staging(staging)

    assert count == 2  # one file + one subdir at top level
    assert staging.exists()
    assert list(staging.iterdir()) == []


def test_wipe_staging_no_op_when_absent(tmp_path: Path) -> None:
    staging = tmp_path / "staging-does-not-exist"
    assert wipe_staging(staging) == 0


def test_wipe_staging_no_op_when_empty(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    assert wipe_staging(staging) == 0
    assert staging.exists()
