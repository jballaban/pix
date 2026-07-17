"""Device-independent tests for `pix import` (see spec/import.md).

The WPD/COM layer needs a real device, so these cover the pure logic: name
sanitization, the incremental skip key, `.aae` skipping, sidecar round-trip,
manifest regeneration, device selection, and the friendly-name registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pix import importer
from pix.importer import ImportError_
from pix.wpd import DeviceInfo, WpdObject


def _obj(**kw: object) -> WpdObject:
    base: dict[str, object] = dict(
        id="o1", name=None, orig="IMG_0001.HEIC", ctype="IMAGE",
        format="{fmt}", size=100, puid="{PUID-1}",
        created="2026/05/31:19:44:31.000", modified=None,
    )
    base.update(kw)
    return WpdObject(**base)  # type: ignore[arg-type]


def _dev(serial: str = "SER1", friendly: str = "iPhone") -> DeviceInfo:
    return DeviceInfo(
        device_id=f"\\\\?\\usb#vid_05ac&pid_12a8#{serial}", manufacturer="Apple Inc.",
        model="Apple iPhone", serial=serial, friendly=friendly,
    )


def _reader(serial: str = "20070818") -> DeviceInfo:
    return DeviceInfo(
        device_id=f"\\\\?\\swd#wpdbusenum#_??_usbstor#disk&ven_generic-&prod_multi-card#{serial}",
        manufacturer="Generic-", model="Multi-Card", serial=serial, friendly="E:\\",
    )


# --- sanitize_component ------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("IMG_0001.HEIC", "IMG_0001.HEIC"),
        ('a<b>c:d"e/f\\g|h?i*j', "a_b_c_d_e_f_g_h_i_j"),
        ("trailing. ", "trailing"),
        ("", "_"),
        ("CON", "_CON"),
        ("con.jpg", "_con.jpg"),
        ("202605_a", "202605_a"),
    ],
)
def test_sanitize_component(raw: str, expected: str) -> None:
    assert importer.sanitize_component(raw) == expected


# --- skip key + companion skip ----------------------------------------------
def test_skip_key_uses_puid_and_size() -> None:
    assert importer._skip_key(_obj()) == ("{PUID-1}", 100)


def test_skip_key_falls_back_without_puid() -> None:
    o = _obj(puid=None, orig="IMG_9.JPG", created="D")
    assert importer._skip_key(o) == ("IMG_9.JPG|D", 100)


def test_skip_key_size_distinguishes_proxy_from_original() -> None:
    # Same PUID, different size (optimized-storage proxy vs full-res) → distinct.
    small = importer._skip_key(_obj(size=50))
    full = importer._skip_key(_obj(size=9000))
    assert small != full


def test_aae_is_skippable() -> None:
    assert importer._is_skippable_companion(_obj(orig="IMG_1.AAE"))
    assert not importer._is_skippable_companion(_obj(orig="IMG_1.HEIC"))
    assert not importer._is_skippable_companion(_obj(orig="clip.MOV"))


# --- sidecar round-trip + manifest ------------------------------------------
def test_sidecar_write_read_and_manifest(tmp_path: Path) -> None:
    landing = tmp_path / "import" / "iPhone"
    landed = landing / "202605_a" / "IMG_0001.HEIC"
    landed.parent.mkdir(parents=True)
    landed.write_bytes(b"pretend-heic")

    obj = _obj()
    importer._write_sidecar(landed, _dev(), obj, "Internal Storage/202605_a/IMG_0001.HEIC")

    sidecar = importer._sidecar_path(landed)
    assert sidecar.exists()
    data = importer._read_sidecar(sidecar)
    assert data is not None
    assert data["puid"] == "{PUID-1}"
    assert data["size"] == 100
    assert data["device_path"] == "Internal Storage/202605_a/IMG_0001.HEIC"

    manifest = importer._scan_manifest(landing)
    assert importer._skip_key(obj) in manifest


def test_scan_manifest_empty_when_no_landing(tmp_path: Path) -> None:
    assert importer._scan_manifest(tmp_path / "nope") == set()


def test_sidecar_write_is_atomic_no_temp_left(tmp_path: Path) -> None:
    landed = tmp_path / "IMG.HEIC"
    landed.write_bytes(b"x")
    importer._write_sidecar(landed, _dev(), _obj(), "dev/IMG.HEIC")
    leftovers = list(tmp_path.glob("*.__import__"))
    assert leftovers == []


# --- temp sweep --------------------------------------------------------------
def test_sweep_temps_removes_partials(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.HEIC.__import__").write_bytes(b"partial")
    (tmp_path / "keep.HEIC").write_bytes(b"real")
    n = importer._sweep_temps(tmp_path)
    assert n == 1
    assert not (tmp_path / "sub" / "a.HEIC.__import__").exists()
    assert (tmp_path / "keep.HEIC").exists()


# --- device selection --------------------------------------------------------
def test_select_single_device() -> None:
    d = _dev()
    assert importer._select_device([d], None) is d


def test_select_none_connected() -> None:
    with pytest.raises(ImportError_, match="no portable devices"):
        importer._select_device([], None)


def test_lone_device_auto_selected_even_if_unknown() -> None:
    d = _dev("SER1")
    assert importer._select_device([d], None, known=set()) is d


def test_one_known_among_several_auto_selected() -> None:
    phone, reader = _dev("SER1", "Apple iPhone"), _reader()
    got = importer._select_device([phone, reader], None, known={"SER1"})
    assert got.serial == "SER1"


def test_zero_known_among_several_needs_choice() -> None:
    with pytest.raises(importer.NeedsDeviceChoice) as ei:
        importer._select_device([_dev("A"), _reader()], None, known=set())
    assert len(ei.value.devices) == 2


def test_multiple_known_needs_choice() -> None:
    with pytest.raises(importer.NeedsDeviceChoice):
        importer._select_device([_dev("A"), _dev("B", "Pixel")], None,
                                known={"A", "B"})


def test_prompt_device_choice_returns_selection(monkeypatch: "pytest.MonkeyPatch") -> None:
    monkeypatch.setattr("builtins.input", lambda: "2")
    devs = [_dev("A", "iPhone"), _dev("B", "Pixel")]
    assert importer._prompt_device_choice(devs) is devs[1]


def test_prompt_device_choice_retries_then_selects(monkeypatch: "pytest.MonkeyPatch") -> None:
    answers = iter(["", "99", "1"])
    monkeypatch.setattr("builtins.input", lambda: next(answers))
    devs = [_dev("A", "iPhone"), _dev("B", "Pixel")]
    assert importer._prompt_device_choice(devs) is devs[0]


def test_prompt_device_choice_eof_raises(monkeypatch: "pytest.MonkeyPatch") -> None:
    def boom() -> str:
        raise EOFError()

    monkeypatch.setattr("builtins.input", boom)
    with pytest.raises(ImportError_, match="no device selected"):
        importer._prompt_device_choice([_dev("A"), _dev("B", "Pixel")])


def test_select_by_serial_substring() -> None:
    a, b = _dev("M2DF33MY06"), _dev("XYZ", "Pixel")
    assert importer._select_device([a, b], "m2df") is a


def test_select_ambiguous_selector() -> None:
    with pytest.raises(ImportError_, match="ambiguous"):
        importer._select_device([_dev("A", "iPhone"), _dev("B", "iPhone-2")], "iphone")


# --- registry / friendly name ------------------------------------------------
def test_friendly_registers_and_persists(tmp_path: Path) -> None:
    (tmp_path / ".pix").mkdir()
    name = importer._friendly_for(tmp_path, _dev("SER1", "Apple iPhone"), interactive=False)
    assert name == "Apple iPhone"
    # Persisted and reused on second call.
    assert importer._load_registry(tmp_path) == {"SER1": "Apple iPhone"}
    assert importer._friendly_for(tmp_path, _dev("SER1", "renamed?"), interactive=False) == "Apple iPhone"


def test_friendly_disambiguates_colliding_serials(tmp_path: Path) -> None:
    (tmp_path / ".pix").mkdir()
    importer._save_registry(tmp_path, {"OTHER": "Apple iPhone"})
    name = importer._friendly_for(tmp_path, _dev("SER2", "Apple iPhone"), interactive=False)
    assert name != "Apple iPhone"
    assert name.startswith("Apple iPhone-")


def test_name_flag_sets_and_is_remembered(tmp_path: Path) -> None:
    (tmp_path / ".pix").mkdir()
    dev = _dev("SER1", "Apple iPhone")
    # Explicit --name wins over the WPD default, and persists.
    got = importer._friendly_for(tmp_path, dev, interactive=False, name="my phone")
    assert got == "my phone"
    assert importer._load_registry(tmp_path) == {"SER1": "my phone"}
    # A later run with no --name reuses the remembered name (no prompt path hit).
    assert importer._friendly_for(tmp_path, dev, interactive=False) == "my phone"


def test_name_flag_renames_known_device(tmp_path: Path) -> None:
    (tmp_path / ".pix").mkdir()
    importer._save_registry(tmp_path, {"SER1": "old-name"})
    got = importer._friendly_for(
        tmp_path, _dev("SER1", "Apple iPhone"), interactive=False, name="new-name"
    )
    assert got == "new-name"
    assert importer._load_registry(tmp_path)["SER1"] == "new-name"


def test_name_flag_sanitized(tmp_path: Path) -> None:
    (tmp_path / ".pix").mkdir()
    got = importer._friendly_for(
        tmp_path, _dev("SER1"), interactive=False, name="a/b:c"
    )
    assert got == "a_b_c"


# --- liveness probe ----------------------------------------------------------
def test_alive_true_when_info_ok() -> None:
    class FakeDev:
        def info(self) -> object:
            return object()

    assert importer._alive(FakeDev()) is True  # type: ignore[arg-type]


def test_alive_false_when_info_raises() -> None:
    class DeadDev:
        def info(self) -> object:
            raise RuntimeError("session dropped")

    assert importer._alive(DeadDev()) is False  # type: ignore[arg-type]


# --- landing path collision --------------------------------------------------
def test_landing_path_disambiguates_collision(tmp_path: Path) -> None:
    used: dict[Path, str] = {}
    a = importer._landing_path(tmp_path, "d/IMG.HEIC", used, _obj(puid="{A}"))
    b = importer._landing_path(tmp_path, "d/IMG.HEIC", used, _obj(puid="{B}"))
    assert a != b
    assert a.name == "IMG.HEIC"
