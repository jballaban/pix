"""Tests for `pix sync` orchestration (commands/sync.py).

The four sub-commands are monkeypatched so these tests exercise only the
pipeline wiring — order, the auto-apply flag, template pass-through, and
stop-on-error — without real library I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import typer

import pix.commands.sync as sync_mod


def test_sync_runs_all_steps_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def recorder(name: str) -> Callable[..., None]:
        def _step(**kwargs: object) -> None:
            calls.append((name, kwargs))

        return _step

    for fn_name, step in (
        ("migrate_folder", "migrate"),
        ("hash_library", "hash"),
        ("dedupe_library", "dedupe"),
        ("organize_library", "organize"),
    ):
        monkeypatch.setattr(sync_mod, fn_name, recorder(step))

    sync_mod.sync_library(Path("X"), template_str="{year}/{event}")

    assert [name for name, _ in calls] == [
        "migrate",
        "hash",
        "dedupe",
        "organize",
    ]
    # Every step auto-applies.
    assert all(kw.get("no_prompt") is True for _, kw in calls)
    # The template is forwarded to organize only.
    assert calls[-1][1].get("template_str") == "{year}/{event}"


def test_sync_stops_on_first_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def ok(name: str) -> Callable[..., None]:
        def _step(**_kwargs: object) -> None:
            calls.append(name)

        return _step

    def boom(**_kwargs: object) -> None:
        calls.append("hash")
        raise typer.Exit(code=1)

    monkeypatch.setattr(sync_mod, "migrate_folder", ok("migrate"))
    monkeypatch.setattr(sync_mod, "hash_library", boom)
    monkeypatch.setattr(sync_mod, "dedupe_library", ok("dedupe"))
    monkeypatch.setattr(sync_mod, "organize_library", ok("organize"))

    with pytest.raises(typer.Exit):
        sync_mod.sync_library(Path("X"))

    # Chain halted at the failing step; dedupe/organize never ran.
    assert calls == ["migrate", "hash"]
