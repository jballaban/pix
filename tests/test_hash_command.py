"""End-to-end smoke tests for `pix hash`."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from pix.commands.hash import hash_library
from pix.commands.init import init_library
from pix.hash_cache import read_cached_hash, write_cached_hash


@pytest.fixture
def auto_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-accept the `Proceed? [Y/n]` prompt."""
    monkeypatch.setattr("pix.commands.hash.prompt_proceed", lambda: True)


def _init_library_with_files(
    root: Path, names: list[str]
) -> list[Path]:
    """Create a library at `root` with files named `names` (under root)."""
    root.mkdir(parents=True, exist_ok=True)
    init_library(root)
    paths: list[Path] = []
    for n in names:
        p = root / n
        p.parent.mkdir(parents=True, exist_ok=True)
        # Tiny but valid-ish bytes — content_hash falls back to raw for
        # extensions we don't have specific framing for.
        p.write_bytes(b"some content for " + n.encode())
        paths.append(p.resolve())
    return paths


def test_hash_populates_cache_for_every_file(
    tmp_path: Path, auto_yes: None, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "lib"
    paths = _init_library_with_files(root, ["a.bin", "b.bin"])

    hash_library(root)

    out = capsys.readouterr().out
    assert "2 file(s) need hashing" in out
    assert "Hashed 2 file(s)" in out
    for p in paths:
        assert read_cached_hash(root, p) is not None


def test_hash_is_idempotent(
    tmp_path: Path, auto_yes: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fully-hashed library produces `0 files need hashing.` and exits."""
    root = tmp_path / "lib"
    paths = _init_library_with_files(root, ["a.bin"])

    # Pre-seed the cache so discovery finds nothing stale.
    for p in paths:
        st = p.stat()
        write_cached_hash(
            root,
            p,
            hash_hex="seeded",
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
        )

    hash_library(root)
    out = capsys.readouterr().out
    assert "0 files need hashing" in out


def test_hash_invalidates_on_file_mutation(
    tmp_path: Path, auto_yes: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mutated file invalidates the cache entry; rerun rehashes it."""
    root = tmp_path / "lib"
    paths = _init_library_with_files(root, ["a.bin"])
    p = paths[0]

    # Seed with the file's original stat — entry is fresh.
    st_old = p.stat()
    write_cached_hash(
        root,
        p,
        hash_hex="seeded",
        size=st_old.st_size,
        mtime_ns=st_old.st_mtime_ns,
    )
    assert read_cached_hash(root, p) == "seeded"

    # Mutate content — different bytes, different size → stale entry.
    p.write_bytes(b"totally different content here")
    assert read_cached_hash(root, p) is None

    hash_library(root)
    out = capsys.readouterr().out
    assert "1 file(s) need hashing" in out
    rehashed = read_cached_hash(root, p)
    assert rehashed is not None
    assert rehashed != "seeded"


def test_hash_continues_past_per_file_failure(
    tmp_path: Path,
    auto_yes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failure on one file logs `Failed` and continues to the next file."""
    root = tmp_path / "lib"
    paths = _init_library_with_files(root, ["a.bin", "b.bin"])
    bad = paths[0]

    real_compute = __import__(
        "pix.commands.hash", fromlist=["compute_content_hash"]
    ).compute_content_hash

    def flaky_compute(p: Path) -> str:
        if p == bad:
            raise OSError("permission denied")
        return real_compute(p)

    monkeypatch.setattr(
        "pix.commands.hash.compute_content_hash", flaky_compute
    )

    with pytest.raises(typer.Exit) as exc:
        hash_library(root)
    assert exc.value.exit_code == 1

    err = capsys.readouterr().err
    assert "1 failed" in err
    # The good file still has a cache entry.
    assert read_cached_hash(root, paths[1]) is not None
    # The bad file has none.
    assert read_cached_hash(root, bad) is None
