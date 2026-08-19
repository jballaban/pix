"""Tests for `pix context-menu` — the cascading Explorer right-click menu.

The Windows registry is faked (injected into `sys.modules` so the command's
lazy `import winreg` picks it up), so these run on any platform and never touch
the real registry. They cover action validation, the platform guard, the
cascade build, the install/status/uninstall round-trip, legacy-key sweeping,
and idempotent uninstall.
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
    """In-memory stand-in for the parts of `winreg` the command uses. Keys are
    a flat dict of full-path -> {value_name: value}; children are derived by
    path prefix so EnumKey / recursive delete work like the real thing."""

    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1
    KEY_ALL_ACCESS = 0xF003F

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, str]] = {}

    def CreateKey(self, _root: str, path: str) -> _FakeKey:
        parts = path.split("\\")
        for i in range(1, len(parts) + 1):
            self.keys.setdefault("\\".join(parts[:i]), {})
        return _FakeKey(self, path)

    def OpenKey(
        self, _root: str, path: str, _reserved: int = 0, _access: int = 0
    ) -> _FakeKey:
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

    def _children(self, path: str) -> list[str]:
        prefix = path + "\\"
        kids: set[str] = set()
        for k in self.keys:
            if k.startswith(prefix):
                kids.add(k[len(prefix):].split("\\")[0])
        return sorted(kids)

    def EnumKey(self, key: _FakeKey, index: int) -> str:
        children = self._children(key.path)
        if index >= len(children):
            raise OSError("no more items")
        return children[index]

    def DeleteKey(self, _root: str, path: str) -> None:
        if path not in self.keys:
            raise FileNotFoundError(path)
        if self._children(path):
            raise OSError("key has subkeys")
        del self.keys[path]

    def CloseKey(self, _key: _FakeKey) -> None:
        pass


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


def test_install_builds_cascade(fake_reg: _FakeWinreg) -> None:
    cm.context_menu(action="install")
    launcher = str(cm._launcher_path())

    for root in cm._root_keys():
        # Parent cascade nodes carry MUIVerb + an (empty) SubCommands trigger,
        # and MultiSelectModel=Player so they survive a >15-item selection.
        assert fake_reg.keys[root]["MUIVerb"] == cm._ROOT_LABEL
        assert "SubCommands" in fake_reg.keys[root]
        assert fake_reg.keys[root]["MultiSelectModel"] == "Player"

        for ti, (tag, tag_label) in enumerate(cm._TAGS, start=1):
            tag_key = f"{root}\\shell\\{ti:02d}_{tag}"
            assert fake_reg.keys[tag_key]["MUIVerb"] == tag_label
            assert "SubCommands" in fake_reg.keys[tag_key]
            assert fake_reg.keys[tag_key]["MultiSelectModel"] == "Player"

            for oi, (op, _op_label) in enumerate(cm._OPS, start=1):
                verb_key = f"{tag_key}\\shell\\{oi:02d}_{op}"
                assert fake_reg.keys[verb_key]["MultiSelectModel"] == "Player"
                command = fake_reg.keys[verb_key + r"\command"][""]
                assert launcher in command
                assert f"-Tag {tag}" in command
                assert f"-Op {op}" in command
                assert command.endswith('"%1"')


def test_info_leaf_is_files_only(fake_reg: _FakeWinreg) -> None:
    """The read-only `meta`/Info leaf is added to the files root only, with no
    -Tag, and never to the folders root."""
    cm.context_menu(action="install")
    files_root, folders_root = cm._root_keys()
    launcher = str(cm._launcher_path())

    for vi, (key, label, op) in enumerate(cm._FILE_VERBS, start=len(cm._TAGS) + 3):
        verb_key = f"{files_root}\\shell\\{vi:02d}_{key}"
        assert fake_reg.keys[verb_key]["MUIVerb"] == label
        assert fake_reg.keys[verb_key]["MultiSelectModel"] == "Player"

        command = fake_reg.keys[verb_key + r"\command"][""]
        assert launcher in command
        assert f"-Op {op}" in command
        assert "-Tag " not in command
        assert command.endswith('"%1"')

        # Folders never get this leaf.
        assert f"{folders_root}\\shell\\{vi:02d}_{key}" not in fake_reg.keys


def test_rating_submenu(fake_reg: _FakeWinreg) -> None:
    """A Rating cascade with star leaves (1-5) + Clear exists on both roots.
    Star leaves call `-Op set -Tag rating -Val <n>`; Clear calls
    `-Op clear -Tag rating`."""
    cm.context_menu(action="install")
    launcher = str(cm._launcher_path())
    rating_n = len(cm._TAGS) + 1
    for root in cm._root_keys():
        rating_key = f"{root}\\shell\\{rating_n:02d}_rating"
        assert fake_reg.keys[rating_key]["MUIVerb"] == cm._RATING_LABEL
        assert "SubCommands" in fake_reg.keys[rating_key]
        for si, (stars, label) in enumerate(cm._RATINGS, start=1):
            verb = f"{rating_key}\\shell\\{si:02d}_star{stars}"
            assert fake_reg.keys[verb]["MUIVerb"] == label
            command = fake_reg.keys[verb + r"\command"][""]
            assert launcher in command
            assert "-Op set" in command
            assert "-Tag rating" in command
            assert f"-Val {stars}" in command
        clear = f"{rating_key}\\shell\\{len(cm._RATINGS) + 1:02d}_clear"
        assert fake_reg.keys[clear]["MUIVerb"] == "Clear"
        clear_cmd = fake_reg.keys[clear + r"\command"][""]
        assert "-Op clear" in clear_cmd
        assert "-Tag rating" in clear_cmd


def test_rotate_submenu(fake_reg: _FakeWinreg) -> None:
    """A Rotate cascade with right/left leaves exists on both roots; each leaf
    calls `-Op rotate -Deg <clockwise>` with no -Tag."""
    cm.context_menu(action="install")
    launcher = str(cm._launcher_path())
    rot_n = len(cm._TAGS) + 2
    for root in cm._root_keys():
        rot_key = f"{root}\\shell\\{rot_n:02d}_rotate"
        assert fake_reg.keys[rot_key]["MUIVerb"] == cm._ROTATE_LABEL
        assert "SubCommands" in fake_reg.keys[rot_key]
        for di, (deg, label) in enumerate(cm._ROTATIONS, start=1):
            verb = f"{rot_key}\\shell\\{di:02d}_deg{deg}"
            assert fake_reg.keys[verb]["MUIVerb"] == label
            command = fake_reg.keys[verb + r"\command"][""]
            assert launcher in command
            assert f"-Deg {deg}" in command
            assert "-Op rotate" in command
            assert "-Tag " not in command


def test_install_status_uninstall_round_trip(
    fake_reg: _FakeWinreg, capsys: pytest.CaptureFixture[str]
) -> None:
    cm.context_menu(action="install")
    cm.context_menu(action="status")
    assert "installed for files, folders" in capsys.readouterr().out

    cm.context_menu(action="uninstall")
    # The whole cascade tree is gone (no key path starts with a Pix root).
    assert all(
        not k.startswith(root) for k in fake_reg.keys for root in cm._root_keys()
    )

    cm.context_menu(action="status")
    assert "not installed" in capsys.readouterr().out


def test_install_sweeps_legacy_flat_verb(fake_reg: _FakeWinreg) -> None:
    """A reinstall removes the old single-verb `pixtag` keys."""
    for legacy in cm._LEGACY_KEYS:
        fake_reg.CreateKey(fake_reg.HKEY_CURRENT_USER, legacy + r"\command")
    cm.context_menu(action="install")
    assert all(legacy not in fake_reg.keys for legacy in cm._LEGACY_KEYS)


def test_uninstall_removes_legacy(
    fake_reg: _FakeWinreg, capsys: pytest.CaptureFixture[str]
) -> None:
    for legacy in cm._LEGACY_KEYS:
        fake_reg.CreateKey(fake_reg.HKEY_CURRENT_USER, legacy)
    cm.context_menu(action="uninstall")
    assert all(legacy not in fake_reg.keys for legacy in cm._LEGACY_KEYS)
    assert "Removed" in capsys.readouterr().out


def test_uninstall_when_absent_is_noop(
    fake_reg: _FakeWinreg, capsys: pytest.CaptureFixture[str]
) -> None:
    cm.context_menu(action="uninstall")
    assert "Nothing to remove" in capsys.readouterr().out


def test_status_warns_on_stale_launcher_path(
    fake_reg: _FakeWinreg, capsys: pytest.CaptureFixture[str]
) -> None:
    """A registration pointing at a different launcher path is flagged."""
    cm.context_menu(action="install")
    # Corrupt the first leaf command to reference a different launcher.
    fake_reg.keys[cm._first_leaf_command_key()][""] = (
        r'"powershell.exe" -File "C:\old\pixtag.ps1" -Tag event -Op set "%1"'
    )
    cm.context_menu(action="status")
    assert "does not match the current launcher" in capsys.readouterr().err


def test_launcher_is_packaged() -> None:
    """The launcher ships inside the package so an installed pix can find it."""
    assert cm._launcher_path().is_file()
