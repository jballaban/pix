"""Tests for `pix.schema` — ensure_current refuses; upgrade does the work."""

from __future__ import annotations

from pathlib import Path

import pytest

from pix.schema import (
    SCHEMA_VERSION,
    SchemaTooNew,
    SchemaUpgradeRequired,
    ensure_current,
    upgrade,
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


# --- ensure_current ----------------------------------------------------------


def test_ensure_current_bootstraps_when_state_missing(tmp_path: Path) -> None:
    """No state.yaml = pre-versioning library; bootstrap silently."""
    root = _make_lib(tmp_path)
    state = root / ".pix" / "state.yaml"
    assert not state.exists()

    ensure_current(root)

    assert state.exists()
    # User's custom config untouched.
    assert "custom: keep" in (root / ".pix" / "config.yaml").read_text(
        encoding="utf-8"
    )


def test_ensure_current_passes_when_version_matches(tmp_path: Path) -> None:
    root = _make_lib(tmp_path)
    _write_state(root, SCHEMA_VERSION)
    custom = (root / ".pix" / "config.yaml").read_text(encoding="utf-8")

    ensure_current(root)  # must not raise

    assert (root / ".pix" / "config.yaml").read_text(
        encoding="utf-8"
    ) == custom


def test_ensure_current_raises_upgrade_required_when_older(
    tmp_path: Path,
) -> None:
    """Older state must NOT auto-archive — user runs `pix upgrade` explicitly."""
    root = _make_lib(tmp_path)
    _write_state(root, 0)

    with pytest.raises(SchemaUpgradeRequired) as exc:
        ensure_current(root)

    assert exc.value.library_version == 0
    assert exc.value.root == root
    # Config still intact — nothing archived.
    assert "custom: keep" in (root / ".pix" / "config.yaml").read_text(
        encoding="utf-8"
    )


def test_ensure_current_raises_too_new_when_higher(tmp_path: Path) -> None:
    root = _make_lib(tmp_path)
    _write_state(root, SCHEMA_VERSION + 5)
    with pytest.raises(SchemaTooNew):
        ensure_current(root)


# --- upgrade -----------------------------------------------------------------


def test_upgrade_archives_and_resets(tmp_path: Path) -> None:
    root = _make_lib(tmp_path)
    _write_state(root, 0)
    (root / ".pix" / "runs").mkdir()
    (root / ".pix" / "runs" / "2024-old-run").mkdir()
    (root / ".pix" / "runs" / "2024-old-run" / "plan.txt").write_text(
        "old plan"
    )

    result = upgrade(root)

    assert result.from_version == 0
    assert result.to_version == SCHEMA_VERSION
    assert result.archive_path == root / ".pix" / "archive" / "v0"

    # Prior contents archived.
    assert (
        root / ".pix" / "archive" / "v0" / "config.yaml"
    ).exists()
    assert "custom: keep" in (
        root / ".pix" / "archive" / "v0" / "config.yaml"
    ).read_text(encoding="utf-8")
    assert (
        root / ".pix" / "archive" / "v0" / "runs" / "2024-old-run" / "plan.txt"
    ).read_text(encoding="utf-8") == "old plan"

    # Fresh defaults written.
    assert (root / ".pix" / "config.yaml").exists()
    assert "custom: keep" not in (
        root / ".pix" / "config.yaml"
    ).read_text(encoding="utf-8")
    assert f"schema_version: {SCHEMA_VERSION}" in (
        root / ".pix" / "state.yaml"
    ).read_text(encoding="utf-8")


def test_upgrade_refuses_when_already_current(tmp_path: Path) -> None:
    root = _make_lib(tmp_path)
    _write_state(root, SCHEMA_VERSION)
    with pytest.raises(ValueError, match="already"):
        upgrade(root)


def test_upgrade_refuses_when_no_state_yaml(tmp_path: Path) -> None:
    """Pre-versioning library has no state.yaml — there's nothing to upgrade.
    A normal command will silently bootstrap; `pix upgrade` refuses."""
    root = _make_lib(tmp_path)
    with pytest.raises(ValueError, match="no state.yaml"):
        upgrade(root)


def test_upgrade_refuses_when_library_newer(tmp_path: Path) -> None:
    root = _make_lib(tmp_path)
    _write_state(root, SCHEMA_VERSION + 5)
    with pytest.raises(SchemaTooNew):
        upgrade(root)


def test_upgrade_refuses_when_archive_target_already_exists(
    tmp_path: Path,
) -> None:
    root = _make_lib(tmp_path)
    _write_state(root, 0)
    (root / ".pix" / "archive" / "v0").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="already exists"):
        upgrade(root)
