"""Tests for `pix.timeout` — thread-based timeout wrapper for in-process ops."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import errno

from pix.timeout import (
    OperationTimeout,
    run_with_timeout,
    safe_move,
    safe_rename,
)


def test_run_with_timeout_returns_value_on_completion() -> None:
    """Fast functions return their value normally."""
    result = run_with_timeout("test", 1.0, lambda: 42)
    assert result == 42


def test_run_with_timeout_propagates_exceptions() -> None:
    """Non-timeout exceptions from the worker propagate to the caller."""
    def fail() -> None:
        raise ValueError("custom error")

    with pytest.raises(ValueError, match="custom error"):
        run_with_timeout("test", 1.0, fail)


def test_run_with_timeout_raises_on_slow_function() -> None:
    """A function that exceeds the timeout raises OperationTimeout."""
    with pytest.raises(OperationTimeout, match="slow op timed out after 0s"):
        run_with_timeout("slow op", 0.1, time.sleep, 1.0)


def test_run_with_timeout_passes_args_and_kwargs() -> None:
    def add(a: int, b: int, *, c: int = 0) -> int:
        return a + b + c

    assert run_with_timeout("test", 1.0, add, 1, 2, c=3) == 6


def test_safe_rename_works_on_fast_filesystem(tmp_path: Path) -> None:
    """Real rename completes well under the default 10s timeout."""
    src = tmp_path / "a.txt"
    dst = tmp_path / "b.txt"
    src.write_bytes(b"x")
    safe_rename(src, dst)
    assert dst.is_file()
    assert not src.exists()


def test_safe_rename_propagates_oserror(tmp_path: Path) -> None:
    """Underlying rename failures (e.g. target exists on Windows) propagate as OSError."""
    src = tmp_path / "a.txt"
    dst = tmp_path / "b.txt"
    src.write_bytes(b"x")
    dst.write_bytes(b"y")
    # Windows raises FileExistsError; POSIX silently overwrites. Skip the
    # negative assertion on POSIX rather than fork the test.
    import os

    if os.name != "nt":
        pytest.skip("rename-over-existing only raises on Windows")
    with pytest.raises(OSError):
        safe_rename(src, dst)


def test_safe_rename_times_out_with_slow_underlying_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the underlying syscall wedges, safe_rename raises OperationTimeout."""
    src = tmp_path / "a.txt"
    dst = tmp_path / "b.txt"
    src.write_bytes(b"x")

    # Monkey-patch Path.rename to simulate a wedged FS.
    def wedged(self: Path, target: Path) -> Path:  # noqa: ARG001
        time.sleep(2.0)
        return target

    monkeypatch.setattr(Path, "rename", wedged)
    with pytest.raises(OperationTimeout):
        safe_rename(src, dst, timeout=0.2)


def test_safe_move_same_volume_renames(tmp_path: Path) -> None:
    """Same-volume target: plain rename, no copy fallback needed."""
    src = tmp_path / "a.txt"
    dst = tmp_path / "b.txt"
    src.write_bytes(b"x")
    safe_move(src, dst)
    assert dst.read_bytes() == b"x"
    assert not src.exists()


def test_safe_move_falls_back_to_copy_on_cross_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cross-device rename error triggers the copy+delete fallback so a
    runs_dir on another volume works. (Path.rename is patched to raise
    EXDEV; shutil.move's own os.rename is unpatched and succeeds.)"""
    src = tmp_path / "a.txt"
    dst = tmp_path / "b.txt"
    src.write_bytes(b"hello")

    def cross_device(self: Path, target: Path) -> Path:  # noqa: ARG001
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(Path, "rename", cross_device)
    safe_move(src, dst)
    assert dst.read_bytes() == b"hello"
    assert not src.exists()


def test_safe_move_propagates_non_cross_device_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-cross-device OSError (e.g. EACCES) propagates — no fallback."""
    src = tmp_path / "a.txt"
    dst = tmp_path / "b.txt"
    src.write_bytes(b"x")

    def eacces(self: Path, target: Path) -> Path:  # noqa: ARG001
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(Path, "rename", eacces)
    with pytest.raises(OSError):
        safe_move(src, dst)
    assert src.exists()  # not moved
