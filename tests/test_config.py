from __future__ import annotations

from pathlib import Path

import pytest

import pix.config as config_mod
from pix.config import (
    CONFIG_FILENAME,
    EXTENSION_POLICY,
    Config,
    new_run_dir,
    orphaned_default_runs,
    set_organize_template,
    warn_orphaned_runs,
)


# --- format policy is a build constant, not per-library config ---


def test_extension_policy_is_the_build_constant() -> None:
    assert EXTENSION_POLICY["jpg"] == "keep"
    assert EXTENSION_POLICY["heic"] == "convert_to_jpg"
    assert EXTENSION_POLICY["dng"] == "convert_to_jpg"
    assert EXTENSION_POLICY["mov"] == "convert_to_mp4"
    assert EXTENSION_POLICY["insv"] == "keep"
    assert EXTENSION_POLICY["thumbs.db"] == "delete"


def test_load_extensions_always_from_build_not_file(tmp_path: Path) -> None:
    """The settings file never overrides format policy — a stray
    `extensions:` block is ignored; `Config.extensions` is the constant."""
    p = tmp_path / CONFIG_FILENAME
    p.write_text("extensions:\n  jpg: delete\n", encoding="utf-8")
    cfg = Config.load(p)
    assert cfg.extensions == EXTENSION_POLICY
    assert cfg.extensions["jpg"] == "keep"  # not the file's "delete"


def test_load_missing_file_is_defaults(tmp_path: Path) -> None:
    cfg = Config.load(tmp_path / "nope.yaml")
    assert cfg.extensions == EXTENSION_POLICY
    assert cfg.runs_dir is None
    assert cfg.organize_template is None


def test_load_empty_file_is_defaults(tmp_path: Path) -> None:
    p = tmp_path / CONFIG_FILENAME
    p.write_text("# just a comment\n", encoding="utf-8")
    cfg = Config.load(p)
    assert cfg.runs_dir is None
    assert cfg.organize_template is None


def test_load_rejects_non_mapping_top_level(tmp_path: Path) -> None:
    p = tmp_path / CONFIG_FILENAME
    p.write_text("- jpg\n- heic\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping"):
        Config.load(p)


# --- runs_dir ---


def test_runs_base_defaults_to_pix_runs() -> None:
    cfg = Config()
    assert cfg.runs_base(Path("G:/lib")) == Path("G:/lib") / ".pix" / "runs"


def test_runs_base_honors_configured_runs_dir() -> None:
    cfg = Config(runs_dir="F:/caps/runs")
    assert cfg.runs_base(Path("G:/lib")) == Path("F:/caps/runs")


def test_load_parses_runs_dir(tmp_path: Path) -> None:
    p = tmp_path / CONFIG_FILENAME
    p.write_text("runs_dir: F:/caps/runs\n", encoding="utf-8")
    assert Config.load(p).runs_dir == "F:/caps/runs"


def test_load_rejects_non_string_runs_dir(tmp_path: Path) -> None:
    p = tmp_path / CONFIG_FILENAME
    p.write_text("runs_dir: 123\n", encoding="utf-8")
    with pytest.raises(ValueError, match="runs_dir"):
        Config.load(p)


# --- organize.template ---


def test_load_parses_organize_template(tmp_path: Path) -> None:
    p = tmp_path / CONFIG_FILENAME
    p.write_text("organize:\n  template: '{year}/{event}'\n", encoding="utf-8")
    assert Config.load(p).organize_template == "{year}/{event}"


def test_set_organize_template_preserves_runs_dir(tmp_path: Path) -> None:
    """Saving the template round-trips the known keys, keeping a hand-set
    runs_dir (and dropping unknown keys/comments)."""
    p = tmp_path / CONFIG_FILENAME
    p.write_text(
        "runs_dir: F:/caps/runs\n# my note\nmystery: 1\n", encoding="utf-8"
    )
    set_organize_template(p, "{year}/{month}")
    cfg = Config.load(p)
    assert cfg.organize_template == "{year}/{month}"
    assert cfg.runs_dir == "F:/caps/runs"  # preserved
    assert "mystery" not in p.read_text(encoding="utf-8")  # unknown dropped


# --- new_run_dir: the single run-folder mint (honors the override) ---

def test_new_run_dir_defaults_under_library(tmp_path: Path) -> None:
    run_id, run_dir = new_run_dir(tmp_path, Config())
    assert run_dir == tmp_path / ".pix" / "runs" / run_id
    assert run_dir.is_dir()


def test_new_run_dir_honors_runs_dir_override(tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    _run_id, run_dir = new_run_dir(tmp_path, Config(runs_dir=str(other)))
    assert run_dir.parent == other  # not <root>/.pix/runs
    assert run_dir.is_dir()


def test_new_run_dir_uniquifies_same_second(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    # Freeze the timestamp so two mints collide → the second gets a suffix.
    class _FixedDT:
        @staticmethod
        def now() -> object:
            class _T:
                @staticmethod
                def strftime(_fmt: str) -> str:
                    return "2026-07-20_10-00-00"
            return _T()

    monkeypatch.setattr(config_mod, "datetime", _FixedDT)
    id1, d1 = new_run_dir(tmp_path, Config())
    id2, d2 = new_run_dir(tmp_path, Config())
    assert id1 == "2026-07-20_10-00-00"
    assert id2 == "2026-07-20_10-00-00_2"
    assert d1 != d2 and d1.is_dir() and d2.is_dir()


# --- orphaned_default_runs + warn (relocation advisory, warn-only) ---

def test_orphaned_none_without_override(tmp_path: Path) -> None:
    (tmp_path / ".pix" / "runs" / "r1").mkdir(parents=True)
    assert orphaned_default_runs(tmp_path, Config()) == []  # no override → nothing


def test_orphaned_none_when_override_equals_default(tmp_path: Path) -> None:
    default = tmp_path / ".pix" / "runs"
    (default / "r1").mkdir(parents=True)
    cfg = Config(runs_dir=str(default))
    assert orphaned_default_runs(tmp_path, cfg) == []  # configured == default


def test_orphaned_detected_when_override_elsewhere(tmp_path: Path) -> None:
    (tmp_path / ".pix" / "runs" / "r1").mkdir(parents=True)
    (tmp_path / ".pix" / "runs" / "r2").mkdir(parents=True)
    cfg = Config(runs_dir=str(tmp_path / "other"))
    orphans = orphaned_default_runs(tmp_path, cfg)
    assert {p.name for p in orphans} == {"r1", "r2"}


def test_orphaned_empty_default_is_nothing(tmp_path: Path) -> None:
    (tmp_path / ".pix" / "runs").mkdir(parents=True)  # exists but empty
    cfg = Config(runs_dir=str(tmp_path / "other"))
    assert orphaned_default_runs(tmp_path, cfg) == []


def test_warn_orphaned_runs_once_per_root(
    tmp_path: Path, capsys: "pytest.CaptureFixture[str]"
) -> None:
    config_mod._RUNS_ORPHAN_WARNED.clear()
    (tmp_path / ".pix" / "runs" / "r1").mkdir(parents=True)
    cfg = Config(runs_dir=str(tmp_path / "other"))
    warn_orphaned_runs(tmp_path, cfg)
    first = capsys.readouterr().err
    assert "old default location" in first
    warn_orphaned_runs(tmp_path, cfg)  # second call: guarded → silent
    assert capsys.readouterr().err == ""
