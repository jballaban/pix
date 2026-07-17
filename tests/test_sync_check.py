"""Read-only sync-client readiness validation (Synology Drive Client)."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping, Sequence, Set as AbstractSet
from pathlib import Path

import pytest
import typer

from pix import sync_check
from pix.sync_check import (
    NOT_INSTALLED,
    NOT_READY,
    NOT_SYNCED,
    READY,
    UNVERIFIABLE,
    SyncReadiness,
    check_sync_readiness,
)

_ALL_RULES = (["/.pix/local"], ["_exiftool_tmp"], ["*.__*"])


def _make_syno(
    tmp: Path,
    sessions: Sequence[Mapping[str, object]],
    filters: dict[int, tuple[list[str], list[str], list[str]] | None],
    dot_off: AbstractSet[int] = frozenset(),  # sessions with dot-prefix sync OFF
) -> Path:
    """Build a fake `%LOCALAPPDATA%\\SynologyDrive\\data` tree; return it.

    `dot_off` sessions get `black_prefix = "."` (dotfiles excluded, i.e. the
    "sync files beginning with ." setting turned off). Absent = dotfiles sync.
    """
    data = tmp / "syno" / "data"
    (data / "db").mkdir(parents=True)
    con = sqlite3.connect(str(data / "db" / "sys.sqlite"))
    con.execute(
        "CREATE TABLE session_table (id INTEGER, share_name TEXT, "
        "sync_folder TEXT, use_windows_cloud_file_api INTEGER, "
        "is_mac_on_demand_sync_enable INTEGER)"
    )
    for s in sessions:
        con.execute(
            "INSERT INTO session_table VALUES (?,?,?,?,?)",
            (s["id"], s["share"], s["folder"], 1 if s["on_demand"] else 0, 0),
        )
    con.commit()
    con.close()
    for sid, f in filters.items():
        if f is None:
            continue
        conf = data / "session" / str(sid) / "conf"
        conf.mkdir(parents=True)
        dirp, suf, glb = f
        q = lambda xs: ", ".join(f'"{x}"' for x in xs)  # noqa: E731
        common = "[Common]\n"
        if sid in dot_off:
            common += 'black_prefix = "."\n'
        common += f"black_dir_prefix = {q(dirp)}\n\n"
        text = (
            "[Version]\nmajor = 1\nminor = 1\n\n"
            + common
            + f"[File]\nblack_suffix = {q(suf)}\nblack_glob = {q(glb)}\n"
        )
        (conf / "blacklist.filter").write_text(text, encoding="utf-8")
    return data


def _sess(root: Path, *, on_demand: bool) -> dict[str, object]:
    return {
        "id": 2,
        "share": "pix",
        "folder": str(root) + os.sep,  # Synology stores a trailing separator
        "on_demand": on_demand,
    }


def test_require_ready_blocks_only_when_mutating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A not-ready task raises (exit) with block=True and only warns with
    block=False."""
    r = SyncReadiness(
        NOT_READY, share="t", folder="x", on_demand=True, missing=()
    )
    monkeypatch.setattr(sync_check, "check_sync_readiness", lambda root: r)
    with pytest.raises(typer.Exit):
        sync_check.require_ready(tmp_path, block=True)
    sync_check.require_ready(tmp_path, block=False)  # must not raise


def test_require_ready_silent_when_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sync_check,
        "check_sync_readiness",
        lambda root: SyncReadiness(READY, share="t", folder="x"),
    )
    sync_check.require_ready(tmp_path, block=True)  # no raise


def test_no_synology_config_is_not_installed(tmp_path: Path) -> None:
    data = tmp_path / "empty"
    data.mkdir()
    r = check_sync_readiness(tmp_path, data_dir=data)
    assert r.state == NOT_INSTALLED


