"""`pix2 import folder` — the folder source adapter (spec/nas-app.md §9).

Stages a folder tree for upload: SD cards, a shared folder someone sent you, or
the legacy library during seeding. It is one of two permanent source adapters —
phones are MTP, anything that mounts as a drive letter is a folder.

**It hardlinks; it does not copy.** Source and staging sit on one NTFS volume, so
links are instant and free — and necessary, since there is nowhere with room to
duplicate a 2.5TB library. Every downstream behaviour is unchanged: culling
deletes the link while the `.manifest/` sidecar survives as the skip record, and
`upload` reads through the link none the wiser. A cross-volume source falls back
to a real copy.

Cancel and resume is free: a landed file whose size matches its source is adopted
(its sidecar is written) rather than re-linked, and anything already in the
manifest is skipped outright.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from pix.markers import IMPORT_TMP_SUFFIX, is_pix_marker
from pix.nas import staging as st
from pix.nas.const import IMPORT_ROOT


class FolderImportError(Exception):
    """Import could not start: bad source, or an unusable staging folder."""


@dataclass
class FolderImportSummary:
    """What one `import folder` run did."""

    name: str
    source: Path
    staging: Path
    linked: int = 0        # hardlinked into staging
    copied: int = 0        # cross-volume fallback
    adopted: int = 0       # already landed from a cancelled run; sidecar written
    skipped: int = 0       # already in the manifest
    ignored: int = 0       # companions we never land
    failed: list[str] = field(default_factory=lambda: [])

    @property
    def landed(self) -> int:
        """Files that are now staged and recorded, however they got there."""
        return self.linked + self.copied + self.adopted


def staging_for(name: str) -> Path:
    """The staging folder for `--name`. Accumulates across runs until uploaded."""
    return IMPORT_ROOT / name


def run_folder_import(
    source: Path,
    name: str,
    *,
    echo: Callable[[str], None] = lambda _: None,
) -> FolderImportSummary:
    """Stage every new file under `source` into `IMPORT_ROOT/<name>/`.

    `name` becomes the `{device}` component of the eventual master folder, so
    seeding a year gives `legacy_2015_<upload-time>`.
    """
    source = source.resolve()
    if not source.is_dir():
        raise FolderImportError(f"source is not a folder: {source}")

    staging = staging_for(name)
    if staging.exists() and not staging.is_dir():
        raise FolderImportError(f"staging path exists and is not a folder: {staging}")
    staging.mkdir(parents=True, exist_ok=True)

    swept = st.sweep_temps(staging)
    if swept:
        echo(f"swept {swept} partial write(s) from an interrupted run")

    manifest = st.scan_manifest(staging)
    if manifest:
        echo(f"{len(manifest)} file(s) already staged for '{name}'")

    summary = FolderImportSummary(name=name, source=source, staging=staging)

    for src in _walk(source):
        rel = src.relative_to(source).as_posix()
        if st.is_skippable(src.name) or is_pix_marker(src.name):
            summary.ignored += 1
            continue
        try:
            stat = src.stat()
        except OSError as e:
            summary.failed.append(f"{rel}: {e}")
            continue

        key = st.skip_key(rel, stat.st_size)
        if key in manifest:
            summary.skipped += 1
            continue

        landed = staging / Path(rel)
        try:
            outcome = _land(src, landed, stat.st_size)
        except OSError as e:
            summary.failed.append(f"{rel}: {e}")
            continue

        st.write_sidecar(
            landed,
            name=name,
            source_root=source,
            rel=rel,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
        manifest.add(key)
        if outcome == "linked":
            summary.linked += 1
        elif outcome == "copied":
            summary.copied += 1
        else:
            summary.adopted += 1

    return summary


def _walk(source: Path) -> list[Path]:
    """Every file under `source`, skipping our own `.manifest/` children.

    Sorted so a run is deterministic and its progress legible — seeding walks
    tens of thousands of files and "where did it get to" should be answerable.
    """
    from pix.ingest import MANIFEST_DIRNAME

    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = sorted(d for d in dirnames if d != MANIFEST_DIRNAME)
        base = Path(dirpath)
        found.extend(base / f for f in sorted(filenames))
    return found


def _land(src: Path, landed: Path, size: int) -> Literal["linked", "copied", "adopted"]:
    """Put `src` at `landed`. Returns the summary field to increment.

    Adopts an existing file of matching size rather than re-linking it — that is
    the straggler case from a cancelled run, and re-doing the work would be
    pointless. A size mismatch means the source changed, so the stale landing is
    replaced.
    """
    landed.parent.mkdir(parents=True, exist_ok=True)

    if landed.exists():
        try:
            if landed.stat().st_size == size:
                return "adopted"
        except OSError:
            pass
        landed.unlink()

    try:
        os.link(src, landed)
        return "linked"
    except OSError:
        # Cross-volume (or a filesystem without links): fall back to a real copy,
        # staged through a marker temp so a kill never leaves a plausible partial.
        tmp = landed.with_name(landed.name + IMPORT_TMP_SUFFIX)
        shutil.copy2(src, tmp)
        os.replace(tmp, landed)
        return "copied"
