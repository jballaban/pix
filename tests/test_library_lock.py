"""Tests for `pix.library_lock` — the library-wide single-active-op lock."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pix import library_lock
from pix.library_lock import LockHeld, acquire


def test_acquire_no_existing_lock(tmp_path: Path) -> None:
    """Acquire on a clean library: lock file appears, then disappears on exit."""
    root = tmp_path / "lib"
    root.mkdir()
    lock_file = root / ".pix" / "local" / "lock"
    assert not lock_file.exists()

    with acquire(root, "hash"):
        assert lock_file.is_file()
        contents = lock_file.read_text(encoding="utf-8")
        lines = contents.strip().splitlines()
        assert int(lines[0]) == os.getpid()
        assert lines[1] == "hash"
        # Third line is the ISO timestamp — just sanity-check shape.
        assert "T" in lines[2]

    assert not lock_file.exists()


def test_acquire_warns_about_orphaned_default_runs(
    tmp_path: Path, capsys: "pytest.CaptureFixture[str]"
) -> None:
    """Acquiring the lock surfaces the runs-relocation advisory: run folders
    left at the default location while runs_dir points elsewhere."""
    import pix.config as config_mod

    config_mod._RUNS_ORPHAN_WARNED.clear()
    root = tmp_path / "lib"
    (root / ".pix" / "runs" / "old-run").mkdir(parents=True)  # orphan at default
    (root / ".pix" / CONFIG_TAIL).write_text(
        f"runs_dir: {tmp_path / 'caps'}\n", encoding="utf-8"
    )
    with acquire(root, "migrate"):
        pass
    assert "old default location" in capsys.readouterr().err


CONFIG_TAIL = "pix.yaml"


def test_acquire_released_on_exception(tmp_path: Path) -> None:
    """An exception in the with-block still releases the lock."""
    root = tmp_path / "lib"
    root.mkdir()
    lock_file = root / ".pix" / "local" / "lock"

    with pytest.raises(RuntimeError):
        with acquire(root, "hash"):
            assert lock_file.is_file()
            raise RuntimeError("boom")

    assert not lock_file.exists()


def test_acquire_refuses_when_live_pix_holds_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock from a live pix process: acquire raises LockHeld."""
    root = tmp_path / "lib"
    (root / ".pix" / "local").mkdir(parents=True)
    lock_file = root / ".pix" / "local" / "lock"
    lock_file.write_text(
        "99999\nmigrate\n2026-05-23T15:32:01\n", encoding="utf-8"
    )

    monkeypatch.setattr(library_lock, "_is_pix_process", lambda pid: True)

    with pytest.raises(LockHeld) as exc:
        with acquire(root, "hash"):
            pass
    assert exc.value.pid == 99999
    assert exc.value.op == "migrate"
    assert exc.value.started_at == "2026-05-23T15:32:01"
    # Lock untouched.
    assert lock_file.read_text(encoding="utf-8").startswith("99999")


def test_acquire_stale_cleans_when_holder_is_dead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A lock from a dead process: stale-clean, log, take the lock."""
    root = tmp_path / "lib"
    (root / ".pix" / "local").mkdir(parents=True)
    lock_file = root / ".pix" / "local" / "lock"
    lock_file.write_text(
        "12345\nmigrate\n2026-05-23T15:32:01\n", encoding="utf-8"
    )

    monkeypatch.setattr(library_lock, "_is_pix_process", lambda pid: False)

    with acquire(root, "hash"):
        # We took the lock — file now reflects our PID and op.
        contents = lock_file.read_text(encoding="utf-8")
        assert contents.startswith(f"{os.getpid()}\n")
        assert "hash" in contents

    assert not lock_file.exists()
    err = capsys.readouterr().err
    assert "cleaning stale lock from PID 12345" in err


def test_acquire_stale_cleans_malformed_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed lock file is treated as stale and removed."""
    root = tmp_path / "lib"
    (root / ".pix" / "local").mkdir(parents=True)
    lock_file = root / ".pix" / "local" / "lock"
    lock_file.write_text("garbage", encoding="utf-8")

    # No PID-check call should reach _is_pix_process for a malformed lock,
    # but stub it defensively in case the implementation changes.
    monkeypatch.setattr(library_lock, "_is_pix_process", lambda pid: False)

    with acquire(root, "hash"):
        pass

    assert not lock_file.exists()


def test_acquire_honors_live_legacy_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live lock left at the pre-`local/` path blocks acquire (upgrade race)."""
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    legacy = root / ".pix" / "lock"
    legacy.write_text("99999\nmigrate\n2026-05-23T15:32:01\n", encoding="utf-8")

    monkeypatch.setattr(library_lock, "_is_pix_process", lambda pid: True)

    with pytest.raises(LockHeld) as exc:
        with acquire(root, "hash"):
            pass
    assert exc.value.pid == 99999
    # Legacy lock left untouched; no new lock created.
    assert legacy.read_text(encoding="utf-8").startswith("99999")
    assert not (root / ".pix" / "local" / "lock").exists()


def test_acquire_stale_cleans_legacy_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead lock at the pre-`local/` path is cleaned, then acquire proceeds
    at the new `.pix/local/lock` location."""
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    legacy = root / ".pix" / "lock"
    legacy.write_text("12345\nmigrate\n2026-05-23T15:32:01\n", encoding="utf-8")

    monkeypatch.setattr(library_lock, "_is_pix_process", lambda pid: False)

    new_lock = root / ".pix" / "local" / "lock"
    with acquire(root, "hash"):
        assert new_lock.is_file()
        assert not legacy.exists()  # legacy cleaned

    assert not new_lock.exists()


def test_is_pix_process_returns_false_for_nonexistent_pid() -> None:
    """A PID extremely unlikely to exist returns False (no Process)."""
    # Pick a PID far above the typical Windows max (32-bit unsigned).
    assert library_lock._is_pix_process(2**31 - 1) is False


def test_is_pix_process_returns_true_for_current_python_when_named_pix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the name check: simulate psutil.Process returning name 'pix.exe'."""
    import psutil

    class FakeProc:
        def __init__(self, pid: int) -> None:
            del pid

        def name(self) -> str:
            return "pix.exe"

    monkeypatch.setattr(psutil, "Process", FakeProc)
    assert library_lock._is_pix_process(os.getpid()) is True


def test_is_pix_process_returns_false_when_name_is_chrome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify PID-reuse mitigation: process exists but isn't pix."""
    import psutil

    class FakeProc:
        def __init__(self, pid: int) -> None:
            del pid

        def name(self) -> str:
            return "chrome.exe"

    monkeypatch.setattr(psutil, "Process", FakeProc)
    assert library_lock._is_pix_process(os.getpid()) is False
