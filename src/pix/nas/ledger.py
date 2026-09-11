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
import os
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
    source_roots: tuple[str, ...] = ()


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
    roots_raw: object = data.get("source_roots")
    roots: tuple[str, ...] = ()
    if isinstance(roots_raw, list):
        items = cast("list[object]", roots_raw)
        roots = tuple(r for r in items if isinstance(r, str))
    elif isinstance(roots_raw, str):
        roots = (roots_raw,)
    return LedgerHeader(
        folder=ledger.parent,
        name=name,
        source=source,
        serial=serial if isinstance(serial, str) else None,
        source_roots=roots,
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


def _norm_root(raw: str) -> str:
    """Normalise a source-root string for comparison.

    Raw string equality is too brittle for a check whose failure mode is
    silently skipping files: separators, trailing slashes and (on Windows) case
    all vary without meaning anything.
    """
    norm = str(Path(raw))
    return norm.casefold() if os.name == "nt" else norm


def committed_folder_keys(name: str, source_root: Path) -> set[tuple[str, int]]:
    """Every `(rel, size)` already uploaded under `name` *from this source root*.

    **Scoped by both, and both are load-bearing.** A folder source has no PUID,
    so its key is `(relative path, size)`, which is only unique *within one
    source tree*:

    - **Name** keeps separate sources apart. Two SD cards can each hold
      `DCIM/100MSDCF/DSC00001.JPG` at an identical size; without the name, one
      card's photos would mask the other's.
    - **Source root** keeps separate batches of the *same* source apart. Importing
      `G:\\pix\\2015` then `G:\\pix\\2016` under one name yields `a/x.jpg` for
      both, so year two would silently skip year one's files. Canonical
      date-stamped filenames make a real collision unlikely, but "unlikely to
      silently drop photos" is not a standard worth holding.

    This is why the ledger header records `source_root` — the year lives there,
    as provenance, rather than being smuggled into the folder name.
    """
    require_share()
    root = _norm_root(str(source_root))
    keys: set[tuple[str, int]] = set()
    for header in iter_headers():
        if header.name != name or header.source != "folder":
            continue
        # Header roots are a cheap pre-filter: skip a whole folder without
        # opening its body. A batch that never saw this root cannot hold it.
        if header.source_roots and root not in {
            _norm_root(r) for r in header.source_roots
        }:
            continue
        for entry in iter_entries(header.folder / LEDGER_NAME):
            rel = entry.get("rel")
            size = entry.get("size")
            if not isinstance(rel, str) or not isinstance(size, int):
                continue
            # One staging folder can accumulate from several sources, so the
            # authoritative root is the entry's own.
            entry_root = entry.get("root")
            if isinstance(entry_root, str) and _norm_root(entry_root) != root:
                continue
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
