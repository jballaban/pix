"""Command-level tests for `pix organize`, esp. the bare/no-template form.

`pix organize <path>` with no template re-applies the stored
`organize.template` (spec/organize.md → Active template persistence).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from pix.commands.organize import organize_library
from pix.config import DEFAULT_CONFIG_YAML
from pix.schema import SCHEMA_VERSION


def _make_library(tmp_path: Path, *, template: str | None = None) -> Path:
    """Create an empty library root with a valid .pix/ and optional template."""
    root = tmp_path / "lib"
    pix = root / ".pix"
    pix.mkdir(parents=True)
    config_text = DEFAULT_CONFIG_YAML
    if template is not None:
        config_text += f'\norganize:\n  template: "{template}"\n'
    (pix / "config.yaml").write_text(config_text, encoding="utf-8")
    (pix / "state.yaml").write_text(
        f"schema_version: {SCHEMA_VERSION}\n", encoding="utf-8"
    )
    return root


def test_bare_organize_errors_when_no_stored_template(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _make_library(tmp_path, template=None)
    with pytest.raises(typer.Exit) as exc:
        organize_library(path=root, template_str=None)
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "no template given and none stored" in err


def test_bare_organize_uses_stored_template(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With a stored template, bare organize falls back to it and proceeds.

    The library is empty, so it short-circuits at "nothing to organize"
    — which proves the template fallback resolved (no required-template
    error, no parse error).
    """
    root = _make_library(tmp_path, template="{year}/{event}")
    organize_library(path=root, template_str=None)  # no raise
    out = capsys.readouterr().out
    assert "empty" in out.lower()
