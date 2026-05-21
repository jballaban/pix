from __future__ import annotations

from pathlib import Path

import pytest

from pix.root import NoLibraryRoot, resolve


def test_resolve_walks_up_to_find_pix_dir(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (root / ".pix").mkdir()

    sub = root / "deep" / "nested"
    sub.mkdir(parents=True)

    assert resolve(start=sub)[0] == root


def test_resolve_returns_start_when_pix_at_start(tmp_path: Path) -> None:
    (tmp_path / ".pix").mkdir()
    assert resolve(start=tmp_path)[0] == tmp_path


def test_resolve_raises_when_no_pix_found(tmp_path: Path) -> None:
    # tmp_path itself has no .pix and (on most CI/dev machines) no ancestor does either.
    # If a developer happens to run tests from inside their own pix library, this would
    # fail; tmp_path is outside any plausible user library, so it's safe.
    with pytest.raises(NoLibraryRoot):
        resolve(start=tmp_path)


def test_resolve_with_override(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (root / ".pix").mkdir()

    sub = tmp_path / "elsewhere"
    sub.mkdir()

    assert resolve(start=sub, override=root)[0] == root


def test_resolve_override_without_pix_raises(tmp_path: Path) -> None:
    with pytest.raises(NoLibraryRoot, match="does not contain a .pix"):
        resolve(override=tmp_path)


def test_resolve_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (root / ".pix").mkdir()

    sub = tmp_path / "elsewhere"
    sub.mkdir()

    monkeypatch.setenv("PIX_ROOT", str(root))
    assert resolve(start=sub)[0] == root


def test_override_beats_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = tmp_path / "a"
    a.mkdir()
    (a / ".pix").mkdir()

    b = tmp_path / "b"
    b.mkdir()
    (b / ".pix").mkdir()

    monkeypatch.setenv("PIX_ROOT", str(a))
    assert resolve(override=b)[0] == b
