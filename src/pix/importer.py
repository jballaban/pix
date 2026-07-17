"""The `pix import` device→disk loop (see spec/import.md).

Pulls new photos/videos off a connected phone and lands them, verified, under
`.pix/local/import/<friendly-name>/`. It does **nothing else** — no convert,
tag, dedupe, or organize; the (not-yet-built) migrate ingest pre-pass consumes
the landed files later. Verification is device→disk only: a file is `VERIFIED`
only when an independent second read agrees, at which point its `.importinfo`
sidecar is written (sidecar presence *is* the VERIFIED marker).

The traversal is a drain-as-you-go DFS (files before subfolders) re-run in a
loop until a pass is "clean" (nothing downloaded); the same pass does discovery
(catching objects MTP reveals lazily), verification, and resume. Termination is
file-level: an object that fails N times is `FAILED`, reported, and skipped.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, IO, cast

import yaml
from blake3 import blake3

from pix import wpd
from pix.markers import IMPORT_TMP_SUFFIX
from pix.progress import LiveProgress
from pix.root import local_dir

# Non-media companions imported never lands (explicit denylist; everything else,
# incl. unknown extensions, lands faithfully). `.aae` = Apple edit sidecars.
_SKIP_EXTENSIONS: frozenset[str] = frozenset({".aae"})

# Per-object attempt cap before FAILED (size mismatch, read-back failure, or two
# device reads that never agree). Run-state only — a later run re-attempts.
_MAX_ATTEMPTS: int = 3

# Backstop against a pathological non-converging loop; the per-file cap is what
# normally ends it.
_MAX_PASSES: int = 100

_SIDECAR_EXT: str = ".importinfo"
_CHUNK: int = 256 * 1024


@dataclass
class ImportSummary:
    device: wpd.DeviceInfo
    landing: Path
    downloaded: int = 0
    verified: int = 0
    skipped: int = 0
    failed: list[str] = field(default_factory=lambda: [])
    bytes_downloaded: int = 0
    passes: int = 0
    apply_log: Path | None = None
    device_lost: bool = False


class ImportError_(Exception):
    """A fatal import setup error (device selection, WPD unavailable)."""


class DeviceLost(Exception):
    """The device session dropped mid-run (unplug / sleep / re-enumerate).

    Distinguished from a transient per-object glitch by a liveness probe
    (`_alive`): a dropped session ends the run gracefully — re-running resumes
    from on-disk state (verified files skipped, partial temps swept).
    """


# --- device registry (durable, synced .pix/devices.yaml) ---------------------
def _registry_path(root: Path) -> Path:
    return root / ".pix" / "devices.yaml"


def _load_registry(root: Path) -> dict[str, str]:
    p = _registry_path(root)
    if not p.is_file():
        return {}
    loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    return cast("dict[str, str]", loaded) if isinstance(loaded, dict) else {}


def _save_registry(root: Path, reg: dict[str, str]) -> None:
    p = _registry_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.safe_dump(reg, default_flow_style=False, sort_keys=True),
        encoding="utf-8",
    )


# --- filesystem-safe names ---------------------------------------------------
_INVALID_CHARS = '<>:"/\\|?*'
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_component(name: str) -> str:
    """Make one path component safe for NTFS (invalid chars, reserved, trailing)."""
    cleaned = "".join("_" if c in _INVALID_CHARS or ord(c) < 32 else c for c in name)
    cleaned = cleaned.rstrip(" .")
    if not cleaned:
        cleaned = "_"
    stem = cleaned.split(".", 1)[0].upper()
    if stem in _RESERVED:
        cleaned = "_" + cleaned
    return cleaned


def _friendly_for(root: Path, info: wpd.DeviceInfo, *, interactive: bool,
                  name: str | None = None) -> str:
    """Resolve serial → friendly folder name, and remember it for future runs.

    Precedence: explicit `--name` > a name remembered from a prior run
    (`.pix/devices.yaml`) > an interactive prompt > auto-name from WPD. Whatever
    is resolved is persisted, so a device is only named once — later runs reuse
    it silently, and a `--name` both sets and remembers (a rename).
    """
    serial = info.serial or info.device_id
    reg = _load_registry(root)
    if name:
        friendly = sanitize_component(name)
    elif serial in reg:
        return reg[serial]  # remembered from a prior run — no prompt
    else:
        default = sanitize_component(info.friendly or info.model or serial)
        if interactive:
            import click  # noqa: PLC0415
            import typer  # noqa: PLC0415

            try:
                answer = typer.prompt(
                    f"New device (serial {serial}). Name it", default=default
                )
            except (click.exceptions.Abort, EOFError):
                answer = default  # stdin reported a TTY but can't be read
            friendly = sanitize_component(answer or default)
        else:
            friendly = default

    # Avoid two different serials colliding on one folder name.
    if any(v == friendly and k != serial for k, v in reg.items()):
        friendly = f"{friendly}-{sanitize_component(serial)[:8]}"
    reg[serial] = friendly
    _save_registry(root, reg)
    return friendly


# --- object identity ---------------------------------------------------------
def _skip_key(obj: wpd.WpdObject) -> tuple[str, int | None]:
    """Incremental skip key: PUID (else name+date fallback) plus size."""
    ident = obj.puid or f"{obj.filename}|{obj.created}"
    return (ident, obj.size)


def _is_skippable_companion(obj: wpd.WpdObject) -> bool:
    return Path(obj.filename).suffix.lower() in _SKIP_EXTENSIONS


# --- sidecar -----------------------------------------------------------------
def _sidecar_path(landed: Path) -> Path:
    return landed.with_name(landed.name + _SIDECAR_EXT)


def _write_sidecar(landed: Path, info: wpd.DeviceInfo, obj: wpd.WpdObject,
                   device_path: str) -> None:
    """Write the `.importinfo` sidecar via temp-then-rename (the VERIFIED commit)."""
    data = {
        "serial": info.serial,
        "friendly": info.friendly,
        "puid": obj.puid,
        "device_path": device_path,
        "original_filename": obj.filename,
        "size": obj.size,
        "capture_date": obj.created,
    }
    sidecar = _sidecar_path(landed)
    tmp = sidecar.with_name(sidecar.name + IMPORT_TMP_SUFFIX)
    tmp.write_text(
        yaml.safe_dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    tmp.replace(sidecar)


def _read_sidecar(sidecar: Path) -> dict[str, Any] | None:
    try:
        loaded = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return cast("dict[str, Any]", loaded) if isinstance(loaded, dict) else None


def _scan_manifest(landing: Path) -> set[tuple[str, int | None]]:
    """Regenerate the pending manifest from `.importinfo` sidecars on disk."""
    manifest: set[tuple[str, int | None]] = set()
    if not landing.is_dir():
        return manifest
    for sidecar in landing.rglob(f"*{_SIDECAR_EXT}"):
        data = _read_sidecar(sidecar)
        if data is None:
            continue
        ident = data.get("puid") or f"{data.get('original_filename')}|{data.get('capture_date')}"
        manifest.add((ident, data.get("size")))
    return manifest


def _sweep_temps(landing: Path) -> int:
    """Delete stale partial-download temps from an interrupted prior run."""
    if not landing.is_dir():
        return 0
    n = 0
    for tmp in landing.rglob(f"*{IMPORT_TMP_SUFFIX}"):
        try:
            tmp.unlink()
            n += 1
        except OSError:
            pass
    return n


# --- device selection --------------------------------------------------------
def _select_device(devices: list[wpd.DeviceInfo], selector: str | None
                   ) -> wpd.DeviceInfo:
    if selector:
        s = selector.lower()
        matches = [
            d for d in devices
            if s in (d.serial or "").lower()
            or s in (d.friendly or "").lower()
            or s in (d.model or "").lower()
        ]
        if not matches:
            raise ImportError_(f"no connected device matches --device {selector!r}.")
        if len(matches) > 1:
            raise ImportError_(
                f"--device {selector!r} is ambiguous: "
                + ", ".join(d.friendly or d.device_id for d in matches)
            )
        return matches[0]
    if not devices:
        raise ImportError_("no portable devices connected.")
    if len(devices) > 1:
        listing = "\n".join(
            f"  - {d.friendly or d.model or '?'} (serial {d.serial})"
            for d in devices
        )
        raise ImportError_(
            "multiple devices connected; pass --device <name-or-serial>:\n" + listing
        )
    return devices[0]


# --- the loop ----------------------------------------------------------------
def run_import(
    root: Path,
    *,
    device: str | None = None,
    name: str | None = None,
    dry_run: bool = False,
    runs_base: Path | None = None,
) -> ImportSummary:
    """Select a device and land its camera roll, verified, under `.pix/local/import/`.

    Device selection and naming happen **before** any run folder is created, so a
    no/multiple/unknown-device error leaves no empty run folder behind. When
    `runs_base` is given, the run folder + `apply.log` are created only once a
    device is settled (its path is returned on `summary.apply_log`).
    """
    try:
        devices = wpd.list_devices()
    except wpd.WpdUnavailable as e:
        raise ImportError_(str(e)) from e

    info = _select_device(devices, device)
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not dry_run
    friendly = _friendly_for(root, info, interactive=interactive, name=name)
    landing = local_dir(root) / "import" / friendly
    summary = ImportSummary(device=info, landing=landing)

    if dry_run:
        _dry_run(info, landing, summary)
        return summary

    # Only now — a device is selected and named — create the run folder + log.
    landing.mkdir(parents=True, exist_ok=True)
    log: IO[str] | None = None
    if runs_base is not None:
        run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        runs_dir = runs_base / run_id
        runs_dir.mkdir(parents=True, exist_ok=True)
        summary.apply_log = runs_dir / "apply.log"
        log = summary.apply_log.open("a", encoding="utf-8")
    try:
        _import_loop(info, friendly, landing, summary, log)
    finally:
        if log is not None:
            log.close()
    return summary


def _import_loop(info: wpd.DeviceInfo, friendly: str, landing: Path,
                 summary: ImportSummary, log: IO[str] | None) -> None:
    """The drain-as-you-go DFS + dirty re-loop (run folder already set up)."""
    swept = _sweep_temps(landing)
    if swept:
        _log(log, "sweep", f"removed {swept} stale temp(s)")

    manifest = _scan_manifest(landing)
    used_paths: dict[Path, str] = {}
    attempts: dict[tuple[str, int | None], int] = {}
    failed_keys: set[tuple[str, int | None]] = set()
    # Count distinct objects by outcome (each pass re-encounters every object).
    downloaded_keys: set[tuple[str, int | None]] = set()
    verified_keys: set[tuple[str, int | None]] = set()
    skipped_keys: set[tuple[str, int | None]] = set()

    try:
        with wpd.open_device(info.device_id) as dev, LiveProgress() as progress:
            progress.begin(f"import {friendly}")

            def act(obj: wpd.WpdObject, device_path: str) -> bool:
                """Handle one file object; return True if it caused a download."""
                key = _skip_key(obj)
                if _is_skippable_companion(obj):
                    skipped_keys.add(key)
                    return False
                if key in failed_keys:
                    return False
                landed = _landing_path(landing, device_path, used_paths, obj)
                sidecar = _sidecar_path(landed)
                if key in manifest or sidecar.exists():  # already imported / VERIFIED
                    if sidecar.exists():
                        manifest.add(key)
                    skipped_keys.add(key)
                    return False
                try:
                    if landed.exists():  # DOWNLOADED straggler → verify
                        status = _verify(dev, obj, landed, device_path, info, manifest,
                                         key, attempts, failed_keys, summary, progress, log)
                        if status == "verified":
                            verified_keys.add(key)
                        elif status == "redownloaded":
                            downloaded_keys.add(key)
                        return True
                    _download(dev, obj, landed, summary, progress, log, device_path)
                    downloaded_keys.add(key)
                    return True
                except DeviceLost:
                    raise
                except Exception as e:  # noqa: BLE001 — per-object isolation
                    if not _alive(dev):
                        raise DeviceLost(device_path) from e
                    _fail(obj, key, device_path, attempts, failed_keys, summary, log, str(e))
                    return False

            for pass_no in range(1, _MAX_PASSES + 1):
                summary.passes = pass_no
                try:
                    dirty = _traverse(dev, wpd.DEVICE_ROOT, "", act)
                except DeviceLost:
                    raise
                except Exception as e:  # enumeration failed — device gone?
                    if not _alive(dev):
                        raise DeviceLost("enumeration") from e
                    raise
                if not dirty:
                    break
    except DeviceLost as e:
        summary.device_lost = True
        _log(log, "DISCONNECT", str(e), detail="device lost; re-run to resume")

    summary.downloaded = len(downloaded_keys)
    summary.verified = len(verified_keys)
    # A file downloaded/verified this run isn't "skipped" even if a later clean
    # pass re-encountered it after its sidecar landed.
    summary.skipped = len(skipped_keys - downloaded_keys - verified_keys)
    return summary


def _traverse(dev: wpd.Device, parent_id: str, rel: str,
              act: Callable[[wpd.WpdObject, str], bool]) -> bool:
    """DFS one pass, files before subfolders; return True if anything downloaded."""
    dirty = False
    children = list(dev.children(parent_id))
    files = [c for c in children if not c.is_folder]
    folders = [c for c in children if c.is_folder]
    for obj in files:
        path = f"{rel}/{obj.filename}" if rel else obj.filename
        if act(obj, path):
            dirty = True
    for obj in folders:
        name = obj.filename
        sub = f"{rel}/{name}" if rel else name
        if _traverse(dev, obj.id, sub, act):
            dirty = True
    return dirty


def _landing_path(landing: Path, device_path: str, used: dict[Path, str],
                  obj: wpd.WpdObject) -> Path:
    """Sanitized landing path, disambiguated if two objects map to the same one."""
    parts = [sanitize_component(p) for p in device_path.split("/") if p]
    landed = landing.joinpath(*parts)
    ident = obj.puid or obj.filename
    prior = used.get(landed)
    if prior is not None and prior != ident:
        suffix = "".join(ch for ch in (obj.puid or "") if ch.isalnum())[:8] or "dup"
        landed = landed.with_name(f"{landed.stem}_{suffix}{landed.suffix}")
    used[landed] = ident
    return landed


def _download(dev: wpd.Device, obj: wpd.WpdObject, landed: Path,
              summary: ImportSummary, progress: LiveProgress,
              log: IO[str] | None, device_path: str) -> None:
    """Stream to temp, verify size + read-back, atomic-rename to final (DOWNLOADED)."""
    landed.parent.mkdir(parents=True, exist_ok=True)
    tmp = landed.with_name(landed.name + IMPORT_TMP_SUFFIX)
    progress.begin("download", device_path)
    hasher = blake3()
    total = 0
    with tmp.open("wb") as f:
        for block in dev.stream(obj.id, _CHUNK):
            f.write(block)
            hasher.update(block)
            total += len(block)
    if obj.size is not None and total != obj.size:
        tmp.unlink(missing_ok=True)
        raise OSError(f"size mismatch: read {total}, expected {obj.size}")
    tmp.replace(landed)
    if _hash_file(landed) != hasher.hexdigest():
        landed.unlink(missing_ok=True)
        raise OSError("disk read-back mismatch after write")
    summary.bytes_downloaded += total
    _log(log, "DOWNLOADED", device_path, size=total)


def _verify(dev: wpd.Device, obj: wpd.WpdObject, landed: Path, device_path: str,
            info: wpd.DeviceInfo, manifest: set[tuple[str, int | None]],
            key: tuple[str, int | None], attempts: dict[tuple[str, int | None], int],
            failed: set[tuple[str, int | None]], summary: ImportSummary,
            progress: LiveProgress, log: IO[str] | None) -> str:
    """Re-read from device and compare to the landed file; commit sidecar on match.

    Returns "verified", "redownloaded" (size changed → fresh pull, verify later),
    or "flaky" (reads disagreed; retry or FAILED at the cap).
    """
    progress.begin("verify", device_path)
    # Cheap size pre-check: a changed on-device size ⇒ re-download fresh.
    if obj.size is not None and landed.stat().st_size != obj.size:
        _download(dev, obj, landed, summary, progress, log, device_path)
        return "redownloaded"
    disk = _hash_file(landed)
    dev_hash = _hash_stream(dev, obj.id)
    if dev_hash == disk:
        _write_sidecar(landed, info, obj, device_path)
        manifest.add(key)
        _log(log, "VERIFIED", device_path)
        return "verified"
    # Disagreement: adjudicate with a third read.
    dev_hash2 = _hash_stream(dev, obj.id)
    if dev_hash == dev_hash2:  # two device reads agree, disk differs → device wins
        _download(dev, obj, landed, summary, progress, log, device_path)
        _write_sidecar(landed, info, obj, device_path)
        manifest.add(key)
        _log(log, "VERIFIED", device_path, detail="device-wins overwrite")
        return "verified"
    attempts[key] = attempts.get(key, 0) + 1
    if attempts[key] >= _MAX_ATTEMPTS:
        failed.add(key)
        summary.failed.append(device_path)
        _log(log, "FAILED", device_path, detail="reads never agreed")
    else:
        _log(log, "FLAKY", device_path, detail=f"attempt {attempts[key]}")
    return "flaky"


def _fail(obj: wpd.WpdObject, key: tuple[str, int | None], device_path: str,
          attempts: dict[tuple[str, int | None], int],
          failed: set[tuple[str, int | None]], summary: ImportSummary,
          log: IO[str] | None, reason: str) -> None:
    attempts[key] = attempts.get(key, 0) + 1
    if attempts[key] >= _MAX_ATTEMPTS:
        failed.add(key)
        summary.failed.append(device_path)
        _log(log, "FAILED", device_path, detail=reason)
    else:
        _log(log, "ERROR", device_path, detail=f"{reason} (attempt {attempts[key]})")


def _dry_run(info: wpd.DeviceInfo, landing: Path, summary: ImportSummary) -> None:
    manifest = _scan_manifest(landing)
    with wpd.open_device(info.device_id) as dev:
        def act(obj: wpd.WpdObject, _path: str) -> bool:
            if _is_skippable_companion(obj):
                summary.skipped += 1
            elif _skip_key(obj) in manifest:
                summary.skipped += 1
            else:
                summary.downloaded += 1  # "would download"
            return False
        _traverse(dev, wpd.DEVICE_ROOT, "", act)


# --- liveness + hashing + logging --------------------------------------------
def _alive(dev: wpd.Device) -> bool:
    """Cheap probe: can we still read a device property? False ⇒ session dropped.

    Used on any mid-run error to tell a real disconnect (end gracefully, resume
    on re-run) from a transient per-object glitch (retry that object, continue).
    """
    try:
        dev.info()
        return True
    except Exception:  # noqa: BLE001 — any failure here means the session is unusable
        return False


def _hash_file(path: Path) -> str:
    hasher = blake3()
    with path.open("rb") as f:
        while True:
            block = f.read(_CHUNK)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


def _hash_stream(dev: wpd.Device, obj_id: str) -> str:
    hasher = blake3()
    for block in dev.stream(obj_id, _CHUNK):
        hasher.update(block)
    return hasher.hexdigest()


def _log(log: IO[str] | None, state: str, path: str,
         detail: str | None = None, *, size: int | None = None) -> None:
    if log is None:
        return
    ts = datetime.now().isoformat(timespec="milliseconds")
    extra = f"  [size={size}]" if size is not None else ""
    tail = f" : {detail}" if detail else ""
    log.write(f"{ts} {state:<11} {path}{extra}{tail}\n")
    log.flush()
