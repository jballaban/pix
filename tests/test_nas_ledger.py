"""The committed half of the skip manifest (spec/nas-app.md §9).

`import` must consult master's ledgers, not just local staging — otherwise
"pull what is new" silently becomes "pull everything again", and the duplicates
land in master where nothing removes them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

import pytest

from pix.nas import folder_import as fi
from pix.nas import ledger
from pix.nas import staging as st


def _write_ledger(folder: Path, header: dict[str, object],
                  entries: Sequence[dict[str, object]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(header)] + [json.dumps(e) for e in entries]
    (folder / ".import.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def master(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the share and master dir at a temp tree."""
    share = tmp_path / "nas"
    master_dir = share / "master"
    master_dir.mkdir(parents=True)
    monkeypatch.setattr(ledger, "MASTER_SHARE", share)
    monkeypatch.setattr(ledger, "MASTER_DIR", master_dir)
    return master_dir


def test_unreachable_share_is_an_error(tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing share must fail loudly, never read as an empty archive."""
    monkeypatch.setattr(ledger, "MASTER_SHARE", tmp_path / "nope")
    with pytest.raises(ledger.NasUnreachable):
        ledger.require_share()


def test_empty_master_is_reachable_and_empty(master: Path) -> None:
    """The share existing with nothing uploaded yet is not an error."""
    assert ledger.committed_folder_keys("legacy_2015") == set()


def test_committed_keys_are_read_from_ledgers(master: Path) -> None:
    _write_ledger(
        master / "legacy_2015_2026-09-11T02-00",
        {"name": "legacy_2015", "source": "folder", "source_root": r"G:\pix\2015"},
        [{"rel": "a/one.jpg", "size": 3, "outcome": "kept"},
         {"rel": "b.heic", "size": 3, "outcome": "kept"}],
    )
    keys = ledger.committed_folder_keys("legacy_2015")
    assert keys == {st.skip_key("a/one.jpg", 3), st.skip_key("b.heic", 3)}


def test_keys_are_scoped_by_name(master: Path) -> None:
    """Two sources can hold the same relative path at the same size.

    A folder has no PUID, so `(rel, size)` is only unique within one source —
    two SD cards both holding DCIM/100MSDCF/DSC00001.JPG would otherwise mask
    each other.
    """
    shared: list[dict[str, object]] = [
        {"rel": "DCIM/100MSDCF/DSC00001.JPG", "size": 1024, "outcome": "kept"}
    ]
    _write_ledger(master / "card_a_2026-09-11T02-00",
                  {"name": "card_a", "source": "folder"}, shared)

    assert ledger.committed_folder_keys("card_a") == {
        st.skip_key("DCIM/100MSDCF/DSC00001.JPG", 1024)
    }
    assert ledger.committed_folder_keys("card_b") == set()


def test_device_ledgers_are_ignored_for_folder_keys(master: Path) -> None:
    """A device batch uses PUID identity; its lines are not folder keys."""
    _write_ledger(
        master / "Jamies-iPhone_2026-09-11T02-00",
        {"name": "Jamies-iPhone", "source": "device", "serial": "ABC123"},
        [{"puid": "p1", "path": "100APPLE/IMG_4471.HEIC", "size": 10}],
    )
    assert ledger.committed_folder_keys("Jamies-iPhone") == set()


def test_known_devices_are_derived_not_stored(master: Path) -> None:
    """Serial -> friendly name comes from headers; there is no registry file."""
    _write_ledger(master / "Jamies-iPhone_2026-09-11T02-00",
                  {"name": "Jamies-iPhone", "source": "device", "serial": "ABC123"}, [])
    _write_ledger(master / "legacy_2015_2026-09-11T02-00",
                  {"name": "legacy_2015", "source": "folder"}, [])

    assert ledger.known_devices() == {"ABC123": "Jamies-iPhone"}


def test_malformed_ledger_is_skipped_not_fatal(master: Path) -> None:
    """One corrupt ledger must not blind the whole lookup."""
    (master / "broken_2026").mkdir()
    (master / "broken_2026" / ".import.jsonl").write_text("{not json\n", encoding="utf-8")
    _write_ledger(master / "legacy_2015_2026-09-11T02-00",
                  {"name": "legacy_2015", "source": "folder"},
                  [{"rel": "a.jpg", "size": 1}])

    assert ledger.committed_folder_keys("legacy_2015") == {st.skip_key("a.jpg", 1)}


# --- integration with the folder import --------------------------------------

def test_import_skips_already_uploaded_files(master: Path, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: an uploaded file is not staged again."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "one.jpg").write_bytes(b"one")
    (src / "two.jpg").write_bytes(b"two")
    monkeypatch.setattr(fi, "IMPORT_ROOT", tmp_path / "staging")

    _write_ledger(master / "legacy_2015_2026-09-11T02-00",
                  {"name": "legacy_2015", "source": "folder"},
                  [{"rel": "one.jpg", "size": 3, "outcome": "kept"}])

    s = fi.run_folder_import(src, "legacy_2015")

    assert s.skipped == 1
    assert s.landed == 1
    assert not (tmp_path / "staging" / "legacy_2015" / "one.jpg").exists()
    assert (tmp_path / "staging" / "legacy_2015" / "two.jpg").exists()


def test_import_fails_when_nas_is_unreachable(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail before touching the source, not halfway through staging it."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "one.jpg").write_bytes(b"one")
    staging = tmp_path / "staging"
    monkeypatch.setattr(fi, "IMPORT_ROOT", staging)
    monkeypatch.setattr(ledger, "MASTER_SHARE", tmp_path / "nope")

    with pytest.raises(ledger.NasUnreachable):
        fi.run_folder_import(src, "legacy_2015")

    assert not staging.exists()


def test_culled_then_uploaded_stays_skipped(master: Path, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Staging is cleared after upload, so the ledger must carry the record."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "one.jpg").write_bytes(b"one")
    monkeypatch.setattr(fi, "IMPORT_ROOT", tmp_path / "staging")

    fi.run_folder_import(src, "legacy_2015")
    # Simulate upload: ledger written, staging cleared.
    _write_ledger(master / "legacy_2015_2026-09-11T02-00",
                  {"name": "legacy_2015", "source": "folder"},
                  [{"rel": "one.jpg", "size": 3, "outcome": "kept"}])
    import shutil
    shutil.rmtree(tmp_path / "staging" / "legacy_2015")

    again = fi.run_folder_import(src, "legacy_2015")
    assert again.landed == 0
    assert again.skipped == 1
