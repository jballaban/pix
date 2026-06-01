"""Per-library settings (`<library-root>/.pix/pix.yaml`).

The format policy (which action each extension gets) is **not** per-library
— it's a property of this pix build, so it lives in the `EXTENSION_POLICY`
constant below, not in any file. `pix.yaml` holds only genuinely
library-specific settings: the optional `runs_dir` (relocate run folders)
and `organize.template` (the library's canonical shape). It's a small,
hand-editable file; pix preserves the keys it knows and drops anything
else (unknown keys, comments) when it rewrites the file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

import yaml

ExtensionAction = Literal[
    "keep", "convert_to_jpg", "convert_to_mp4", "delete", "stash"
]

# Per-library settings file. (Was `config.yaml`; renamed since it no longer
# holds the format policy — it's pix's small per-library settings file.)
CONFIG_FILENAME: str = "pix.yaml"


def settings_path(root: Path) -> Path:
    """Path to a library's settings file: `<root>/.pix/pix.yaml`."""
    return root / ".pix" / CONFIG_FILENAME


# The format policy — what action each source extension gets. This is a
# property of the pix *build*, not of any library, so it lives here as a
# constant (no per-library copy, no override). Updating pix updates the
# policy for every library with zero migration. Unknown extensions abort
# migrate (see plan.lookup_policy). Adding a new *target* action still
# requires code (a converter); adding an extension to an existing action
# is a one-line edit here.
EXTENSION_POLICY: dict[str, ExtensionAction] = {
    "jpg": "keep",
    "jpeg": "keep",
    "mp4": "keep",
    "m4v": "keep",  # Apple-branded MP4; same bytes, different extension
    "heic": "convert_to_jpg",
    "heif": "convert_to_jpg",
    "png": "convert_to_jpg",
    "bmp": "convert_to_jpg",  # uncompressed bitmap; re-encode reclaims space
    "mov": "convert_to_mp4",
    "avi": "convert_to_mp4",
    "mts": "convert_to_mp4",  # AVCHD camcorder MPEG-TS
    "mpg": "convert_to_mp4",  # MPEG-1/2 Program Stream
    "mpeg": "convert_to_mp4",
    "vob": "convert_to_mp4",  # DVD-Video object (MPEG-2 PS)
    "dng": "convert_to_jpg",  # raw photo → JPG; un-developable raws fail → .pix/errors/
    "insp": "keep",  # Insta360 360 photo — kept verbatim, tagged, not renamed
    "insv": "keep",  # Insta360 360 video — kept verbatim, tagged, not renamed
    "ds_store": "delete",  # macOS junk
    "thumbs.db": "delete",  # Windows junk
    "ini": "delete",  # desktop.ini etc.
    "txt": "delete",
    "json": "delete",
    "gif": "delete",  # web-format throwaways
    "webp": "delete",
    "jwt": "delete",  # MSAL broker manifests OneDrive syncs in
}


@dataclass(frozen=True)
class Config:
    """A library's settings.

    `extensions` is the build's `EXTENSION_POLICY` (never read from the
    settings file); the field exists so callers and tests can inject a
    policy, but `load` always populates it from the constant.
    """

    extensions: dict[str, ExtensionAction] = field(
        default_factory=lambda: dict(EXTENSION_POLICY)
    )
    organize_template: str | None = None
    runs_dir: str | None = None

    def runs_base(self, root: Path) -> Path:
        """Directory that holds per-run folders (`<base>/<run-id>/`).

        Defaults to `<root>/.pix/runs`. Repointable onto another volume via
        the optional `runs_dir` setting — handy when the library drive is
        full and the conserved-original captures are large. Captures then
        move cross-volume via `timeout.safe_move` (copy+delete) instead of
        an atomic same-volume rename. Only the run folders relocate;
        staging, markers, and the media tree stay on the library volume.
        """
        if self.runs_dir:
            return Path(self.runs_dir)
        return root / ".pix" / "runs"

    @classmethod
    def load(cls, path: Path) -> Config:
        """Load a library's settings. Missing or empty file → defaults.

        Never reads format policy from the file — `extensions` is always
        the build's `EXTENSION_POLICY`.
        """
        if not path.is_file():
            return cls()
        loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        if loaded is None:
            return cls()
        if not isinstance(loaded, dict):
            raise ValueError(
                f"{path}: top-level must be a mapping, "
                f"got {type(loaded).__name__}"
            )
        data = cast("dict[str, object]", loaded)
        return cls(
            organize_template=_parse_organize_template(
                path, data.get("organize")
            ),
            runs_dir=_parse_runs_dir(path, data.get("runs_dir")),
        )


def set_organize_template(path: Path, template: str) -> None:
    """Persist `template` as `organize.template` in `pix.yaml`.

    Writes only the keys pix knows (`runs_dir`, `organize.template`),
    preserving an existing `runs_dir`; unknown keys and comments are
    dropped (the file is pix-managed, hand-editable for the known keys).
    """
    existing = Config.load(path)
    _write_settings(path, runs_dir=existing.runs_dir, organize_template=template)


def _write_settings(
    path: Path, *, runs_dir: str | None, organize_template: str | None
) -> None:
    data: dict[str, object] = {}
    if runs_dir:
        data["runs_dir"] = runs_dir
    if organize_template:
        data["organize"] = {"template": organize_template}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
        if data
        else "",
        encoding="utf-8",
    )


def _parse_runs_dir(path: Path, raw: object | None) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(
            f"{path}: 'runs_dir' must be a string path, "
            f"got {type(raw).__name__}"
        )
    return raw or None


def _parse_organize_template(path: Path, raw: object | None) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(
            f"{path}: 'organize' must be a mapping, "
            f"got {type(raw).__name__}"
        )
    template = cast("dict[str, object]", raw).get("template")
    if template is None:
        return None
    if not isinstance(template, str):
        raise ValueError(
            f"{path}: 'organize.template' must be a string, "
            f"got {type(template).__name__}"
        )
    return template
