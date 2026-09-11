"""Shared pytest fixtures for the pix test suite."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from pix import cache_db, sync_check


@pytest.fixture(autouse=True)
def _neutralize_sync_check(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`resolve` runs a sync-client readiness `boot_check` on every command.
    Tests must not depend on whether the dev machine has Synology Drive Client
    installed, so make the detector see no client (→ silent no-op). Tests that
    exercise `sync_check` directly pass an explicit `data_dir` and bypass this.
    """
    monkeypatch.setattr(sync_check, "_synology_data_dir", lambda: None)
    sync_check._cache.clear()


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


@pytest.fixture(autouse=True)
def _isolate_nas_paths(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No test may read or write the real NAS share or the real staging drive.

    The `pix.nas` modules bind their roots at import time
    (`from ... import MASTER_DIR`), so each module holds its own reference and a
    test has to patch every one it touches. That is a trap: adding a new tier
    means every existing fixture is silently wrong, and the failure mode is
    writing into the live archive rather than an error.

    This ran into reality — a `META_DIR` added without updating two fixtures put
    test folders on the production share. So rather than trusting fixtures to
    keep up, any module attribute pointing under a real root is redirected to a
    per-test sandbox. Tests that patch explicitly still win; they simply no
    longer *have* to.

    **Every module in `pix.nas` is scanned**, discovered rather than listed.
    A hard-coded list is the same trap one level up: `ACCOUNTS_FILE` arrived in
    a module the list did not name, and would have written the household's
    logins onto the live share on the next test run.
    """
    import importlib
    import pkgutil

    import pix.nas
    from pix.nas import const, derive

    modules = [const]
    for info in pkgutil.iter_modules(pix.nas.__path__):
        try:
            modules.append(importlib.import_module(f"pix.nas.{info.name}"))
        except Exception:                        # noqa: BLE001
            continue                             # optional deps, not our problem

    real_roots = (const.MASTER_SHARE, const.LOCAL_ROOT)
    sandbox = tmp_path / "_nas_sandbox"

    def under_real_root(value: Path) -> bool:
        for root in real_roots:
            try:
                value.relative_to(root)
                return True
            except ValueError:
                continue
        return value in real_roots

    # `_scratch` is a *function*, so the attribute scan below cannot see it —
    # and it is the one that bit us: a test suite sweeping the shared poster-frame
    # directory deleted a live run's temps mid-read.
    monkeypatch.setattr(derive, "_scratch", lambda: sandbox / "scratch")

    for module in modules:
        for name, value in list(vars(module).items()):
            if isinstance(value, Path) and under_real_root(value):
                monkeypatch.setattr(module, name, sandbox / name.lower(),
                                    raising=False)
