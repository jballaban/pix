"""Tests for `pix.telemetry` — end-of-run summary block."""

from __future__ import annotations

import io

from pix.telemetry import LineRecord, _percentile, write_summary


def _rec(
    line_id: str,
    action: str,
    duration: float,
    rel: str = "f.jpg",
    size: int | None = None,
    failed: bool = False,
) -> LineRecord:
    return LineRecord(
        line_id=line_id,
        action=action,
        duration_seconds=duration,
        rel_path=rel,
        size_bytes=size,
        failed=failed,
    )


def test_percentile_basic() -> None:
    """p50 of [1..9] sorted is the middle element."""
    vals = sorted([1.0, 2, 3, 4, 5, 6, 7, 8, 9])
    assert _percentile(vals, 0.5) == 5.0
    assert _percentile(vals, 0.0) == 1.0
    assert _percentile(vals, 1.0) == 9.0


def test_percentile_empty_returns_zero() -> None:
    assert _percentile([], 0.5) == 0.0


def test_write_summary_empty_records_writes_nothing() -> None:
    buf = io.StringIO()
    write_summary(buf, [])
    assert buf.getvalue() == ""


def test_write_summary_per_action_stats() -> None:
    buf = io.StringIO()
    records = [
        _rec("L001", "TAG", 0.010),
        _rec("L002", "TAG", 0.020),
        _rec("L003", "TAG", 0.030),
        _rec("L004", "RENAME", 0.001),
        _rec("L005", "RENAME", 0.002),
    ]
    write_summary(buf, records)
    out = buf.getvalue()
    assert "=== Summary ===" in out
    assert "TAG" in out
    assert "RENAME" in out
    assert "3 lines" in out  # TAG count
    assert "2 lines" in out  # RENAME count
    assert "Top 10 slowest:" in out


def test_write_summary_orders_top_slowest_descending() -> None:
    buf = io.StringIO()
    records = [
        _rec("L001", "TAG", 0.001),
        _rec("L002", "TAG", 5.0, rel="slow.jpg"),
        _rec("L003", "TAG", 0.5, rel="medium.jpg"),
    ]
    write_summary(buf, records)
    out = buf.getvalue()
    slow_idx = out.index("slow.jpg")
    medium_idx = out.index("medium.jpg")
    assert slow_idx < medium_idx  # slowest first in Top 10


def test_write_summary_renders_size_when_present() -> None:
    buf = io.StringIO()
    records = [
        _rec(
            "L001", "CONVERT+RENAME+TAG", 30.0,
            rel="big.MOV", size=4_300_000_000,
        )
    ]
    write_summary(buf, records)
    out = buf.getvalue()
    assert "big.MOV" in out
    assert "GB" in out  # ~4.0 GB rendering


def test_write_summary_flags_failed_lines() -> None:
    buf = io.StringIO()
    records = [
        _rec("L001", "CONVERT+RENAME+TAG", 0.5),
        _rec("L002", "CONVERT+RENAME+TAG", 0.6, failed=True),
    ]
    write_summary(buf, records)
    out = buf.getvalue()
    assert "1 failed" in out
