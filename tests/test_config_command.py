"""`pix info config` — the read-only view of a library's pix.yaml."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from pix import export_manifest
from pix.commands.config import show_config
from pix.export_manifest import Manifest, Member


def _library(tmp_path: Path, body: str = "") -> Path:
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    if body:
        (root / ".pix" / "pix.yaml").write_text(body, encoding="utf-8")
    return root


def _run(root: Path, capsys: pytest.CaptureFixture[str]) -> str:
    show_config(root)
    return capsys.readouterr().out


def test_marks_defaulted_runs_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _run(_library(tmp_path), capsys)
    assert "runs_dir:" in out
    assert "(default)" in out


def test_shows_a_configured_runs_dir_without_the_default_marker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _run(_library(tmp_path, "runs_dir: F:/elsewhere\n"), capsys)
    assert "F:/elsewhere" in out
    assert "F:/elsewhere  (default)" not in out


def test_shows_the_organize_template(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _library(tmp_path, "organize:\n  template: '{year}/{event}'\n")
    assert "{year}/{event}" in _run(root, capsys)


def test_unset_organize_template_says_how_to_set_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _run(_library(tmp_path), capsys)
    assert "not set" in out
    assert "pix organize" in out


def test_no_exports_configured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "exports: none configured." in _run(_library(tmp_path), capsys)


def _with_exports(tmp_path: Path) -> Path:
    return _library(
        tmp_path,
        "exports:\n"
        "  general:\n"
        "    path: 'D:/G'\n"
        "    filter: 'rating:3,4,5'\n"
        "    template: '{year}/{event}'\n"
        "  photos:\n"
        "    path: 'D:/P'\n"
        "    extensions: 'jpg'\n"
        "    template: '{year}'\n",
    )


def test_lists_each_distribution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _run(_with_exports(tmp_path), capsys)
    assert "exports: 2 distribution(s)" in out
    assert "general" in out and "photos" in out
    assert "rating:3,4,5" in out
    assert "{year}/{event}" in out


def test_marks_default_extensions_but_not_explicit_ones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _run(_with_exports(tmp_path), capsys)
    assert "jpg,mp4  (default)" in out
    assert "jpg\n" in out.replace("    ", "")  # the explicit photos tier


def test_absent_filter_reads_as_everything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "(everything)" in _run(_with_exports(tmp_path), capsys)


def test_reports_whether_the_target_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    root = _library(
        tmp_path,
        "exports:\n"
        "  here:\n"
        f"    path: '{delivery.as_posix()}'\n"
        "    template: '{year}'\n"
        "  gone:\n"
        "    path: 'D:/nowhere-at-all'\n"
        "    template: '{year}'\n",
    )
    out = _run(root, capsys)
    assert "(exists)" in out
    assert "(does not exist yet)" in out


def test_invalid_template_is_flagged_not_raised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The whole point of the command is surfacing config problems, so a bad
    # template must be reported against its distribution, not crash the run.
    root = _library(
        tmp_path,
        "exports:\n  bad:\n    path: 'D:/B'\n    template: '{quarter}'\n",
    )
    out = _run(root, capsys)
    assert "INVALID" in out
    assert "unknown token" in out


def test_reports_provisioned_member_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _with_exports(tmp_path)
    export_manifest.save(
        root,
        Manifest(
            distribution="general",
            target="D:/G",
            members={"2023/a.jpg": Member("h", 1, 2)},
        ),
    )
    out = _run(root, capsys)
    assert "1 member(s) provisioned" in out
    assert "never provisioned" in out  # the photos tier


def test_reports_a_repointed_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _with_exports(tmp_path)
    export_manifest.save(
        root,
        Manifest(
            distribution="general",
            target="D:/somewhere-else",
            members={"2023/a.jpg": Member("h", 1, 2)},
        ),
    )
    out = _run(root, capsys)
    assert "target changed" in out
    assert "D:/somewhere-else" in out


def test_malformed_config_exits_with_the_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _library(
        tmp_path,
        "exports:\n  bad:\n    path: 'D:/B'\n    filter: 'quarter:1'\n"
        "    template: '{year}'\n",
    )
    with pytest.raises(typer.Exit) as exc:
        show_config(root)
    assert exc.value.exit_code == 1
    assert "quarter" in capsys.readouterr().err
