"""Tests for `pix.schema` — ensure_current refuses; upgrade applies per-version
upgrades to the user's existing config (additive/removal/conflict)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pix import schema
from pix.schema import (
    SCHEMA_VERSION,
    SchemaTooNew,
    SchemaUpgradeRequired,
    Upgrade,
    ensure_current,
    upgrade,
)


def _make_lib(tmp_path: Path, config_text: str | None = None) -> Path:
    """Create a library root with a `.pix/` and a config.yaml."""
    root = tmp_path / "lib"
    root.mkdir()
    (root / ".pix").mkdir()
    if config_text is None:
        config_text = "extensions:\n  jpg: keep\n  custom: keep\n"
    (root / ".pix" / "config.yaml").write_text(
        config_text, encoding="utf-8"
    )
    return root


def _write_state(root: Path, version: int) -> None:
    (root / ".pix" / "state.yaml").write_text(
        f"schema_version: {version}\n", encoding="utf-8"
    )


# --- ensure_current ----------------------------------------------------------


def test_ensure_current_bootstraps_when_state_missing(tmp_path: Path) -> None:
    root = _make_lib(tmp_path)
    state = root / ".pix" / "state.yaml"
    assert not state.exists()

    ensure_current(root)

    assert state.exists()
    assert "custom: keep" in (root / ".pix" / "config.yaml").read_text(
        encoding="utf-8"
    )


def test_ensure_current_passes_when_version_matches(tmp_path: Path) -> None:
    root = _make_lib(tmp_path)
    _write_state(root, SCHEMA_VERSION)
    ensure_current(root)  # must not raise


def test_ensure_current_raises_upgrade_required_when_older(
    tmp_path: Path,
) -> None:
    root = _make_lib(tmp_path)
    _write_state(root, 0)
    with pytest.raises(SchemaUpgradeRequired) as exc:
        ensure_current(root)
    assert exc.value.library_version == 0
    assert exc.value.root == root


def test_ensure_current_raises_too_new_when_higher(tmp_path: Path) -> None:
    root = _make_lib(tmp_path)
    _write_state(root, SCHEMA_VERSION + 5)
    with pytest.raises(SchemaTooNew):
        ensure_current(root)


# --- upgrade: refusals -------------------------------------------------------


def test_upgrade_refuses_when_already_current(tmp_path: Path) -> None:
    root = _make_lib(tmp_path)
    _write_state(root, SCHEMA_VERSION)
    with pytest.raises(ValueError, match="already"):
        upgrade(root)


def test_upgrade_refuses_when_no_state_yaml(tmp_path: Path) -> None:
    root = _make_lib(tmp_path)
    with pytest.raises(ValueError, match="no state.yaml"):
        upgrade(root)


def test_upgrade_refuses_when_library_newer(tmp_path: Path) -> None:
    root = _make_lib(tmp_path)
    _write_state(root, SCHEMA_VERSION + 5)
    with pytest.raises(SchemaTooNew):
        upgrade(root)


# --- upgrade: additive / removal application --------------------------------


def test_upgrade_applies_additive_changes_to_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v1→v2 adds dng/insp/insv. User's custom mts entry is preserved."""
    root = _make_lib(
        tmp_path,
        config_text=(
            "extensions:\n"
            "  jpg: keep\n"
            "  mts: convert_to_mp4\n"
            "  custom: keep\n"
        ),
    )
    _write_state(root, 1)

    # Force a stable UPGRADES table for the test (use the real v2 entry).
    monkeypatch.setattr(
        schema,
        "UPGRADES",
        {
            2: Upgrade(
                add_extensions={"dng": "stash", "insp": "stash"},
                description="test",
            )
        },
    )
    # And SCHEMA_VERSION just to ensure consistent expectations.
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 2)

    result = upgrade(root)

    # Reports.
    assert result.from_version == 1
    assert result.to_version == 2
    assert any("dng = stash" in entry for entry in result.added)
    assert any("insp = stash" in entry for entry in result.added)
    assert result.conflicts == []

    # On-disk config has both old customizations and new defaults.
    text = (root / ".pix" / "config.yaml").read_text(encoding="utf-8")
    parsed: dict[str, dict[str, str]] = yaml.safe_load(text)
    assert parsed["extensions"]["jpg"] == "keep"
    assert parsed["extensions"]["mts"] == "convert_to_mp4"  # preserved
    assert parsed["extensions"]["custom"] == "keep"          # preserved
    assert parsed["extensions"]["dng"] == "stash"            # added
    assert parsed["extensions"]["insp"] == "stash"           # added

    # Archive captured the prior contents.
    assert (root / ".pix" / "archive" / "v1" / "config.yaml").exists()


