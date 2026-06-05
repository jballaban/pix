# Explorer integration — "Tag with pix"

Right-click selected files **and/or folders** in Windows Explorer and tag them
in one shot, without typing paths. A thin shell wrapper around
[`pix set` / `pix clear`](../src/pix/commands/set.py).

## Install

```powershell
pwsh tools\install-pixtag.ps1     # or: powershell -File tools\install-pixtag.ps1
```

This writes per-user (`HKCU`) registry keys — **no admin needed** — adding a
**Tag with pix** entry to the right-click menu for both files and folders.
Re-run any time to refresh; it's idempotent. Custom label:

```powershell
powershell -File tools\install-pixtag.ps1 -Label "pix: tag"
```

Uninstall:

```powershell
powershell -File tools\uninstall-pixtag.ps1
```

## Use

1. Select any mix of media files and/or folders inside a pix library.
2. Right-click → **Tag with pix**.
3. A console asks for the **tag** (`event` or `date`) and a **value**:
   - **event** — the event name. Blank **clears** the override.
   - **date** — a `YYYY-MM-DD-HH:MM:SS` pattern with `*` for unpinned parts
     (e.g. `2022-*-*-*:*:*` to pin just the year). Blank **clears** it.
4. `pix` prints its plan and asks `Apply? [Y/e/n]` in the same window — nothing
   is written until you confirm.
5. Run `pix organize` afterward to reshape the library to match.

Folders expand to the taggable media inside them (pix decides what's taggable
via its `EXTENSION_POLICY`), so dropping a whole `2023\Hawaii` folder works.

## How multi-select works (the collation shim)

The classic registry context menu invokes its command **once per selected
item**. `pixtag.ps1` therefore runs in two stages:

- **Collate (hidden):** each per-item process appends its path to a shared
  pending list and races to become "leader". Non-leaders exit silently (hidden
  window — no flashing). The leader waits for the burst to settle, snapshots the
  full selection, then relaunches itself visibly.
- **Run (visible):** the leader reads the collected paths, prompts once, and
  calls `pix set` / `pix clear` with the whole list.

This is the COM-free way to aggregate a multi-select with a pure-registry menu.
It's mildly timing-based (a ~400 ms quiet-period debounce); for a single-user
tool that's a fine trade for not shipping a compiled shell extension.

## Requirements

- `pix` must be on `PATH` (the launcher resolves it via `Get-Command pix`).
- Windows PowerShell 5.1 (`powershell.exe`, present on every Windows install) —
  no PowerShell 7 dependency.
