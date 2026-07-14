"""Implementation of `pix context-menu` — manage the Windows Explorer entry.

`pix context-menu <install|uninstall|status>` (status by default) registers or
removes a cascading "Pix" right-click menu:

    Pix  >  Event | Date  >  Set value... | Clear

It writes per-user (`HKCU`) registry keys — no admin needed — for both files
(`*`) and folders (`Directory`). The cascade uses the registry-only nesting
trick: a parent verb with `MUIVerb` + an empty `SubCommands` value makes
Explorer render its `shell` subkey as a submenu, repeated to nest. Each leaf's
`command` points at the packaged launcher `pix/resources/pixtag.ps1` with the
chosen `-Tag`/`-Op`; the launcher (a collation shim) aggregates a multi-select
and calls `pix set` / `pix clear` (see the script header).

Windows-only: the registry + Explorer integration has no meaning elsewhere, so
the command refuses up front on other platforms. `winreg` is imported lazily,
after the platform guard, so importing this module never fails off-Windows.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer

import pix
from pix import banner

# Data-driven cascade: adding a tag or op is a one-line change here.
_ROOT_LABEL = "Pix"
_ROOT_KEY = "Pix"
# Without this, Explorer hides a static verb once >15 items are selected
# (the "Document" default). "Player" raises the cap for a legacy (registry,
# %1) verb to 100 items, while keeping the same per-item invocation our
# collation shim relies on. Beyond 100 would need a COM handler.
_MULTISELECT_MODEL = "Player"
_TAGS: tuple[tuple[str, str], ...] = (("event", "Event"), ("date", "Date"))
_OPS: tuple[tuple[str, str], ...] = (("set", "Set value..."), ("clear", "Clear"))
# Rotate submenu: clockwise degrees per direction (run twice for 180).
_ROTATE_LABEL = "Rotate"
_ROTATIONS: tuple[tuple[int, str], ...] = ((90, "Rotate right"), (270, "Rotate left"))
# Top-level leaves shown only on files (not folders), as (key, label, op).
# `meta` is the read-only single-file inspector (`pix info meta`).
_FILE_VERBS: tuple[tuple[str, str, str], ...] = (("info", "Info", "meta"),)

# Parents under which the "Pix" cascade lives (files `*` and folders).
_SHELL_PARENTS: tuple[tuple[str, str], ...] = (
    (r"Software\Classes\*\shell", "files"),
    (r"Software\Classes\Directory\shell", "folders"),
)
# Single-verb keys from earlier pix versions — swept on (re)install/uninstall.
_LEGACY_KEYS: tuple[str, ...] = (
    r"Software\Classes\*\shell\pixtag",
    r"Software\Classes\Directory\shell\pixtag",
)
_ACTIONS = ("install", "uninstall", "status")


def _fail(msg: str) -> None:
    typer.echo(f"Error: {msg}", err=True)
    raise typer.Exit(code=1)


def _launcher_path() -> Path:
    """Absolute path to the packaged Explorer launcher script."""
    return Path(pix.__file__).parent / "resources" / "pixtag.ps1"


def _powershell_exe() -> str:
    """Windows PowerShell 5.1, present on every Windows install."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = (
        Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    return str(candidate) if candidate.is_file() else "powershell.exe"


def _command_string(op: str, tag: str | None = None, deg: int | None = None) -> str:
    """The registry `command` for one leaf: launch the hidden collate stage.

    `tag`/`deg` are included only when relevant (`-Tag` for set/clear, `-Deg`
    for rotate); `meta` needs neither."""
    extra = ""
    if tag is not None:
        extra += f"-Tag {tag} "
    if deg is not None:
        extra += f"-Deg {deg} "
    return (
        f'"{_powershell_exe()}" -NoProfile -ExecutionPolicy Bypass '
        f'-WindowStyle Hidden -File "{_launcher_path()}" '
        f'{extra}-Op {op} "%1"'
    )


def _root_keys() -> list[str]:
    """The two `...\\shell\\Pix` cascade roots (files + folders)."""
    return [f"{parent}\\{_ROOT_KEY}" for parent, _scope in _SHELL_PARENTS]


def _first_leaf_command_key() -> str:
    """A deterministic leaf `command` path, used by status to check drift."""
    parent = _SHELL_PARENTS[0][0]
    tag = _TAGS[0][0]
    op = _OPS[0][0]
    return f"{parent}\\{_ROOT_KEY}\\shell\\01_{tag}\\shell\\01_{op}\\command"


def context_menu(action: str = "status") -> None:
    """Install/uninstall/report the cascading Explorer "Pix" context menu."""
    banner()
    action = action.lower()
    if action not in _ACTIONS:
        _fail(f"unknown action {action!r}; expected one of: {', '.join(_ACTIONS)}.")
        return
    if sys.platform != "win32":
        _fail(
            "the context menu is a Windows-only feature (it edits the Windows "
            "registry and hooks Explorer)."
        )
        return

    if action == "install":
        _install()
    elif action == "uninstall":
        _uninstall()
    else:
        _status()


def _make_cascade(key_path: str, label: str, icon: str | None = None) -> None:
    """A parent menu node: MUIVerb label + empty SubCommands triggers Explorer
    to render this key's `shell` subkey as a cascading submenu."""
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "MUIVerb", 0, winreg.REG_SZ, label)
        winreg.SetValueEx(key, "SubCommands", 0, winreg.REG_SZ, "")
        winreg.SetValueEx(key, "MultiSelectModel", 0, winreg.REG_SZ, _MULTISELECT_MODEL)
        if icon is not None:
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon)


