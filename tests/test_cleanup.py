from __future__ import annotations

from pathlib import Path

from pix.cleanup import cleanup_rename_orphans


def test_cleanup_reverts_orphan_intermediate(tmp_path: Path) -> None:
    """An orphan `.__pixrename__` from a crashed rename is reverted to original."""
    src = tmp_path / "src"
    src.mkdir()
    orphan = src / "FOO.JPG.__pixrename__"
    orphan.write_bytes(b"img")

    resolved = cleanup_rename_orphans(src)

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

    cleanup_rename_orphans(src)

    assert original.exists()
    assert original.read_bytes() == b"original"
    assert not orphan.exists()


def test_cleanup_returns_empty_when_no_orphans(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "normal.jpg").write_bytes(b"")
    assert cleanup_rename_orphans(src) == []


def test_cleanup_skips_pix_state_directory(tmp_path: Path) -> None:
    """Same `.pix/` skip semantics as walk_source_files."""
    src = tmp_path / "src"
    src.mkdir()
    (src / ".pix").mkdir()
    (src / ".pix" / "weird.__pixrename__").write_bytes(b"")

    resolved = cleanup_rename_orphans(src)
    assert resolved == []
    # The pix-state file is untouched.
    assert (src / ".pix" / "weird.__pixrename__").exists()


def test_cleanup_handles_nested_subdirectories(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "deep" / "nested").mkdir(parents=True)
    orphan = src / "deep" / "nested" / "FILE.JPG.__pixrename__"
    orphan.write_bytes(b"img")

    cleanup_rename_orphans(src)
    assert not orphan.exists()
    assert (src / "deep" / "nested" / "FILE.JPG").exists()
