"""Command-level tests for `pix dedupe` console output."""

from __future__ import annotations

from pathlib import Path

import pytest

from pix.commands.dedupe import dedupe_library
from pix.config import DEFAULT_CONFIG_YAML
from pix.hash_cache import write_cached_hash
from pix.metadata_cache import PerFileCache
from pix.plan import PIX_ORIGINAL_PATH
from pix.schema import SCHEMA_VERSION


def _make_library(tmp_path: Path) -> Path:
    root = tmp_path / "lib"
    pix = root / ".pix"
    pix.mkdir(parents=True)
    (pix / "config.yaml").write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    (pix / "state.yaml").write_text(
        f"schema_version: {SCHEMA_VERSION}\n", encoding="utf-8"
    )
    return root


def test_noop_dedupe_is_terse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No duplicates → just the no-op line, no 'Plan written' / 'Summary'
    (matches migrate/hash/organize)."""
    root = _make_library(tmp_path)
    a = (root / "a.jpg").resolve()
    b = (root / "b.jpg").resolve()
    a.write_bytes(b"aaaa")
    b.write_bytes(b"bbbb")

    # Seed caches so prereqs pass with no ExifTool / hash compute.
    cache = PerFileCache.for_library(root)
    for f, h in ((a, "h1"), (b, "h2")):  # distinct hashes ⇒ no duplicates
        cache.add(f, {PIX_ORIGINAL_PATH: f"F:/source/{f.name}"})
        st = f.stat()
        write_cached_hash(
            root, f, hash_hex=h, size=st.st_size, mtime_ns=st.st_mtime_ns
        )

    dedupe_library(path=root)
    out = capsys.readouterr().out
    assert "no duplicates found" in out.lower()
    assert "Plan written" not in out
    assert "Summary" not in out
