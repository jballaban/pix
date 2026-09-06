"""Tests for `--paths-from <file>` on `pix tag set` / `clear` / `rotate`.

Windows caps a process command line at 32767 characters, so an Explorer
selection of a few hundred media files can't be splatted onto argv — it
fails with "The filename or extension is too long" before pix starts. The
context-menu launcher snapshots the selection to a file and passes that
instead, so these cover the reading of it: encoding (PowerShell writes a
UTF-8 BOM), blank/whitespace lines, combining with positional paths, and
the unreadable-file error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import pix.cli as cli
from pix.cli import app

runner = CliRunner()


def _capture_set(monkeypatch: pytest.MonkeyPatch) -> list[list[Path]]:
    seen: list[list[Path]] = []

    def fake(*, tag: str, value: str, paths: list[Path], no_prompt: bool = False,
             clear: bool = False) -> None:
        seen.append(paths)

    monkeypatch.setattr(cli, "set_override", fake)
    return seen


def _capture_rotate(monkeypatch: pytest.MonkeyPatch) -> list[list[Path]]:
    seen: list[list[Path]] = []

    def fake(*, degrees: int, paths: list[Path], no_prompt: bool = False) -> None:
        seen.append(paths)

    monkeypatch.setattr(cli, "rotate_videos", fake)
    return seen


def _list_file(tmp_path: Path, lines: list[str], encoding: str = "utf-8") -> Path:
    f = tmp_path / "items.txt"
    f.write_text("\n".join(lines) + "\n", encoding=encoding)
    return f


def test_set_reads_paths_from_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_set(monkeypatch)
    listing = _list_file(tmp_path, [str(tmp_path / "a.jpg"), str(tmp_path / "b.jpg")])

    result = runner.invoke(
        app, ["tag", "set", "event", "Sicily", "--paths-from", str(listing)]
    )

    assert result.exit_code == 0, result.output
    assert seen == [[tmp_path / "a.jpg", tmp_path / "b.jpg"]]


def test_list_file_tolerates_bom_blank_lines_and_crlf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows PowerShell's `Set-Content -Encoding UTF8` writes a BOM and CRLF;
    # neither may leak into the first path or the last.
    seen = _capture_set(monkeypatch)
    listing = tmp_path / "items.txt"
    body = f"{tmp_path / 'a.jpg'}\r\n\r\n   \r\n{tmp_path / 'b.jpg'}\r\n"
    listing.write_text(body, encoding="utf-8-sig")

    result = runner.invoke(
        app, ["tag", "set", "event", "Sicily", "--paths-from", str(listing)]
    )

    assert result.exit_code == 0, result.output
    assert seen == [[tmp_path / "a.jpg", tmp_path / "b.jpg"]]


def test_list_file_scales_past_the_command_line_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real failure: ~660 paths of ~48 chars overflow argv's 32767-char cap.
    seen = _capture_set(monkeypatch)
    many = [str(tmp_path / f"2026-08-26_1720{i:02d}.jpg") for i in range(700)]
    assert sum(len(p) + 3 for p in many) > 32767
    listing = _list_file(tmp_path, many)

    result = runner.invoke(
        app, ["tag", "set", "event", "Sicily", "--paths-from", str(listing)]
    )

    assert result.exit_code == 0, result.output
    assert seen[0] == [Path(p) for p in many]


def test_positional_paths_and_list_file_combine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_set(monkeypatch)
    listing = _list_file(tmp_path, [str(tmp_path / "b.jpg")])

    result = runner.invoke(
        app,
        ["tag", "set", "event", "Sicily", str(tmp_path / "a.jpg"),
         "--paths-from", str(listing)],
    )

    assert result.exit_code == 0, result.output
    assert seen == [[tmp_path / "a.jpg", tmp_path / "b.jpg"]]


def test_clear_reads_paths_from_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_set(monkeypatch)
    listing = _list_file(tmp_path, [str(tmp_path / "a.jpg")])

    result = runner.invoke(
        app, ["tag", "clear", "event", "--paths-from", str(listing)]
    )

    assert result.exit_code == 0, result.output
    assert seen == [[tmp_path / "a.jpg"]]


def test_rotate_reads_paths_from_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_rotate(monkeypatch)
    listing = _list_file(tmp_path, [str(tmp_path / "clip.mp4")])

    result = runner.invoke(
        app, ["tag", "rotate", "90", "--paths-from", str(listing)]
    )

    assert result.exit_code == 0, result.output
    assert seen == [[tmp_path / "clip.mp4"]]


def test_missing_list_file_is_an_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["tag", "set", "event", "Sicily", "--paths-from", str(tmp_path / "nope.txt")],
    )

    assert result.exit_code == 1
    assert "--paths-from" in result.output


def test_no_paths_at_all_still_errors(tmp_path: Path) -> None:
    # `paths` became optional so --paths-from can stand alone; giving neither
    # must still be rejected rather than silently doing nothing.
    result = runner.invoke(app, ["tag", "set", "event", "Sicily"])

    assert result.exit_code == 1
    assert "no files given" in result.output
