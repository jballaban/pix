"""Cancel, import more, re-upload — when a new master folder is created.

The target is decided by a `.upload-target` marker inside staging, which dies
only when staging is cleared after a verified upload. So an interrupted batch
that is later extended stays one upload, landing in one master folder.
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


def _corrupting(real: object):
    """Wrap the copy so the landed bytes differ from what was hashed."""
    def wrapped(src: Path, dst: Path) -> str:
        digest = real(src, dst)  # type: ignore[operator]
        data = bytearray(dst.read_bytes())
        data[0] ^= 0x01
        dst.write_bytes(bytes(data))
        return digest
    return wrapped


def _source(tmp: Path, name: str, files: dict[str, bytes]) -> Path:
    root = tmp / name
    root.mkdir(parents=True, exist_ok=True)
    for rel, payload in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(payload)
    return root


def test_cancelled_then_extended_batch_stays_one_folder(
    roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Import A, fail the upload, import B, upload again -> one master folder."""
    a = _source(roots["tmp"], "A", {"one.jpg": b"one"})
    fi.run_folder_import(a, "legacy")

    # First attempt fails verification, so staging (and its marker) survive.
    real = up._copy_hashing
    monkeypatch.setattr(up, "_copy_hashing", _corrupting(real))
    [first] = up.run_upload()
    assert first.staging_cleared is False

    b = _source(roots["tmp"], "B", {"two.jpg": b"two"})
    fi.run_folder_import(b, "legacy")

    monkeypatch.setattr(up, "_copy_hashing", real)
    [second] = up.run_upload()

    assert second.master_folder == first.master_folder
    assert len(list(roots["master"].iterdir())) == 1


def test_extended_batch_files_are_still_skipped_on_reimport(
    roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this guards: B joins A's folder, whose header lists only A's root.

    Filtering on that stale header would hide B's uploaded files from the skip
    check, and they would be re-imported and re-uploaded as duplicates.
    """
    a = _source(roots["tmp"], "A", {"one.jpg": b"one"})
    fi.run_folder_import(a, "legacy")

    real = up._copy_hashing
    monkeypatch.setattr(up, "_copy_hashing", _corrupting(real))
    up.run_upload()

    b = _source(roots["tmp"], "B", {"two.jpg": b"two"})
    fi.run_folder_import(b, "legacy")
    monkeypatch.setattr(up, "_copy_hashing", real)
    up.run_upload()

    # B's root is not in the header, but its entries are in the ledger.
    again = fi.run_folder_import(b, "legacy")
    assert again.landed == 0
    assert again.skipped == 1


def test_a_verified_upload_starts_a_fresh_folder_next_time(
    roots: dict[str, Path]
) -> None:
    """Clearing staging takes the marker with it, so the next batch is its own."""
    a = _source(roots["tmp"], "A", {"one.jpg": b"one"})
    fi.run_folder_import(a, "legacy")
    [first] = up.run_upload()
    assert first.staging_cleared is True

    b = _source(roots["tmp"], "B", {"two.jpg": b"two"})
    fi.run_folder_import(b, "legacy")
    [second] = up.run_upload()

    assert second.master_folder != first.master_folder
    assert len(list(roots["master"].iterdir())) == 2


def test_marker_is_never_uploaded_as_media(roots: dict[str, Path]) -> None:
    """`.upload-target` lives in staging; it must not land in the archive."""
    a = _source(roots["tmp"], "A", {"one.jpg": b"one"})
    fi.run_folder_import(a, "legacy")
    [s] = up.run_upload()

    assert not (s.master_folder / up.TARGET_MARKER).exists()
    assert not any(p.name.endswith(up.TARGET_MARKER)
                   for p in s.master_folder.iterdir())
