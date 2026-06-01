from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pix.cli import app

runner = CliRunner()


def test_init_creates_pix_dir_and_settings_file(tmp_path: Path) -> None:
    target = tmp_path / "library"
    result = runner.invoke(app, ["init", str(target)])

    assert result.exit_code == 0, result.output
    assert (target / ".pix").is_dir()

    # Settings file is pix.yaml now (format policy is a build constant, not
    # written per-library); no state.yaml (the library is version-less).
    assert (target / ".pix" / "pix.yaml").is_file()
    assert not (target / ".pix" / "state.yaml").exists()
    assert not (target / ".pix" / "config.yaml").exists()

    # A fresh settings file loads to defaults (build policy).
    from pix.config import Config, settings_path

    cfg = Config.load(settings_path(target))
    assert cfg.extensions["jpg"] == "keep"
    assert cfg.runs_dir is None
    assert cfg.organize_template is None


def test_init_fails_if_already_initialized(tmp_path: Path) -> None:
    target = tmp_path / "library"
    target.mkdir()
    (target / ".pix").mkdir()

    result = runner.invoke(app, ["init", str(target)])

    assert result.exit_code != 0
    assert "already" in result.output.lower()


def test_init_fails_if_nested(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / ".pix").mkdir()

    inner = outer / "inner"
    result = runner.invoke(app, ["init", str(inner)])

    assert result.exit_code != 0
    assert "nested" in result.output.lower()


def test_init_defaults_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".pix").is_dir()
