"""Library-state schema versioning + per-version config upgrades.

Persisted state lives in `<root>/.pix/`. The single integer
`schema_version` in `<root>/.pix/state.yaml` records what schema this
library was last touched at. The constant `SCHEMA_VERSION` below tracks
what this build of pix expects.

When a command (other than `init` and `upgrade`) sees a mismatch,
`ensure_current` raises so the user can decide explicitly. The `pix
upgrade` command then walks the per-version `UPGRADES` table to apply
additive / removal changes to the user's *existing* config, keeping
their customizations intact wherever possible. Conflicts get git-style
inline markers in the rewritten config; pix refuses to operate on a
config with unresolved markers until the user picks a side.

Terminology note: schema **upgrades** here are internal `.pix/` state
transitions. They're distinct from `pix migrate`, which is the user-
facing file-normalization command. Always say "upgrade" for the former.

Bump `SCHEMA_VERSION` and add a `UPGRADES[N]` entry whenever something
material in `.pix/` changes — typically a new mandatory config field or
a renamed/removed default. Most pix releases don't touch persisted state
and should not bump it.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import yaml

from pix.config import DEFAULT_CONFIG_YAML


# v1 — Initial schema. .pix/{config.yaml, state.yaml, runs/, staging/}.
# v2 — Added `stash` extension action and the `.pix/stash/` subfolder
#      (created lazily on first stash). Default config gains
#      dng/insp/insv → stash.
# v3 — Default config gains ini/txt/json/gif/webp → delete (Windows
#      junk sidecars, web-format throwaways, sundry sidecar formats).
# v4 — Default config gains jwt → delete (MSAL broker manifests
#      synced by OneDrive into media-backup folders).
# v5 — Default config gains m4v → keep, with `m4v` aliased to `mp4`
#      for canonical extension. M4V is functionally identical to MP4
#      (Apple-branded ISO BMFF); renames are free byte-preserving
#      moves rather than ffmpeg work.
# v6 — Default config gains mpg / mpeg → convert_to_mp4 (MPEG-1/2
#      Program Stream; full re-encode to H.265 since MP4 doesn't
#      carry MPEG-2 video in practice).
# v7 — Default config gains vob → convert_to_mp4 (DVD-Video object;
#      same MPEG-2 PS content as .mpg with DVD-specific extras that
#      get dropped on re-encode). Single-VOB rips convert cleanly;
#      multi-VOB DVD rips would be split file-by-file (use HandBrake
#      for those — known limitation, not auto-handled).
# v8 — Default config gains bmp → convert_to_jpg (uncompressed bitmap;
#      Pillow decodes it natively, re-encode to JPEG reclaims space).
# v9 — Default config flips insv / insp from stash → keep. Insta360 .insv
#      (video) and .insp (photo) are MP4/JPEG containers carrying a
#      proprietary Insta360 trailer (gyro + dual-fisheye lens calibration);
#      exiftool writes pix:* XMP into them while preserving that trailer
#      byte-for-byte, so they become first-class keep media (tagged,
#      dated, organized, hashed, deduped). They are NOT renamed to the
#      canonical date-based name — see plan.NAME_PRESERVING_KEEP — because
#      each recording's two lens files share a timestamp and Insta360
#      Studio pairs them by their original VID_<date>_<time>_<lens>_<seq>
#      filename. Existing libraries hold `insv/insp: stash` (the v2
#      default), so this flip surfaces as a config conflict the user
#      resolves to `keep`.
# v10 — Default config flips dng from stash → convert_to_jpg. Develop-able
#      raws (e.g. iPhone ProRAW) decode in Pillow and become JPGs; raws
#      Pillow can't decode (e.g. Insta360 360 Bayer dng) fail CONVERT and
#      quarantine to .pix/errors/ (original preserved, nothing destroyed).
#      Existing libraries on `dng: stash` see a config conflict to resolve.
SCHEMA_VERSION: int = 10


# --- Upgrades --------------------------------------------------------------


@dataclass(frozen=True)
class Upgrade:
    """Per-version config changes from previous version to this one.

    `add_extensions` and `remove_extensions` both target `extensions`
    in config.yaml (the only structured section today). Future upgrades
    may want a richer model; extend then.
    """

    add_extensions: dict[str, str] = field(default_factory=lambda: {})
    remove_extensions: list[str] = field(default_factory=lambda: [])
    description: str = ""


UPGRADES: dict[int, Upgrade] = {
    2: Upgrade(
        add_extensions={
            "dng": "stash",
            "insp": "stash",
            "insv": "stash",
        },
        description=(
            "Added `stash` extension action; default config gains "
            "dng / insp / insv → stash."
        ),
    ),
    3: Upgrade(
        add_extensions={
            "ini": "delete",
            "txt": "delete",
            "json": "delete",
            "gif": "delete",
            "webp": "delete",
        },
        description=(
            "Default config gains ini / txt / json / gif / webp → "
            "delete (Windows junk sidecars, web-format throwaways)."
        ),
    ),
    4: Upgrade(
        add_extensions={"jwt": "delete"},
        description=(
            "Default config gains jwt → delete (MSAL broker trust "
            "manifests that OneDrive backup syncs alongside media)."
        ),
    ),
    5: Upgrade(
        add_extensions={"m4v": "keep"},
        description=(
            "Default config gains m4v → keep; canonical extension "
            "alias makes them rename to .mp4 (Apple-branded MP4, "
            "same bytes)."
        ),
    ),
    6: Upgrade(
        add_extensions={
            "mpg": "convert_to_mp4",
            "mpeg": "convert_to_mp4",
        },
        description=(
            "Default config gains mpg / mpeg → convert_to_mp4 (MPEG-1/2 "
            "Program Stream; mandatory full re-encode to H.265)."
        ),
    ),
    7: Upgrade(
        add_extensions={"vob": "convert_to_mp4"},
        description=(
            "Default config gains vob → convert_to_mp4 (DVD-Video "
            "object; same MPEG-2 PS re-encode path as .mpg)."
        ),
    ),
    8: Upgrade(
        add_extensions={"bmp": "convert_to_jpg"},
        description=(
            "Default config gains bmp → convert_to_jpg (uncompressed "
            "bitmap; Pillow decodes natively, re-encode reclaims space)."
        ),
    ),
    9: Upgrade(
        add_extensions={"insv": "keep", "insp": "keep"},
        description=(
            "Default config flips insv / insp from stash → keep "
            "(Insta360 360 media — tagged + organized in place, kept in "
            "their proprietary format and original filename). Libraries "
            "on the old `stash` default will see a conflict to resolve."
        ),
    ),
    10: Upgrade(
        add_extensions={"dng": "convert_to_jpg"},
        description=(
            "Default config flips dng from stash → convert_to_jpg "
            "(develop-able raws become JPGs; un-developable raws fail "
            "CONVERT and quarantine to .pix/errors/). Libraries on the "
            "old `stash` default will see a conflict to resolve."
        ),
    ),
}


# --- Exceptions --------------------------------------------------------------


class SchemaTooNew(Exception):
    """Library was touched by a newer pix version."""


class SchemaUpgradeRequired(Exception):
    """Library schema is older than this pix; user must run `pix upgrade`.

    The default message is just the short headline — explicitly says
    "schema" to disambiguate from pix's own tool version (those are
    independent counters). Commands append a "Run pix upgrade <path>"
    suggestion using whatever path the user actually typed, so the
    suggestion stays copy-paste friendly.
    """

    def __init__(self, library_version: int, root: Path) -> None:
        super().__init__(
            f"Pix schema upgrade required: library at schema v"
            f"{library_version}, this pix expects v{SCHEMA_VERSION}."
        )
        self.library_version = library_version
        self.root = root


# --- Results -----------------------------------------------------------------


@dataclass(frozen=True)
class ConflictDetail:
    """One conflict between user's existing config and a upgrade_step's default."""

    section: str  # e.g., "extensions"
    key: str  # e.g., "dng"
    current_value: str
    new_default: str
    version: int


# Subfolders of `.pix/` that hold irreplaceable *user* files (not
# regenerable tool state). Archiving sweeps them along with everything
# else; the upgrade command warns loudly when any held files so the user
# doesn't forget them in the archive. `runs/`, `cache/`, `staging/`, and
# `faces/` are deliberately absent — all are rebuildable from the library.
_USER_DATA_DIRS: tuple[str, ...] = ("stash", "errors")


@dataclass(frozen=True)
class ArchivedUserData:
    """A user-data subfolder swept into the archive, with its file count."""

    name: str  # e.g. "stash" / "errors"
    path: Path  # archive/v<old>/<name>
    file_count: int


@dataclass(frozen=True)
class UpgradeResult:
    """What `upgrade` did. Returned to the `pix upgrade` command."""

    from_version: int
    to_version: int
    archive_path: Path
    added: list[str]
    removed: list[str]
    conflicts: list[ConflictDetail]
    archived_user_data: list[ArchivedUserData]


# --- Public surface ----------------------------------------------------------


def ensure_current(root: Path) -> None:
    """Verify the library schema is current; bootstrap if state is missing.

    Raises `SchemaUpgradeRequired` when older, `SchemaTooNew` when newer.
    Bootstrapping (writing a fresh `state.yaml` at `SCHEMA_VERSION` when
    none exists) happens silently — it touches no customizations.
    """
    state_path = root / ".pix" / "state.yaml"

    if not state_path.exists():
        _write_state(state_path, SCHEMA_VERSION)
        return

    library_version = _read_version(state_path)

    if library_version == SCHEMA_VERSION:
        return

    if library_version > SCHEMA_VERSION:
        raise SchemaTooNew(
            f"Library at {root} has schema_version={library_version}, "
            f"but this pix only understands up to v{SCHEMA_VERSION}. "
            f"A newer pix touched this library; upgrade pix and re-run."
        )

    raise SchemaUpgradeRequired(library_version, root)


def upgrade(root: Path) -> UpgradeResult:
    """Apply pending upgrade_steps to the library's config.

    Always archives prior `.pix/` contents to `.pix/archive/v<old>/`
    first. Then walks `UPGRADES[old+1..current]`, applying additions
    and removals to the user's existing config. Conflicts (user has a
    key with a value that differs from the new default) are written
    into the new config with git-style markers; pix refuses to operate
    on the library until the user resolves them.

    Raises `ValueError` if the library is already current or has no
    state.yaml. Raises `SchemaTooNew` if the library is newer than
    this pix.
    """
    state_path = root / ".pix" / "state.yaml"
    if not state_path.exists():
        raise ValueError(
            f"Library at {root} has no state.yaml — there's nothing "
            f"to upgrade. Run any pix command to bootstrap the "
            f"version stamp silently."
        )
    library_version = _read_version(state_path)

    if library_version == SCHEMA_VERSION:
        raise ValueError(
            f"Library at {root} is already at schema_version="
            f"{SCHEMA_VERSION}. Nothing to upgrade."
        )
    if library_version > SCHEMA_VERSION:
        raise SchemaTooNew(
            f"Library at {root} has schema_version={library_version}, "
            f"but this pix only understands up to v{SCHEMA_VERSION}. "
            f"Upgrade pix, not the library."
        )

    # Load the user's existing config BEFORE archiving so we have it
    # in memory to merge against.
    user_config_path = root / ".pix" / "config.yaml"
    if user_config_path.exists():
        user_config: dict[str, object] = _load_config_data(user_config_path)
    else:
        user_config = cast(
            "dict[str, object]",
            yaml.safe_load(DEFAULT_CONFIG_YAML) or {},
        )

    # Archive everything (including the soon-to-be-rewritten config.yaml).
    archive_path = _archive_prior_contents(root, library_version)
    archived_user_data = _scan_archived_user_data(archive_path)

    # Apply upgrade_steps in order: library_version+1 .. SCHEMA_VERSION.
    added: list[str] = []
    removed: list[str] = []
    conflicts: list[ConflictDetail] = []
    for v in range(library_version + 1, SCHEMA_VERSION + 1):
        upgrade_step = UPGRADES.get(v)
        if upgrade_step is None:
            continue
        _apply_upgrade_step_to_config(
            config=user_config,
            upgrade_step=upgrade_step,
            version=v,
            added=added,
            removed=removed,
            conflicts=conflicts,
        )

    # Write the merged config — with markers if there are conflicts.
    if conflicts:
        text = _render_config_with_markers(user_config, conflicts)
    else:
        text = yaml.safe_dump(
            user_config, default_flow_style=False, sort_keys=False
        )
    user_config_path.write_text(text, encoding="utf-8")

    _write_state(state_path, SCHEMA_VERSION)

    return UpgradeResult(
        from_version=library_version,
        to_version=SCHEMA_VERSION,
        archive_path=archive_path,
        added=added,
        removed=removed,
        conflicts=conflicts,
        archived_user_data=archived_user_data,
    )


# --- Internals --------------------------------------------------------------


def _apply_upgrade_step_to_config(
    *,
    config: dict[str, object],
    upgrade_step: Upgrade,
    version: int,
    added: list[str],
    removed: list[str],
    conflicts: list[ConflictDetail],
) -> None:
    """Apply one upgrade_step in place to `config`. Appends to the three
    out-params describing what happened."""
    extensions_obj = config.setdefault("extensions", {})
    if not isinstance(extensions_obj, dict):
        # Malformed config; treat as empty and overwrite.
        extensions_obj = {}
        config["extensions"] = extensions_obj
    extensions = cast("dict[str, object]", extensions_obj)

    for key, default_value in upgrade_step.add_extensions.items():
        if key not in extensions:
            extensions[key] = default_value
            added.append(f"extensions.{key} = {default_value}  (v{version})")
        elif extensions[key] == default_value:
            pass  # user already matches the new default — no-op
        else:
            conflicts.append(
                ConflictDetail(
                    section="extensions",
                    key=key,
                    current_value=str(extensions[key]),
                    new_default=default_value,
                    version=version,
                )
            )

    for key in upgrade_step.remove_extensions:
        if key in extensions:
            del extensions[key]
            removed.append(f"extensions.{key}  (v{version})")


def _render_config_with_markers(
    config: dict[str, object], conflicts: list[ConflictDetail]
) -> str:
    """Render YAML with git-style markers replacing conflicted entries.

    Strategy: yaml.safe_dump the merged config (which retains the user's
    value for each conflicted key), then post-process line-by-line and
    replace the conflicted entry's line with a marker block.
    """
    base = yaml.safe_dump(config, default_flow_style=False, sort_keys=False)
    conflict_by_key: dict[tuple[str, str], ConflictDetail] = {
        (c.section, c.key): c for c in conflicts
    }

    out: list[str] = []
    current_section: str | None = None
    for line in base.splitlines():
        # Top-level section header: zero indent, ends with `:`.
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            current_section = line.rstrip()[:-1]
            out.append(line)
            continue

        # Entry under a section: split on the first `:` to get the key.
        if current_section is not None and ":" in line:
            stripped = line.lstrip()
            indent = line[: len(line) - len(stripped)]
            key = stripped.split(":", 1)[0]
            detail = conflict_by_key.get((current_section, key))
            if detail is not None:
                out.append("<<<<<<< current")
                out.append(f"{indent}{detail.key}: {detail.current_value}")
                out.append("=======")
                out.append(f"{indent}{detail.key}: {detail.new_default}")
                out.append(f">>>>>>> v{detail.version} default")
                continue

        out.append(line)

    return "\n".join(out) + "\n"


def _scan_archived_user_data(archive_path: Path) -> list[ArchivedUserData]:
    """Find user-data subfolders (`stash`/`errors`) that hold files in the
    freshly-written archive, so the command can warn the user not to
    abandon their only copies there."""
    out: list[ArchivedUserData] = []
    for name in _USER_DATA_DIRS:
        d = archive_path / name
        if not d.is_dir():
            continue
        count = sum(1 for p in d.rglob("*") if p.is_file())
        if count > 0:
            out.append(ArchivedUserData(name=name, path=d, file_count=count))
    return out


def _archive_prior_contents(root: Path, from_version: int) -> Path:
    """Move everything in `.pix/` (except `archive/`) into `archive/v<old>/`."""
    pix_dir = root / ".pix"
    archive_dir = pix_dir / "archive" / f"v{from_version}"
    if archive_dir.exists():
        raise RuntimeError(
            f"Archive folder {archive_dir} already exists; refusing "
            f"to overwrite. Move or delete it and re-run."
        )
    archive_dir.mkdir(parents=True)

    for item in pix_dir.iterdir():
        if item.name == "archive":
            continue
        shutil.move(str(item), str(archive_dir / item.name))
    return archive_dir


def _read_version(state_path: Path) -> int:
    """Parse `schema_version` from `state.yaml`."""
    loaded: object = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(
            f"{state_path}: top-level must be a mapping, got "
            f"{type(loaded).__name__}"
        )
    version: object = cast("dict[str, object]", loaded).get("schema_version")
    if not isinstance(version, int):
        raise ValueError(
            f"{state_path}: schema_version must be an integer, got "
            f"{type(version).__name__}"
        )
    return version


def _write_state(state_path: Path, version: int) -> None:
    """Write a fresh `state.yaml` at the given schema version."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        f"schema_version: {version}\n", encoding="utf-8"
    )


def _load_config_data(path: Path) -> dict[str, object]:
    """Parse config.yaml into a dict (no validation here)."""
    loaded: object = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        return {}
    return cast("dict[str, object]", loaded)
