"""Shared pytest fixtures for the pix test suite."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from pix import cache_db


@pytest.fixture(autouse=True)
def _close_cache_db_connections() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Close any cached SQLite connections after each test.

    The store keeps one process-wide connection per library (pix runs one
    command per process). Tests create many short-lived libraries under
    tmp_path; closing connections at teardown frees the file handles so
    Windows can clean up the temp dirs.
    """
    yield
    cache_db.close_all()


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
