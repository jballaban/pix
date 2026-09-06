"""Per-distribution export manifests (`.pix/local/exports/<name>.json`).

An [export](../../spec/export.md) reconcile has to know what it already put
in the delivery target without re-hashing a network-backed tree every run.
The manifest is that record: one row per provisioned member, holding

- the **source content hash** — which master file this copy is, so a changed
  source is detected from the (already cached) library-side hash alone;
- the target's **size + mtime_ns as written** — the cheap fingerprint that
  makes verification one `stat` per member, no bytes read back. It's the
  same `(size, mtime_ns)` key `pix.cache_db` uses for the library, on
  purpose.

Two invariants ride on this file:

1. **Export only touches paths the manifest records.** Anything else in the
   target is foreign — reported, never modified, never deleted. That's what
   makes removal safe even when `path:` points somewhere unexpected.
2. **The recorded `target` must still match the distribution's configured
   `path`.** A repointed distribution means the manifest describes a
   different folder, so it can't be trusted to say what we own there.

It lives under `.pix/local/` because it's machine-local, recomputable state
that must never sync (same treatment as `cache.db` — see
spec/implementation.md → Cache store). Losing it isn't fatal: export falls
back to adopting the target by hash.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pix.root import local_dir

# Subfolder of `.pix/local/` holding one JSON manifest per distribution.
EXPORTS_DIRNAME: str = "exports"

# Bumped only if the on-disk shape changes incompatibly; an unreadable or
# unknown-format manifest is treated as absent (adopt-by-hash), never as an
# error — it's recomputable state.
FORMAT: int = 1


@dataclass(frozen=True)
class Member:
    """One provisioned file, as `export` last wrote it."""

    source_hash: str
    size: int
    mtime_ns: int

    def matches(self, st: os.stat_result) -> bool:
        """Is the file on disk still byte-identical to what we wrote?

        Cheap proxy — same `(size, mtime_ns)` test the metadata cache uses.
        A sync client that rewrites a file changes both.
        """
        return st.st_size == self.size and st.st_mtime_ns == self.mtime_ns


@dataclass(frozen=True)
class Manifest:
    """What a distribution has provisioned into its target."""

    distribution: str
    target: str
    members: dict[str, Member]  # target relpath (forward slashes) -> member


def exports_dir(library_root: Path) -> Path:
    """`<library>/.pix/local/exports/` — machine-local, never synced."""
    return local_dir(library_root) / EXPORTS_DIRNAME


def manifest_path(library_root: Path, name: str) -> Path:
    """Manifest file for one distribution (name charset enforced by config)."""
    return exports_dir(library_root) / f"{name}.json"


def load(library_root: Path, name: str) -> Manifest | None:
    """Read a distribution's manifest; None when absent or unreadable.

    Unreadable is deliberately not an error: the manifest is recomputable,
    and a corrupt one should degrade to adopt-by-hash rather than block a
    provision.
    """
    path = manifest_path(library_root, name)
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    data = cast("dict[str, object]", raw)
    if data.get("format") != FORMAT:
        return None

    target = data.get("target")
    raw_members = data.get("members")
    if not isinstance(target, str) or not isinstance(raw_members, dict):
        return None

    members: dict[str, Member] = {}
    for key, value in cast("dict[object, object]", raw_members).items():
        if not isinstance(key, str) or not isinstance(value, dict):
            return None
        row = cast("dict[str, object]", value)
        source_hash = row.get("hash")
        size = row.get("size")
        mtime_ns = row.get("mtime_ns")
        if (
            not isinstance(source_hash, str)
            or not isinstance(size, int)
            or not isinstance(mtime_ns, int)
        ):
            return None
        members[key] = Member(
            source_hash=source_hash, size=size, mtime_ns=mtime_ns
        )

    return Manifest(distribution=name, target=target, members=members)


def save(library_root: Path, manifest: Manifest) -> None:
    """Write a manifest atomically (temp + replace).

    Atomic because a half-written manifest after a CTRL+C would understate
    what we own, and anything it forgets becomes foreign on the next run.
    """
    path = manifest_path(library_root, manifest.distribution)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": FORMAT,
        "distribution": manifest.distribution,
        "target": manifest.target,
        "members": {
            rel: {
                "hash": m.source_hash,
                "size": m.size,
                "mtime_ns": m.mtime_ns,
            }
            for rel, m in sorted(manifest.members.items())
        },
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, indent=1, sort_keys=False), encoding="utf-8"
    )
    os.replace(tmp, path)


def discard(library_root: Path, name: str) -> bool:
    """Delete a distribution's manifest. True if one was there."""
    path = manifest_path(library_root, name)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
