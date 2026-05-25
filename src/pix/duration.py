"""Tiered duration formatting.

See spec/migrate.md → Duration format. Two flavors:

- `format_duration(seconds)` — integer-only; used in progress-line
  `(Xs)` suffixes where sub-second precision is noise.
- `format_duration_precise(seconds)` — adds one decimal place when
  under 60s; used in post-phase summary lines where measuring a fast
  phase is informative. From 60s upward, identical to the integer
  form (precision below the minute is irrelevant at that scale).

Tier breakpoints:

    < 60s        →  Xs        (e.g. 3s, 42s; or 3.4s, 42.7s precise)
    < 3600s      →  XmYs      (e.g. 1m03s, 27m08s)
    >= 3600s     →  XhYmZs    (e.g. 1h03m02s, 12h45m07s)

Minutes and seconds use two-digit zero-padding inside the larger tiers
to keep the rendering monospaced.
"""

from __future__ import annotations


def format_duration(seconds: float) -> str:
    """Integer-tiered duration. See module docstring."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m{s % 60:02d}s"


def format_duration_precise(seconds: float) -> str:
    """Same tiered format, with one decimal place under 60s."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    s = int(seconds)
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m{s % 60:02d}s"
