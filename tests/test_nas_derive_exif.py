"""The self-healing ExifTool pool (spec/nas-app.md §9).

`ExifToolSession.execute` kills the process on timeout, so a single slow file
leaves the session dead and every read after it fails. Observed in the wild: one
bad file cost the metadata for every file that followed it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from pix.exiftool_session import ExifToolTimeout
from pix.nas import derive
from pix.nas import ledger


class _FakeSession:
    """A session that fails a scripted number of times, then works."""

    created: int = 0

    def __init__(self, script: list[object]) -> None:
        self._script = script
        _FakeSession.created += 1
        self.closed = False

    def read_metadata(self, media: Path) -> dict[str, object] | None:
        if not self._script:
            return {"SourceFile": str(media)}
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return {"SourceFile": str(media)}

    def close(self) -> None:
        self.closed = True


def _pool(script: list[object]) -> derive._ExifPool:
    pool = derive._ExifPool()
    _FakeSession.created = 0
    pool._ensure = lambda: _session_for(pool, script)   # type: ignore[method-assign]
    return pool


def _session_for(pool: derive._ExifPool, script: list[object]) -> object:
    if pool._session is None:
        pool._session = _FakeSession(script)  # type: ignore[assignment]
        pool.restarts += 1
    return pool._session


def test_a_dead_session_is_replaced_and_the_file_retried(tmp_path: Path) -> None:
    """The cascade this exists to prevent: one death, one lost file at most."""
    pool = _pool([RuntimeError("subprocess exited unexpectedly")])

    result = pool.read(tmp_path / "a.jpg")

    assert result is not None
    assert pool.restarts == 2          # original plus the replacement


def test_a_broken_pipe_is_also_recoverable(tmp_path: Path) -> None:
    """Writing to a killed session's stdin surfaces as OSError EINVAL."""
    pool = _pool([OSError(22, "Invalid argument")])

    assert pool.read(tmp_path / "a.jpg") is not None


def test_later_files_still_work_after_a_death(tmp_path: Path) -> None:
    """The actual regression: file N+1 must not inherit file N's failure."""
    pool = _pool([RuntimeError("dead")])

    first = pool.read(tmp_path / "a.jpg")
    second = pool.read(tmp_path / "b.jpg")
    third = pool.read(tmp_path / "c.jpg")

    assert first is not None and second is not None and third is not None


def test_a_timeout_skips_that_file_without_poisoning_the_run(
    tmp_path: Path
) -> None:
    """A slow file is genuinely slow — skip it, do not spend the timeout twice."""
    pool = _pool([ExifToolTimeout("too slow")])

    assert pool.read(tmp_path / "slow.jpg") is None
    assert pool.read(tmp_path / "next.jpg") is not None


def test_two_consecutive_deaths_give_up_on_that_file(tmp_path: Path) -> None:
    """If a fresh session dies too, the problem is not the session."""
    pool = _pool([RuntimeError("dead"), RuntimeError("dead again")])

    with pytest.raises(RuntimeError):
        pool.read(tmp_path / "a.jpg")


# --- reporting ---------------------------------------------------------------

@pytest.fixture
def tiers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    share = tmp_path / "nas"
    master = share / "master"
    master.mkdir(parents=True)
    monkeypatch.setattr(derive, "MASTER_DIR", master)
    monkeypatch.setattr(derive, "THUMB_DIR", share / "thumb")
    monkeypatch.setattr(derive, "PREVIEW_DIR", share / "preview")
    monkeypatch.setattr(derive, "META_DIR", share / "meta")
    monkeypatch.setattr(ledger, "MASTER_SHARE", share)
    monkeypatch.setattr(ledger, "MASTER_DIR", master)
    return {"master": master}


def test_failures_while_cancelling_are_not_reported(
    tiers: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shutdown artefact is not a fact about the file, and the next run redoes it.

    Listing them would bury any real failure under a wall of noise — which is
    exactly what the observed run did.
    """
    folder = tiers["master"] / "legacy_2026"
    folder.mkdir(parents=True)
    Image.new("RGB", (50, 50)).save(folder / "a.jpg", "JPEG")

    def boom(media: Path, exif: object) -> bool:
        raise RuntimeError("subprocess exited unexpectedly")

    monkeypatch.setattr(derive, "_write_meta", boom)

    summary = derive.ProcessSummary()
    derive._derive_one(folder / "a.jpg", summary, derive.threading.Lock(),
                       derive._ExifPool(), {"cancelling": 1})

    assert summary.failed == []


def test_failures_outside_a_cancel_are_reported(
    tiers: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tiers["master"] / "legacy_2026"
    folder.mkdir(parents=True)
    Image.new("RGB", (50, 50)).save(folder / "a.jpg", "JPEG")

    def boom(media: Path, exif: object) -> bool:
        raise RuntimeError("something real")

    monkeypatch.setattr(derive, "_write_meta", boom)

    summary = derive.ProcessSummary()
    derive._derive_one(folder / "a.jpg", summary, derive.threading.Lock(),
                       derive._ExifPool(), {"cancelling": 0})

    assert len(summary.failed) == 1
    assert "something real" in summary.failed[0]
