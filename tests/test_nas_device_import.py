"""`pix2 import device` — the NAS plumbing around the reused import loop.

The loop itself is covered by `test_import.py`; what is new here is where the
two halves of the skip manifest come from, and how a device gets its name
without a registry file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from pix.nas import device_import as di
from pix.nas import ledger
from pix.wpd import DeviceInfo


@pytest.fixture(autouse=True)
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    staging = tmp_path / "staging"
    share = tmp_path / "nas"
    master = share / "master"
    master.mkdir(parents=True)
    monkeypatch.setattr(di, "IMPORT_ROOT", staging)
    monkeypatch.setattr(ledger, "MASTER_SHARE", share)
    monkeypatch.setattr(ledger, "MASTER_DIR", master)
    return {"staging": staging, "master": master}


def _dev(serial: str = "SER1", friendly: str = "Apple iPhone") -> DeviceInfo:
    return DeviceInfo(device_id=f"usb#{serial}", manufacturer="Apple Inc.",
                      model="Apple iPhone", serial=serial, friendly=friendly)


def _ledger(master: Path, folder: str, header: dict[str, object],
            entries: list[dict[str, object]]) -> None:
    d = master / folder
    d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(header)] + [json.dumps(e) for e in entries]
    (d / ".import.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _staged_sidecar(staging: Path, friendly: str, serial: str) -> None:
    d = staging / friendly / ".manifest"
    d.mkdir(parents=True, exist_ok=True)
    (d / "IMG_0001.HEIC.importinfo").write_text(
        yaml.safe_dump({"serial": serial, "device_name": friendly,
                        "puid": "{P1}", "size": 100}),
        encoding="utf-8")


# --- the committed half ------------------------------------------------------

def test_committed_ids_are_scoped_to_the_device(roots: dict[str, Path]) -> None:
    """Another phone's uploads must not mask this one's."""
    _ledger(roots["master"], "iPhone_2026",
            {"name": "iPhone", "source": "device", "serial": "SER1"},
            [{"puid": "{P1}", "size": 10}, {"puid": "{P2}", "size": 20}])
    _ledger(roots["master"], "Pixel_2026",
            {"name": "Pixel", "source": "device", "serial": "SER2"},
            [{"puid": "{P9}", "size": 30}])

    assert ledger.committed_import_ids("SER1") == {"SER1:{P1}", "SER1:{P2}"}
    assert ledger.committed_import_ids("SER2") == {"SER2:{P9}"}


def test_folder_batches_contribute_no_device_ids(roots: dict[str, Path]) -> None:
    _ledger(roots["master"], "legacy_2026",
            {"name": "legacy", "source": "folder"},
            [{"rel": "a/x.jpg", "size": 3}])

    assert ledger.committed_import_ids("SER1") == set()


def test_committed_ids_use_the_shape_the_loop_expects(
    roots: dict[str, Path]
) -> None:
    """`import_loop` compares `f"{serial}:{puid}"`, so this must match exactly."""
    _ledger(roots["master"], "iPhone_2026",
            {"name": "iPhone", "source": "device", "serial": "SER1"},
            [{"puid": "{P1}", "size": 10}])

    assert "SER1:{P1}" in ledger.committed_import_ids("SER1")


# --- the derived registry ----------------------------------------------------

def test_name_comes_from_a_previous_upload(roots: dict[str, Path]) -> None:
    _ledger(roots["master"], "Jamies-iPhone_2026",
            {"name": "Jamies-iPhone", "source": "device", "serial": "SER1"}, [])

    assert di._known_names() == {"SER1": "Jamies-iPhone"}


def test_name_comes_from_pending_staging_too(roots: dict[str, Path]) -> None:
    """A phone imported but not yet uploaded has no ledger — staging knows it.

    Without this, a second import before uploading would ask for a name already
    given, and a different answer would split one phone across two folders.
    """
    _staged_sidecar(roots["staging"], "Jamies-iPhone", "SER1")

    assert di._known_names() == {"SER1": "Jamies-iPhone"}


def test_uploaded_name_wins_over_pending(roots: dict[str, Path]) -> None:
    _ledger(roots["master"], "Jamies-iPhone_2026",
            {"name": "Jamies-iPhone", "source": "device", "serial": "SER1"}, [])
    _staged_sidecar(roots["staging"], "Stale-Name", "SER1")

    assert di._known_names()["SER1"] == "Jamies-iPhone"


def test_known_device_is_not_prompted(roots: dict[str, Path]) -> None:
    known = {"SER1": "Jamies-iPhone"}
    assert di._friendly_for(_dev(), "SER1", known, None, lambda _: None) \
        == "Jamies-iPhone"


def test_explicit_name_overrides_everything(roots: dict[str, Path]) -> None:
    known = {"SER1": "Jamies-iPhone"}
    assert di._friendly_for(_dev(), "SER1", known, "Other", lambda _: None) \
        == "Other"


def test_a_new_device_is_named_once(
    roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(di.typer, "prompt", lambda *a, **k: "Wifes-iPhone")

    assert di._friendly_for(_dev("SER9"), "SER9", {}, None, lambda _: None) \
        == "Wifes-iPhone"


def test_two_serials_never_share_a_staging_folder(
    roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Their files would mix and one device's manifest would mask the other's."""
    monkeypatch.setattr(di.typer, "prompt", lambda *a, **k: "iPhone")

    name = di._friendly_for(_dev("SER9"), "SER9", {"SER1": "iPhone"}, None,
                            lambda _: None)

    assert name != "iPhone"
    assert name.startswith("iPhone-")


def test_non_interactive_falls_back_to_the_device_name(
    roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_tty(*a: object, **k: object) -> str:
        raise EOFError

    monkeypatch.setattr(di.typer, "prompt", no_tty)

    assert di._friendly_for(_dev("SER9"), "SER9", {}, None, lambda _: None) \
        == "Apple iPhone"


# --- the NAS precondition ----------------------------------------------------

def test_import_fails_before_touching_the_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the committed half this is a different operation, not a degraded one."""
    monkeypatch.setattr(ledger, "MASTER_SHARE", tmp_path / "nope")
    touched: list[str] = []
    monkeypatch.setattr(di.wpd, "list_devices",
                        lambda: touched.append("listed") or [])

    with pytest.raises(ledger.NasUnreachable):
        di.run_device_import()

    assert touched == []
