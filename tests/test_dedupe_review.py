"""Tests for the dedupe checkout/commit review folder (pix.dedupe_review)."""

from __future__ import annotations

from pathlib import Path

from pix.dedupe import DedupeGroup
from pix.dedupe_review import (
    MANIFEST_NAME,
    clear_review_artifacts,
    montage_name,
    read_manifest,
    surviving_member_sets,
    write_manifest,
)


def _group(root: Path, keeper: str, losers: list[str], dist: int) -> DedupeGroup:
    return DedupeGroup(
        content_hash="",
        keeper=root / keeper,
        losers=tuple(root / x for x in losers),
        kind="perceptual",
        distance=dist,
    )


def test_manifest_roundtrip_and_readme(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    review = tmp_path / "review"
    g1 = _group(root, "a.mp4", ["b.mp4"], 5)
    g2 = _group(root, "c.mp4", ["d.mp4", "e.mp4"], 18)
    write_manifest(review, root, [("g0001", g1), ("g0002", g2)], 0, 30)

    assert (review / MANIFEST_NAME).is_file()
    assert (review / "_README.txt").is_file()
    m = read_manifest(review)
    assert m["library_root"] == str(root)
    assert m["min_distance"] == 0 and m["max_distance"] == 30
    assert [g["id"] for g in m["groups"]] == ["g0001", "g0002"]
    assert m["groups"][0]["keeper"] == "a.mp4"
    assert set(m["groups"][1]["members"]) == {"c.mp4", "d.mp4", "e.mp4"}


def test_surviving_reflects_montage_presence(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    review = tmp_path / "review"
    g1 = _group(root, "a.mp4", ["b.mp4"], 5)
    g2 = _group(root, "c.mp4", ["d.mp4"], 18)
    write_manifest(review, root, [("g0001", g1), ("g0002", g2)], 0, 30)

    # No montages rendered yet → nothing survives.
    assert surviving_member_sets(review) == []

    # Render both (simulate by touching the montage files).
    (review / montage_name("g0001", 5)).write_bytes(b"x")
    (review / montage_name("g0002", 18)).write_bytes(b"x")
    survivors = surviving_member_sets(review)
    assert frozenset({"a.mp4", "b.mp4"}) in survivors
    assert frozenset({"c.mp4", "d.mp4"}) in survivors

    # Delete g0002's montage → that group is no longer selected.
    (review / montage_name("g0002", 18)).unlink()
    survivors = surviving_member_sets(review)
    assert survivors == [frozenset({"a.mp4", "b.mp4"})]


def test_clear_review_artifacts_scoped(tmp_path: Path) -> None:
    review = tmp_path / "review"
    review.mkdir()
    (review / MANIFEST_NAME).write_text("{}", encoding="utf-8")
    (review / "_README.txt").write_text("x", encoding="utf-8")
    (review / montage_name("g0001", 5)).write_bytes(b"m")
    (review / montage_name("g0042", 130)).write_bytes(b"m")
    # unrelated files the user might have in the folder — must be kept
    (review / "notes.txt").write_text("keep", encoding="utf-8")
    (review / "vacation.jpg").write_bytes(b"keep")

    clear_review_artifacts(review)

    names = {p.name for p in review.iterdir()}
    assert names == {"notes.txt", "vacation.jpg"}
