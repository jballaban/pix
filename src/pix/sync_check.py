"""Read-only validation that a file-sync client is safely configured for a
pix library.

pix never *writes* the sync client's config (it's an undocumented, per-version
private format — see the investigation notes in spec/implementation.md). It
only *reads* it and tells the user exactly what to fix. Worst case the format
changes and we can't read it: we warn and continue (self-correcting — update
this module for the new format), never block on uncertainty.

Currently understands **Synology Drive Client on Windows**. Its per-task config
lives under ``%LOCALAPPDATA%\\SynologyDrive\\data``:

- ``db/sys.sqlite`` → ``session_table`` maps each sync task to its local
  ``sync_folder`` and its On-Demand flag (``use_windows_cloud_file_api``).
- ``session/<id>/conf/blacklist.filter`` → the operative exclude rules the
  daemon reads at startup (``black_dir_prefix`` / ``black_suffix`` /
  ``black_glob``).

For the task covering a given library we validate two things:

1. **Files are persisted, not On-Demand placeholders.** pix reads/hashes/
   converts the actual bytes; On-Demand (Cloud Files API) would trigger
   downloads or fail.
2. **pix's transient churn is excluded** — ``.pix/local/`` and the transient
   markers (``*.__*`` and ``*_exiftool_tmp``, see `pix.markers`). Sample marker
   names are matched against the configured globs/suffixes semantically, so an
   equivalent-but-different rule still counts.

Anywhere else (no Synology client, non-Windows, unreadable config) the check is
a no-op or a warning; it only hard-blocks on a *confidently* not-ready task.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

import typer

from pix import markers

# Readiness states.
NOT_INSTALLED = "not_installed"  # no Synology Drive Client config on this machine
NOT_SYNCED = "not_synced"        # client present, but no task covers this library
READY = "ready"                  # covering task is correctly configured
NOT_READY = "not_ready"          # covering task has a confirmed problem (block)
UNVERIFIABLE = "unverifiable"    # config present but couldn't be parsed (warn)


@dataclass(frozen=True)
class SyncReadiness:
    state: str
    share: str | None = None
    folder: str | None = None
    on_demand: bool = False
    missing: tuple[str, ...] = field(default_factory=tuple)  # remediation lines
    note: str = ""  # reason for not_synced / unverifiable


@dataclass(frozen=True)
class _Session:
    session_id: int
    share: str
    folder: str
    on_demand: bool


# Per-process memo: the config doesn't change mid-invocation, and `sync` runs
# four lock-acquiring sub-commands, so compute once per root.
_cache: dict[str, SyncReadiness] = {}


def _synology_data_dir() -> Path | None:
    """`%LOCALAPPDATA%\\SynologyDrive\\data` if it looks present, else None."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    data = Path(local) / "SynologyDrive" / "data"
    return data if (data / "db" / "sys.sqlite").exists() else None


def _norm(p: str | Path) -> str:
    """Normalized absolute path for prefix comparison (OS case rules)."""
    return os.path.normcase(os.path.normpath(str(p)))


def _read_sessions(data_dir: Path) -> list[_Session] | None:
    """Read `session_table` from a private snapshot of `sys.sqlite`.

    Copies the DB (+ any WAL sidecars) to a temp dir first so we never touch or
    lock the client's live file. Returns None on any read error.
    """
    db_dir = data_dir / "db"
    if not (db_dir / "sys.sqlite").exists():
        return None
    tmp = Path(tempfile.mkdtemp(prefix="pix-syno-"))
    try:
        for f in db_dir.glob("sys.sqlite*"):
            shutil.copy2(f, tmp / f.name)
        con = sqlite3.connect(str(tmp / "sys.sqlite"))
        try:
            rows = con.execute(
                "SELECT id, share_name, sync_folder, "
                "use_windows_cloud_file_api, is_mac_on_demand_sync_enable "
                "FROM session_table"
            ).fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out: list[_Session] = []
    for sid, share, folder, win_odm, mac_odm in rows:
        if not folder:
            continue
        out.append(
            _Session(
                session_id=int(sid),
                share=str(share or ""),
                folder=str(folder),
                on_demand=bool(win_odm) or bool(mac_odm),
            )
        )
    return out


