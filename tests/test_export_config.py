"""`exports:` parsing in pix.yaml, and the per-distribution manifest."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pix import export_manifest
from pix.config import Config, Distribution, set_organize_template
from pix.export_manifest import Manifest, Member


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "pix.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# --- exports: parsing --------------------------------------------------------


def test_no_exports_section(tmp_path: Path) -> None:
    assert Config.load(_write(tmp_path, "runs_dir: F:\\runs\n")).exports == {}


def test_parses_a_distribution(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "exports:\n"
        "  general:\n"
        "    path: 'D:\\Photos-General'\n"
        "    filter: 'rating:3,4,5'\n"
        "    template: '{year}/{event}'\n",
    )
    dist = Config.load(path).exports["general"]
    assert dist == Distribution(
        name="general",
        path="D:\\Photos-General",
        template="{year}/{event}",
        filter=dist.filter,
    )
    assert dist.filter.clauses[0].values == frozenset({"3", "4", "5"})


def test_filter_is_optional_and_matches_everything(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "exports:\n  all:\n    path: 'D:\\All'\n    template: '{year}'\n",
    )
    assert Config.load(path).exports["all"].filter.clauses == ()


def test_multiple_distributions(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "exports:\n"
        "  general:\n"
        "    path: 'D:\\G'\n"
        "    filter: 'rating:3,4,5'\n"
        "    template: '{year}'\n"
        "  top:\n"
        "    path: 'D:\\T'\n"
        "    filter: 'rating:5'\n"
        "    template: '{year}'\n",
    )
    assert sorted(Config.load(path).exports) == ["general", "top"]


def test_bad_filter_names_the_distribution(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "exports:\n"
        "  top:\n"
        "    path: 'D:\\T'\n"
        "    filter: 'quarter:1'\n"
        "    template: '{year}'\n",
    )
    with pytest.raises(ValueError, match="export 'top' filter"):
        Config.load(path)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    # `filters:` for `filter:` would otherwise silently ship the whole
    # library to the delivery target.
    path = _write(
        tmp_path,
        "exports:\n"
        "  top:\n"
        "    path: 'D:\\T'\n"
        "    filters: 'rating:5'\n"
        "    template: '{year}'\n",
    )
    with pytest.raises(ValueError, match="unknown key"):
        Config.load(path)


def test_missing_path_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path, "exports:\n  top:\n    template: '{year}'\n"
    )
    with pytest.raises(ValueError, match="missing 'path'"):
        Config.load(path)


def test_missing_template_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "exports:\n  top:\n    path: 'D:\\T'\n")
    with pytest.raises(ValueError, match="missing 'template'"):
        Config.load(path)


def test_empty_path_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "exports:\n  top:\n    path: '  '\n    template: '{year}'\n",
    )
    with pytest.raises(ValueError, match="non-empty string"):
        Config.load(path)


def test_bad_name_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "exports:\n  'my export':\n    path: 'D:\\T'\n    template: '{y}'\n",
    )
    with pytest.raises(ValueError, match="must start with a letter"):
        Config.load(path)


def test_exports_must_be_a_mapping(tmp_path: Path) -> None:
    path = _write(tmp_path, "exports:\n  - top\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        Config.load(path)


def test_exports_survive_an_organize_rewrite(tmp_path: Path) -> None:
    # organize persists its template after every successful run; that
    # rewrite must not drop the delivery distributions.
    path = _write(
        tmp_path,
        "runs_dir: F:\\runs\n"
        "exports:\n"
        "  general:\n"
        "    path: 'D:\\G'\n"
        "    filter: 'rating:3,4,5'\n"
        "    template: '{year}/{event}'\n",
    )
    set_organize_template(path, "{year}/{month}")

    reloaded = Config.load(path)
    assert reloaded.organize_template == "{year}/{month}"
    assert reloaded.runs_dir == "F:\\runs"
    dist = reloaded.exports["general"]
    assert dist.path == "D:\\G"
    assert dist.template == "{year}/{event}"
    assert dist.filter.raw == "rating:3,4,5"


def test_filterless_export_round_trips_without_a_filter_key(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        "exports:\n  all:\n    path: 'D:\\All'\n    template: '{year}'\n",
    )
    set_organize_template(path, "{year}")
    assert "filter" not in path.read_text(encoding="utf-8")
    assert Config.load(path).exports["all"].filter.clauses == ()


# --- manifest ----------------------------------------------------------------


def _manifest() -> Manifest:
    return Manifest(
        distribution="general",
        target="D:\\Photos-General",
        members={
            "2023/Hawaii/2023-08-15_143205.jpg": Member(
                source_hash="abc123", size=1024, mtime_ns=1_700_000_000_000
            )
        },
    )


def test_manifest_round_trip(tmp_path: Path) -> None:
    export_manifest.save(tmp_path, _manifest())
    loaded = export_manifest.load(tmp_path, "general")
    assert loaded == _manifest()


def test_manifest_lives_under_local(tmp_path: Path) -> None:
    # Machine-local and sync-excluded, like cache.db.
    path = export_manifest.manifest_path(tmp_path, "general")
    assert path.parent == tmp_path / ".pix" / "local" / "exports"


def test_missing_manifest_is_none(tmp_path: Path) -> None:
    assert export_manifest.load(tmp_path, "general") is None


def test_corrupt_manifest_is_none_not_an_error(tmp_path: Path) -> None:
    path = export_manifest.manifest_path(tmp_path, "general")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert export_manifest.load(tmp_path, "general") is None


def test_unknown_format_is_none(tmp_path: Path) -> None:
    path = export_manifest.manifest_path(tmp_path, "general")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"format": 99, "target": "D:\\G", "members": {}}),
        encoding="utf-8",
    )
    assert export_manifest.load(tmp_path, "general") is None


def test_manifest_save_is_atomic_leaves_no_temp(tmp_path: Path) -> None:
    export_manifest.save(tmp_path, _manifest())
    leftovers = list(export_manifest.exports_dir(tmp_path).glob("*.tmp"))
    assert leftovers == []


def test_member_matches_stat(tmp_path: Path) -> None:
    f = tmp_path / "x.jpg"
    f.write_bytes(b"hello")
    st = f.stat()
    member = Member(
        source_hash="h", size=st.st_size, mtime_ns=st.st_mtime_ns
    )
    assert member.matches(st)

    # Rewriting the file (as a sync client would) breaks the fingerprint.
    f.write_bytes(b"hello world")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1000))
    assert not member.matches(f.stat())


def test_discard(tmp_path: Path) -> None:
    export_manifest.save(tmp_path, _manifest())
    assert export_manifest.discard(tmp_path, "general") is True
    assert export_manifest.discard(tmp_path, "general") is False
