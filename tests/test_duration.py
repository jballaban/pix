"""Tests for `pix.duration` — tiered duration formatting."""

from __future__ import annotations

import pytest

from pix.duration import (
    format_duration,
    format_duration_compact,
    format_duration_precise,
    format_size,
)


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0s"),
        (1, "1s"),
        (3, "3s"),
        (42, "42s"),
        (59, "59s"),
        (60, "1m00s"),
        (63, "1m03s"),
        (63.9, "1m03s"),  # truncates, doesn't round
        (628, "10m28s"),
        (3599, "59m59s"),
        (3600, "1h00m00s"),
        (3725, "1h02m05s"),
        (45907, "12h45m07s"),
    ],
)
def test_format_duration_integer_tiers(
    seconds: float, expected: str
) -> None:
    assert format_duration(seconds) == expected


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0.0s"),
        (0.4, "0.4s"),
        (3.4, "3.4s"),
        (42.7, "42.7s"),
        (59.9, "59.9s"),
        # >= 60s: identical to integer form.
        (60, "1m00s"),
        (63, "1m03s"),
        (3725, "1h02m05s"),
    ],
)
def test_format_duration_precise_tiers(
    seconds: float, expected: str
) -> None:
    assert format_duration_precise(seconds) == expected


def test_format_duration_pads_to_two_digit_minutes_seconds() -> None:
    """At the m and h tiers, sub-units are zero-padded for alignment."""
    assert format_duration(61) == "1m01s"
    assert format_duration(3601) == "1h00m01s"
    assert format_duration(3661) == "1h01m01s"


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0ms"),
        (0.001, "1ms"),
        (0.042, "42ms"),
        (0.850, "850ms"),
        (0.999, "999ms"),
        (1.0, "1.0s"),
        (1.3, "1.3s"),
        (59.9, "59.9s"),
        (60, "1m00s"),
        (3725, "1h02m05s"),
    ],
)
def test_format_duration_compact_tiers(
    seconds: float, expected: str
) -> None:
    assert format_duration_compact(seconds) == expected


@pytest.mark.parametrize(
    "num_bytes,expected",
    [
        (0, "0 B"),
        (42, "42 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024 * 1024, "1.0 MB"),
        (350 * 1024 * 1024, "350.0 MB"),
        (4_300_000_000, "4.0 GB"),  # ~4 GB
        (1024 * 1024 * 1024 * 1024, "1.0 TB"),
    ],
)
def test_format_size_tiers(num_bytes: int, expected: str) -> None:
    assert format_size(num_bytes) == expected