def _covering_session(
    sessions: list[_Session], root: Path
) -> _Session | None:
    """The session whose `sync_folder` is an ancestor of (or equals) `root`.
    Deepest match wins (in case of nested tasks)."""
    rootn = _norm(root)
    best: _Session | None = None
    best_len = -1
    for s in sessions:
        fn = _norm(s.folder)
        if rootn == fn or rootn.startswith(fn + os.sep):
            if len(fn) > best_len:
                best, best_len = s, len(fn)
    return best


def _parse_blacklist(
    data_dir: Path, session_id: int
) -> dict[str, list[str]] | None:
    """Parse a session's operative `blacklist.filter` into token lists keyed by
    directive (`black_dir_prefix`, `black_suffix`, `black_glob`). None on error.
    """
    path = data_dir / "session" / str(session_id) / "conf" / "blacklist.filter"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    assigns: dict[str, str] = {}
    current = ""  # directive currently being accumulated ("" = none / in a section header)
    for line in text.splitlines():
        if line.lstrip().startswith("["):
            current = ""
            continue
        m = re.match(r"^\s*([A-Za-z_]\w*)\s*=\s*(.*)$", line)
        if m:
            current = str(m.group(1))
            assigns[current] = str(m.group(2))
        elif current:
            assigns[current] += " " + line.strip()
    return {
        k: re.findall(r'"([^"]*)"', assigns.get(k, ""))
        for k in ("black_dir_prefix", "black_suffix", "black_glob")
    }


def _expected_local_prefix(root: Path, folder: str) -> str:
    """The blacklist dir-prefix that would exclude this library's `.pix/local`,
    e.g. `/.pix/local` (library == sync root) or `/sub/.pix/local`."""
    rel = os.path.relpath(root, folder)
    if rel in (".", ""):
        return "/.pix/local"
    return "/" + rel.replace("\\", "/").strip("/") + "/.pix/local"


def _dir_excluded(expected_prefix: str, tokens: list[str]) -> bool:
    """True if some configured dir-prefix is an ancestor of (or equals) the
    expected `.pix/local` path."""
    def norm(x: str) -> str:
        x = x.strip().replace("\\", "/").casefold()
        if not x.startswith("/"):
            x = "/" + x
        return x.rstrip("/") or "/"

    want = norm(expected_prefix)
    for t in tokens:
        tt = norm(t)
        if want == tt or want.startswith(tt + "/"):
            return True
    return False


def _sample_markers() -> tuple[list[str], str]:
    """Representative transient filenames, derived from `pix.markers` so they
    stay in step with the real marker shapes. Returns (pix-markers, exiftool)."""
    return (
        [
            f"x{markers.RENAME_SUFFIX}",
            f"x.heic{markers.CONVERT_INFIX}jpg",
            f"L001{markers.ORGANIZE_TMP_SUFFIX}",
            f"clip{markers.ROTATE_INFIX}.mp4",
        ],
        f"img.jpg{markers.EXIFTOOL_TMP_SUFFIX}",
    )


def _excluded(name: str, filt: dict[str, list[str]]) -> bool:
    """Would the configured globs/suffixes exclude a file named `name`?"""
    if any(fnmatch(name, g) for g in filt["black_glob"]):
        return True
    return any(name.endswith(sfx) for sfx in filt["black_suffix"])


def check_sync_readiness(
    root: Path, *, data_dir: Path | None = None
) -> SyncReadiness:
    """Validate the sync task covering `root`. Pure and read-only.

    `data_dir` overrides Synology detection (for tests); production passes None.
    """
    key = _norm(root)
    if data_dir is None and key in _cache:
        return _cache[key]

    result = _compute(root, data_dir)
    if data_dir is None:
        _cache[key] = result
    return result


