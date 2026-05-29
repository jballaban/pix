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
    compute_pix_writes,
    create_checkout,
    diff_workspace,
    discard,
    ensure_no_open_checkout,
    file_id,
    is_open,
    parse_checkout_path,
    read_snapshot,
    template_token_names,
    write_snapshot,
)
from pix.metadata import FileMetadata
from pix.organize import parse_template
from pix.plan import (
    PIX_DATE_AUTO,
    PIX_DATE_AUTO_PREVIOUS,
    PIX_DATE_OVERRIDE,
    PIX_EVENT_AUTO,
    PIX_EVENT_AUTO_PREVIOUS,
    PIX_EVENT_OVERRIDE,
    PIX_ORIGINAL_PATH,
)


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


def _write_minimal_snapshot(root: Path) -> None:
    checkout_dir(root).mkdir(parents=True, exist_ok=True)
    write_snapshot(
        root,
        Snapshot(
            template="{year}", scope="x", created="2026-05-29T00:00:00", links=[]
        ),
    )


def test_ensure_no_open_checkout_raises_when_open(tmp_path: Path) -> None:
    _write_minimal_snapshot(tmp_path)  # open = snapshot present
    with pytest.raises(CheckoutOpen):
        ensure_no_open_checkout(tmp_path)


def test_empty_checkout_folder_is_not_open(tmp_path: Path) -> None:
    """A folder with no snapshot (e.g. left empty after a commit) is closed."""
    checkout_dir(tmp_path).mkdir(parents=True)
    assert not is_open(tmp_path)
    ensure_no_open_checkout(tmp_path)  # no raise


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


def test_no_event_in_event_first_template_rests_in_root(tmp_path: Path) -> None:
    """{event}/{year} with no event → file sits in the workspace root
    (we stop at the first missing value; no bucket, no year breakdown)."""
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
    assert (checkout_dir(root) / "2023-08-15_143205.jpg").exists()
    assert not (checkout_dir(root) / "(none)").exists()
    assert not (checkout_dir(root) / "2023").exists()


def test_checkout_template_must_be_single_bare_tokens() -> None:
    from pix.checkout import CheckoutError, validate_checkout_template

    with pytest.raises(CheckoutError):
        validate_checkout_template(parse_template("{year}-archive/{event}"))
    # A clean single-tag-per-level template passes.
    validate_checkout_template(parse_template("{year}/{event}"))


# --- discard -----------------------------------------------------------------


def test_discard_empties_but_keeps_folder(tmp_path: Path) -> None:
    cdir = checkout_dir(tmp_path)
    (cdir / "2023" / "Hawaii").mkdir(parents=True)
    (cdir / "2023" / "Hawaii" / "x.jpg").write_bytes(b"link")
    _write_minimal_snapshot(tmp_path)

    assert is_open(tmp_path)
    assert discard(tmp_path) is True  # was open
    assert not is_open(tmp_path)  # snapshot gone
    assert cdir.is_dir()  # folder persists
    assert list(cdir.iterdir()) == []  # but empty


def test_discard_no_op_when_absent(tmp_path: Path) -> None:
    assert discard(tmp_path) is False


# --- commit: path parsing ----------------------------------------------------


def test_parse_checkout_path_full() -> None:
    t = parse_template("{year}/{event}")
    assert parse_checkout_path(t, ("2023", "Hawaii")) == {
        "year": "2023",
        "event": "Hawaii",
    }


def test_parse_checkout_path_trailing_unset() -> None:
    t = parse_template("{year}/{event}")
    assert parse_checkout_path(t, ("2023",)) == {"year": "2023", "event": None}


def test_parse_checkout_path_root() -> None:
    t = parse_template("{event}/{year}")
    assert parse_checkout_path(t, ()) == {"event": None, "year": None}


# --- commit: override math ---------------------------------------------------


def test_writes_event_assign_new_value() -> None:
    meta = _meta(Path("/x.jpg"), **{PIX_EVENT_AUTO: "Hawaii"})
    writes, _ = compute_pix_writes({"event": "Birthday"}, meta)
    assert writes == {PIX_EVENT_OVERRIDE: "Birthday"}


def test_writes_event_assign_matching_auto_clears_override() -> None:
    meta = _meta(
        Path("/x.jpg"),
        **{PIX_EVENT_AUTO: "Hawaii", PIX_EVENT_OVERRIDE: "Party"},
    )
    writes, _ = compute_pix_writes({"event": "Hawaii"}, meta)
    assert writes == {PIX_EVENT_OVERRIDE: ""}  # cleared (D1)


def test_writes_event_clear_reconciles_autoprevious() -> None:
    meta = _meta(
        Path("/x.jpg"),
        **{
            PIX_EVENT_AUTO: "Hawaii",
            PIX_EVENT_OVERRIDE: "Party",
            PIX_EVENT_AUTO_PREVIOUS: "Beach",
        },
    )
    writes, _ = compute_pix_writes({"event": "Hawaii"}, meta)
    assert writes == {PIX_EVENT_OVERRIDE: "", PIX_EVENT_AUTO_PREVIOUS: ""}


