"""Tests for `pix tag set` / `pix tag clear` — tag-override writes on specific files.

The exiftool-backed metadata read and the apply are monkeypatched so these
stay fast and deterministic; they exercise validation, library scoping,
no-op detection, and plan construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

import pix.commands.set as set_mod
from pix.checkout import CheckoutOpen
from pix.commands.set import set_override
from pix.events import EVENT_NULL, PIX_EVENT_AUTO
from pix.library_lock import LockHeld
from pix.metadata import FileMetadata
from pix.plan import PIX_EVENT_OVERRIDE, Plan, PlanLine

_ApplyResult = tuple[int, list[tuple[PlanLine, str]]]


def _no_metas(paths: list[Path]) -> dict[Path, FileMetadata]:
    return {}


def _metas(**by_path: FileMetadata):
    def reader(paths: list[Path]) -> dict[Path, FileMetadata]:
        return {Path(k): v for k, v in by_path.items()}

    return reader


def _lib(tmp_path: Path) -> Path:
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    return root


def _patch_apply(monkeypatch: pytest.MonkeyPatch, seen: list[Plan], n: int) -> None:
    def fake_apply(*, plan: Plan, plan_path: Path, run_dir: Path,
                   kept_line_ids: set[str],
                   library_root: Path | None = None) -> _ApplyResult:
        seen.append(plan)
        return (n, [])

    monkeypatch.setattr(set_mod, "apply_plan", fake_apply)


def test_set_event_writes_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _lib(tmp_path)
    f = root / "2023-08-15_143205.jpg"
    f.write_bytes(b"x")
    seen: list[Plan] = []
    _patch_apply(monkeypatch, seen, 1)
    monkeypatch.setattr(set_mod, "read_metadata_batched", _no_metas)

    set_override(tag="event", value="Hawaii", paths=[f], no_prompt=True)

    assert len(seen) == 1
    line = seen[0].lines[0]
    assert line.pix_writes == {PIX_EVENT_OVERRIDE: "Hawaii"}
    assert line.abs_path == f


def test_set_event_noop_when_already_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file already at the target value is skipped — apply isn't called."""
    root = _lib(tmp_path)
    f = root / "x.jpg"
    f.write_bytes(b"x")
    seen: list[Plan] = []
    _patch_apply(monkeypatch, seen, 0)
    monkeypatch.setattr(
        set_mod, "read_metadata_batched",
        _metas(**{str(f): FileMetadata(
            path=f, raw={"SourceFile": str(f), PIX_EVENT_OVERRIDE: "Hawaii"})}),
    )

    set_override(tag="event", value="Hawaii", paths=[f], no_prompt=True)
    assert seen == []  # nothing to do → no apply


