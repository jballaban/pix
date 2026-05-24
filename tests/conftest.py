"""Shared pytest fixtures for the pix test suite."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def patched_hash_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[Path, str]:
    """Patches `read_cached_hash` to read from a dict.

    Returns the dict — tests populate it with `{resolved_path: hash}` and
    dedupe/organize will see those hashes as if they came from the real
    `.pix/cache/.../<filename>.hash` layer.

    Production code's `read_cached_hash` always returns None (stub until
    `pix hash` lands); production-code consumers (`pix.dedupe`,
    `pix.organize`) import the symbol at module load, so we patch each
    consumer's binding to keep the override scoped per-test.
    """
    hashes: dict[Path, str] = {}

    def fake_read(library_root: Path, file_path: Path) -> str | None:
        del library_root
        return hashes.get(file_path)

    for module in ("pix.dedupe", "pix.organize"):
        monkeypatch.setattr(f"{module}.read_cached_hash", fake_read)

    return hashes