def test_writes_year_assign_new_value() -> None:
    meta = _meta(Path("/x.jpg"), **{PIX_DATE_AUTO: "2023-08-15-14:32:05"})
    writes, _ = compute_pix_writes({"year": "2024"}, meta)
    assert writes == {PIX_DATE_OVERRIDE: "2024-*-*-*:*:*"}


def test_writes_year_matching_auto_deletes_lone_override() -> None:
    meta = _meta(
        Path("/x.jpg"),
        **{
            PIX_DATE_AUTO: "2023-08-15-14:32:05",
            PIX_DATE_OVERRIDE: "2020-*-*-*:*:*",
        },
    )
    # New year 2023 == auto year → clear that field → all-* → delete.
    writes, _ = compute_pix_writes({"year": "2023"}, meta)
    assert writes == {PIX_DATE_OVERRIDE: ""}


def test_writes_month_patches_existing_override() -> None:
    meta = _meta(
        Path("/x.jpg"),
        **{
            PIX_DATE_AUTO: "2023-08-15-14:32:05",
            PIX_DATE_OVERRIDE: "2020-*-*-*:*:*",
        },
    )
    writes, _ = compute_pix_writes({"month": "03"}, meta)
    assert writes == {PIX_DATE_OVERRIDE: "2020-03-*-*:*:*"}


def test_writes_year_on_undated_file() -> None:
    meta = _meta(Path("/x.jpg"))  # no DateAuto
    writes, _ = compute_pix_writes({"year": "2008"}, meta)
    assert writes == {PIX_DATE_OVERRIDE: "2008-*-*-*:*:*"}


def test_writes_year_and_event_bundle() -> None:
    meta = _meta(
        Path("/x.jpg"),
        **{PIX_DATE_AUTO: "2023-08-15-14:32:05", PIX_EVENT_AUTO: "Hawaii"},
    )
    writes, _ = compute_pix_writes(
        {"year": "2024", "event": "Birthday"}, meta
    )
    assert writes == {
        PIX_DATE_OVERRIDE: "2024-*-*-*:*:*",
        PIX_EVENT_OVERRIDE: "Birthday",
    }


# --- commit: workspace diff --------------------------------------------------


def _open_checkout(tmp_path: Path, template: str) -> Path:
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    a = (root / "2023-08-15_143205.jpg").resolve()
    a.write_bytes(b"a")
    cache = {
        a: _migrated(a, date="2023-08-15-14:32:05", event="Hawaii")
    }
    create_checkout(
        library_root=root,
        scope=root,
        template=parse_template(template),
        cache=cache,
    )
    return root


def test_diff_detects_year_assign(tmp_path: Path) -> None:
    root = _open_checkout(tmp_path, "{year}/{event}")
    cdir = checkout_dir(root)
    link = cdir / "2023" / "Hawaii" / "2023-08-15_143205.jpg"
    dest = cdir / "2024" / "Hawaii"
    dest.mkdir(parents=True)
    link.rename(dest / "2023-08-15_143205.jpg")  # move 2023 → 2024

    snap = read_snapshot(root)
    assert snap is not None
    diff = diff_workspace(root, parse_template("{year}/{event}"), snap)
    assert len(diff.assigns) == 1
    assert diff.assigns[0].token_changes == {"year": "2024"}
    assert diff.skipped_removals == []


def test_diff_unchanged_when_not_moved(tmp_path: Path) -> None:
    root = _open_checkout(tmp_path, "{year}/{event}")
    snap = read_snapshot(root)
    assert snap is not None
    diff = diff_workspace(root, parse_template("{year}/{event}"), snap)
    assert diff.assigns == []
    assert diff.unchanged == 1


def test_diff_flags_foreign_file(tmp_path: Path) -> None:
    root = _open_checkout(tmp_path, "{year}/{event}")
    (checkout_dir(root) / "stranger.jpg").write_bytes(b"new")  # not a link
    snap = read_snapshot(root)
    assert snap is not None
    diff = diff_workspace(root, parse_template("{year}/{event}"), snap)
    assert len(diff.foreign) == 1


def test_writes_date_clear_reconciles_autoprevious() -> None:
    meta = _meta(
        Path("/x.jpg"),
        **{
            PIX_DATE_AUTO: "2023-08-15-14:32:05",
            PIX_DATE_OVERRIDE: "2020-*-*-*:*:*",
            PIX_DATE_AUTO_PREVIOUS: "2019-01-01-00:00:00",
        },
    )
    # year 2023 == auto → clears the lone override → delete it + drop the
    # now-meaningless DateAutoPrevious dirty flag.
    writes, _ = compute_pix_writes({"year": "2023"}, meta)
    assert writes == {PIX_DATE_OVERRIDE: "", PIX_DATE_AUTO_PREVIOUS: ""}
