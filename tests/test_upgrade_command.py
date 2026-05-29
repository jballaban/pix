"""Command-level test for `pix upgrade`'s archived-user-data warning.

`pix upgrade` archives the whole `.pix/` (including stash/ and errors/, which
hold the only copies of user files) into `.pix/archive/v<old>/`. The command
must warn loudly so those files aren't abandoned in the archive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pix import schema
from pix.commands.upgrade import upgrade_library
from pix.schema import Upgrade


def _make_lib(tmp_path: Path) -> Path:
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    (root / ".pix" / "config.yaml").write_text(
        "extensions:\n  jpg: keep\n", encoding="utf-8"
    )
    (root / ".pix" / "state.yaml").write_text(
        "schema_version: 1\n", encoding="utf-8"
    )
    return root


def test_upgrade_warns_about_archived_stash_and_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _make_lib(tmp_path)
    pix = root / ".pix"
    (pix / "stash").mkdir()
    (pix / "stash" / "keep.dng").write_bytes(b"raw")
    (pix / "errors").mkdir()
    (pix / "errors" / "bad.mp4").write_bytes(b"x")

    monkeypatch.setattr(
        schema, "UPGRADES", {2: Upgrade(add_extensions={"dng": "stash"})}
    )
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 2)

    upgrade_library(root)

    err = capsys.readouterr().err
    assert "IMPORTANT" in err
    assert ".pix/stash/" in err
    assert ".pix/errors/" in err
    assert "auto-retried" in err  # explains the migrate consequence


def test_upgrade_quiet_when_no_user_data(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No scary warning when there's nothing irreplaceable in the archive."""
    root = _make_lib(tmp_path)
    monkeypatch.setattr(
        schema, "UPGRADES", {2: Upgrade(add_extensions={"dng": "stash"})}
    )
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 2)

    upgrade_library(root)

    captured = capsys.readouterr()
    assert "IMPORTANT" not in (captured.out + captured.err)
