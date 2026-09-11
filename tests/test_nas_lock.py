"""Single-writer lock for upload (spec/nas-app.md §9).

Two uploads appending to one ledger interleave their writes. An observed run
produced 94 torn JSON lines and up to three records per file because a second
upload was started while the first was running.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pix.nas.lock import LOCK_NAME, UploadLock, UploadLocked


def test_lock_is_taken_and_released(tmp_path: Path) -> None:
    with UploadLock(tmp_path):
        assert (tmp_path / LOCK_NAME).is_file()
    assert not (tmp_path / LOCK_NAME).exists()


def test_second_upload_is_refused(tmp_path: Path) -> None:
    with UploadLock(tmp_path):
        with pytest.raises(UploadLocked):
            with UploadLock(tmp_path):
                pass


def test_error_names_the_owning_process(tmp_path: Path) -> None:
    """The message has to be actionable — which PID, and how to clear it."""
    with UploadLock(tmp_path):
        with pytest.raises(UploadLocked) as e:
            with UploadLock(tmp_path):
                pass
    assert str(os.getpid()) in str(e.value)
    assert LOCK_NAME in str(e.value)


def test_stale_lock_from_a_dead_process_is_taken_over(tmp_path: Path) -> None:
    """A crash must never block every future run."""
    (tmp_path / LOCK_NAME).write_text("999999999", encoding="ascii")

    with UploadLock(tmp_path):
        assert (tmp_path / LOCK_NAME).read_text(encoding="ascii") == str(os.getpid())


def test_unreadable_lock_is_treated_as_stale(tmp_path: Path) -> None:
    """A torn lock file should not be more durable than a real one."""
    (tmp_path / LOCK_NAME).write_text("not-a-pid", encoding="ascii")

    with UploadLock(tmp_path):
        assert (tmp_path / LOCK_NAME).is_file()


def test_lock_released_after_an_exception(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        with UploadLock(tmp_path):
            raise RuntimeError("boom")
    assert not (tmp_path / LOCK_NAME).exists()


def test_root_is_created_if_missing(tmp_path: Path) -> None:
    root = tmp_path / "not-yet"
    with UploadLock(root):
        assert (root / LOCK_NAME).is_file()
