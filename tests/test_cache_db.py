"""Tests for the SQLite cache store (`pix.cache_db`)."""

from __future__ import annotations

import json
from pathlib import Path

from pix import cache_base, cache_db


def _stamp() -> tuple[int, int]:
    return 100, 200  # (size, mtime_ns)


def test_put_and_get_each_column(tmp_path: Path) -> None:
    p = tmp_path / "a.jpg"
    size, mtime_ns = _stamp()
    cache_db.put_meta(tmp_path, p, {"k": "v"}, size=size, mtime_ns=mtime_ns)
    cache_db.put_hash(tmp_path, p, "abc123", size=size, mtime_ns=mtime_ns)
    cache_db.put_vfp(tmp_path, p, {"frames": [1, 2]}, size=size, mtime_ns=mtime_ns)

    row = cache_db.get(tmp_path, p)
    assert row is not None
    assert row.size == size and row.mtime_ns == mtime_ns
    assert row.meta == {"k": "v"}
    assert row.hash == "abc123"
    assert row.vfp == {"frames": [1, 2]}


def test_get_missing_returns_none(tmp_path: Path) -> None:
    assert cache_db.get(tmp_path, tmp_path / "nope.jpg") is None


def test_new_stamp_replaces_row_and_nulls_other_columns(tmp_path: Path) -> None:
    """Writing one column at a *new* stamp means the file changed — the other
    columns were computed against the old stamp and must be dropped."""
    p = tmp_path / "a.jpg"
    cache_db.put_meta(tmp_path, p, {"k": "v"}, size=1, mtime_ns=1)
    cache_db.put_hash(tmp_path, p, "h", size=1, mtime_ns=1)
    # File changed → new stamp; write only hash.
    cache_db.put_hash(tmp_path, p, "h2", size=2, mtime_ns=2)

    row = cache_db.get(tmp_path, p)
    assert row is not None
    assert (row.size, row.mtime_ns) == (2, 2)
    assert row.hash == "h2"
    assert row.meta is None  # dropped — was stale
    assert row.vfp is None


def test_same_stamp_updates_only_target_column(tmp_path: Path) -> None:
    p = tmp_path / "a.jpg"
    cache_db.put_meta(tmp_path, p, {"k": "v"}, size=1, mtime_ns=1)
    cache_db.put_hash(tmp_path, p, "h", size=1, mtime_ns=1)
    row = cache_db.get(tmp_path, p)
    assert row is not None and row.meta == {"k": "v"} and row.hash == "h"


def test_note_inplace_carries_hash_and_vfp_forward(tmp_path: Path) -> None:
    """A metadata-only write re-stamps and merges meta, but preserves the
    content hash + fingerprint (content unchanged)."""
    p = tmp_path / "a.jpg"
    cache_db.put_meta(tmp_path, p, {"XMP:EventAuto": "1"}, size=1, mtime_ns=1)
    cache_db.put_hash(tmp_path, p, "keepme", size=1, mtime_ns=1)
    cache_db.put_vfp(tmp_path, p, {"frames": [9]}, size=1, mtime_ns=1)

    cache_db.note_inplace_metadata_change(
        tmp_path, p, meta_updates={"XMP:EventOverride": "2"}, size=5, mtime_ns=5
    )

    row = cache_db.get(tmp_path, p)
    assert row is not None
    assert (row.size, row.mtime_ns) == (5, 5)
    assert row.meta == {"XMP:EventAuto": "1", "XMP:EventOverride": "2"}
    assert row.hash == "keepme"  # carried forward
    assert row.vfp == {"frames": [9]}  # carried forward


def test_note_inplace_noop_without_row(tmp_path: Path) -> None:
    p = tmp_path / "a.jpg"
    cache_db.note_inplace_metadata_change(
        tmp_path, p, meta_updates={"b": "2"}, size=5, mtime_ns=5
    )
    assert cache_db.get(tmp_path, p) is None


def test_relocate_moves_row(tmp_path: Path) -> None:
    old = tmp_path / "old.jpg"
    new = tmp_path / "sub" / "new.jpg"
    cache_db.put_hash(tmp_path, old, "h", size=1, mtime_ns=1)
    cache_db.relocate(tmp_path, old, new)
    assert cache_db.get(tmp_path, old) is None
    row = cache_db.get(tmp_path, new)
    assert row is not None and row.hash == "h"