def _compute(root: Path, data_dir: Path | None) -> SyncReadiness:
    data = data_dir if data_dir is not None else _synology_data_dir()
    if data is None or not (data / "db" / "sys.sqlite").exists():
        return SyncReadiness(NOT_INSTALLED)

    sessions = _read_sessions(data)
    if sessions is None:
        return SyncReadiness(
            UNVERIFIABLE, note="couldn't read Synology session config"
        )

    sess = _covering_session(sessions, root)
    if sess is None:
        return SyncReadiness(NOT_SYNCED)

    filt = _parse_blacklist(data, sess.session_id)
    missing: list[str] = []
    rules_checked = filt is not None
    if filt is not None:
        prefix = _expected_local_prefix(root, sess.folder)
        if not _dir_excluded(prefix, filt["black_dir_prefix"]):
            missing.append(
                f"Exclude the folder '.pix/local' from this task "
                f"(its path here: {prefix})."
            )
        marker_names, exif_name = _sample_markers()
        if not all(_excluded(n, filt) for n in marker_names):
            missing.append(
                f"Add a filename exclude rule: {markers.PIX_MARKER_GLOB}"
            )
        if not _excluded(exif_name, filt):
            missing.append(
                f"Add a filename exclude rule: {markers.EXIFTOOL_TMP_GLOB}"
            )

    if sess.on_demand or missing:
        return SyncReadiness(
            NOT_READY,
            share=sess.share,
            folder=sess.folder,
            on_demand=sess.on_demand,
            missing=tuple(missing),
        )
    if not rules_checked:
        return SyncReadiness(
            UNVERIFIABLE,
            share=sess.share,
            folder=sess.folder,
            note="couldn't read the task's blacklist.filter",
        )
    return SyncReadiness(READY, share=sess.share, folder=sess.folder)


def _render_not_ready(r: SyncReadiness) -> str:
    lines = [
        f"Synology Drive sync task '{r.share}' ({r.folder}) is not ready for pix:"
    ]
    if r.on_demand:
        lines.append(
            "  - On-Demand Sync is ON — files are placeholders, not real bytes; "
            "pix needs the actual files on disk."
        )
        lines.append(
            "    Fix: in Synology Drive Client, disable On-Demand Sync for this "
            "task (or mark its files 'Available on this device') and let them "
            "hydrate."
        )
    for m in r.missing:
        lines.append(f"  - {m}")
    if r.missing:
        lines.append(
            "  (Synology Drive Client → the task → Sync Rules; then restart the "
            "client so the rules take effect.)"
        )
    return "\n".join(lines)


def require_ready(root: Path, *, block: bool) -> None:
    """Run the readiness check and act on it.

    `block=True` (write-mode ops): raise `typer.Exit(1)` on a confirmed
    not-ready task. `block=False` (e.g. `pix init`): report only.
    NOT_SYNCED / UNVERIFIABLE always warn-and-continue; NOT_INSTALLED / READY
    are silent.
    """
    r = check_sync_readiness(root)
    if r.state in (NOT_INSTALLED, READY):
        return
    if r.state == NOT_SYNCED:
        typer.echo(
            "Note: this library is not inside a Synology Drive sync task. If you "
            "sync it, keep On-Demand Sync off and exclude .pix/local plus "
            f"filename rules {markers.PIX_MARKER_GLOB} and "
            f"{markers.EXIFTOOL_TMP_GLOB}.",
            err=True,
        )
        return
    if r.state == UNVERIFIABLE:
        typer.echo(
            f"Note: couldn't verify Synology sync readiness ({r.note}); "
            "continuing.",
            err=True,
        )
        return
    # NOT_READY
    msg = _render_not_ready(r)
    typer.echo(msg, err=True)
    if block:
        raise typer.Exit(code=1)
