"""Configuration handling for `<library-root>/.pix/config.yaml`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml

ExtensionAction = Literal["keep", "convert_to_jpg", "convert_to_mp4", "delete"]
VALID_ACTIONS: frozenset[str] = frozenset(
    {"keep", "convert_to_jpg", "convert_to_mp4", "delete"}
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
  ds_store: delete    # macOS system junk
  thumbs.db: delete   # Windows system junk
"""


@dataclass(frozen=True)
class Config:
    """Parsed pix configuration. Currently extension policy only."""

    extensions: dict[str, ExtensionAction]

    @classmethod
    def load(cls, path: Path) -> Config:
        with path.open(encoding="utf-8") as f:
            loaded: object = yaml.safe_load(f)

        if not isinstance(loaded, dict):
            raise ValueError(
                f"{path}: top-level must be a mapping, "
                f"got {type(loaded).__name__}"
            )
        data = cast("dict[str, object]", loaded)
        return cls(extensions=_parse_extensions(path, data.get("extensions")))


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
