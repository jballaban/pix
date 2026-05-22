"""Configuration handling for `<library-root>/.pix/config.yaml`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml

ExtensionAction = Literal[
    "keep", "convert_to_jpg", "convert_to_mp4", "delete", "stash"
]
VALID_ACTIONS: frozenset[str] = frozenset(
    {"keep", "convert_to_jpg", "convert_to_mp4", "delete", "stash"}
)

DEFAULT_CONFIG_YAML: str = """\
extensions:
  jpg:     keep
  jpeg:    keep
  mp4:     keep
  heic:    convert_to_jpg
  heif:    convert_to_jpg
  png:     convert_to_jpg
  mov:     convert_to_mp4
  avi:     convert_to_mp4
  mts:     convert_to_mp4   # AVCHD camcorder MPEG-TS; usually H.264, remuxes cheaply
  dng:     stash            # Adobe Digital Negative — raw sensor data; preserved for future processing
  insp:    stash            # Insta360 proprietary photo
  insv:    stash            # Insta360 proprietary video
  ds_store: delete    # macOS system junk
  thumbs.db: delete   # Windows system junk
  ini:     delete    # desktop.ini and other Windows config sidecars
  txt:     delete    # plain text files (notes, release-notes, manifests)
  json:    delete    # metadata exports, sidecars
  gif:     delete    # web-format animated images (memes, downloads)
  webp:    delete    # web image format (downloads, screenshots)
  jwt:     delete    # Microsoft auth-broker trust manifests synced by OneDrive
"""


@dataclass(frozen=True)
class Config:
    """Parsed pix configuration."""

    extensions: dict[str, ExtensionAction]
    organize_template: str | None = None

    @classmethod
    def load(cls, path: Path) -> Config:
        text = path.read_text(encoding="utf-8")

        # An upgrade may have inserted git-style conflict markers when
        # the user's existing value differed from a new default. Detect
        # before YAML parses, since markers are not valid YAML and a
        # generic parse error would be confusing.
        if "<<<<<<< " in text or "\n=======" in text or ">>>>>>> " in text:
            raise ValueError(
                f"{path}: unresolved upgrade conflict markers present. "
                f"Edit the file to remove `<<<<<<<`, `=======`, and "
                f"`>>>>>>>` markers, keeping only the line you want for "
                f"each conflicted entry."
            )

        loaded: object = yaml.safe_load(text)

        if not isinstance(loaded, dict):
            raise ValueError(
                f"{path}: top-level must be a mapping, "
                f"got {type(loaded).__name__}"
            )
        data = cast("dict[str, object]", loaded)
        return cls(
            extensions=_parse_extensions(path, data.get("extensions")),
            organize_template=_parse_organize_template(
                path, data.get("organize")
            ),
        )


def set_organize_template(path: Path, template: str) -> None:
    """Persist `template` to `config.yaml` under `organize.template`.

    Round-trips the full YAML file; comments and key ordering are not
    preserved (yaml.safe_dump output). The default config has no
    user-customized comments to lose at v1.
    """
    with path.open(encoding="utf-8") as f:
        loaded: object = yaml.safe_load(f) or {}
    if not isinstance(loaded, dict):
        raise ValueError(
            f"{path}: top-level must be a mapping, "
            f"got {type(loaded).__name__}"
        )
    data = cast("dict[str, object]", loaded)
    organize = data.get("organize")
    if not isinstance(organize, dict):
        organize = {}
    cast("dict[str, object]", organize)["template"] = template
    data["organize"] = organize
    path.write_text(
        yaml.safe_dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


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


def _parse_extensions(
    path: Path, raw: object | None
) -> dict[str, ExtensionAction]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{path}: 'extensions' must be a mapping, "
            f"got {type(raw).__name__}"
        )
    raw_dict = cast("dict[object, object]", raw)

    extensions: dict[str, ExtensionAction] = {}
    for ext_raw, action in raw_dict.items():
        ext = str(ext_raw).lower().lstrip(".")
        if action not in VALID_ACTIONS:
            raise ValueError(
                f"{path}: invalid action {action!r} for extension {ext!r}. "
                f"Must be one of {sorted(VALID_ACTIONS)}."
            )
        extensions[ext] = cast(ExtensionAction, action)
    return extensions
