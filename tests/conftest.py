"""Shared pytest fixtures for the pix test suite."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def patched_hash_cache() -> dict[Path, str | None]:
    """Returns a `{resolved_path: hash}` dict that tests populate.

    Both dedupe and organize consume a precomputed hash map directly —
    tests pass this dict to
    `generate_plan(..., hashes=patched_hash_cache)` instead of relying
    on a monkeypatched `read_cached_hash`. The value type mirrors that
    `hashes` parameter (`str | None`; None marks a file with no cached
    hash). The fixture exists so test setup can pass the hash dict around
    like the cache dict.
    """
    return {}
