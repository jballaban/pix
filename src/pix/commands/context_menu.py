"""Implementation of `pix context-menu` — manage the Windows Explorer entry.

`pix context-menu <install|uninstall|status>` (status by default) registers or
removes a cascading "Pix" right-click menu:

    Pix  >  Event | Date  >  Set value... | Clear
            Rating         >  ★ .. ★★★★★ | Clear   (preset, no prompt)
            Rotate         >  Rotate right | left
    Pix Info                                    (files only, own root)

It writes per-user (`HKCU`) registry keys — no admin needed — for both files
(`*`) and folders (`Directory`). The cascade uses the registry-only nesting
trick: a parent verb with `MUIVerb` + an empty `SubCommands` value makes
Explorer render its `shell` subkey as a submenu, repeated to nest. Each leaf's
`command` points at the packaged launcher `pix/resources/pixtag.ps1` with the
chosen `-Tag`/`-Op`; the launcher (a collation shim) aggregates a multi-select
and calls `pix tag set` / `pix tag clear` (see the script header).

Explorer renders at most `_MENU_NODE_BUDGET` nodes per cascade root, counting
submenu headers as well as every nested leaf; node 17 and beyond are dropped
with no error and nothing for `status` to notice. That budget is per root, so
standalone verbs (`Pix Info`) get their own — which is why they sit beside the
cascade rather than inside it. `_install` asserts the budget up front so an
added submenu fails loudly instead of silently truncating the menu.

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
# Rating submenu: discrete star values (1-5) that set XMP:Rating directly, plus
# a Clear (remove the rating → unrated). Like Rotate, these are preset values —
# each leaf calls pix straight away, no "Set value..." prompt. See spec/tags.md
# → Rating and spec/tag-editing.md.
_RATING_LABEL = "Rating"
_RATINGS: tuple[tuple[int, str], ...] = (
    (1, "★"),
    (2, "★★"),
    (3, "★★★"),
    (4, "★★★★"),
    (5, "★★★★★"),
)
# Rotate submenu: clockwise degrees per direction (run twice for 180).
_ROTATE_LABEL = "Rotate"
_ROTATIONS: tuple[tuple[int, str], ...] = ((90, "Rotate right"), (270, "Rotate left"))
# Standalone top-level verbs shown only on files (not folders), as
# (key, label, op). Each gets its own `...\shell\Pix<Key>` root rather than a
# slot in the cascade: the cascade is already at its 16-node budget, and the
# budget is per root. `meta` is the read-only single-file inspector
# (`pix info meta`).
_FILE_VERBS: tuple[tuple[str, str, str], ...] = (("info", "Info", "meta"),)
# Explorer's hard ceiling on a static-verb cascade: 16 nodes per root, counting
# submenu headers and every nested leaf at any depth. Overflow is dropped
# silently, so `_install` checks the built tree against this.
_MENU_NODE_BUDGET = 16

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


def _command_string(
    op: str,
    tag: str | None = None,
    deg: int | None = None,
    val: int | None = None,
) -> str:
    """The registry `command` for one leaf: launch the hidden collate stage.

    `tag`/`deg`/`val` are included only when relevant (`-Tag` for set/clear,
    `-Deg` for rotate, `-Val` for a preset rating star); `meta` needs none."""
    extra = ""
    if tag is not None:
        extra += f"-Tag {tag} "
    if deg is not None:
        extra += f"-Deg {deg} "
    if val is not None:
        extra += f"-Val {val} "
    return (
        f'"{_powershell_exe()}" -NoProfile -ExecutionPolicy Bypass '
        f'-WindowStyle Hidden -File "{_launcher_path()}" '
        f'{extra}-Op {op} "%1"'
    )


def _root_keys() -> list[str]:
    """The two `...\\shell\\Pix` cascade roots (files + folders)."""
    return [f"{parent}\\{_ROOT_KEY}" for parent, _scope in _SHELL_PARENTS]


def _file_verb_keys() -> list[tuple[str, str, str]]:
    """The standalone files-only verb roots, as (key_path, label, op).

    Siblings of the cascade under `*\\shell`, not children of it — see the
    node-budget note in the module docstring."""
    files_parent = _SHELL_PARENTS[0][0]
    return [
        (
            f"{files_parent}\\{_ROOT_KEY}{key.capitalize()}",
            f"{_ROOT_LABEL} {label}",
            op,
        )
        for key, label, op in _FILE_VERBS
    ]


def _all_root_keys() -> list[str]:
    """Every top-level key this command owns — swept on install, removed on
    uninstall."""
    return _root_keys() + [key for key, _label, _op in _file_verb_keys()]


def _key_exists(key_path: str) -> bool:
    import winreg

    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path))
    except FileNotFoundError:
        return False
    return True


def _cascade_node_count() -> int:
    """Nodes in one cascade root, counted the way Explorer counts them against
    `_MENU_NODE_BUDGET`: every submenu header plus every nested leaf."""
    tags = len(_TAGS) * (1 + len(_OPS))
    rating = 1 + len(_RATINGS) + 1  # header + stars + Clear
    rotate = 1 + len(_ROTATIONS)
    return tags + rating + rotate


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


def _make_verb(
    key_path: str, label: str, command: str, icon: str | None = None
) -> None:
    """A leaf menu item: MUIVerb label + a `command` subkey to run."""
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "MUIVerb", 0, winreg.REG_SZ, label)
        winreg.SetValueEx(key, "MultiSelectModel", 0, winreg.REG_SZ, _MULTISELECT_MODEL)
        if icon is not None:
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon)
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

    # Explorer truncates a cascade past its node budget without a word, so
    # catch an over-budget layout here rather than shipping a menu whose tail
    # items just never appear.
    nodes = _cascade_node_count()
    if nodes > _MENU_NODE_BUDGET:
        _fail(
            f"the '{_ROOT_LABEL}' cascade needs {nodes} menu nodes but Explorer "
            f"renders only {_MENU_NODE_BUDGET} per root, so the last "
            f"{nodes - _MENU_NODE_BUDGET} would silently vanish. Drop a submenu "
            "or move an entry to its own root (see _FILE_VERBS)."
        )
        return

    icon = _powershell_exe()
    # Clean slate so a structural change (or an old flat-verb install) never
    # leaves stale leaves behind.
    for key in _all_root_keys():
        _delete_tree(key)
    for legacy in _LEGACY_KEYS:
        _delete_tree(legacy)

    for root in _root_keys():
        _make_cascade(root, _ROOT_LABEL, icon=icon)
        for ti, (tag, tag_label) in enumerate(_TAGS, start=1):
            tag_key = f"{root}\\shell\\{ti:02d}_{tag}"
            _make_cascade(tag_key, tag_label)
            for oi, (op, op_label) in enumerate(_OPS, start=1):
                leaf = f"{tag_key}\\shell\\{oi:02d}_{op}"
                _make_verb(leaf, op_label, _command_string(op, tag))
        # Rating submenu (star values 1-5 + Clear), numbered right after the
        # tags. Each star leaf sets XMP:Rating directly (preset -Val, no prompt).
        rating_n = len(_TAGS) + 1
        rating_key = f"{root}\\shell\\{rating_n:02d}_rating"
        _make_cascade(rating_key, _RATING_LABEL)
        for si, (stars, label) in enumerate(_RATINGS, start=1):
            leaf = f"{rating_key}\\shell\\{si:02d}_star{stars}"
            _make_verb(leaf, label, _command_string("set", tag="rating", val=stars))
        clear_leaf = f"{rating_key}\\shell\\{len(_RATINGS) + 1:02d}_clear"
        _make_verb(clear_leaf, "Clear", _command_string("clear", tag="rating"))
        # Rotate submenu (right/left), numbered after the tags + Rating.
        rot_n = len(_TAGS) + 2
        rot_key = f"{root}\\shell\\{rot_n:02d}_rotate"
        _make_cascade(rot_key, _ROTATE_LABEL)
        for di, (deg, label) in enumerate(_ROTATIONS, start=1):
            leaf = f"{rot_key}\\shell\\{di:02d}_deg{deg}"
            _make_verb(leaf, label, _command_string("rotate", deg=deg))
    # Files-only standalone verbs (e.g. read-only `meta`), each its own root
    # beside the cascade — the cascade has no node budget left, and a fresh
    # root starts with a full one. Folders skip these.
    for key, label, op in _file_verb_keys():
        _make_verb(key, label, _command_string(op), icon=icon)

    typer.echo(f"Installed the '{_ROOT_LABEL}' menu for files and folders (current user).")
    typer.echo(f"Layout:   {_ROOT_LABEL} > " + " | ".join(t for _t, t in _TAGS)
               + " > " + " | ".join(o for _o, o in _OPS)
               + f"  |  {_RATING_LABEL} > "
               + " | ".join(lbl for _s, lbl in _RATINGS) + " | Clear"
               + f"  |  {_ROTATE_LABEL} > " + " | ".join(l for _d, l in _ROTATIONS)
               + f"   [{nodes}/{_MENU_NODE_BUDGET} menu nodes]")
    typer.echo("Also:     " + ", ".join(lbl for _k, lbl, _o in _file_verb_keys())
               + " (files only, top level)")
    typer.echo(f"Launcher: {launcher}")
    typer.echo(
        "Right-click media files/folders in a pix library to use it. On Windows "
        "11 it's under 'Show more options' (Shift+F10). It acts on the whole "
        "Explorer selection, regardless of count."
    )


def _uninstall() -> None:
    removed = False
    for key in _all_root_keys():
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

    scopes = [
        scope
        for root, (_parent, scope) in zip(_root_keys(), _SHELL_PARENTS)
        if _key_exists(root)
    ]

    if not scopes:
        typer.echo("Context menu: not installed.")
        typer.echo(
            "Run `pix context-menu install` to add the 'Pix' right-click menu."
        )
        return

    typer.echo("Context menu: installed for " + ", ".join(scopes) + ".")
    # The standalone files-only verbs live outside the cascade, so a partial
    # install (or a sweep by an older pix) leaves them missing on their own.
    missing = [
        label for key, label, _op in _file_verb_keys() if not _key_exists(key)
    ]
    if missing:
        typer.echo(
            "Warning: " + ", ".join(missing) + " is not registered. Run "
            "`pix context-menu install` to add it.",
            err=True,
        )
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
