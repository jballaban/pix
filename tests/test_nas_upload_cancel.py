"""Cancelling an upload (spec/nas-app.md §9).

Ctrl+C must be visibly a cancellation, not an apparent hang: the queue is
dropped, in-flight copies close out where the operator can watch the count fall,
and nothing is verified or deleted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pix.nas import folder_import as fi
from pix.nas import ledger
from pix.nas import upload as up


@pytest.fixture(autouse=True)
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    staging = tmp_path / "staging"
    share = tmp_path / "nas"
    master = share / "master"
    master.mkdir(parents=True)
    monkeypatch.setattr(fi, "IMPORT_ROOT", staging)
    monkeypatch.setattr(up, "IMPORT_ROOT", staging)
    monkeypatch.setattr(up, "MASTER_DIR", master)
    monkeypatch.setattr(ledger, "MASTER_SHARE", share)
    monkeypatch.setattr(ledger, "MASTER_DIR", master)
    return {"staging": staging, "master": master, "tmp": tmp_path}


def _staged(roots: dict[str, Path], name: str, count: int) -> Path:
    src = roots["tmp"] / f"src_{name}"
    src.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (src / f"{i:03d}.jpg").write_bytes(bytes([i % 256]) * 32)
    fi.run_folder_import(src, name)
    return src


def test_cancel_keeps_staging_and_skips_verification(
    roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The destructive step must not run on a partial batch."""
    _staged(roots, "legacy", 20)

    real = up._copy_hashing
    seen = {"n": 0}

    def interrupt_partway(src: Path, dst: Path) -> str:
        seen["n"] += 1
        if seen["n"] == 3:
            raise KeyboardInterrupt
        return real(src, dst)

    monkeypatch.setattr(up, "_copy_hashing", interrupt_partway)
    verified: list[object] = []
    monkeypatch.setattr(up, "_verify",
                        lambda *a: verified.append(a) or True)

    [s] = up.run_upload()

    assert s.cancelled is True
    assert s.verified is False
    assert s.staging_cleared is False
    assert (roots["staging"] / "legacy").is_dir()
    assert verified == []          # verification never attempted


def test_cancel_stops_later_staging_folders(
    roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel means stop, not 'carry on with the next batch'."""
    _staged(roots, "aaa", 5)
    _staged(roots, "zzz", 5)

    def interrupt(src: Path, dst: Path) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(up, "_copy_hashing", interrupt)

    summaries = up.run_upload()

    assert len(summaries) == 1
    assert summaries[0].name == "aaa"
    assert (roots["staging"] / "zzz").is_dir()


def test_cancelled_run_resumes_into_the_same_folder(
    roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running after a cancel continues the batch rather than splitting it."""
    _staged(roots, "legacy", 10)

    real = up._copy_hashing
    seen = {"n": 0}

    def interrupt_partway(src: Path, dst: Path) -> str:
        seen["n"] += 1
        if seen["n"] == 3:
            raise KeyboardInterrupt
        return real(src, dst)

    monkeypatch.setattr(up, "_copy_hashing", interrupt_partway)
    [first] = up.run_upload()

    monkeypatch.setattr(up, "_copy_hashing", real)
    [second] = up.run_upload()

    assert first.cancelled is True
    assert second.master_folder == first.master_folder
    assert second.staging_cleared is True
    assert len(list(roots["master"].iterdir())) == 1


def test_status_line_reports_the_drain() -> None:
    """What the operator sees instead of a frozen terminal."""
    s = up.UploadSummary(name="legacy", master_folder=Path("."))
    body = up._status(s, 100, 1000, 0.0, {"cancelling": 1, "inflight": 7})

    assert "CANCELLING" in body
    assert "7" in body


def test_status_line_is_normal_when_not_cancelling() -> None:
    s = up.UploadSummary(name="legacy", master_folder=Path("."))
    body = up._status(s, 100, 1000, 0.0, {"cancelling": 0, "inflight": 3})

    assert "CANCELLING" not in body
    assert body.startswith("0/100")
