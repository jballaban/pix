"""The `pix import` device→disk loop (see spec/import.md).

Pulls new photos/videos off a connected phone and lands them, verified, under
`.pix/local/import/<friendly-name>/`. It does **nothing else** — no convert,
tag, dedupe, or organize; the (not-yet-built) migrate ingest pre-pass consumes
the landed files later.

Verification is device→disk only, on two independent axes:

- **Transfer integrity** — bytes-read == MTP-reported size, plus a disk
  read-back == the received stream. Established at download, no second transfer.
- **Media integrity** — a local, read-only `media_check` (Pillow / ffprobe)
  confirms the landed bytes actually parse. On a **hard** failure the recovery
  ladder escalates the freshness of the transfer: one same-session re-download,
  then a persisted `needs-session` state (retried once on a later, operator-
  reconnected run), then a terminal `failed` state reported to the user. A clean
  media_check writes the `.importinfo` sidecar (its presence *is* VERIFIED).

The traversal is a drain-as-you-go DFS (files before subfolders) re-run in one
loop until a pass is "clean" (nothing downloaded); the same pass does discovery
(catching objects MTP reveals lazily), recovery, and resume. Termination is
file-level (attempt caps / bounded ladder), never loop-level.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, IO, cast

import yaml
from blake3 import blake3

from pix import wpd
from pix.duration import format_duration_compact
from pix.ingest import committed_import_ids
from pix.markers import IMPORT_TMP_SUFFIX
from pix.media_check import media_check
from pix.progress import LiveProgress
from pix.root import local_dir

# Non-media companions import never lands (everything else, incl. unknown
# extensions, lands faithfully). Two rules: this explicit extension denylist
# (`.aae` = Apple edit sidecars) plus bare dotfiles (`_is_bare_dotfile`).
_SKIP_EXTENSIONS: frozenset[str] = frozenset({".aae"})

# Per-object attempt cap before FAILED (size mismatch, read-back failure, or two
# device reads that never agree). Run-state only — a later run re-attempts.
_MAX_ATTEMPTS: int = 3

# Backstop against a pathological non-converging loop; the per-file cap is what
# normally ends it.
_MAX_PASSES: int = 100

_SIDECAR_EXT: str = ".importinfo"
# The media-integrity problem marker (mutually exclusive with `.importinfo`).
_ISSUE_EXT: str = ".importissue"
# Durable, append-only media-recovery event log (synced `.pix/` tier).
_VERIFY_LOG_NAME: str = "import-verify.log"
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
    # Media-integrity recovery outcomes (spec/import.md → Recovery ladder):
    recovered: int = 0                # media-check fails fixed by a re-download
    needs_session: list[str] = field(default_factory=lambda: [])  # awaiting reconnect
    failed_media: list[str] = field(default_factory=lambda: [])   # terminal, resolve on device
    # Import-seed manifest (deprecated-tool skip list) outcomes:
    seed_skipped: int = 0             # objects skipped via a seed manifest
    manifests_deprecated: bool = False  # seed folder present but empty → remove the code


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
            import typer  # noqa: PLC0415

            # Ctrl-C / EOF here must CANCEL the run, not fall through to a default
            # and register the device. Let the abort propagate — nothing below
            # (including the registry save) runs.
            answer = typer.prompt(
                f"New device (serial {serial}). Name it", default=default
            )
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


def _is_bare_dotfile(name: str) -> bool:
    """True for a leading-dot name with no extension (`.nomedia`,
    `.database_uuid`, `.DS_Store`). These are device/OS metadata, never user
    media, so import skips them rather than landing junk that migrate would only
    have to delete. A dotfile *with* a real extension (`.hidden.jpg`) is not
    bare — it could be media — and still lands."""
    return name.startswith(".") and Path(name).suffix == ""


def _is_skippable_companion(obj: wpd.WpdObject) -> bool:
    """True for non-media files import never lands: the explicit extension
    denylist (`.aae`) or a bare dotfile (see `_is_bare_dotfile`)."""
    name = obj.filename
    return _is_bare_dotfile(name) or Path(name).suffix.lower() in _SKIP_EXTENSIONS


# --- sidecar -----------------------------------------------------------------
def _sidecar_path(landed: Path) -> Path:
    return landed.with_name(landed.name + _SIDECAR_EXT)


def _write_sidecar(landed: Path, info: wpd.DeviceInfo, friendly: str,
                   obj: wpd.WpdObject, device_path: str) -> None:
    """Write the `.importinfo` sidecar via temp-then-rename (the VERIFIED commit).

    `friendly` is the **registry** name (the `.pix/local/import/<friendly>/`
    folder), recorded as `device_name` because the flat `incoming/` landing loses
    it — ingest derives the synthetic event `"<device_name> - <imported_at>"` from
    these two fields. `imported_at` is stamped now (verify time), day granularity.
    """
    data = {
        "serial": info.serial,
        "friendly": info.friendly,       # WPD device friendly (informational)
        "device_name": friendly,         # registry folder name → event prefix
        "imported_at": datetime.now().strftime("%Y%m%d"),
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


# --- media-integrity problem marker (.importissue) ---------------------------
def _issue_path(landed: Path) -> Path:
    return landed.with_name(landed.name + _ISSUE_EXT)


def _write_issue(landed: Path, info: wpd.DeviceInfo, obj: wpd.WpdObject,
                 device_path: str, *, state: str, attempts: int,
                 last_error: str) -> None:
    """Write/replace the `.importissue` marker (temp-then-rename), the record of
    a media-integrity dead-end. Mutually exclusive with `.importinfo`."""
    data = {
        "state": state,               # "needs-session" | "failed"
        "attempts": attempts,
        "last_error": last_error,
        "serial": info.serial,
        "puid": obj.puid,
        "device_path": device_path,
        "original_filename": obj.filename,
        "size": obj.size,
    }
    issue = _issue_path(landed)
    tmp = issue.with_name(issue.name + IMPORT_TMP_SUFFIX)
    tmp.write_text(
        yaml.safe_dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    tmp.replace(issue)


def _read_issue(issue: Path) -> dict[str, Any] | None:
    try:
        loaded = yaml.safe_load(issue.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return cast("dict[str, Any]", loaded) if isinstance(loaded, dict) else None


def _scan_issues(landing: Path) -> tuple[list[str], list[str]]:
    """Partition on-disk `.importissue` markers into (needs_session, failed) —
    each a list of device paths, for the start/end-of-run reporting."""
    needs: list[str] = []
    failed: list[str] = []
    if not landing.is_dir():
        return needs, failed
    for issue in landing.rglob(f"*{_ISSUE_EXT}"):
        data = _read_issue(issue)
        if data is None:
            continue
        where = str(data.get("device_path") or issue.name)
        if data.get("state") == "failed":
            failed.append(where)
        elif data.get("state") == "needs-session":
            needs.append(where)
    return needs, failed


# --- durable media-recovery event log ----------------------------------------
def _append_verify_log(root: Path, friendly: str, device_path: str,
                       event: str, detail: str) -> None:
    """Append one tab-separated line to `.pix/import-verify.log` (durable/synced).

    Rare by design (media-check hard-fails only), so this file stays tiny; a
    nonzero line count after real use answers "did re-download ever help?".
    """
    p = root / ".pix" / _VERIFY_LOG_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat(timespec="milliseconds")
    with p.open("a", encoding="utf-8") as f:
        f.write(f"{ts}\t{friendly}\t{event}\t{device_path}\t{detail}\n")


# --- import-seed manifests (skip lists from the deprecated external MTP tool) -
# A one-time bridge: those manifests predate this system and carry no PUID, so
# their entries can't ride the primary PUID+size skip key — the loop consults
# this (filename.lower(), size) set separately. Placed by hand (no CLI) under
# `.pix/import-manifests/manifest.<friendly>.json` in the canonical schema
# {"files": [[name, size], ...]}. Delete a device's file once its phone-side
# copies are gone; when the folder is empty the feature is spent (see
# `_manifests_all_consumed`) and this whole path can be removed.
_MANIFEST_DIRNAME: str = "import-manifests"


def _manifest_dir(root: Path) -> Path:
    return root / ".pix" / _MANIFEST_DIRNAME


def _load_seed(root: Path, friendly: str) -> set[tuple[str, int]]:
    """Load the seed skip-set for `friendly` → {(filename.lower(), size)}.
    Empty if there's no manifest for this device (or it's unreadable)."""
    p = _manifest_dir(root) / f"manifest.{friendly}.json"
    if not p.is_file():
        return set()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    out: set[tuple[str, int]] = set()
    if not isinstance(raw, dict):
        return out
    files = cast("dict[str, Any]", raw).get("files", [])
    if not isinstance(files, list):
        return out
    for entry in cast("list[object]", files):
        if not isinstance(entry, (list, tuple)):
            continue
        seq = cast("list[object]", entry)
        if len(seq) != 2:
            continue
        pair = [str(x) for x in seq]
        try:
            out.add((pair[0].lower(), int(pair[1])))
        except ValueError:
            continue
    return out


def _manifests_all_consumed(root: Path) -> bool:
    """True iff the seed folder exists but holds no `manifest.*.json` — the
    feature has been fully used up, so the whole seed path is now dead code."""
    d = _manifest_dir(root)
    return d.is_dir() and not any(d.glob("manifest.*.json"))


def _seed_key(obj: wpd.WpdObject) -> tuple[str, int] | None:
    """The seed fingerprint for a live object, or None if size is unknown."""
    if obj.size is None:
        return None
    return (obj.filename.lower(), obj.size)


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
class NeedsDeviceChoice(Exception):
    """Internal signal: the caller must pick a device (prompt, or require --device).

    Carries the devices to choose among.
    """

    def __init__(self, devices: list[wpd.DeviceInfo]) -> None:
        super().__init__("device choice required")
        self.devices = devices


def _describe(d: wpd.DeviceInfo) -> str:
    return f"{d.friendly or d.model or '?'} (serial {d.serial})"


def _select_device(devices: list[wpd.DeviceInfo], selector: str | None,
                   known: frozenset[str] | set[str] = frozenset()) -> wpd.DeviceInfo:
    """Pick the import source, registry-driven.

    `--device` matches by serial/name substring (power-user override). Otherwise
    only **exactly one known** device (in `.pix/devices.yaml`) is auto-selected;
    anything else — zero known, multiple known, or a single *unknown* device —
    raises `NeedsDeviceChoice` so the caller prompts a picker (and a cancel there
    registers nothing).
    """
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
                + ", ".join(_describe(d) for d in matches)
            )
        return matches[0]

    if not devices:
        raise ImportError_("no portable devices connected.")
    known_connected = [d for d in devices if d.serial in known]
    if len(known_connected) == 1:
        return known_connected[0]
    # Zero or multiple known (even a single unknown device) → ask; never
    # silently auto-select/register a device the user didn't choose.
    raise NeedsDeviceChoice(devices)


def _prompt_device_choice(devices: list[wpd.DeviceInfo]) -> wpd.DeviceInfo:
    """Interactive numbered picker over connected devices.

    Reads via the builtin `input()` (not click.prompt) to keep the console
    interaction minimal and portable. Raises `ImportError_` on EOF / Ctrl-C /
    repeated invalid input; the caller downgrades any failure to the
    non-interactive "pass --device" guidance.
    """
    import typer  # noqa: PLC0415

    typer.echo("Select a device to import from:")
    for i, d in enumerate(devices, start=1):
        typer.echo(f"  [{i}] {_describe(d)}")
    for _ in range(5):
        typer.echo(f"Device number [1-{len(devices)}]: ", nl=False)
        try:
            raw = input().strip()
        except (EOFError, KeyboardInterrupt) as e:
            raise ImportError_("no device selected.") from e
        if raw.isdigit() and 1 <= int(raw) <= len(devices):
            return devices[int(raw) - 1]
        typer.echo(f"  not a valid choice; enter 1-{len(devices)}.")
    raise ImportError_("no valid device selection after several attempts.")


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

    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not dry_run
    try:
        info = _select_device(devices, device, known=set(_load_registry(root)))
    except NeedsDeviceChoice as e:
        listing = "\n".join(f"  - {_describe(d)}" for d in e.devices)
        base = "pass --device <name-or-serial>:\n" + listing
        if not interactive:
            raise ImportError_(
                "more than one device connected and not exactly one known; " + base
            ) from e
        try:
            info = _prompt_device_choice(e.devices)
        except ImportError_:
            # user made no selection (declined / EOF)
            raise ImportError_("no device selected; " + base) from None
        except Exception as pe:  # noqa: BLE001 — picker unusable in this terminal
            # Don't crash: fall back to the non-interactive guidance, surfacing
            # the underlying cause so it can be diagnosed/reported.
            raise ImportError_(
                f"interactive device picker unavailable ({pe!r}); " + base
            ) from pe
    friendly = _friendly_for(root, info, interactive=interactive, name=name)
    landing = local_dir(root) / "import" / friendly
    summary = ImportSummary(device=info, landing=landing)
    summary.manifests_deprecated = _manifests_all_consumed(root)

    if dry_run:
        _dry_run(info, landing, summary, _load_seed(root, friendly),
                 committed_import_ids(root))
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
        _import_loop(root, info, friendly, landing, summary, log)
    finally:
        if log is not None:
            log.close()
    return summary


def _import_loop(root: Path, info: wpd.DeviceInfo, friendly: str, landing: Path,
                 summary: ImportSummary, log: IO[str] | None) -> None:
    """The drain-as-you-go DFS + dirty re-loop (run folder already set up)."""
    swept = _sweep_temps(landing)
    if swept:
        _log(log, "sweep", f"removed {swept} stale temp(s)")

    # Operator-driven fresh-session retry: if a prior run parked files as
    # `needs-session`, this run *is* the fresh session (we trust the operator
    # reconnected). Announce it so they can abort + reconnect if they didn't.
    pending_reconnect, _ = _scan_issues(landing)
    if pending_reconnect:
        _echo_line(
            f"{len(pending_reconnect)} file(s) need a device reconnect to retry — "
            "if you haven't unplugged/replugged since the last run, quit, "
            "reconnect, and re-run."
        )

    manifest = _scan_manifest(landing)
    seed = _load_seed(root, friendly)  # deprecated-tool skip list, if any
    committed = committed_import_ids(root)  # ImportIds already in the library
    used_paths: dict[Path, str] = {}
    attempts: dict[tuple[str, int | None], int] = {}
    failed_keys: set[tuple[str, int | None]] = set()
    # Count distinct objects by outcome (each pass re-encounters every object).
    downloaded_keys: set[tuple[str, int | None]] = set()
    verified_keys: set[tuple[str, int | None]] = set()
    skipped_keys: set[tuple[str, int | None]] = set()
    recovered_keys: set[tuple[str, int | None]] = set()
    seed_skipped_keys: set[tuple[str, int | None]] = set()
    # Keys parked as `needs-session` *this run*. The dirty-loop re-traverses, so
    # without this a marker just written would be re-read as a fresh-session
    # retry in the next pass of the same session — collapsing the escalation.
    # A future run (fresh session_parked) picks the on-disk marker up correctly.
    session_parked: set[tuple[str, int | None]] = set()

    active = _ActiveTransfer()
    try:
        with (
            wpd.open_device(info.device_id) as dev,
            LiveProgress(status_provider=active.render) as progress,
        ):
            progress.begin(f"import {friendly}")

            def commit_verified(landed: Path, obj: wpd.WpdObject,
                                device_path: str, key: tuple[str, int | None]) -> None:
                """Write the `.importinfo` sidecar (the VERIFIED commit) and drop
                any stale `.importissue` marker — the two are mutually exclusive."""
                _write_sidecar(landed, info, friendly, obj, device_path)
                manifest.add(key)
                _issue_path(landed).unlink(missing_ok=True)
                verified_keys.add(key)
                _log(log, "VERIFIED", device_path)

            def event(device_path: str, ev: str, detail: str) -> None:
                _append_verify_log(root, friendly, device_path, ev, detail)
                _echo_line(f"  ! {ev}: {device_path}" + (f"  ({detail})" if detail else ""))

            def validate_and_commit(obj: wpd.WpdObject, landed: Path,
                                    device_path: str, key: tuple[str, int | None],
                                    *, just_downloaded: bool) -> bool:
                """Media-check the landed bytes; commit VERIFIED, or run the
                same-session recovery step (one re-download) and, on continued
                failure, park the file as `needs-session`.

                `just_downloaded` distinguishes a fresh NEW download (transfer
                work already done) from re-probing a straggler's on-disk bytes.
                Returns True if any transfer work happened (drives the dirty-loop).
                """
                progress.begin("verify", device_path)
                reason = media_check(landed)
                if reason is None:
                    commit_verified(landed, obj, device_path, key)
                    return just_downloaded
                # Hard media failure → one same-session re-download.
                event(device_path, "media-check-failed", reason)
                _download(dev, obj, landed, summary, progress, log, device_path, active)
                downloaded_keys.add(key)
                reason2 = media_check(landed)
                if reason2 is None:
                    event(device_path, "recovered-same-session", "")
                    recovered_keys.add(key)
                    commit_verified(landed, obj, device_path, key)
                    return True
                _write_issue(landed, info, obj, device_path, state="needs-session",
                             attempts=1, last_error=reason2)
                session_parked.add(key)
                event(device_path, "needs-session", reason2)
                return True

            def fresh_session_retry(obj: wpd.WpdObject, landed: Path,
                                    device_path: str, key: tuple[str, int | None],
                                    prior: dict[str, Any]) -> bool:
                """A `needs-session` file, retried once on this (fresh) session.
                Recovers → VERIFIED; still broken → terminal `failed`."""
                _download(dev, obj, landed, summary, progress, log, device_path, active)
                downloaded_keys.add(key)
                reason = media_check(landed)
                if reason is None:
                    event(device_path, "recovered-fresh-session", "")
                    recovered_keys.add(key)
                    commit_verified(landed, obj, device_path, key)
                    return True
                attempts_n = int(prior.get("attempts", 1)) + 1
                _write_issue(landed, info, obj, device_path, state="failed",
                             attempts=attempts_n, last_error=reason)
                event(device_path, "failed-terminal", reason)
                return True

            def act(obj: wpd.WpdObject, device_path: str) -> bool:
                """Handle one file object; return True if it did transfer work."""
                key = _skip_key(obj)
                if _is_skippable_companion(obj):
                    progress.begin("skipped", device_path)
                    skipped_keys.add(key)
                    return False
                if key in failed_keys:
                    return False
                if key in session_parked:  # parked this run; retry only next run
                    progress.begin("parked", device_path)
                    return False
                landed = _landing_path(landing, device_path, used_paths, obj)
                sidecar = _sidecar_path(landed)
                if key in manifest or sidecar.exists():  # already imported / VERIFIED
                    if sidecar.exists():
                        manifest.add(key)
                    progress.begin("skipped", device_path)
                    skipped_keys.add(key)
                    return False
                if (obj.puid and info.serial
                        and f"{info.serial}:{obj.puid}" in committed):
                    # Already ingested into the library (durable ImportId record).
                    progress.begin("skipped", device_path)
                    skipped_keys.add(key)
                    return False
                sk = _seed_key(obj)
                if sk is not None and sk in seed:  # deprecated-tool already had it
                    progress.begin("seed-skip", device_path)
                    seed_skipped_keys.add(key)
                    _log(log, "SEED-SKIP", device_path)
                    return False
                try:
                    issue = _issue_path(landed)
                    if issue.exists():
                        data = _read_issue(issue) or {}
                        if data.get("state") == "failed":  # terminal — leave it
                            progress.begin("failed", device_path)
                            skipped_keys.add(key)
                            return False
                        # needs-session → one fresh-session retry (this run).
                        return fresh_session_retry(obj, landed, device_path, key, data)
                    if landed.exists():  # DOWNLOADED straggler (no marker yet)
                        # Cheap size pre-check: a changed on-device size ⇒ the
                        # object changed (edit / optimized-storage rehydration) →
                        # re-download fresh; else re-probe the local bytes.
                        if obj.size is not None and landed.stat().st_size != obj.size:
                            _download(dev, obj, landed, summary, progress, log,
                                      device_path, active)
                            downloaded_keys.add(key)
                            return validate_and_commit(obj, landed, device_path, key,
                                                       just_downloaded=True)
                        return validate_and_commit(obj, landed, device_path, key,
                                                   just_downloaded=False)
                    _download(dev, obj, landed, summary, progress, log, device_path,
                              active)
                    downloaded_keys.add(key)
                    return validate_and_commit(obj, landed, device_path, key,
                                               just_downloaded=True)
                except DeviceLost:
                    raise
                except Exception as e:  # noqa: BLE001 — per-object isolation
                    if not _alive(dev):
                        raise DeviceLost(device_path) from e
                    _fail(obj, key, device_path, attempts, failed_keys, summary, log, str(e))
                    return False

            # One dirty-loop: download + local validate + recovery all inline.
            # Repeat full traversals until a pass does no transfer work (so
            # lazily-revealed objects are still caught).
            for _ in range(_MAX_PASSES):
                summary.passes += 1
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
    summary.recovered = len(recovered_keys)
    summary.seed_skipped = len(seed_skipped_keys)
    # A file downloaded/verified this run isn't "skipped" even if a later clean
    # pass re-encountered it after its sidecar landed.
    summary.skipped = len(skipped_keys - downloaded_keys - verified_keys)
    # Authoritative problem set = the markers left on disk at the end.
    summary.needs_session, summary.failed_media = _scan_issues(landing)


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


class _ActiveTransfer:
    """Mutable per-file transfer state backing the live status line.

    Shared with LiveProgress via `status_provider=render`, so the current file's
    percentage and its own elapsed timer are recomputed on every render —
    including the 1s background tick, so the per-file timer keeps climbing even if
    a read stalls (that's how a stuck transfer shows up). The size is known up
    front (`WPD_OBJECT_SIZE`), so a real percentage is available.

    A **lock** is essential: `render` runs on LiveProgress's background thread
    while `begin`/`advance` mutate on the main thread. Without it a render could
    read a new (small) file's `total` alongside the previous (large) file's stale
    `done` → absurd percentages (e.g. a 250 MB video's bytes over a 5 MB JPG =
    5000%). The lock lets `render` snapshot a consistent (done, total).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._verb = ""
        self._name = ""
        self._done = 0
        self._total: int | None = None
        self._start = 0.0
        self._active = False

    def begin(self, verb: str, name: str, total: int | None) -> None:
        with self._lock:
            self._verb, self._name, self._total = verb, name, total
            self._done, self._start, self._active = 0, time.monotonic(), True

    def advance(self, n: int) -> None:
        with self._lock:
            self._done += n

    def clear(self) -> None:
        with self._lock:
            self._active = False

    def render(self) -> str | None:
        with self._lock:  # consistent snapshot; see class docstring
            if not self._active:
                return None
            verb, name, done, total, start = (
                self._verb, self._name, self._done, self._total, self._start
            )
        parts = [f"{verb} {name}"]
        if total:
            parts.append(f"{min(100, done * 100 // total):>3}%")
        elapsed = time.monotonic() - start
        if elapsed >= 1.0:  # per-file timer, only once it's worth showing
            parts.append(f"[{format_duration_compact(elapsed)}]")
        return "  ".join(parts)


def _download(dev: wpd.Device, obj: wpd.WpdObject, landed: Path,
              summary: ImportSummary, progress: LiveProgress,
              log: IO[str] | None, device_path: str,
              active: _ActiveTransfer) -> None:
    """Stream to temp, verify size + read-back, atomic-rename to final (DOWNLOADED)."""
    landed.parent.mkdir(parents=True, exist_ok=True)
    tmp = landed.with_name(landed.name + IMPORT_TMP_SUFFIX)
    progress.begin("download", device_path)
    active.begin("download", obj.filename, obj.size)
    hasher = blake3()
    total = 0
    try:
        with tmp.open("wb") as f:
            for block in dev.stream(obj.id, _CHUNK):
                f.write(block)
                hasher.update(block)
                total += len(block)
                active.advance(len(block))
    finally:
        active.clear()
    if obj.size is not None and total != obj.size:
        tmp.unlink(missing_ok=True)
        raise OSError(f"size mismatch: read {total}, expected {obj.size}")
    tmp.replace(landed)
    if _hash_file(landed) != hasher.hexdigest():
        landed.unlink(missing_ok=True)
        raise OSError("disk read-back mismatch after write")
    summary.bytes_downloaded += total
    _log(log, "DOWNLOADED", device_path, size=total)


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


def _dry_run(info: wpd.DeviceInfo, landing: Path, summary: ImportSummary,
             seed: set[tuple[str, int]], committed: set[str]) -> None:
    manifest = _scan_manifest(landing)
    with wpd.open_device(info.device_id) as dev:
        def act(obj: wpd.WpdObject, _path: str) -> bool:
            sk = _seed_key(obj)
            if _is_skippable_companion(obj):
                summary.skipped += 1
            elif _skip_key(obj) in manifest:
                summary.skipped += 1
            elif obj.puid and info.serial and f"{info.serial}:{obj.puid}" in committed:
                summary.skipped += 1  # already ingested into the library
            elif sk is not None and sk in seed:
                summary.seed_skipped += 1  # "would skip via manifest"
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


def _log(log: IO[str] | None, state: str, path: str,
         detail: str | None = None, *, size: int | None = None) -> None:
    if log is None:
        return
    ts = datetime.now().isoformat(timespec="milliseconds")
    extra = f"  [size={size}]" if size is not None else ""
    tail = f" : {detail}" if detail else ""
    log.write(f"{ts} {state:<11} {path}{extra}{tail}\n")
    log.flush()


def _echo_line(msg: str) -> None:
    """Print a standalone console line during a run. A leading newline clears any
    live `\\r` progress line so the message lands cleanly on its own row."""
    import typer  # noqa: PLC0415

    typer.echo("\n" + msg)