def test_upgrade_reports_archived_user_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stash/ and errors/ swept into the archive are reported with counts so
    the command can warn — runs/ (regenerable) is not reported."""
    root = _make_lib(tmp_path)
    _write_state(root, 1)
    pix = root / ".pix"
    (pix / "stash").mkdir()
    (pix / "stash" / "a.dng").write_bytes(b"raw")
    (pix / "errors" / "G" / "pix").mkdir(parents=True)
    (pix / "errors" / "G" / "pix" / "bad.mp4").write_bytes(b"x")
    (pix / "errors" / "G" / "pix" / "bad.mp4.errorinfo").write_text(
        "original_path: G:/pix/bad.mp4\n", encoding="utf-8"
    )
    (pix / "runs").mkdir()
    (pix / "runs" / "plan.txt").write_text("noise", encoding="utf-8")

    monkeypatch.setattr(
        schema, "UPGRADES", {2: Upgrade(add_extensions={"dng": "stash"})}
    )
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 2)

    result = upgrade(root)

    by_name = {d.name: d for d in result.archived_user_data}
    assert set(by_name) == {"stash", "errors"}  # not "runs"
    assert by_name["stash"].file_count == 1
    assert by_name["errors"].file_count == 2  # data file + sidecar
    assert by_name["stash"].path == pix / "archive" / "v1" / "stash"
    # The files really moved into the archive.
    assert (pix / "archive" / "v1" / "stash" / "a.dng").is_file()
    assert not (pix / "stash").exists()


def test_upgrade_reports_no_user_data_when_dirs_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No warning data when stash/errors are absent or empty."""
    root = _make_lib(tmp_path)
    _write_state(root, 1)
    (root / ".pix" / "errors").mkdir()  # present but empty
    monkeypatch.setattr(
        schema, "UPGRADES", {2: Upgrade(add_extensions={"dng": "stash"})}
    )
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 2)

    result = upgrade(root)

    assert result.archived_user_data == []


def test_upgrade_skips_addition_when_user_already_matches_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If user already has `dng: stash`, the v2 addition is a no-op."""
    root = _make_lib(
        tmp_path,
        config_text="extensions:\n  jpg: keep\n  dng: stash\n",
    )
    _write_state(root, 1)
    monkeypatch.setattr(
        schema,
        "UPGRADES",
        {2: Upgrade(add_extensions={"dng": "stash"})},
    )
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 2)

    result = upgrade(root)

    assert result.added == []
    assert result.conflicts == []


def test_upgrade_records_conflict_when_user_value_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_lib(
        tmp_path,
        config_text="extensions:\n  jpg: keep\n  dng: convert_to_jpg\n",
    )
    _write_state(root, 1)
    monkeypatch.setattr(
        schema,
        "UPGRADES",
        {2: Upgrade(add_extensions={"dng": "stash"})},
    )
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 2)

    result = upgrade(root)

    assert len(result.conflicts) == 1
    c = result.conflicts[0]
    assert c.section == "extensions"
    assert c.key == "dng"
    assert c.current_value == "convert_to_jpg"
    assert c.new_default == "stash"
    assert c.version == 2


def test_upgrade_writes_conflict_markers_into_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_lib(
        tmp_path,
        config_text="extensions:\n  jpg: keep\n  dng: convert_to_jpg\n",
    )
    _write_state(root, 1)
    monkeypatch.setattr(
        schema,
        "UPGRADES",
        {2: Upgrade(add_extensions={"dng": "stash"})},
    )
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 2)

    upgrade(root)

    text = (root / ".pix" / "config.yaml").read_text(encoding="utf-8")
    assert "<<<<<<< current" in text
    assert "  dng: convert_to_jpg" in text
    assert "=======" in text
    assert "  dng: stash" in text
    assert ">>>>>>> v2 default" in text


def test_upgrade_applies_removals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_lib(
        tmp_path,
        config_text=(
            "extensions:\n"
            "  jpg: keep\n"
            "  obsolete_ext: delete\n"
        ),
    )
    _write_state(root, 1)
    monkeypatch.setattr(
        schema,
        "UPGRADES",
        {2: Upgrade(remove_extensions=["obsolete_ext"])},
    )
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 2)

    result = upgrade(root)

    assert any("obsolete_ext" in entry for entry in result.removed)
    text = (root / ".pix" / "config.yaml").read_text(encoding="utf-8")
    assert "obsolete_ext" not in text
    # Archive has the entry.
    archived = (
        root / ".pix" / "archive" / "v1" / "config.yaml"
    ).read_text(encoding="utf-8")
    assert "obsolete_ext: delete" in archived


def test_upgrade_walks_multiple_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v1→v3 applies v2 then v3 entries in order."""
    root = _make_lib(
        tmp_path, config_text="extensions:\n  jpg: keep\n"
    )
    _write_state(root, 1)
    monkeypatch.setattr(
        schema,
        "UPGRADES",
        {
            2: Upgrade(add_extensions={"dng": "stash"}),
            3: Upgrade(add_extensions={"webp": "convert_to_jpg"}),
        },
    )
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 3)

    result = upgrade(root)

    assert result.from_version == 1
    assert result.to_version == 3
    text = (root / ".pix" / "config.yaml").read_text(encoding="utf-8")
    parsed: dict[str, dict[str, str]] = yaml.safe_load(text)
    assert parsed["extensions"]["dng"] == "stash"
    assert parsed["extensions"]["webp"] == "convert_to_jpg"
