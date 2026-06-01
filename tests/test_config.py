from __future__ import annotations

from pathlib import Path

import pytest

from pix.config import (
    CONFIG_FILENAME,
    EXTENSION_POLICY,
    Config,
    set_organize_template,
)


# --- format policy is a build constant, not per-library config ---


def test_extension_policy_is_the_build_constant() -> None:
    assert EXTENSION_POLICY["jpg"] == "keep"
    assert EXTENSION_POLICY["heic"] == "convert_to_jpg"
    assert EXTENSION_POLICY["dng"] == "convert_to_jpg"
    assert EXTENSION_POLICY["mov"] == "convert_to_mp4"
    assert EXTENSION_POLICY["insv"] == "keep"
    assert EXTENSION_POLICY["thumbs.db"] == "delete"


def test_load_extensions_always_from_build_not_file(tmp_path: Path) -> None:
    """The settings file never overrides format policy — a stray
    `extensions:` block is ignored; `Config.extensions` is the constant."""
    p = tmp_path / CONFIG_FILENAME
    p.write_text("extensions:\n  jpg: delete\n", encoding="utf-8")
    cfg = Config.load(p)
    assert cfg.extensions == EXTENSION_POLICY
    assert cfg.extensions["jpg"] == "keep"  # not the file's "delete"


def test_load_missing_file_is_defaults(tmp_path: Path) -> None:
    cfg = Config.load(tmp_path / "nope.yaml")
    assert cfg.extensions == EXTENSION_POLICY
    assert cfg.runs_dir is None
    assert cfg.organize_template is None


def test_load_empty_file_is_defaults(tmp_path: Path) -> None:
    p = tmp_path / CONFIG_FILENAME
    p.write_text("# just a comment\n", encoding="utf-8")
    cfg = Config.load(p)
    assert cfg.runs_dir is None
    assert cfg.organize_template is None


def test_load_rejects_non_mapping_top_level(tmp_path: Path) -> None:
    p = tmp_path / CONFIG_FILENAME
    p.write_text("- jpg\n- heic\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping"):
        Config.load(p)


# --- runs_dir ---


def test_runs_base_defaults_to_pix_runs() -> None:
    cfg = Config()
    assert cfg.runs_base(Path("G:/lib")) == Path("G:/lib") / ".pix" / "runs"


def test_runs_base_honors_configured_runs_dir() -> None:
    cfg = Config(runs_dir="F:/caps/runs")
    assert cfg.runs_base(Path("G:/lib")) == Path("F:/caps/runs")


def test_load_parses_runs_dir(tmp_path: Path) -> None:
    p = tmp_path / CONFIG_FILENAME
    p.write_text("runs_dir: F:/caps/runs\n", encoding="utf-8")
    assert Config.load(p).runs_dir == "F:/caps/runs"


def test_load_rejects_non_string_runs_dir(tmp_path: Path) -> None:
    p = tmp_path / CONFIG_FILENAME
    p.write_text("runs_dir: 123\n", encoding="utf-8")
    with pytest.raises(ValueError, match="runs_dir"):
        Config.load(p)


# --- organize.template ---


def test_load_parses_organize_template(tmp_path: Path) -> None:
    p = tmp_path / CONFIG_FILENAME
    p.write_text("organize:\n  template: '{year}/{event}'\n", encoding="utf-8")
    assert Config.load(p).organize_template == "{year}/{event}"


def test_set_organize_template_preserves_runs_dir(tmp_path: Path) -> None:
    """Saving the template round-trips the known keys, keeping a hand-set
    runs_dir (and dropping unknown keys/comments)."""
    p = tmp_path / CONFIG_FILENAME
    p.write_text(
        "runs_dir: F:/caps/runs\n# my note\nmystery: 1\n", encoding="utf-8"
    )
    set_organize_template(p, "{year}/{month}")
    cfg = Config.load(p)
    assert cfg.organize_template == "{year}/{month}"
    assert cfg.runs_dir == "F:/caps/runs"  # preserved
    assert "mystery" not in p.read_text(encoding="utf-8")  # unknown dropped
