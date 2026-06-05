"""Implementation of `pix context-menu` — manage the Windows Explorer entry.

`pix context-menu <install|uninstall|status>` (status by default) registers or
removes the "Tag with pix" right-click menu. It writes per-user (`HKCU`)
registry keys — no admin needed — for both files (`*`) and folders
(`Directory`), pointing Explorer at the packaged launcher
`pix/resources/pixtag.ps1`. That launcher is the collation shim that
aggregates a multi-select and calls `pix set` / `pix clear` (see the script's
header for the two-stage design).

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

# The two classic-menu roots: files (`*`) and folders (`Directory`). Both get
# the same verb so a mixed selection of files and folders fires it.
_MENU_KEYS: tuple[tuple[str, str], ...] = (
    (r"Software\Classes\*\shell\pixtag", "files"),
    (r"Software\Classes\Directory\shell\pixtag", "folders"),
)
_LABEL = "Tag with pix"
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


def _command_string() -> str:
    """The registry `command` value: launch the hidden collate stage on `%1`."""
    return (
        f'"{_powershell_exe()}" -NoProfile -ExecutionPolicy Bypass '
        f'-WindowStyle Hidden -File "{_launcher_path()}" "%1"'
    )


def context_menu(action: str = "status") -> None:
    """Install/uninstall/report the Explorer "Tag with pix" context menu."""
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


def _install() -> None:
    import winreg

    launcher = _launcher_path()
    if not launcher.is_file():
        _fail(f"launcher not found at {launcher}; reinstall pix and try again.")
        return

    command = _command_string()
    icon = _powershell_exe()
    for key_path, _scope in _MENU_KEYS:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, _LABEL)
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon)
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, key_path + r"\command"
        ) as cmd_key:
            winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, command)

    typer.echo(f"Installed '{_LABEL}' for files and folders (current user).")
    typer.echo(f"Launcher: {launcher}")
    typer.echo(
        "Right-click media files/folders in a pix library and choose the entry."
    )


def _uninstall() -> None:
    import winreg

    removed = False
    for key_path, _scope in _MENU_KEYS:
        # DeleteKey requires the key to have no subkeys, so remove `command`
        # (the leaf) before its parent. Missing keys are fine — idempotent.
        for sub in (key_path + r"\command", key_path):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
                removed = True
            except FileNotFoundError:
                pass

    if removed:
        typer.echo(f"Removed the '{_LABEL}' context menu (current user).")
    else:
        typer.echo("Nothing to remove — the context menu was not installed.")


def _status() -> None:
    import winreg

    found: list[tuple[str, str]] = []
    for key_path, scope in _MENU_KEYS:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, key_path + r"\command"
            ) as key:
                cmd, _ = winreg.QueryValueEx(key, "")
            found.append((scope, str(cmd)))
        except FileNotFoundError:
            pass

    if not found:
        typer.echo("Context menu: not installed.")
        typer.echo(
            "Run `pix context-menu install` to add the 'Tag with pix' "
            "right-click entry."
        )
        return

    typer.echo(
        "Context menu: installed for " + ", ".join(s for s, _ in found) + "."
    )
    launcher = _launcher_path()
    typer.echo(f"Launcher: {launcher}")
    # Flag a stale registration whose command no longer points at this build's
    # launcher (e.g. pix was reinstalled to a different location).
    if any(str(launcher) not in cmd for _s, cmd in found):
        typer.echo(
            "Warning: the registered command does not match the current "
            "launcher path. Run `pix context-menu install` to refresh it.",
            err=True,
        )
