"""The upload progress line (spec/nas-app.md §9).

A seeding batch is up to 946GB, so the line has to answer "how fast, how much
left" — not just spin.
"""

from __future__ import annotations

import time
from pathlib import Path

from pix.nas.upload import UploadSummary, _status


def _summary(copied: int = 0, skipped: int = 0, sent: int = 0,
             failed: int = 0) -> UploadSummary:
    s = UploadSummary(name="legacy", master_folder=Path("."))
    s.copied, s.skipped, s.bytes_copied = copied, skipped, sent
    s.failed = [f"f{i}" for i in range(failed)]
    return s


def test_counts_every_resolved_file() -> None:
    """Skips and failures are progress too — the bar must not stall on them."""
    body = _status(_summary(copied=3, skipped=2, failed=1), 10, 1000,
                   time.monotonic() - 1)
    assert body.startswith("6/10")


def test_reports_transferred_against_total() -> None:
    body = _status(_summary(copied=1, sent=5 * 1024 ** 3), 2, 10 * 1024 ** 3,
                   time.monotonic() - 1)
    assert "5.0 GB of 10.0 GB" in body


def test_shows_rate_and_eta_once_bytes_have_moved() -> None:
    """The two numbers that matter on a multi-hour run."""
    body = _status(_summary(copied=1, sent=100 * 1024 ** 2), 2, 200 * 1024 ** 2,
                   time.monotonic() - 10)
    assert "/s" in body
    assert "ETA" in body


def test_no_rate_before_the_first_byte() -> None:
    """Dividing by a zero-byte sample would print a meaningless rate."""
    body = _status(_summary(), 10, 1000, time.monotonic())
    assert "/s" not in body
    assert "ETA" not in body


def test_no_eta_when_everything_is_sent() -> None:
    body = _status(_summary(copied=2, sent=1000), 2, 1000, time.monotonic() - 5)
    assert "ETA" not in body


def test_status_is_safe_while_counters_move() -> None:
    """It runs on the progress thread a second at a time while workers mutate
    the summary, so it must never raise on a half-updated view.

    It deliberately reads without the upload lock — taking it could deadlock a
    worker mid-ledger-append, and a line one file stale costs nothing.
    """
    import threading

    s = _summary()
    total, stop = 500, threading.Event()
    errors: list[BaseException] = []

    def mutate() -> None:
        for i in range(total):
            s.copied = i
            s.bytes_copied = i * 1024
        stop.set()

    def poll() -> None:
        try:
            while not stop.is_set():
                _status(s, total, total * 1024, time.monotonic() - 1)
        except BaseException as e:       # pragma: no cover - failure path
            errors.append(e)

    threads = [threading.Thread(target=mutate), threading.Thread(target=poll)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not any(t.is_alive() for t in threads)
    assert errors == []