def test_set_clear_skips_files_without_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing a file that has no override is a no-op (would otherwise be a
    '0 updated' write the apply path treats as failure)."""
    root = _lib(tmp_path)
    f = root / "x.jpg"
    f.write_bytes(b"x")
    seen: list[Plan] = []
    _patch_apply(monkeypatch, seen, 0)
    monkeypatch.setattr(set_mod, "read_metadata_batched", _no_metas)

    set_override(tag="event", value="", paths=[f], no_prompt=True, clear=True)
    assert seen == []


def test_set_clear_writes_when_override_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _lib(tmp_path)
    f = root / "x.jpg"
    f.write_bytes(b"x")
    seen: list[Plan] = []
    _patch_apply(monkeypatch, seen, 1)
    monkeypatch.setattr(
        set_mod, "read_metadata_batched",
        _metas(**{str(f): FileMetadata(
            path=f, raw={"SourceFile": str(f), PIX_EVENT_OVERRIDE: "Hawaii"})}),
    )

    set_override(tag="event", value="", paths=[f], no_prompt=True, clear=True)
    assert len(seen) == 1
    assert seen[0].lines[0].pix_writes == {PIX_EVENT_OVERRIDE: ""}  # clear


def test_set_rejects_unknown_tag(tmp_path: Path) -> None:
    with pytest.raises(typer.Exit):
        set_override(tag="bogus", value="x", paths=[tmp_path / "f"], no_prompt=True)


def test_set_rejects_invalid_date_override(tmp_path: Path) -> None:
    with pytest.raises(typer.Exit):
        set_override(tag="date", value="not-a-date", paths=[tmp_path / "f"], no_prompt=True)


def test_set_accepts_valid_date_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _lib(tmp_path)
    f = root / "x.mp4"
    f.write_bytes(b"x")
    seen: list[Plan] = []
    _patch_apply(monkeypatch, seen, 1)
    monkeypatch.setattr(set_mod, "read_metadata_batched", _no_metas)
    set_override(tag="date", value="2022-*-*-*:*:*", paths=[f], no_prompt=True)
    assert len(seen) == 1


def test_set_rejects_no_files(tmp_path: Path) -> None:
    with pytest.raises(typer.Exit):
        set_override(tag="event", value="Hawaii", paths=[], no_prompt=True)


def test_set_rejects_file_outside_any_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PIX_ROOT", raising=False)
    f = tmp_path / "loose.jpg"  # no .pix anywhere above
    f.write_bytes(b"x")
    with pytest.raises(typer.Exit):
        set_override(tag="event", value="Hawaii", paths=[f], no_prompt=True)


def test_set_expands_folder_to_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder arg expands to the taggable media inside it (recursively),
    skipping junk per EXTENSION_POLICY."""
    root = _lib(tmp_path)
    sub = root / "2023"
    sub.mkdir()
    a = sub / "a.jpg"
    a.write_bytes(b"x")
    b = sub / "deep" / "b.mp4"
    b.parent.mkdir()
    b.write_bytes(b"x")
    (sub / "Thumbs.db").write_bytes(b"x")  # junk → skipped
    seen: list[Plan] = []
    _patch_apply(monkeypatch, seen, 2)
    monkeypatch.setattr(set_mod, "read_metadata_batched", _no_metas)

    set_override(tag="event", value="Hawaii", paths=[sub], no_prompt=True)

    assert len(seen) == 1
    tagged = {ln.abs_path for ln in seen[0].lines}
    assert tagged == {a, b}


def test_set_dedupes_overlapping_file_and_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file named both directly and via its enclosing folder is written once."""
    root = _lib(tmp_path)
    sub = root / "2023"
    sub.mkdir()
    a = sub / "a.jpg"
    a.write_bytes(b"x")
    seen: list[Plan] = []
    _patch_apply(monkeypatch, seen, 1)
    monkeypatch.setattr(set_mod, "read_metadata_batched", _no_metas)

    set_override(tag="event", value="Hawaii", paths=[a, sub], no_prompt=True)

    assert len(seen) == 1
    assert [ln.abs_path for ln in seen[0].lines] == [a]


def _seed_events_cache(root: Path, *names: str) -> None:
    """Write the events cache (name<TAB>range lines) used for case-alignment."""
    (root / ".pix" / "local").mkdir(parents=True, exist_ok=True)
    (root / ".pix" / "local" / "events.cache").write_text(
        "".join(f"{n}\t\n" for n in names), encoding="utf-8"
    )


def test_set_event_aligns_casing_to_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting 'karate' when 'Karate' already exists writes the existing
    casing, so NTFS doesn't get case-variant event folders."""
    root = _lib(tmp_path)
    _seed_events_cache(root, "Karate", "Hawaii")
    f = root / "x.jpg"
    f.write_bytes(b"x")
    seen: list[Plan] = []
    _patch_apply(monkeypatch, seen, 1)
    monkeypatch.setattr(set_mod, "read_metadata_batched", _no_metas)

    set_override(tag="event", value="karate", paths=[f], no_prompt=True)
    assert seen[0].lines[0].pix_writes == {PIX_EVENT_OVERRIDE: "Karate"}


