"""Tests for `pix.checkout` — freeze guard, snapshot, materialization."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pix.checkout import (
    CheckoutOpen,
    CheckoutUnmigratedError,
    Snapshot,
    SnapshotLink,
    checkout_dir,
    create_checkout,
    discard,
    ensure_no_open_checkout,
    file_id,
    is_open,
    read_snapshot,
    template_token_names,
    write_snapshot,
)
from pix.metadata import FileMetadata
from pix.organize import parse_template
from pix.plan import PIX_DATE_AUTO, PIX_EVENT_AUTO, PIX_ORIGINAL_PATH


def _meta(path: Path, **fields: object) -> FileMetadata:
    return FileMetadata(path=path, raw={"SourceFile": str(path), **fields})


def _migrated(path: Path, *, date: str, event: str) -> FileMetadata:
    return _meta(
        path,
        **{
            PIX_ORIGINAL_PATH: f"F:/source/{path.name}",
            PIX_DATE_AUTO: date,
            PIX_EVENT_AUTO: event,
        },
    )


# --- template token names ----------------------------------------------------


def test_template_token_names_in_order() -> None:
    t = parse_template("{year}/{month}/{event}")
    assert template_token_names(t) == ["year", "month", "event"]


def test_template_token_names_deduped() -> None:
    t = parse_template("{year}/{event}/{year}")
    assert template_token_names(t) == ["year", "event"]


def test_template_token_names_ignores_literals() -> None:
    t = parse_template("Photos/{year}-archive")
    assert template_token_names(t) == ["year"]


# --- file-id identity --------------------------------------------------------


def test_file_id_shared_by_hard_links(tmp_path: Path) -> None:
    src = tmp_path / "a.bin"
    src.write_bytes(b"data")
    link = tmp_path / "b.bin"
    os.link(src, link)
    assert file_id(src.stat()) == file_id(link.stat())


def test_file_id_differs_between_files(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    assert file_id(a.stat()) != file_id(b.stat())


# --- snapshot round-trip + tolerance -----------------------------------------


def test_snapshot_round_trip(tmp_path: Path) -> None:
    checkout_dir(tmp_path).mkdir(parents=True)
    snap = Snapshot(
        template="{year}/{event}",
        scope="F:/photos/2023",
        created="2026-05-28T15:00:00",
        links=[
            SnapshotLink(
                ino="0x1_0000000000000abc",
                library_path="F:/photos/2023/Hawaii/2023-08-15_143205.jpg",
                values={"year": "2023", "event": "Hawaii"},
            )
        ],
    )
    write_snapshot(tmp_path, snap)
    got = read_snapshot(tmp_path)
    assert got == snap


def test_read_snapshot_missing_returns_none(tmp_path: Path) -> None:
    assert read_snapshot(tmp_path) is None


def test_read_snapshot_corrupt_returns_none(tmp_path: Path) -> None:
    checkout_dir(tmp_path).mkdir(parents=True)
    (checkout_dir(tmp_path) / "snapshot.json").write_text(
        "{not valid json", encoding="utf-8"
    )
    assert read_snapshot(tmp_path) is None


def test_read_snapshot_missing_field_returns_none(tmp_path: Path) -> None:
    checkout_dir(tmp_path).mkdir(parents=True)
    (checkout_dir(tmp_path) / "snapshot.json").write_text(
        json.dumps({"template": "{year}", "links": []}), encoding="utf-8"
    )
    # 'scope' and 'created' absent → None, not a crash.
    assert read_snapshot(tmp_path) is None


# --- freeze guard ------------------------------------------------------------


def test_ensure_no_open_checkout_passes_when_absent(tmp_path: Path) -> None:
    ensure_no_open_checkout(tmp_path)  # no raise


def test_ensure_no_open_checkout_raises_when_open(tmp_path: Path) -> None:
    checkout_dir(tmp_path).mkdir(parents=True)
    with pytest.raises(CheckoutOpen):
        ensure_no_open_checkout(tmp_path)


def test_checkout_open_message_includes_template(tmp_path: Path) -> None:
    checkout_dir(tmp_path).mkdir(parents=True)
    write_snapshot(
        tmp_path,
        Snapshot(
            template="{year}/{event}",
            scope="F:/photos",
            created="2026-05-28T15:00:00",
            links=[],
        ),
    )
    with pytest.raises(CheckoutOpen, match=r"\{year\}/\{event\}"):
        ensure_no_open_checkout(tmp_path)


# --- materialization ---------------------------------------------------------


def test_create_checkout_links_and_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    a = root / "2023-08-15_143205.jpg"
    b = root / "2023-08-15_150000.jpg"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    cache = {
        a.resolve(): _migrated(a.resolve(), date="2023-08-15-14:32:05", event="Hawaii"),
        b.resolve(): _migrated(b.resolve(), date="2023-08-15-15:00:00", event="Hawaii"),
    }
    template = parse_template("{year}/{event}")

    count = create_checkout(
        library_root=root, scope=root, template=template, cache=cache
    )

    assert count == 2
    assert is_open(root)
    link_a = checkout_dir(root) / "2023" / "Hawaii" / "2023-08-15_143205.jpg"
    link_b = checkout_dir(root) / "2023" / "Hawaii" / "2023-08-15_150000.jpg"
    assert link_a.exists()
    assert link_b.exists()
    # Hard link, not a copy: same inode as the library file.
    assert file_id(link_a.stat()) == file_id(a.resolve().stat())
    snap = read_snapshot(root)
    assert snap is not None
    assert snap.template == "{year}/{event}"
    assert len(snap.links) == 2
    inos = {ln.ino for ln in snap.links}
    assert file_id(a.resolve().stat()) in inos
    by_path = {ln.library_path: ln for ln in snap.links}
    assert by_path[a.resolve().as_posix()].values == {
        "year": "2023",
        "event": "Hawaii",
    }


def test_create_checkout_suffixes_workspace_collisions(tmp_path: Path) -> None:
    """Two files rendering to the same folder + bare name get _NNN links."""
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    a = root / "a.jpg"
    b = root / "b.jpg"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    # Identical effective date + event ⇒ identical bare canonical name.
    cache = {
        a.resolve(): _migrated(a.resolve(), date="2023-08-15-14:32:05", event="Hawaii"),
        b.resolve(): _migrated(b.resolve(), date="2023-08-15-14:32:05", event="Hawaii"),
    }
    create_checkout(
        library_root=root,
        scope=root,
        template=parse_template("{event}"),
        cache=cache,
    )
    folder = checkout_dir(root) / "Hawaii"
    names = sorted(p.name for p in folder.iterdir())
    assert names == ["2023-08-15_143205.jpg", "2023-08-15_143205_001.jpg"]


def test_create_checkout_refuses_unmigrated(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    p = root / "x.jpg"
    p.write_bytes(b"")
    cache = {p.resolve(): _meta(p.resolve())}  # no OriginalPath
    with pytest.raises(CheckoutUnmigratedError):
        create_checkout(
            library_root=root,
            scope=root,
            template=parse_template("{year}"),
            cache=cache,
        )
    # Prereq fails before any workspace is built — library stays unfrozen.
    assert not is_open(root)


def test_trailing_null_rests_in_parent(tmp_path: Path) -> None:
    """{year}/{event} with no event → file sits in the year folder (no
    bucket), since the missing value is trailing."""
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    p = root / "2023-08-15_143205.jpg"
    p.write_bytes(b"")
    cache = {
        p.resolve(): _meta(
            p.resolve(),
            **{
                PIX_ORIGINAL_PATH: "F:/source/x.jpg",
                PIX_DATE_AUTO: "2023-08-15-14:32:05",
            },
        )
    }
    create_checkout(
        library_root=root,
        scope=root,
        template=parse_template("{year}/{event}"),
        cache=cache,
    )
    assert (checkout_dir(root) / "2023" / "2023-08-15_143205.jpg").exists()
    assert not (checkout_dir(root) / "2023" / "(none)").exists()


def test_non_trailing_null_uses_flat_none_bucket(tmp_path: Path) -> None:
    """{event}/{year} with no event → file sits flat in (none)/, with NO
    year breakdown beneath it (the missing value is non-trailing)."""
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    p = root / "2023-08-15_143205.jpg"
    p.write_bytes(b"")
    cache = {
        p.resolve(): _meta(
            p.resolve(),
            **{
                PIX_ORIGINAL_PATH: "F:/source/x.jpg",
                PIX_DATE_AUTO: "2023-08-15-14:32:05",
            },
        )
    }
    create_checkout(
        library_root=root,
        scope=root,
        template=parse_template("{event}/{year}"),
        cache=cache,
    )
    assert (checkout_dir(root) / "(none)" / "2023-08-15_143205.jpg").exists()
    # No year subfolder under (none).
    assert not (checkout_dir(root) / "(none)" / "2023").exists()


def test_checkout_template_must_be_single_bare_tokens() -> None:
    from pix.checkout import CheckoutError, validate_checkout_template

    with pytest.raises(CheckoutError):
        validate_checkout_template(parse_template("{year}-archive/{event}"))
    # A clean single-tag-per-level template passes.
    validate_checkout_template(parse_template("{year}/{event}"))


# --- discard -----------------------------------------------------------------


def test_discard_removes_workspace(tmp_path: Path) -> None:
    checkout_dir(tmp_path).mkdir(parents=True)
    assert discard(tmp_path) is True
    assert not is_open(tmp_path)


def test_discard_no_op_when_absent(tmp_path: Path) -> None:
    assert discard(tmp_path) is False