def test_fully_configured_task_is_ready(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    data = _make_syno(tmp_path, [_sess(lib, on_demand=False)], {2: _ALL_RULES})
    r = check_sync_readiness(lib, data_dir=data)
    assert r.state == READY, r
    assert r.share == "pix"


def test_on_demand_blocks(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    data = _make_syno(tmp_path, [_sess(lib, on_demand=True)], {2: _ALL_RULES})
    r = check_sync_readiness(lib, data_dir=data)
    assert r.state == NOT_READY
    assert r.on_demand is True
    assert r.missing == ()  # rules fine, only on-demand is the problem


def test_missing_marker_glob_is_not_ready(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    # dir + exiftool present, but no glob → the *.__* markers aren't excluded
    data = _make_syno(
        tmp_path, [_sess(lib, on_demand=False)], {2: (["/.pix/local"], ["_exiftool_tmp"], [])}
    )
    r = check_sync_readiness(lib, data_dir=data)
    assert r.state == NOT_READY
    assert any("*.__*" in m for m in r.missing)


def test_missing_local_exclusion_is_not_ready(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    data = _make_syno(
        tmp_path, [_sess(lib, on_demand=False)], {2: ([], ["_exiftool_tmp"], ["*.__*"])}
    )
    r = check_sync_readiness(lib, data_dir=data)
    assert r.state == NOT_READY
    assert any(".pix/local" in m for m in r.missing)


def test_exiftool_rule_via_glob_counts(tmp_path: Path) -> None:
    """A `*_exiftool_tmp` glob satisfies the exiftool rule (semantic match),
    even without a `_exiftool_tmp` suffix entry."""
    lib = tmp_path / "lib"
    lib.mkdir()
    data = _make_syno(
        tmp_path,
        [_sess(lib, on_demand=False)],
        {2: (["/.pix/local"], [], ["*.__*", "*_exiftool_tmp"])},
    )
    r = check_sync_readiness(lib, data_dir=data)
    assert r.state == READY, r


def test_dot_prefix_off_is_not_ready(tmp_path: Path) -> None:
    """Dot-prefix sync off (`.pix` never syncs → runs/errors/stash not backed
    up) is a confirmed not-ready condition, even though it's corruption-safe."""
    lib = tmp_path / "lib"
    lib.mkdir()
    data = _make_syno(
        tmp_path, [_sess(lib, on_demand=False)], {2: _ALL_RULES}, dot_off={2}
    )
    r = check_sync_readiness(lib, data_dir=data)
    assert r.state == NOT_READY
    assert any("period" in m.lower() for m in r.missing)
    # It must NOT complain about .pix/local here (moot — all of .pix excluded).
    assert not any(".pix/local" in m for m in r.missing)


def test_markers_still_required_when_dot_prefix_off(tmp_path: Path) -> None:
    """Transient markers aren't dot-prefixed, so they must be excluded even
    when dot-prefix sync is off."""
    lib = tmp_path / "lib"
    lib.mkdir()
    data = _make_syno(
        tmp_path, [_sess(lib, on_demand=False)],
        {2: (["/.pix/local"], ["_exiftool_tmp"], [])},  # no *.__* glob
        dot_off={2},
    )
    r = check_sync_readiness(lib, data_dir=data)
    assert r.state == NOT_READY
    assert any("*.__*" in m for m in r.missing)  # marker rule still demanded
    assert any("period" in m.lower() for m in r.missing)


def test_library_not_covered_by_any_task(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    other = tmp_path / "somewhere_else"
    other.mkdir()
    data = _make_syno(tmp_path, [_sess(other, on_demand=False)], {2: _ALL_RULES})
    r = check_sync_readiness(lib, data_dir=data)
    assert r.state == NOT_SYNCED


def test_missing_filter_file_is_unverifiable(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    # session covers lib, files persisted, but no blacklist.filter written
    data = _make_syno(tmp_path, [_sess(lib, on_demand=False)], {2: None})
    r = check_sync_readiness(lib, data_dir=data)
    assert r.state == UNVERIFIABLE


def test_subfolder_library_needs_its_relative_prefix(tmp_path: Path) -> None:
    """When the library is a subfolder of the sync root, the exclusion must
    name the library-relative path."""
    sync_root = tmp_path / "drive"
    lib = sync_root / "photos"
    lib.mkdir(parents=True)
    sess = {"id": 2, "share": "d", "folder": str(sync_root) + os.sep, "on_demand": False}
    # Wrong: bare /.pix/local doesn't cover /photos/.pix/local
    data = _make_syno(tmp_path, [sess], {2: (["/.pix/local"], ["_exiftool_tmp"], ["*.__*"])})
    assert check_sync_readiness(lib, data_dir=data).state == NOT_READY
    # Right: the library-relative prefix
    data2 = _make_syno(
        tmp_path / "b", [sess], {2: (["/photos/.pix/local"], ["_exiftool_tmp"], ["*.__*"])}
    )
    assert check_sync_readiness(lib, data_dir=data2).state == READY
