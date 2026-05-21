"""Tests for the streaming debug log."""

from __future__ import annotations

from pathlib import Path

from pix import debug


def test_log_is_noop_outside_writing_to(tmp_path: Path) -> None:
    """log() / section() / for_file() outside an active stream are silent."""
    # Should not raise, should not create any files.
    debug.log("this vanishes")
    debug.section("this too")
    p = tmp_path / "x.jpg"
    p.write_bytes(b"")
    with debug.for_file(p):
        debug.log("still vanishes")
    assert list(tmp_path.iterdir()) == [p]


def test_writing_to_streams_to_debug_log(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "test-run"
    target = tmp_path / "photo.jpg"
    target.write_bytes(b"")

    with debug.writing_to(run_dir):
        with debug.for_file(target):
            debug.section("Section A")
            debug.log("under-A")

    out = (run_dir / "debug.log").read_text(encoding="utf-8")
    assert str(target.resolve()) in out
    assert "--- Section A ---" in out
    assert "under-A" in out


def test_two_files_produce_two_sections(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "test-run"
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    a.write_bytes(b"")
    b.write_bytes(b"")

    with debug.writing_to(run_dir):
        with debug.for_file(a):
            debug.log("a-detail")
        with debug.for_file(b):
            debug.log("b-detail")

    out = (run_dir / "debug.log").read_text(encoding="utf-8")
    assert "a-detail" in out
    assert "b-detail" in out
    # Both file headers present, a before b.
    assert out.index(str(a.resolve())) < out.index(str(b.resolve()))


def test_log_without_for_file_context_is_silent(tmp_path: Path) -> None:
    """Stream is open but no current file → log/section are no-ops."""
    run_dir = tmp_path / "runs" / "test-run"
    with debug.writing_to(run_dir):
        debug.log("no current path")
        debug.section("also no current path")

    out = (run_dir / "debug.log").read_text(encoding="utf-8")
    assert "no current path" not in out
    assert "also no current path" not in out


def test_for_file_nesting_restores_prior_context(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "test-run"
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    a.write_bytes(b"")
    b.write_bytes(b"")

    with debug.writing_to(run_dir):
        with debug.for_file(a):
            debug.log("outer-a-1")
            with debug.for_file(b):
                debug.log("inner-b")
            debug.log("outer-a-2")

    out = (run_dir / "debug.log").read_text(encoding="utf-8")
    # All three log lines made it to the single stream.
    assert "outer-a-1" in out
    assert "inner-b" in out
    assert "outer-a-2" in out
    # outer-a-1 appears under a's header; inner-b under b's header;
    # outer-a-2 appears after the second a header (re-emitted on exit
    # of b's context). The latest a header is between inner-b and
    # outer-a-2 — confirm by string ordering.
    a_str = str(a.resolve())
    b_str = str(b.resolve())
    assert out.index(a_str) < out.index(b_str)
    assert out.index(b_str) < out.index("outer-a-2")
