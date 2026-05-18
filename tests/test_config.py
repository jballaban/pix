from __future__ import annotations

from pathlib import Path

import pytest

from pix.config import DEFAULT_CONFIG_YAML, Config


def test_default_config_parses(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")

    cfg = Config.load(config_path)

    assert cfg.extensions["jpg"] == "keep"
    assert cfg.extensions["heic"] == "convert_to_jpg"
    assert cfg.extensions["mov"] == "convert_to_mp4"
    assert cfg.extensions["thumbs.db"] == "delete"


def test_config_normalizes_extension_case(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "extensions:\n  JPG: keep\n  .HEIC: convert_to_jpg\n",
        encoding="utf-8",
    )

    cfg = Config.load(config_path)

    assert cfg.extensions["jpg"] == "keep"
    assert cfg.extensions["heic"] == "convert_to_jpg"


def test_config_rejects_invalid_action(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "extensions:\n  jpg: invalid_action\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid action"):
        Config.load(config_path)


def test_config_rejects_non_mapping_top_level(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("- jpg\n- heic\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must be a mapping"):
        Config.load(config_path)


def test_empty_extensions_section(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("extensions: {}\n", encoding="utf-8")

    cfg = Config.load(config_path)
    assert cfg.extensions == {}