def test_set_event_new_value_kept_as_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brand-new event (no case-insensitive match) is stored verbatim."""
    root = _lib(tmp_path)
    _seed_events_cache(root, "Karate")
    f = root / "x.jpg"
    f.write_bytes(b"x")
    seen: list[Plan] = []
    _patch_apply(monkeypatch, seen, 1)
    monkeypatch.setattr(set_mod, "read_metadata_batched", _no_metas)

    set_override(tag="event", value="Skiing", paths=[f], no_prompt=True)
    assert seen[0].lines[0].pix_writes == {PIX_EVENT_OVERRIDE: "Skiing"}


def test_clear_event_blanks_auto_derived_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing an event that comes from EventAuto (no override) writes the
    force-null sentinel so the effective event becomes empty — the case where
    `pix tag clear` used to be a confusing no-op."""
    root = _lib(tmp_path)
    f = root / "x.jpg"
    f.write_bytes(b"x")
    seen: list[Plan] = []
    _patch_apply(monkeypatch, seen, 1)
    monkeypatch.setattr(
        set_mod, "read_metadata_batched",
        _metas(**{str(f): FileMetadata(
            path=f, raw={"SourceFile": str(f), PIX_EVENT_AUTO: "Camera"})}),
    )

    set_override(tag="event", value="", paths=[f], no_prompt=True, clear=True)
    assert len(seen) == 1
    assert seen[0].lines[0].pix_writes == {PIX_EVENT_OVERRIDE: EVENT_NULL}


def test_clear_event_drops_override_when_no_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a manual override but no auto, clearing just removes the override
    (no force-null needed — absence already means no event)."""
    root = _lib(tmp_path)
    f = root / "x.jpg"
    f.write_bytes(b"x")
    seen: list[Plan] = []
    _patch_apply(monkeypatch, seen, 1)
    monkeypatch.setattr(
        set_mod, "read_metadata_batched",
        _metas(**{str(f): FileMetadata(
            path=f, raw={"SourceFile": str(f), PIX_EVENT_OVERRIDE: "Hawaii"})}),
    )

    set_override(tag="event", value="", paths=[f], no_prompt=True, clear=True)
    assert len(seen) == 1
    assert seen[0].lines[0].pix_writes == {PIX_EVENT_OVERRIDE: ""}


def test_clear_event_noop_when_already_blanked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file already force-null (or with no event at all) is skipped."""
    root = _lib(tmp_path)
    f = root / "x.jpg"
    f.write_bytes(b"x")
    seen: list[Plan] = []
    _patch_apply(monkeypatch, seen, 0)
    monkeypatch.setattr(
        set_mod, "read_metadata_batched",
        _metas(**{str(f): FileMetadata(
            path=f, raw={"SourceFile": str(f),
                         PIX_EVENT_AUTO: "Camera",
                         PIX_EVENT_OVERRIDE: EVENT_NULL})}),
    )

    set_override(tag="event", value="", paths=[f], no_prompt=True, clear=True)
    assert seen == []


def test_set_refuses_when_checkout_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An open checkout freezes the library — set/clear must refuse like its
    inode-mutating peers (it would orphan the checkout's hard links)."""
    root = _lib(tmp_path)
    f = root / "x.jpg"
    f.write_bytes(b"x")

    def boom(_root: Path) -> None:
        raise CheckoutOpen(None)

    monkeypatch.setattr(set_mod, "ensure_no_open_checkout", boom)
    with pytest.raises(typer.Exit):
        set_override(tag="event", value="Hawaii", paths=[f], no_prompt=True)


def test_set_refuses_when_lock_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """set/clear runs its writes under the library lock; a held lock aborts."""
    root = _lib(tmp_path)
    f = root / "x.jpg"
    f.write_bytes(b"x")

    def boom(_root: Path, _label: str) -> object:
        raise LockHeld(pid=1234, op="migrate", started_at="2026-06-05T10:00:00")

    monkeypatch.setattr(set_mod, "acquire_lock", boom)
    with pytest.raises(typer.Exit):
        set_override(tag="event", value="Hawaii", paths=[f], no_prompt=True)


def test_set_rejects_folder_with_no_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder that expands to nothing taggable is an error, not a no-op."""
    root = _lib(tmp_path)
    sub = root / "empty"
    sub.mkdir()
    (sub / "notes.txt").write_bytes(b"x")  # 'delete' policy → not taggable
    monkeypatch.setattr(set_mod, "read_metadata_batched", _no_metas)
    with pytest.raises(typer.Exit):
        set_override(tag="event", value="Hawaii", paths=[sub], no_prompt=True)
