"""Shared pytest fixtures for the pix test suite."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def patched_hash_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[Path, str]:
    """Returns a `{resolved_path: hash}` dict that tests populate.

    Dedupe consumes the dict directly — tests pass it to
    `generate_plan(..., hashes=patched_hash_cache)` as the precomputed
    hash map (built in production via `read_all_cached_hashes`).

    Organize still calls `read_cached_hash` per-file at the module
    level, so its binding is monkeypatched to read from this dict.
    """
    hashes: dict[Path, str] = {}

    def fake_read(library_root: Path, file_path: Path) -> str | None:
        del library_root
        return hashes.get(file_path)

    monkeypatch.setattr("pix.organize.read_cached_hash", fake_read)

    return hashes
