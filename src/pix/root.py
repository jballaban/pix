"""Library-root resolution.

A library root is a directory containing a `.pix/` directory. Every `pix`
command (other than `init`) resolves its root before doing any work.
Resolution order, per spec/library.md:

  1. Walk up from `start` (the path arg the command was given, if any) —
     finds the library when the user pointed at it or at a subfolder.
  2. The `PIX_ROOT` environment variable.
  3. Walk up from CWD — interactive fallback when the user is inside a
     library and didn't bother to pass a path.

The library is version-less — there's no schema check or upgrade step.
Format drift in `.pix/` is handled structurally (regenerable caches are
rebuilt; only-copy provenance is restored from its stable path field;
run folders are left as-is). See spec/library.md.
"""

from __future__ import annotations

import os
from pathlib import Path

from pix import sync_check


class NoLibraryRoot(Exception):
    """Raised when no library root can be resolved."""


def resolve(start: Path | None = None) -> Path:
    """Resolve the library root. Raises `NoLibraryRoot` if none is found.

    Every resolution runs `boot_check` — the single per-command bootstrap
    validation — so the checks live in one place rather than drifting across
    steps.
    """
    root = _resolve_root(start)
    boot_check(root)
    return root


def boot_check(library_root: Path) -> None:
    """The single per-command bootstrap validation.

    Kept deliberately in one function, run from `resolve` (which every command
    goes through), so validations don't accrete at different steps. Each check
    is cheap, so running them on every command — even when a given command
    wouldn't strictly need one — is fine.

    1. **Layout** — ensure `.pix/local/` exists and fold any legacy top-level
       state into it (idempotent).
    2. **Sync readiness** — refuse (exit non-zero) if a file-sync client is
       covering this library unsafely (On-Demand placeholders, or pix's churn
       not excluded); warn-and-continue for uncertain states. Read-only,
       self-correcting. See `pix.sync_check`.

    `pix init` doesn't resolve a root (it creates one), so it runs its own
    non-blocking readiness report instead.
    """
    ensure_local_layout(library_root)
    sync_check.require_ready(library_root, block=True)


def _resolve_root(start: Path | None) -> Path:
    if start is not None:
        found = _walk_up(start.resolve())
        if found is not None:
            return found

    env_root = os.environ.get("PIX_ROOT")
    if env_root:
        candidate = Path(env_root).resolve()
        if not (candidate / ".pix").is_dir():
            raise NoLibraryRoot(
                f"PIX_ROOT={candidate} does not contain a .pix directory. "
                f"Run 'pix init {candidate}' to establish one."
            )
        return candidate

    found = _walk_up(Path.cwd().resolve())
    if found is not None:
        return found

    raise NoLibraryRoot(
        "No pix library root found. Pass a path inside a library, set "
        "PIX_ROOT, or run 'pix init <path>' to establish one."
    )


def _walk_up(start: Path) -> Path | None:
    """Walk up from `start` looking for a `.pix/` directory; first match wins."""
    for parent in (start, *start.parents):
        if (parent / ".pix").is_dir():
            return parent
    return None


# --- The machine-local state dir (`.pix/local/`) -----------------------------

LOCAL_DIRNAME: str = "local"

# State folded from `.pix/<name>` into `.pix/local/<name>` for libraries
# created before the local/ grouping existed. All of it is regenerable cache
# or transient workspace — safe to relocate. `cache.db` is NOT listed here:
# it needs a WAL checkpoint before the move, so `cache_db` relocates it lazily
# (and falls back to the old path until then). The `lock` is ephemeral, so
# there's nothing durable to move — `library_lock` just stale-cleans any
# pre-upgrade lock at the old path.
_LEGACY_LOCAL_ITEMS: tuple[str, ...] = ("staging", "checkout", "events.cache")


def local_dir(library_root: Path) -> Path:
    """The machine-local, never-synced state dir: `<library>/.pix/local/`.

    Holds regenerable caches (`cache.db`), the library `lock`, and transient
    workspaces (`staging/`, `checkout/`, `faces/`). Grouped under one folder so
    a file-sync client (Synology Drive, ...) can exclude it as a single unit
    while still syncing the durable `.pix/{runs,errors,stash}` data.
    """
    return library_root / ".pix" / LOCAL_DIRNAME


def ensure_local_layout(library_root: Path) -> None:
    """Create `.pix/local/` and fold any legacy top-level state into it.

    One-time, idempotent, best-effort. Runs on every root resolution (cheap
    once migrated: a `mkdir(exist_ok)` plus a few stats). A move that fails —
    e.g. a directory held open by a concurrent process — is left for the next
    run; nothing is lost. `cache.db` is relocated separately by `cache_db`
    (it needs a checkpoint first), and the `lock` by `library_lock`.
    """
    pix = library_root / ".pix"
    local = pix / LOCAL_DIRNAME
    try:
        local.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for name in _LEGACY_LOCAL_ITEMS:
        src = pix / name
        dst = local / name
        if not src.exists() or dst.exists():
            continue
        try:
            src.rename(dst)  # same-volume, atomic
        except OSError:
            pass  # deferred to the next run