def _make_verb(key_path: str, label: str, command: str) -> None:
    """A leaf menu item: MUIVerb label + a `command` subkey to run."""
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "MUIVerb", 0, winreg.REG_SZ, label)
        winreg.SetValueEx(key, "MultiSelectModel", 0, winreg.REG_SZ, _MULTISELECT_MODEL)
    with winreg.CreateKey(
        winreg.HKEY_CURRENT_USER, key_path + r"\command"
    ) as cmd_key:
        winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, command)


def _delete_tree(key_path: str) -> bool:
    """Recursively delete an HKCU key and all subkeys. Returns True if the key
    existed. `winreg.DeleteKey` only removes childless keys, so recurse first."""
    import winreg

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS
        )
    except FileNotFoundError:
        return False
    try:
        while True:
            try:
                child = winreg.EnumKey(key, 0)
            except OSError:
                break  # no more subkeys
            _delete_tree(key_path + "\\" + child)
    finally:
        winreg.CloseKey(key)
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
    return True


def _install() -> None:
    launcher = _launcher_path()
    if not launcher.is_file():
        _fail(f"launcher not found at {launcher}; reinstall pix and try again.")
        return

    icon = _powershell_exe()
    # Clean slate so a structural change (or an old flat-verb install) never
    # leaves stale leaves behind.
    for key in _root_keys():
        _delete_tree(key)
    for legacy in _LEGACY_KEYS:
        _delete_tree(legacy)

    files_root = _root_keys()[0]
    for root in _root_keys():
        _make_cascade(root, _ROOT_LABEL, icon=icon)
        for ti, (tag, tag_label) in enumerate(_TAGS, start=1):
            tag_key = f"{root}\\shell\\{ti:02d}_{tag}"
            _make_cascade(tag_key, tag_label)
            for oi, (op, op_label) in enumerate(_OPS, start=1):
                leaf = f"{tag_key}\\shell\\{oi:02d}_{op}"
                _make_verb(leaf, op_label, _command_string(op, tag))
        # Rotate submenu (right/left), numbered after the tags.
        rot_n = len(_TAGS) + 1
        rot_key = f"{root}\\shell\\{rot_n:02d}_rotate"
        _make_cascade(rot_key, _ROTATE_LABEL)
        for di, (deg, label) in enumerate(_ROTATIONS, start=1):
            leaf = f"{rot_key}\\shell\\{di:02d}_deg{deg}"
            _make_verb(leaf, label, _command_string("rotate", deg=deg))
        # File-only top-level leaves (e.g. read-only `meta`) — folders skip
        # these. Numbered after the tag submenus + Rotate.
        if root == files_root:
            for vi, (key, label, op) in enumerate(
                _FILE_VERBS, start=len(_TAGS) + 2
            ):
                leaf = f"{root}\\shell\\{vi:02d}_{key}"
                _make_verb(leaf, label, _command_string(op))

    typer.echo(f"Installed the '{_ROOT_LABEL}' menu for files and folders (current user).")
    typer.echo(f"Layout:   {_ROOT_LABEL} > " + " | ".join(t for _t, t in _TAGS)
               + " > " + " | ".join(o for _o, o in _OPS)
               + f"  |  {_ROTATE_LABEL} > " + " | ".join(l for _d, l in _ROTATIONS)
               + "   (+ " + ", ".join(lbl for _k, lbl, _o in _FILE_VERBS)
               + " on files)")
    typer.echo(f"Launcher: {launcher}")
    typer.echo(
        "Right-click media files/folders in a pix library to use it. On Windows "
        "11 it's under 'Show more options' (Shift+F10). It acts on the whole "
        "Explorer selection, regardless of count."
    )


def _uninstall() -> None:
    removed = False
    for key in _root_keys():
        if _delete_tree(key):
            removed = True
    for legacy in _LEGACY_KEYS:
        if _delete_tree(legacy):
            removed = True

    if removed:
        typer.echo(f"Removed the '{_ROOT_LABEL}' context menu (current user).")
    else:
        typer.echo("Nothing to remove — the context menu was not installed.")


def _status() -> None:
    import winreg

    scopes: list[str] = []
    for root, (_parent, scope) in zip(_root_keys(), _SHELL_PARENTS):
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, root))
            scopes.append(scope)
        except FileNotFoundError:
            pass

    if not scopes:
        typer.echo("Context menu: not installed.")
        typer.echo(
            "Run `pix context-menu install` to add the 'Pix' right-click menu."
        )
        return

    typer.echo("Context menu: installed for " + ", ".join(scopes) + ".")
    launcher = _launcher_path()
    typer.echo(f"Launcher: {launcher}")

    # Flag a stale registration whose command no longer points at this build's
    # launcher (e.g. pix was reinstalled to a different location).
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _first_leaf_command_key()
        ) as key:
            cmd, _ = winreg.QueryValueEx(key, "")
        if str(launcher) not in str(cmd):
            typer.echo(
                "Warning: the registered command does not match the current "
                "launcher path. Run `pix context-menu install` to refresh it.",
                err=True,
            )
    except FileNotFoundError:
        pass
