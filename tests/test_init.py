from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pix.cli import app

runner = CliRunner()


def test_init_creates_pix_dir_and_default_config(tmp_path: Path) -> None:
    target = tmp_path / "library"
    result = runner.invoke(app, ["init", str(target)])

    assert result.exit_code == 0, result.output
    assert (target / ".pix").is_dir()

    config_path = target / ".pix" / "config.yaml"
    assert config_path.is_file()

    config_text = config_path.read_text(encoding="utf-8")
    assert "jpg:" in config_text
    assert "heic:" in config_text
    assert "mov:" in config_text
    assert "thumbs.db" in config_text

    state_path = target / ".pix" / "state.yaml"
    assert state_path.is_file()
    assert "schema_version:" in state_path.read_text(encoding="utf-8")


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
