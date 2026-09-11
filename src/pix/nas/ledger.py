"""The committed half of the skip manifest — master's `.import.jsonl` ledgers.

Each master folder carries a ledger whose **first line is a header** describing
the source, followed by one line per object that batch pulled. The header is what
makes this cheap: to answer "what has this source already given me?" you read the
first line of every ledger and only open the bodies of the ones that match.

It is also what makes the known-device registry *derivable* rather than stored —
a `devices.yaml` could drift from reality, a header cannot (spec/nas-app.md §9).

**The NAS is required.** Without the committed half `import` is not a degraded
operation, it is a different one: "pull what is new" silently becomes "pull
everything again", and the duplicates land in master where nothing removes them.
Failing is strictly better, and failing *before* the source is touched is better
still.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, cast

from pix.nas.const import LEDGER_NAME, MASTER_DIR, MASTER_SHARE
from pix.nas.staging import skip_key


class NasUnreachable(Exception):
    """The master share could not be reached, so the committed half is unknown."""


@dataclass(frozen=True)
class LedgerHeader:
    """The first line of a ledger: which source produced this master folder."""

    folder: Path
    name: str
    source: str              # "device" | "folder"
    serial: str | None = None
    source_root: str | None = None


def require_share() -> None:
    """Fail unless the master share is reachable.

    Distinguishes infrastructure from data: the **share** must exist (a missing
    one means the NAS is down, unmounted, or the path is wrong), while a missing
    `master/` inside it merely means nothing has been uploaded yet. Conflating
    the two would let a typo'd path read as an empty archive, which is exactly
    how you end up re-importing everything.
    """
    try:
        reachable = MASTER_SHARE.is_dir()
    except OSError as e:
        raise NasUnreachable(f"cannot reach {MASTER_SHARE}: {e}") from e
    if not reachable:
        raise NasUnreachable(
            f"cannot reach {MASTER_SHARE}. Import needs the committed manifest "
            f"to skip what is already uploaded; without it every file would be "
            f"imported again."
        )


def iter_headers() -> Iterator[LedgerHeader]:
    """Yield one header per master folder, reading only each ledger's first line."""
    if not MASTER_DIR.is_dir():
        return
    for folder in sorted(p for p in MASTER_DIR.iterdir() if p.is_dir()):
        header = read_header(folder / LEDGER_NAME)
        if header is not None:
            yield header


def read_header(ledger: Path) -> LedgerHeader | None:
    """Parse a ledger's header line, or None if absent or malformed."""
    try:
        with ledger.open("r", encoding="utf-8") as f:
            first = f.readline()
    except OSError:
        return None
    if not first.strip():
        return None
    try:
        parsed: object = json.loads(first)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    data = cast("dict[str, Any]", parsed)
    name = data.get("name")
    source = data.get("source")
    if not isinstance(name, str) or not isinstance(source, str):
        return None
    serial = data.get("serial")
    root = data.get("source_root")
    return LedgerHeader(
        folder=ledger.parent,
        name=name,
        source=source,
        serial=serial if isinstance(serial, str) else None,
        source_root=root if isinstance(root, str) else None,
    )


def iter_entries(ledger: Path) -> Iterator[dict[str, Any]]:
    """Yield the per-object lines of a ledger, skipping its header."""
    try:
        with ledger.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f):
                if lineno == 0 or not line.strip():
                    continue
                try:
                    parsed: object = json.loads(line)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    yield cast("dict[str, Any]", parsed)
    except OSError:
        return


def committed_folder_keys(name: str) -> set[tuple[str, int]]:
    """Every `(rel, size)` already uploaded under `name`.

    **Scoped by name, deliberately.** A folder source has no PUID, so its key is
    `(relative path, size)` — and two different SD cards can both hold
    `DCIM/100MSDCF/DSC00001.JPG` at an identical size. Scoping to the name the
    user gave (which is also the master folder's prefix) keeps those namespaces
    apart, so one card's photos can never mask another's.
    """
    require_share()
    keys: set[tuple[str, int]] = set()
    for header in iter_headers():
        if header.name != name or header.source != "folder":
            continue
        for entry in iter_entries(header.folder / LEDGER_NAME):
            rel = entry.get("rel")
            size = entry.get("size")
            if isinstance(rel, str) and isinstance(size, int):
                keys.add(skip_key(rel, size))
    return keys


def known_devices() -> dict[str, str]:
    """Serial → friendly name, derived from ledger headers.

    There is no registry file: a stored one could drift from what master actually
    holds, and this cannot.
    """
    return {
        h.serial: h.name
        for h in iter_headers()
        if h.source == "device" and h.serial
    }
