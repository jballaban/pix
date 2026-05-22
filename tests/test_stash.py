"""Tests for `pix.stash` — sidecar round-trip, index, stash_file."""

from __future__ import annotations

from pathlib import Path

from pix.stash import (
    SIDECAR_SUFFIX,
    StashSidecar,
    load_stash_index,
    read_sidecar,
    sidecar_path_for,
    stash_file,
    write_sidecar,
)


# --- Sidecar serialization --------------------------------------------------


def test_sidecar_round_trip_minimal() -> None:
    s = StashSidecar(hash="abc123", origins=["F:/a/b.dng"])
    text = s.to_yaml()
    parsed = StashSidecar.from_yaml(text)
    assert parsed.hash == "abc123"
    assert parsed.origins == ["F:/a/b.dng"]
    assert parsed.original_filename is None


def test_sidecar_round_trip_with_original_filename() -> None:
    s = StashSidecar(
        hash="abc",
        origins=["F:/a/IMG_001.dng"],
        original_filename="IMG_001.dng",
    )
    parsed = StashSidecar.from_yaml(s.to_yaml())
    assert parsed.original_filename == "IMG_001.dng"


def test_sidecar_yaml_omits_original_filename_when_none() -> None:
    """We only store the field when the stash filename was modified."""
    s = StashSidecar(hash="abc", origins=["F:/a.dng"])
    text = s.to_yaml()
    assert "original_filename" not in text


def test_write_and_read_sidecar(tmp_path: Path) -> None:
    stash_file_path = tmp_path / "IMG_001.dng"
    stash_file_path.write_bytes(b"")
    sidecar = StashSidecar(hash="h", origins=["F:/src.dng"])
    write_sidecar(stash_file_path, sidecar)
    assert sidecar_path_for(stash_file_path).exists()
    loaded = read_sidecar(stash_file_path)
    assert loaded is not None
    assert loaded.hash == "h"


def test_read_sidecar_returns_none_when_missing(tmp_path: Path) -> None:
    assert read_sidecar(tmp_path / "nonexistent.dng") is None


def test_read_sidecar_returns_none_on_malformed_yaml(tmp_path: Path) -> None:
    stash_file_path = tmp_path / "IMG_001.dng"
    stash_file_path.write_bytes(b"")
    sidecar_path_for(stash_file_path).write_text(
        "not: valid: yaml: at: all", encoding="utf-8"
    )
    assert read_sidecar(stash_file_path) is None


# --- Index loading ----------------------------------------------------------


def test_load_index_empty_when_dir_missing(tmp_path: Path) -> None:
    assert load_stash_index(tmp_path / "no-such-dir") == {}


def test_load_index_builds_hash_to_path_map(tmp_path: Path) -> None:
    stash_dir = tmp_path / "stash"
    stash_dir.mkdir()
    a = stash_dir / "a.dng"
    b = stash_dir / "b.dng"
    a.write_bytes(b"")
    b.write_bytes(b"")
    write_sidecar(a, StashSidecar(hash="hash-a", origins=["F:/a.dng"]))
    write_sidecar(b, StashSidecar(hash="hash-b", origins=["F:/b.dng"]))
    index = load_stash_index(stash_dir)
    assert index == {"hash-a": a, "hash-b": b}


def test_load_index_skips_orphan_sidecars(tmp_path: Path) -> None:
    """A sidecar without a matching stash file is ignored."""
    stash_dir = tmp_path / "stash"
    stash_dir.mkdir()
    # Sidecar with no corresponding stash file.
    (stash_dir / f"missing.dng{SIDECAR_SUFFIX}").write_text(
        "hash: h\norigins: []\n", encoding="utf-8"
    )
    assert load_stash_index(stash_dir) == {}


# --- stash_file (new entry) -------------------------------------------------


def _make_source(tmp_path: Path, name: str, content: bytes) -> Path:
    src_dir = tmp_path / "src"
    src_dir.mkdir(exist_ok=True)
    p = src_dir / name
    p.write_bytes(content)
    return p


