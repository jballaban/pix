from __future__ import annotations

from pathlib import Path

import pytest

from pix.root import NoLibraryRoot, resolve
from pix.schema import SCHEMA_VERSION, SchemaUpgradeRequired


def _make_lib(parent: Path, name: str = "library") -> Path:
    """Create a library root with .pix/ and a current-version state.yaml."""
    root = parent / name
    root.mkdir()
    (root / ".pix").mkdir()
    (root / ".pix" / "state.yaml").write_text(
        f"schema_version: {SCHEMA_VERSION}\n", encoding="utf-8"
    )
    return root


def test_resolve_walks_up_from_start(tmp_path: Path) -> None:
    root = _make_lib(tmp_path)
    sub = root / "deep" / "nested"
    sub.mkdir(parents=True)
    assert resolve(start=sub) == root


def test_resolve_returns_start_when_pix_at_start(tmp_path: Path) -> None:
    (tmp_path / ".pix").mkdir()
    (tmp_path / ".pix" / "state.yaml").write_text(
        f"schema_version: {SCHEMA_VERSION}\n", encoding="utf-8"
    )
    assert resolve(start=tmp_path) == tmp_path


def test_resolve_falls_back_to_cwd_walk_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When start is outside any library, walk up from CWD."""
    root = _make_lib(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(root)
    assert resolve(start=elsewhere) == root


def test_resolve_raises_when_no_pix_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PIX_ROOT", raising=False)
    with pytest.raises(NoLibraryRoot):
        resolve(start=tmp_path)


def test_resolve_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_lib(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("PIX_ROOT", str(root))
    monkeypatch.chdir(elsewhere)
    assert resolve(start=elsewhere) == root


def test_resolve_env_var_must_be_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bogus = tmp_path / "not-a-library"
    bogus.mkdir()
    monkeypatch.setenv("PIX_ROOT", str(bogus))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(NoLibraryRoot, match="does not contain a .pix"):
        resolve(start=tmp_path / "elsewhere")


def test_start_takes_precedence_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _make_lib(tmp_path, "a")
    b = _make_lib(tmp_path, "b")
    monkeypatch.setenv("PIX_ROOT", str(b))
    assert resolve(start=a) == a


def test_resolve_raises_schema_upgrade_required_when_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older schema_version is no longer auto-fixed."""
    root = tmp_path / "library"
    root.mkdir()
    (root / ".pix").mkdir()
    # Pretend the library is at v1 while pix expects v2+.
    (root / ".pix" / "state.yaml").write_text(
        "schema_version: 1\n", encoding="utf-8"
    )
    monkeypatch.delenv("PIX_ROOT", raising=False)
    with pytest.raises(SchemaUpgradeRequired):
        resolve(start=root)


def test_resolve_check_schema_false_skips_version_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pix upgrade` resolves without the schema check."""
    root = tmp_path / "library"
    root.mkdir()
    (root / ".pix").mkdir()
    (root / ".pix" / "state.yaml").write_text(
        "schema_version: 1\n", encoding="utf-8"
    )
    monkeypatch.delenv("PIX_ROOT", raising=False)
    # Would raise without `check_schema=False`.
    assert resolve(start=root, check_schema=False) == root
