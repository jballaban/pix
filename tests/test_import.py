"""Device-independent tests for `pix import` (see spec/import.md).

The WPD/COM layer needs a real device, so these cover the pure logic: name
sanitization, the incremental skip key, `.aae` skipping, sidecar round-trip,
manifest regeneration, device selection, and the friendly-name registry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

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


def test_select_none_connected() -> None:
    with pytest.raises(ImportError_, match="no portable devices"):
        importer._select_device([], None)


def test_lone_unknown_device_needs_choice() -> None:
    # A single *unknown* device is NOT auto-selected — ask (so a cancel saves nothing).
    with pytest.raises(importer.NeedsDeviceChoice):
        importer._select_device([_dev("SER1")], None, known=set())


def test_lone_known_device_auto_selected() -> None:
    d = _dev("SER1")
    assert importer._select_device([d], None, known={"SER1"}) is d


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


def test_naming_abort_does_not_register(tmp_path: Path, monkeypatch: "pytest.MonkeyPatch") -> None:
    import typer

    (tmp_path / ".pix").mkdir()

    def abort(*a: object, **k: object) -> object:
        raise typer.Abort()

    monkeypatch.setattr(typer, "prompt", abort)
    with pytest.raises(typer.Abort):
        importer._friendly_for(tmp_path, _dev("SER1"), interactive=True)
    # The whole point: a cancelled naming prompt writes nothing to the registry.
    assert importer._load_registry(tmp_path) == {}


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


# --- per-file transfer progress ----------------------------------------------
def test_active_transfer_percent_and_timer(monkeypatch: "pytest.MonkeyPatch") -> None:
    a = importer._ActiveTransfer()
    assert a.render() is None  # nothing active

    clock = [1000.0]
    monkeypatch.setattr(importer.time, "monotonic", lambda: clock[0])
    a.begin("download", "IMG.MOV", 200)
    a.advance(50)
    body = a.render()
    assert body is not None
    assert "download IMG.MOV" in body
    assert "25%" in body
    assert "/" not in body and "MB" not in body  # no byte counter
    assert "[" not in body  # <1s elapsed → no per-file timer yet

    clock[0] = 1002.5  # 2.5s elapsed
    assert "[" in (a.render() or "")  # per-file timer now shown

    a.clear()
    assert a.render() is None


def test_active_transfer_no_total_shows_no_percent(monkeypatch: "pytest.MonkeyPatch") -> None:
    a = importer._ActiveTransfer()
    monkeypatch.setattr(importer.time, "monotonic", lambda: 5.0)
    a.begin("download", "X", None)  # size unknown
    a.advance(1_500_000)
    body = a.render() or ""
    assert body == "download X"  # just verb + name; no %, no byte counter


def test_active_transfer_begin_resets_done_no_stale_percent(monkeypatch: "pytest.MonkeyPatch") -> None:
    # Guards the 5000% bug: a new small file must not show the previous large
    # file's byte count. begin() resets done under the lock.
    a = importer._ActiveTransfer()
    monkeypatch.setattr(importer.time, "monotonic", lambda: 0.0)
    a.begin("download", "BIG.MOV", 250_000_000)
    a.advance(250_000_000)  # 100% of the big file
    a.begin("download", "SMALL.JPG", 5_000_000)  # switch to a small file
    body = a.render() or ""
    assert "0%" in body
    assert "5000%" not in body


def test_active_transfer_percent_clamped(monkeypatch: "pytest.MonkeyPatch") -> None:
    a = importer._ActiveTransfer()
    monkeypatch.setattr(importer.time, "monotonic", lambda: 0.0)
    a.begin("download", "X", 100)
    a.advance(500)  # done > total → clamp to 100%, never absurd
    assert "100%" in (a.render() or "")


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


# --- media_check (read-only validation tripwire) -----------------------------
def test_media_check_valid_image(tmp_path: Path) -> None:
    from PIL import Image

    from pix.media_check import media_check

    p = tmp_path / "ok.jpg"
    Image.new("RGB", (4, 4), "red").save(p, "JPEG")
    assert media_check(p) is None


def test_media_check_corrupt_image_returns_reason(tmp_path: Path) -> None:
    from pix.media_check import media_check

    p = tmp_path / "broken.jpg"
    p.write_bytes(b"this is not a jpeg")
    reason = media_check(p)
    assert reason is not None and "decode failed" in reason


def test_media_check_unknown_extension_is_exempt(tmp_path: Path) -> None:
    from pix.media_check import media_check

    p = tmp_path / "mystery.dat"
    p.write_bytes(b"\x00\x01\x02 whatever")
    assert media_check(p) is None  # no validator → verified on bytes alone


def test_media_check_video_exempt_when_ffprobe_missing(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    from pix import media_check as mc_mod

    monkeypatch.setattr(mc_mod.shutil, "which", lambda _name: None)
    p = tmp_path / "clip.mov"
    p.write_bytes(b"garbage")
    # Missing tool is an environment problem, not a bad file → exempt.
    assert mc_mod.media_check(p) is None


# --- .importissue marker round-trip ------------------------------------------
def test_issue_write_read_and_scan(tmp_path: Path) -> None:
    landing = tmp_path / "import" / "iPhone"
    a = landing / "A.HEIC"
    b = landing / "B.HEIC"
    a.parent.mkdir(parents=True)
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    importer._write_issue(a, _dev(), _obj(orig="A.HEIC"), "A.HEIC",
                          state="needs-session", attempts=1, last_error="bad")
    importer._write_issue(b, _dev(), _obj(orig="B.HEIC"), "B.HEIC",
                          state="failed", attempts=2, last_error="still bad")

    data = importer._read_issue(importer._issue_path(a))
    assert data is not None and data["state"] == "needs-session"
    assert data["attempts"] == 1

    needs, failed = importer._scan_issues(landing)
    assert needs == ["A.HEIC"]
    assert failed == ["B.HEIC"]


def test_issue_write_is_atomic_no_temp_left(tmp_path: Path) -> None:
    landed = tmp_path / "IMG.HEIC"
    landed.write_bytes(b"x")
    importer._write_issue(landed, _dev(), _obj(), "dev/IMG.HEIC",
                          state="failed", attempts=3, last_error="e")
    assert list(tmp_path.glob("*.__import__")) == []


# --- import loop: download → validate → recover (fake device) ----------------
class _FakeDev:
    """Minimal stand-in for wpd.Device: a static child tree + byte content."""

    def __init__(self, tree: dict[str, list[WpdObject]],
                 content: dict[str, bytes]) -> None:
        self._tree = tree
        self._content = content

    def __enter__(self) -> "_FakeDev":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def children(self, parent_id: str = "DEVICE"):  # noqa: ANN201
        return list(self._tree.get(parent_id, []))

    def stream(self, obj_id: str, chunk: int = 256 * 1024):  # noqa: ANN201
        data = self._content[obj_id]
        for i in range(0, len(data), chunk):
            yield data[i:i + chunk]

    def info(self) -> DeviceInfo:
        return _dev()


def _file_obj(oid: str, name: str, data: bytes) -> WpdObject:
    return _obj(id=oid, orig=name, size=len(data), puid=f"{{P-{oid}}}")


def _scripted_media_check(script: dict[str, list[str | None]]):  # noqa: ANN201
    """A fake media_check driven by a per-filename queue of results."""
    def mc(path: Path) -> str | None:
        seq = script.get(path.name)
        if seq:
            return seq.pop(0)
        return None
    return mc


def _run_loop(
    monkeypatch: "pytest.MonkeyPatch", tmp_path: Path,
    tree: dict[str, list[WpdObject]], content: dict[str, bytes],
    script: dict[str, list[str | None]],
    setup: Callable[[Path], None] | None = None,
) -> "tuple[importer.ImportSummary, Path]":
    landing = tmp_path / "import" / "iPhone"
    landing.mkdir(parents=True, exist_ok=True)
    if setup is not None:
        setup(landing)
    summary = importer.ImportSummary(device=_dev(), landing=landing)
    fake = _FakeDev(tree, content)
    monkeypatch.setattr(importer.wpd, "open_device", lambda _dev_id: fake)
    monkeypatch.setattr(importer, "media_check", _scripted_media_check(script))
    importer._import_loop(tmp_path, _dev(), "iPhone", landing, summary, None)
    return summary, landing


def test_loop_happy_path_single_download(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    obj = _file_obj("o1", "IMG.HEIC", b"good-bytes")
    summary, landing = _run_loop(
        monkeypatch, tmp_path, {"DEVICE": [obj]}, {"o1": b"good-bytes"}, {}
    )
    assert summary.downloaded == 1
    assert summary.verified == 1
    assert summary.recovered == 0
    assert summary.needs_session == [] and summary.failed_media == []
    assert importer._sidecar_path(landing / "IMG.HEIC").exists()


def test_loop_recovers_same_session(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    obj = _file_obj("o1", "IMG.HEIC", b"bytes")
    # First probe fails, the same-session re-download's probe passes.
    summary, landing = _run_loop(
        monkeypatch, tmp_path, {"DEVICE": [obj]}, {"o1": b"bytes"},
        {"IMG.HEIC": ["decode failed: x", None]},
    )
    assert summary.recovered == 1
    assert summary.verified == 1
    assert not importer._issue_path(landing / "IMG.HEIC").exists()
    log = tmp_path / ".pix" / "import-verify.log"
    text = log.read_text(encoding="utf-8")
    assert "media-check-failed" in text and "recovered-same-session" in text


def test_loop_parks_needs_session_after_same_session_fail(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    obj = _file_obj("o1", "IMG.HEIC", b"bytes")
    # Both the initial probe and the same-session re-download fail.
    summary, landing = _run_loop(
        monkeypatch, tmp_path, {"DEVICE": [obj]}, {"o1": b"bytes"},
        {"IMG.HEIC": ["bad-1", "bad-2"]},
    )
    assert summary.verified == 0
    assert summary.needs_session == ["IMG.HEIC"]
    issue = importer._read_issue(importer._issue_path(landing / "IMG.HEIC"))
    assert issue is not None and issue["state"] == "needs-session"
    # Parked this run — NOT retried as a fresh session within the same run.
    assert summary.failed_media == []


def test_loop_fresh_session_retry_recovers(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch", capsys: "pytest.CaptureFixture[str]"
) -> None:
    obj = _file_obj("o1", "IMG.HEIC", b"fresh-good")

    def setup(landing: Path) -> None:
        landed = landing / "IMG.HEIC"
        landed.write_bytes(b"old-bad")
        importer._write_issue(landed, _dev(), obj, "IMG.HEIC",
                              state="needs-session", attempts=1, last_error="bad")

    # Retry's probe passes (script empty → None).
    summary, landing = _run_loop(
        monkeypatch, tmp_path, {"DEVICE": [obj]}, {"o1": b"fresh-good"}, {}, setup
    )
    assert summary.recovered == 1
    assert summary.verified == 1
    assert not importer._issue_path(landing / "IMG.HEIC").exists()
    assert importer._sidecar_path(landing / "IMG.HEIC").exists()
    # Start-of-run reconnect notice printed for the pre-existing needs-session.
    assert "need a device reconnect" in capsys.readouterr().out


def test_loop_fresh_session_retry_goes_terminal(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    obj = _file_obj("o1", "IMG.HEIC", b"still-bad")

    def setup(landing: Path) -> None:
        landed = landing / "IMG.HEIC"
        landed.write_bytes(b"old-bad")
        importer._write_issue(landed, _dev(), obj, "IMG.HEIC",
                              state="needs-session", attempts=1, last_error="bad")

    summary, landing = _run_loop(
        monkeypatch, tmp_path, {"DEVICE": [obj]}, {"o1": b"still-bad"},
        {"IMG.HEIC": ["still bad after reconnect"]}, setup,
    )
    assert summary.verified == 0
    assert summary.failed_media == ["IMG.HEIC"]
    issue = importer._read_issue(importer._issue_path(landing / "IMG.HEIC"))
    assert issue is not None and issue["state"] == "failed"
    assert issue["attempts"] == 2


def test_loop_terminal_failed_is_skipped(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    obj = _file_obj("o1", "IMG.HEIC", b"whatever")

    def setup(landing: Path) -> None:
        landed = landing / "IMG.HEIC"
        landed.write_bytes(b"landed")
        importer._write_issue(landed, _dev(), obj, "IMG.HEIC",
                              state="failed", attempts=2, last_error="dead")

    summary, _ = _run_loop(
        monkeypatch, tmp_path, {"DEVICE": [obj]}, {"o1": b"whatever"}, {}, setup
    )
    # Terminal → never re-downloaded, still reported so the user keeps seeing it.
    assert summary.downloaded == 0
    assert summary.verified == 0
    assert summary.failed_media == ["IMG.HEIC"]
