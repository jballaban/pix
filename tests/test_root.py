from __future__ import annotations

from pathlib import Path

import pytest

from pix.root import NoLibraryRoot, resolve


def test_resolve_walks_up_from_start(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (root / ".pix").mkdir()

    sub = root / "deep" / "nested"
    sub.mkdir(parents=True)

    assert resolve(start=sub)[0] == root


def test_resolve_returns_start_when_pix_at_start(tmp_path: Path) -> None:
    (tmp_path / ".pix").mkdir()
    assert resolve(start=tmp_path)[0] == tmp_path


def test_resolve_falls_back_to_cwd_walk_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When start is outside any library, walk up from CWD."""
    root = tmp_path / "library"
    root.mkdir()
    (root / ".pix").mkdir()

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    # CWD inside library; start outside.
    monkeypatch.chdir(root)
    assert resolve(start=elsewhere)[0] == root


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
    root = tmp_path / "library"
    root.mkdir()
    (root / ".pix").mkdir()

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    monkeypatch.setenv("PIX_ROOT", str(root))
    # start outside library, CWD outside library — env should win.
    monkeypatch.chdir(elsewhere)
    assert resolve(start=elsewhere)[0] == root


def test_resolve_env_var_must_be_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PIX_ROOT set to a non-library path is an error."""
    bogus = tmp_path / "not-a-library"
    bogus.mkdir()
    monkeypatch.setenv("PIX_ROOT", str(bogus))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(NoLibraryRoot, match="does not contain a .pix"):
        resolve(start=tmp_path / "elsewhere")


def test_start_takes_precedence_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `start` finds a library, env var is not consulted."""
    a = tmp_path / "a"
    a.mkdir()
    (a / ".pix").mkdir()

    b = tmp_path / "b"
    b.mkdir()
    (b / ".pix").mkdir()

    monkeypatch.setenv("PIX_ROOT", str(b))
    # start at `a` finds a; env wanted `b` but is ignored.
    assert resolve(start=a)[0] == a
