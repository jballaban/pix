"""Import-ingest pre-pass: move VERIFIED device imports into the library tree.

Runs at the start of `pix migrate` (see `commands/migrate`), mirroring the
`errors/`/`stash/` restore passes — but, unlike those, it moves files into the
tree for the *first* time (import files have no in-tree origin to restore to).
It drains `.pix/local/import/<friendly>/**` files that reached `VERIFIED` (carry
an `.importinfo` sidecar) into a single flat `<root>/incoming/`, carrying each
sidecar alongside so migrate's plan-gen can write provenance
(`pix:OriginalPath`/`pix:ImportId`) and the synthetic event.

Live Photo motion clips — a short `.mov` sharing its stem with a sibling image —
are **dropped** (soft-moved to the run folder, recoverable; the phone still holds
them). Only `VERIFIED` files ingest; `.importissue` (needs-session/failed) and
unprobed files stay in `.pix/local/import/`.

See spec/import.md → Ingestion (migrate pre-pass).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import yaml

from pix.root import local_dir
from pix.timeout import safe_move

INCOMING_DIRNAME: str = "incoming"
_SIDECAR_EXT: str = ".importinfo"
_ISSUE_EXT: str = ".importissue"
# Durable, synced record of ImportIds now in the library — the import manifest's
# "committed half". Recorded here (at ingest, when a file enters the tree) so a
# later `pix import` skips it even after its `.importinfo` sidecar is dropped.
# Survives a `.pix/local` cache wipe; may drift if a library file is later
# deleted (accepted — see spec/import.md delete semantics; a full rescan is the
# authoritative rebuild, deferred).
_COMMITTED_NAME: str = "import-committed.json"

# Image extensions whose same-stem `.mov` sibling marks a Live Photo.
_LIVE_PHOTO_IMAGE_EXTS: frozenset[str] = frozenset(
    {".heic", ".heif", ".jpg", ".jpeg", ".png"}
)
# A paired `.mov` at or under this many seconds is a Live Photo motion clip.
_LIVE_PHOTO_MAX_SECONDS: float = 5.0
_FFPROBE_TIMEOUT: float = 30.0


@dataclass
class IngestSummary:
    ingested: int = 0
    live_photos_dropped: int = 0
    notes: list[str] = field(default_factory=lambda: [])


def incoming_dir(root: Path) -> Path:
    return root / INCOMING_DIRNAME


def import_root(root: Path) -> Path:
    return local_dir(root) / "import"


def should_ingest(root: Path, folder: Path) -> bool:
    """True if `<root>/incoming/` is within the folder being migrated, so
    ingested files land where this run's walk will pick them up."""
    inc = incoming_dir(root).resolve()
    f = folder.resolve()
    return inc == f or f in inc.parents


def _committed_path(root: Path) -> Path:
    return root / ".pix" / _COMMITTED_NAME


def committed_import_ids(root: Path) -> set[str]:
    """The set of ImportIds ('<serial>:<puid>') already in the library."""
    p = _committed_path(root)
    if not p.is_file():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(x) for x in cast("list[object]", data)}


def _record_committed(root: Path, ids: set[str]) -> None:
    if not ids:
        return
    merged = committed_import_ids(root) | ids
    p = _committed_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(merged)), encoding="utf-8")


def _import_id_from_sidecar(sidecar: Path) -> str | None:
    try:
        data = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    d = cast("dict[str, object]", data)
    serial = str(d.get("serial") or "")
    puid = str(d.get("puid") or "")
    return f"{serial}:{puid}" if serial and puid else None


def _sidecar_of(media: Path) -> Path:
    return media.with_name(media.name + _SIDECAR_EXT)


def _is_marker(p: Path) -> bool:
    return p.name.endswith(_SIDECAR_EXT) or p.name.endswith(_ISSUE_EXT)


