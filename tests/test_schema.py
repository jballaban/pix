"""Tests for `pix.schema` — library-state version check + archive-reset."""

from __future__ import annotations

from pathlib import Path

import pytest

from pix.schema import (
    SCHEMA_VERSION,
    SchemaTooNew,
    ensure_current,
)


def _make_lib(tmp_path: Path) -> Path:
    """Create a library root with a `.pix/` and a custom config file."""
    root = tmp_path / "lib"
    root.mkdir()
    (root / ".pix").mkdir()
    (root / ".pix" / "config.yaml").write_text(
        "extensions:\n  jpg: keep\n  custom: keep\n",
        encoding="utf-8",
    )
    return root


def _write_state(root: Path, version: int) -> None:
    (root / ".pix" / "state.yaml").write_text(
        f"schema_version: {version}\n", encoding="utf-8"
    )


def test_bootstrap_writes_state_when_missing(tmp_path: Path) -> None:
    root = _make_lib(tmp_path)
    state_path = root / ".pix" / "state.yaml"
    assert not state_path.exists()

    result = ensure_current(root)

    assert result.archived_from is None
    assert state_path.exists()
    # The user's custom config is untouched — bootstrap doesn't archive.
    assert "custom: keep" in (root / ".pix" / "config.yaml").read_text(
        encoding="utf-8"
    )


def test_no_action_when_version_matches(tmp_path: Path) -> None:
    root = _make_lib(tmp_path)
    _write_state(root, SCHEMA_VERSION)
    custom_config = (root / ".pix" / "config.yaml").read_text(encoding="utf-8")

    result = ensure_current(root)

    assert result.archived_from is None
    assert (root / ".pix" / "config.yaml").read_text(
        encoding="utf-8"
    ) == custom_config


def test_archives_and_resets_when_version_older(tmp_path: Path) -> None:
    root = _make_lib(tmp_path)
    _write_state(root, 0)  # pretend prior schema
    (root / ".pix" / "runs").mkdir()
    (root / ".pix" / "runs" / "2024-old-run").mkdir()
    (root / ".pix" / "runs" / "2024-old-run" / "plan.txt").write_text("old plan")

    # Sanity: we're at SCHEMA_VERSION 1 in the test environment.
    assert SCHEMA_VERSION >= 1

    result = ensure_current(root)

    assert result.archived_from == 0
    archive = root / ".pix" / "archive" / "v0"
    assert archive.exists()
    # Prior contents now live in the archive.
    assert (archive / "config.yaml").exists()
    assert "custom: keep" in (archive / "config.yaml").read_text(
        encoding="utf-8"
    )
    assert (archive / "runs" / "2024-old-run" / "plan.txt").read_text(
        encoding="utf-8"
    ) == "old plan"
    assert (archive / "state.yaml").exists()
    # Fresh state created at the current schema version.
    assert (root / ".pix" / "config.yaml").exists()
    assert (root / ".pix" / "state.yaml").exists()
    assert f"schema_version: {SCHEMA_VERSION}" in (
        root / ".pix" / "state.yaml"
    ).read_text(encoding="utf-8")
    # Fresh config is the default (no `custom: keep`).
    assert "custom: keep" not in (
        root / ".pix" / "config.yaml"
    ).read_text(encoding="utf-8")


def test_refuses_when_version_newer(tmp_path: Path) -> None:
    root = _make_lib(tmp_path)
    _write_state(root, SCHEMA_VERSION + 5)

    with pytest.raises(SchemaTooNew):
        ensure_current(root)

    # Nothing was touched.
    assert "custom: keep" in (root / ".pix" / "config.yaml").read_text(
        encoding="utf-8"
    )


def test_archive_folder_itself_is_not_archived(tmp_path: Path) -> None:
    """A second reset shouldn't move an existing archive/ into a new archive/."""
    root = _make_lib(tmp_path)
    _write_state(root, 0)
    # Pre-existing archive from some imagined prior reset.
    (root / ".pix" / "archive").mkdir()
    (root / ".pix" / "archive" / "marker").write_text("kept")

    ensure_current(root)

    # archive/ stayed at the top level, with the new v0 subfolder added.
    assert (root / ".pix" / "archive" / "marker").exists()
    assert (root / ".pix" / "archive" / "v0").exists()


def test_refuses_when_archive_target_already_exists(tmp_path: Path) -> None:
    """Hitting the same archive path twice would silently lose data."""
    root = _make_lib(tmp_path)
    _write_state(root, 0)
    (root / ".pix" / "archive" / "v0").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="already exists"):
        ensure_current(root)
