"""Tests for `pix context-menu` — the Explorer right-click registration.

The Windows registry is faked (injected into `sys.modules` so the command's
lazy `import winreg` picks it up), so these run on any platform and never touch
the real registry. They cover action validation, the platform guard, the
install/status/uninstall round-trip, and idempotent uninstall.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false

import sys

import pytest
import typer

import pix.commands.context_menu as cm


class _FakeKey:
    def __init__(self, store: "_FakeWinreg", path: str) -> None:
        self.store = store
        self.path = path

    def __enter__(self) -> "_FakeKey":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeWinreg:
    """Minimal in-memory stand-in for the parts of `winreg` we use."""

    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, str]] = {}

    def CreateKey(self, _root: str, path: str) -> _FakeKey:
        self.keys.setdefault(path, {})
        return _FakeKey(self, path)

    def OpenKey(self, _root: str, path: str) -> _FakeKey:
        if path not in self.keys:
            raise FileNotFoundError(path)
        return _FakeKey(self, path)

    def SetValueEx(
        self, key: _FakeKey, name: str, _reserved: int, _typ: int, value: str
    ) -> None:
        self.keys[key.path][name] = value

    def QueryValueEx(self, key: _FakeKey, name: str) -> tuple[str, int]:
        if name not in self.keys[key.path]:
            raise FileNotFoundError(name)
        return (self.keys[key.path][name], self.REG_SZ)

    def DeleteKey(self, _root: str, path: str) -> None:
        if path not in self.keys:
            raise FileNotFoundError(path)
        del self.keys[path]


@pytest.fixture
def fake_reg(monkeypatch: pytest.MonkeyPatch) -> _FakeWinreg:
    reg = _FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", reg)
    monkeypatch.setattr(sys, "platform", "win32")
    return reg


def test_rejects_unknown_action() -> None:
    with pytest.raises(typer.Exit):
        cm.context_menu(action="frobnicate")


def test_rejects_non_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(typer.Exit):
        cm.context_menu(action="install")


def test_install_writes_both_roots(fake_reg: _FakeWinreg) -> None:
    cm.context_menu(action="install")
    launcher = str(cm._launcher_path())
    for key_path, _scope in cm._MENU_KEYS:
        assert fake_reg.keys[key_path][""] == cm._LABEL
        cmd = fake_reg.keys[key_path + r"\command"][""]
        assert launcher in cmd
        assert "%1" in cmd


def test_install_status_uninstall_round_trip(
    fake_reg: _FakeWinreg, capsys: pytest.CaptureFixture[str]
) -> None:
    cm.context_menu(action="install")
    cm.context_menu(action="status")
    assert "installed for files, folders" in capsys.readouterr().out

    cm.context_menu(action="uninstall")
    assert all(k not in fake_reg.keys for k, _ in cm._MENU_KEYS)

    cm.context_menu(action="status")
    assert "not installed" in capsys.readouterr().out


def test_uninstall_when_absent_is_noop(
    fake_reg: _FakeWinreg, capsys: pytest.CaptureFixture[str]
) -> None:
    cm.context_menu(action="uninstall")
    assert "Nothing to remove" in capsys.readouterr().out


def test_status_warns_on_stale_launcher_path(
    fake_reg: _FakeWinreg, capsys: pytest.CaptureFixture[str]
) -> None:
    """A registration pointing at a different launcher path is flagged."""
    for key_path, _scope in cm._MENU_KEYS:
        fake_reg.keys[key_path + r"\command"] = {
            "": r'"powershell.exe" -File "C:\old\pixtag.ps1" "%1"'
        }
    cm.context_menu(action="status")
    assert "does not match the current launcher" in capsys.readouterr().err


def test_launcher_is_packaged() -> None:
    """The launcher ships inside the package so an installed pix can find it."""
    assert cm._launcher_path().is_file()
