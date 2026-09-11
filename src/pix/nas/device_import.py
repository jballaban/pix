"""`pix2 import device` — the MTP source adapter (spec/nas-app.md §9).

Reuses `importer.import_loop` unchanged: ~200 lines of drain-as-you-go DFS and
recovery-ladder logic validated against a physical iPhone. Only the plumbing
differs — where files land, and where the two halves of the skip manifest come
from.

What changes from the legacy path:

- **Landing** is `IMPORT_ROOT/<friendly>/`, not a library-relative folder.
- **The committed half** comes from master's ledgers rather than `pix:ImportId`
  tags, because nothing is written to master files any more.
- **The device registry is derived, not stored.** Serial → friendly name comes
  from ledger headers plus pending staging sidecars; there is no `devices.yaml`
  to drift from what the archive actually holds.
- **Seed manifests are gone.** They were a one-off skip list for a retired tool.

The NAS is required, and this fails before touching the device — without the
committed half, "pull what is new" silently becomes "pull everything again",
and the duplicates land in master where nothing removes them.
"""

from __future__ import annotations

from typing import Callable

import typer

from pix import wpd
from pix.importer import (
    ImportError_,
    ImportSummary,
    NeedsDeviceChoice,
    import_loop,
    prompt_device_choice,
    select_device,
    sanitize_component,
)
from pix.nas import ledger
from pix.nas.const import IMPORT_ROOT


def run_device_import(
    *,
    device: str | None = None,
    name: str | None = None,
    echo: Callable[[str], None] = lambda _: None,
) -> ImportSummary:
    """Pull new media off a connected device into staging.

    Interactive by default: a single **known** device is selected automatically,
    anything else lists and asks. A new serial is named once, and that name is
    remembered by being written into the sidecars and, after upload, the ledger
    header — never into a registry file.
    """
    # Before the device is touched: without the committed half this is not a
    # degraded import, it is a different operation.
    ledger.require_share()

    devices = wpd.list_devices()
    known = _known_names()
    try:
        info = select_device(devices, device, known=set(known))
    except NeedsDeviceChoice:
        info = prompt_device_choice(devices)

    serial = info.serial or info.device_id
    friendly = _friendly_for(info, serial, known, name, echo)
    landing = IMPORT_ROOT / friendly
    landing.mkdir(parents=True, exist_ok=True)

    committed = ledger.committed_import_ids(serial)
    if committed:
        echo(f"{len(committed)} object(s) already uploaded from {friendly}")

    summary = ImportSummary(device=info, landing=landing)
    import_loop(
        info, friendly, landing, summary, None,
        seed=set(),                       # retired: seed manifests are gone
        committed=committed,
        # The ledger is the durable record; a separate verify log would be a
        # second, weaker account of the same events.
        log_verify=lambda device_path, event, detail: None,
    )
    return summary


def _known_names() -> dict[str, str]:
    """Serial → friendly, from master's ledgers plus pending staging.

    Both halves matter. Master knows every device ever uploaded; staging knows
    the one imported an hour ago and not yet uploaded. Without staging, a second
    import before uploading would ask for a name already given — and a different
    answer would split one phone across two staging folders.
    """
    names = dict(ledger.known_devices())
    for serial, friendly in ledger.pending_devices(IMPORT_ROOT).items():
        names.setdefault(serial, friendly)
    return names


def _friendly_for(info: wpd.DeviceInfo, serial: str, known: dict[str, str],
                  name: str | None, echo: Callable[[str], None]) -> str:
    """Resolve this device's staging folder name, prompting only when new."""
    if name:
        return sanitize_component(name)
    if serial in known:
        return known[serial]

    default = sanitize_component(info.friendly or info.model or serial)
    try:
        answer = typer.prompt(f"New device (serial {serial}). Name it",
                              default=default)
    except (EOFError, OSError):
        echo(f"non-interactive; naming this device {default}")
        return default
    friendly = sanitize_component(answer or default)

    # Two serials must never share a staging folder: their files would mix and
    # one device's manifest would mask the other's.
    if friendly in known.values():
        friendly = f"{friendly}-{sanitize_component(serial)[:8]}"
    return friendly


__all__ = ["ImportError_", "ImportSummary", "run_device_import"]
