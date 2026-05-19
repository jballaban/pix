from __future__ import annotations

from pathlib import Path

from pix import debug


def test_log_is_noop_when_disabled() -> None:
    """Calling log() outside an `enabled()` context must do nothing."""
    debug.log("this should vanish")  # no exception, no buffer
    assert not debug.is_enabled()


def test_log_captures_within_enabled_context(tmp_path: Path) -> None:
    target = tmp_path / "photo.jpg"
    target.write_bytes(b"")

    with debug.enabled():
        assert debug.is_enabled()
        with debug.for_file(target):
            debug.log("hello")
            debug.section("Section A")
            debug.log("under-A")

        # Outside `for_file` but still inside `enabled()`, log is a no-op
        # (no current path bound).
        debug.log("no current path")

    # After exiting `enabled()`, the buffer is discarded.
    assert not debug.is_enabled()


def test_dump_writes_per_file_logs(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    a = source / "a.jpg"
    a.write_bytes(b"")
    b = source / "sub" / "b.jpg"
    b.parent.mkdir()
    b.write_bytes(b"")

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    with debug.enabled():
        with debug.for_file(a):
            debug.log("a-detail")
        with debug.for_file(b):
            debug.log("b-detail")
        debug.dump_to(runs_dir, source, line_id_by_path={a.resolve(): "L001"})

    a_log = runs_dir / "debug" / "a.jpg.log"
    b_log = runs_dir / "debug" / "sub" / "b.jpg.log"
    assert a_log.is_file()
    assert b_log.is_file()

    a_text = a_log.read_text(encoding="utf-8")
    assert "Plan line: L001" in a_text
    assert "a-detail" in a_text

    b_text = b_log.read_text(encoding="utf-8")
    assert "Plan line: (none" in b_text
    assert "b-detail" in b_text


def test_dump_is_noop_when_disabled(tmp_path: Path) -> None:
    """dump_to() outside an enabled context creates nothing."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    debug.dump_to(runs_dir, tmp_path, {})
    assert not (runs_dir / "debug").exists()


def test_for_file_nesting_restores_prior_context(tmp_path: Path) -> None:
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    a.write_bytes(b"")
    b.write_bytes(b"")

    with debug.enabled():
        with debug.for_file(a):
            debug.log("outer-a-1")
            with debug.for_file(b):
                debug.log("inner-b")
            debug.log("outer-a-2")
        debug.dump_to(tmp_path / "runs", tmp_path, {})

    a_text = (tmp_path / "runs" / "debug" / "a.jpg.log").read_text(
        encoding="utf-8"
    )
    b_text = (tmp_path / "runs" / "debug" / "b.jpg.log").read_text(
        encoding="utf-8"
    )
    assert "outer-a-1" in a_text and "outer-a-2" in a_text
    assert "inner-b" not in a_text
    assert "inner-b" in b_text
