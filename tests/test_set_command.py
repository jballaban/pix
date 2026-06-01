"""Tests for `pix set` / `pix clear` — tag-override writes on specific files.

The exiftool-backed metadata read and the apply are monkeypatched so these
stay fast and deterministic; they exercise validation, library scoping,
no-op detection, and plan construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

import pix.commands.set as set_mod
from pix.commands.set import set_override
from pix.metadata import FileMetadata
from pix.plan import PIX_EVENT_OVERRIDE, Plan, PlanLine

_ApplyResult = tuple[int, list[tuple[PlanLine, str]]]


def _no_hash(root: Path, p: Path) -> str | None:
    return None


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
    monkeypatch.setattr(set_mod, "read_cached_hash", _no_hash)


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