def test_relocate_overwrites_stale_target_row(tmp_path: Path) -> None:
    old = tmp_path / "old.jpg"
    new = tmp_path / "new.jpg"
    cache_db.put_hash(tmp_path, old, "fromold", size=1, mtime_ns=1)
    cache_db.put_hash(tmp_path, new, "stale", size=9, mtime_ns=9)
    cache_db.relocate(tmp_path, old, new)
    row = cache_db.get(tmp_path, new)
    assert row is not None and row.hash == "fromold"


def test_remove_deletes_row(tmp_path: Path) -> None:
    p = tmp_path / "a.jpg"
    cache_db.put_hash(tmp_path, p, "h", size=1, mtime_ns=1)
    cache_db.remove(tmp_path, p)
    assert cache_db.get(tmp_path, p) is None


def test_prune_drops_unexpected_rows(tmp_path: Path) -> None:
    keep = tmp_path / "keep.jpg"
    gone = tmp_path / "gone.jpg"
    cache_db.put_hash(tmp_path, keep, "h", size=1, mtime_ns=1)
    cache_db.put_hash(tmp_path, gone, "h", size=1, mtime_ns=1)
    stats = cache_db.prune(tmp_path, {keep})
    assert stats.orphans_removed == 1
    assert cache_db.get(tmp_path, keep) is not None
    assert cache_db.get(tmp_path, gone) is None


def test_prune_respects_allowed_prefix(tmp_path: Path) -> None:
    inside = tmp_path / "sub_a" / "gone.jpg"
    outside = tmp_path / "sub_b" / "gone.jpg"
    cache_db.put_hash(tmp_path, inside, "h", size=1, mtime_ns=1)
    cache_db.put_hash(tmp_path, outside, "h", size=1, mtime_ns=1)
    stats = cache_db.prune(
        tmp_path, expected_paths=set(), allowed_prefix=tmp_path / "sub_a"
    )
    assert stats.orphans_removed == 1
    assert cache_db.get(tmp_path, inside) is None
    assert cache_db.get(tmp_path, outside) is not None  # out of scope, kept


def test_load_all_and_iter_meta(tmp_path: Path) -> None:
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    cache_db.put_meta(tmp_path, a, {"event": "Hawaii"}, size=1, mtime_ns=1)
    cache_db.put_hash(tmp_path, b, "h", size=1, mtime_ns=1)  # no meta

    rows = cache_db.load_all(tmp_path)
    assert set(rows) == {a, b}

    metas = dict(cache_db.iter_meta(tmp_path))
    assert metas == {a: {"event": "Hawaii"}}  # b has no meta, skipped


def _write_old_sidecar(
    root: Path, media: Path, suffix: str, payload: dict[str, object]
) -> None:
    side = cache_base.cache_path_for(root, media, suffix)
    side.parent.mkdir(parents=True, exist_ok=True)
    side.write_text(json.dumps(payload), encoding="utf-8")


def test_one_time_import_folds_valid_sidecars_and_reaps_tree(
    tmp_path: Path,
) -> None:
    root = tmp_path / "lib"
    media = root / "2023" / "clip.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"hello-bytes")
    st = media.stat()

    # Valid legacy sidecars (meta validates on size; hash/vfp on size+mtime).
    _write_old_sidecar(
        root, media, ".meta",
        {"v": 1, "size": st.st_size, "metadata": {"XMP:EventAuto": "Trip"}},
    )
    _write_old_sidecar(
        root, media, ".hash",
        {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "hash": "abc", "computed_at": "x"},
    )
    # Stale hash for a vanished file → not imported.
    gone = root / "gone.jpg"
    _write_old_sidecar(
        root, gone, ".hash", {"size": 1, "mtime_ns": 1, "hash": "dead"}
    )

    # First touch triggers the import.
    row = cache_db.get(root, media)
    assert row is not None
    assert row.meta == {"XMP:EventAuto": "Trip"}
    assert row.hash == "abc"
    assert (row.size, row.mtime_ns) == (st.st_size, st.st_mtime_ns)

    # Vanished file's sidecar wasn't carried over.
    assert cache_db.get(root, gone) is None
    # Legacy tree reaped; flag set so it doesn't run again.
    assert not cache_base.cache_root_for(root).exists()
    assert cache_db.db_path(root).exists()
