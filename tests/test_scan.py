from __future__ import annotations

from pathlib import Path

from pix.scan import walk_source_files


def test_walk_skips_pix_state_directory(tmp_path: Path) -> None:
    """Migrate must never iterate `.pix/` even when run from inside a library root."""
    root = tmp_path / "library"
    root.mkdir()

    # The library's own state directory.
    pix_dir = root / ".pix"
    (pix_dir / "runs" / "2026-05-18_18-00-00").mkdir(parents=True)
    (pix_dir / "runs" / "2026-05-18_18-00-00" / "plan.txt").write_text("x")
    (pix_dir / "staging").mkdir()
    (pix_dir / "staging" / "scratch.tmp").write_text("x")
    (pix_dir / "pix.yaml").write_text("")

    # Real source files that should be picked up.
    (root / "photo.jpg").write_bytes(b"")
    (root / "trip").mkdir()
    (root / "trip" / "another.jpg").write_bytes(b"")

    files = walk_source_files(root)

    found = {p.relative_to(root.resolve()).as_posix() for p, _, _ in files}
    assert found == {"photo.jpg", "trip/another.jpg"}


def test_walk_skips_nested_pix_dirs(tmp_path: Path) -> None:
    """A `.pix/` at any depth is skipped — even if the user has stray state."""
    root = tmp_path / "src"
    root.mkdir()
    (root / "good.jpg").write_bytes(b"")
    (root / "subdir" / ".pix").mkdir(parents=True)
    (root / "subdir" / ".pix" / "weird.txt").write_text("x")
    (root / "subdir" / "alsogood.jpg").write_bytes(b"")

    files = walk_source_files(root)
    found = {p.relative_to(root.resolve()).as_posix() for p, _, _ in files}
    assert found == {"good.jpg", "subdir/alsogood.jpg"}


def test_walk_returns_file_sizes(tmp_path: Path) -> None:
    """Each entry pairs the absolute path with its size and mtime_ns."""
    root = tmp_path / "src"
    root.mkdir()
    (root / "small.jpg").write_bytes(b"abc")  # 3 bytes
    (root / "bigger.jpg").write_bytes(b"x" * 1024)  # 1024 bytes

    entries = {p.name: (sz, mt) for p, sz, mt in walk_source_files(root)}
    assert {n: sz for n, (sz, _) in entries.items()} == {
        "small.jpg": 3,
        "bigger.jpg": 1024,
    }
    # mtime_ns is populated and non-zero for real files.
    assert all(mt > 0 for _, mt in entries.values())
