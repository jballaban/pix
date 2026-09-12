"""Shared pytest fixtures for the pix test suite."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pix import cache_db, sync_check

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient


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


# --- the NAS app ---------------------------------------------------------------
#
# Shared here rather than in one test module, because several exercise the same
# running app: the browse page, the account boundary, and the operation log.
# Importing a fixture from another test module works at runtime and does not
# type-check, which is a needless thing to explain to the next reader.

@pytest.fixture
def app_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A small archive on disk, indexed, with the app pointed at it."""
    import json

    from pix.nas import accounts, web
    from pix.nas import index as ix

    share = tmp_path / "nas"
    meta, master = share / "meta", share / "master"
    thumb, preview = share / "thumb", share / "preview"
    for d in (meta, master, thumb, preview):
        d.mkdir(parents=True)

    (meta / "init_2026").mkdir()
    (meta / "init_2026" / "a.jpg.json").write_text(json.dumps({
        "file": "a.jpg", "folder": "init_2026", "size": 10, "mtime_ns": 1,
        "exif": {"EXIF:DateTimeOriginal": "2026:08:30 15:34:55",
                 "XMP:EventAuto": "Italy - Sicily"},
    }), encoding="utf-8")
    (meta / "init_2026" / "b.mp4.json").write_text(json.dumps({
        "file": "b.mp4", "folder": "init_2026", "size": 20, "mtime_ns": 1,
        "exif": {"QuickTime:Duration": "75 s",
                 "XMP:EventAuto": "Italy - Sicily"},
    }), encoding="utf-8")

    for tier in (thumb, preview):
        (tier / "init_2026").mkdir()
        (tier / "init_2026" / "a.jpg.jpg").write_bytes(b"\xff\xd8fake-jpeg")
        (tier / "init_2026" / "b.mp4.jpg").write_bytes(b"\xff\xd8fake-jpeg")

    db = tmp_path / "index.db"
    ix.build(db, meta_dir=meta, master_dir=master)

    monkeypatch.setattr(web, "DB_PATH", db)
    monkeypatch.setattr(web, "THUMB_DIR", thumb)
    monkeypatch.setattr(web, "PREVIEW_DIR", preview)
    # Accounts live in the sandbox; the autouse NAS guard already keeps
    # ACCOUNTS_FILE off the real share, and this pins it per test.
    monkeypatch.setattr(accounts, "ACCOUNTS_FILE", tmp_path / "users.json")
    return {"share": share, "thumb": thumb, "db": db}


@pytest.fixture
def master(app_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> Path:
    """A master folder holding the real files the media endpoint streams."""
    from pix.nas import web

    m = app_env["share"] / "master" / "init_2026"
    m.mkdir(parents=True, exist_ok=True)
    (m / "b.mp4").write_bytes(bytes([0, 0, 0, 0x18]) + b"ftypmp42" + b"x" * 400)
    monkeypatch.setattr(web, "MASTER_DIR", app_env["share"] / "master")
    return m


@pytest.fixture
def writable(app_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> Path:
    """Master with the real files a decision attaches to, and the tier roots the
    endpoints resolve against."""
    from pix.nas import web

    m = app_env["share"] / "master" / "init_2026"
    m.mkdir(parents=True, exist_ok=True)
    (m / "a.jpg").write_bytes(b"\xff\xd8original")
    monkeypatch.setattr(web, "MASTER_DIR", app_env["share"] / "master")
    monkeypatch.setattr(web, "META_DIR", app_env["share"] / "meta")
    return m


@pytest.fixture
def sign_in() -> "Callable[[str, str], TestClient]":
    """Sign a client in, through the form rather than by forging a cookie, so
    the tests exercise the path a browser actually takes.

    A fixture rather than an importable function: several modules need it, and
    a fixture is how pytest shares one without an import that works at runtime
    and not for the type checker.
    """
    from fastapi.testclient import TestClient

    from pix.nas import web

    def go(name: str, password: str) -> TestClient:
        client = TestClient(web.app)
        r = client.post("/login", data={"name": name, "password": password},
                        follow_redirects=False)
        assert r.status_code == 303, r.text[:200]
        return client

    return go


@pytest.fixture
def add_user() -> "Callable[..., None]":
    """Create an account directly, for tests that only need one to exist."""
    def go(name: str, password: str, groups: tuple[str, ...] = ()) -> None:
        from pix.nas import accounts, auth

        book = accounts.load()
        book.users[name] = accounts.Account(
            name, auth.hash_password(password), groups)
        book.groups = sorted({*book.groups, *groups})
        accounts.save(book)

    return go


@pytest.fixture
def client(app_env: dict[str, Path],
           sign_in: "Callable[[str, str], TestClient]") -> "TestClient":
    """Signed in as the built-in admin, which is what curating is done as."""
    from pix.nas import accounts

    return sign_in(accounts.ADMIN, "admin")