def _duration_seconds(path: Path) -> float | None:
    """Media duration via ffprobe, or None if ffprobe is absent/errors."""
    ffprobe = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if ffprobe is None:
        return None
    cmd = [
        ffprobe, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nokey=1:noprint_wrappers=1", str(path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            check=False, timeout=_FFPROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def _is_live_photo_mov(mov: Path, image_stems: set[tuple[Path, str]]) -> bool:
    """A `.mov` that shares its stem with a sibling image in the same source
    folder AND is short enough to be a Live Photo motion clip (not a real video).

    `image_stems` (set of `(parent, stem.lower())`) is computed up front from the
    original file set, so an image already moved into `incoming/` doesn't hide
    its clip's pairing."""
    if mov.suffix.lower() != ".mov":
        return False
    if (mov.parent, mov.stem.lower()) not in image_stems:
        return False
    dur = _duration_seconds(mov)
    return dur is not None and dur <= _LIVE_PHOTO_MAX_SECONDS


def _collision_free(dest_dir: Path, name: str, used: set[str]) -> Path:
    """A landing path in `dest_dir` for `name`, suffixed if taken. Transient —
    migrate renames to the canonical date-based name immediately after."""
    candidate = dest_dir / name
    if name.lower() not in used and not candidate.exists():
        used.add(name.lower())
        return candidate
    stem, dot, ext = name.partition(".")
    n = 2
    while True:
        alt = f"{stem}_{n}{dot}{ext}"
        if alt.lower() not in used and not (dest_dir / alt).exists():
            used.add(alt.lower())
            return dest_dir / alt
        n += 1


def run_ingest(root: Path, folder: Path, runs_dir: Path) -> IngestSummary:
    """Drain VERIFIED imports into `<root>/incoming/`. No-op unless
    `<root>/incoming/` is within `folder`. Returns a summary for logging."""
    summary = IngestSummary()
    if not should_ingest(root, folder):
        return summary
    imp = import_root(root)
    if not imp.is_dir():
        return summary

    inc = incoming_dir(root)
    inc.mkdir(parents=True, exist_ok=True)
    used: set[str] = {p.name.lower() for p in inc.iterdir()} if inc.is_dir() else set()
    dropped_dir = runs_dir / "dropped-live-photos"
    new_committed: set[str] = set()

    for friendly_dir in sorted(p for p in imp.iterdir() if p.is_dir()):
        # VERIFIED media = files with an .importinfo sidecar (excludes markers,
        # unprobed stragglers, and .importissue files).
        media = sorted(
            p for p in friendly_dir.rglob("*")
            if p.is_file() and not _is_marker(p) and _sidecar_of(p).is_file()
        )
        # Image stems per source folder, computed BEFORE any move, so a Live
        # Photo's image (moved first) doesn't hide its clip's pairing.
        image_stems: set[tuple[Path, str]] = {
            (p.parent, p.stem.lower())
            for p in media
            if p.suffix.lower() in _LIVE_PHOTO_IMAGE_EXTS
        }
        for src in media:
            sidecar = _sidecar_of(src)
            if _is_live_photo_mov(src, image_stems):
                dropped_dir.mkdir(parents=True, exist_ok=True)
                safe_move(src, _collision_free(dropped_dir, src.name, set()))
                safe_move(sidecar, dropped_dir / (src.name + _SIDECAR_EXT))
                summary.live_photos_dropped += 1
                continue
            import_id = _import_id_from_sidecar(sidecar)  # read before the move
            dest = _collision_free(inc, src.name, used)
            safe_move(src, dest)
            safe_move(sidecar, _sidecar_of(dest))
            summary.ingested += 1
            if import_id:
                new_committed.add(import_id)

    _record_committed(root, new_committed)

    if summary.ingested or summary.live_photos_dropped:
        summary.notes.append(
            f"ingest: {summary.ingested} file(s) → {inc}"
            + (f", {summary.live_photos_dropped} Live Photo clip(s) dropped"
               if summary.live_photos_dropped else "")
        )
    return summary