def test_stash_file_new_entry_moves_source_and_writes_sidecar(
    tmp_path: Path,
) -> None:
    source = _make_source(tmp_path, "IMG_001.dng", b"raw-bytes-1")
    stash_dir = tmp_path / "stash"
    capture = tmp_path / "runs" / "r" / "data" / "L001_IMG_001.dng"
    index: dict[str, Path] = {}

    was_dup, final = stash_file(
        source=source,
        stash_dir=stash_dir,
        index=index,
        dup_capture_path=capture,
    )

    assert was_dup is False
    assert final == stash_dir / "IMG_001.dng"
    assert not source.exists()
    assert final.exists()
    assert final.read_bytes() == b"raw-bytes-1"
    # Sidecar exists and records origin.
    info = read_sidecar(final)
    assert info is not None
    assert info.origins == [str(source)]
    assert info.original_filename is None
    # Index is updated.
    assert info.hash in index
    assert index[info.hash] == final


# --- stash_file (dup) -------------------------------------------------------


def test_stash_file_dup_routes_source_to_capture(tmp_path: Path) -> None:
    """Two source files with identical content: the second is captured."""
    first = _make_source(tmp_path, "first.dng", b"same-bytes")
    stash_dir = tmp_path / "stash"
    cap1 = tmp_path / "runs" / "r" / "data" / "L001_first.dng"
    index: dict[str, Path] = {}

    stash_file(
        source=first, stash_dir=stash_dir, index=index, dup_capture_path=cap1
    )
    # Now a second source with the same content.
    second = _make_source(tmp_path, "second.dng", b"same-bytes")
    cap2 = tmp_path / "runs" / "r" / "data" / "L002_second.dng"
    was_dup, final = stash_file(
        source=second, stash_dir=stash_dir, index=index, dup_capture_path=cap2
    )

    assert was_dup is True
    assert final == stash_dir / "first.dng"  # the keeper stays
    # Second source moved to dup-capture, not to stash.
    assert not second.exists()
    assert cap2.exists()
    assert cap2.read_bytes() == b"same-bytes"
    # Sidecar gained the new origin.
    info = read_sidecar(stash_dir / "first.dng")
    assert info is not None
    assert info.origins == [str(first), str(second)]


# --- stash_file (filename collision) ----------------------------------------


def test_stash_file_filename_collision_suffixes(tmp_path: Path) -> None:
    """Different content, same source filename → suffix the new entry."""
    a = _make_source(tmp_path, "IMG_001.dng", b"content-A")
    stash_dir = tmp_path / "stash"
    index: dict[str, Path] = {}
    stash_file(
        source=a,
        stash_dir=stash_dir,
        index=index,
        dup_capture_path=tmp_path / "runs" / "data" / "L001_IMG_001.dng",
    )

    # A different source folder happens to have IMG_001.dng with
    # different content. The bytes differ, so no dedup.
    src2 = tmp_path / "src2"
    src2.mkdir()
    b = src2 / "IMG_001.dng"
    b.write_bytes(b"content-B")

    was_dup, final = stash_file(
        source=b,
        stash_dir=stash_dir,
        index=index,
        dup_capture_path=tmp_path / "runs" / "data" / "L002_IMG_001.dng",
    )

    assert was_dup is False
    assert final.name == "IMG_001_001.dng"
    # Original filename recorded.
    info = read_sidecar(final)
    assert info is not None
    assert info.original_filename == "IMG_001.dng"


# --- stash_file (corrupt sidecar self-heal) ---------------------------------


def test_stash_file_dup_with_corrupt_existing_sidecar(
    tmp_path: Path,
) -> None:
    """When the existing sidecar is corrupt, dup still works: we rebuild
    a minimal sidecar from the dup's hash and origin."""
    stash_dir = tmp_path / "stash"
    stash_dir.mkdir()
    existing = stash_dir / "a.dng"
    existing.write_bytes(b"X")
    # Plant a corrupt sidecar.
    sidecar_path_for(existing).write_text(
        "garbage{{{{", encoding="utf-8"
    )
    # Index says hash maps to this existing file.
    index = {"hash-X": existing}

    # Source with the same hash (we pre-seeded index, but the
    # implementation should still re-hash the source and find the match).
    source = _make_source(tmp_path, "src.dng", b"X")
    cap = tmp_path / "runs" / "r" / "data" / "L001_src.dng"
    # The actual hash of b"X" won't equal "hash-X" — so this test would
    # falsely take the new-entry branch. Use the real hash for the seed:
    from pix.content_hash import compute_content_hash

    real_hash = compute_content_hash(existing)
    index.clear()
    index[real_hash] = existing

    was_dup, final = stash_file(
        source=source, stash_dir=stash_dir, index=index, dup_capture_path=cap
    )
    assert was_dup is True
    assert final == existing
    info = read_sidecar(existing)
    assert info is not None
    assert str(source) in info.origins
