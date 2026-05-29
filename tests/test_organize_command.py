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
from pix.hash_cache import write_cached_hash
from pix.metadata_cache import PerFileCache
from pix.plan import PIX_DATE_AUTO, PIX_EVENT_AUTO, PIX_ORIGINAL_PATH
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


def test_noop_organize_is_terse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A library already in the target shape prints just the no-op line —
    no 'Plan written' / 'Summary' noise (matches migrate/hash)."""
    root = _make_library(tmp_path, template="{year}/{event}")
    # File already at its canonical target for {year}/{event}.
    media = (root / "2023" / "Hawaii" / "2023-08-15_143205.jpg").resolve()
    media.parent.mkdir(parents=True)
    media.write_bytes(b"data")

    # Seed both caches so plan-gen needs neither ExifTool nor a hash compute.
    PerFileCache.for_library(root).add(
        media,
        {
            PIX_ORIGINAL_PATH: "F:/source/x.jpg",
            PIX_DATE_AUTO: "2023-08-15-14:32:05",
            PIX_EVENT_AUTO: "Hawaii",
        },
    )
    st = media.stat()
    write_cached_hash(
        root, media, hash_hex="h", size=st.st_size, mtime_ns=st.st_mtime_ns
    )

    organize_library(path=root, template_str="{year}/{event}")
    out = capsys.readouterr().out
    assert "nothing to do" in out.lower()
    assert "Plan written" not in out
    assert "Summary" not in out
