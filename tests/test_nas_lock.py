"""Single-writer locks on the share (spec/nas-app.md §9).

Two uploads appending to one ledger interleave their writes. An observed run
produced 94 torn JSON lines and up to three records per file because a second
upload was started while the first was running. `process` has the same shape:
it decides what is missing and then spends minutes making it, and a second run
decides the same thing about the same files.

The lock is on the share, which is what makes the owner's *host* part of the
answer — see `test_a_lock_held_by_another_machine_is_left_alone`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import json
import socket

from pix.nas.lock import (
    PROCESS_LOCK, UPLOAD_LOCK as LOCK_NAME, Locked, ProcessLock,
    UploadLock, UploadLocked,
)


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
    (tmp_path / LOCK_NAME).write_text(json.dumps(
        {"host": socket.gethostname(), "pid": 999999999}), encoding="utf-8")

    with UploadLock(tmp_path):
        owner = json.loads((tmp_path / LOCK_NAME).read_text(encoding="utf-8"))
    assert owner["pid"] == os.getpid()


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


def test_a_lock_held_by_another_machine_is_left_alone(tmp_path: Path) -> None:
    """The reason the owner is not a bare process id.

    A PID means nothing on the machine that did not issue it: reading `5000`
    off the share tells this desktop nothing about a run on another one, and
    small numbers are reused constantly. Judging it by the local process table
    is how a lock gets *stolen* — the second machine finds no 5000, calls it a
    crash, and starts a concurrent run of the thing the lock exists to
    serialise.
    """
    (tmp_path / LOCK_NAME).write_text(json.dumps(
        {"host": "some-other-desktop", "pid": 999999999}), encoding="utf-8")

    with pytest.raises(UploadLocked) as e:
        with UploadLock(tmp_path):
            pass
    assert "some-other-desktop" in str(e.value)


def test_process_takes_a_lock_of_its_own(tmp_path: Path) -> None:
    """One lock per activity: uploading while processing is fine, and two of
    either is not."""
    with ProcessLock(tmp_path):
        assert (tmp_path / PROCESS_LOCK).is_file()
        with pytest.raises(Locked):
            with ProcessLock(tmp_path):
                pass
        # An upload alongside is somebody else's business.
        with UploadLock(tmp_path):
            pass
    assert not (tmp_path / PROCESS_LOCK).exists()


def test_the_message_says_how_long_it_has_been_running(tmp_path: Path) -> None:
    """*Wait for it* is only advice if you can tell whether it is nearly
    done."""
    with ProcessLock(tmp_path):
        with pytest.raises(Locked) as e:
            with ProcessLock(tmp_path):
                pass
    assert "min ago" in str(e.value)
