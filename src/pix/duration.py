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


def format_duration_compact(seconds: float) -> str:
    """Compact telemetry format with ms tier for sub-second actions.

    Tier breakpoints:

        < 1s       →  Xms     (e.g. 42ms, 850ms)
        < 60s      →  X.Xs    (e.g. 1.3s, 42.7s)
        < 3600s    →  XmYYs   (e.g. 1m03s, 27m08s)
        >= 3600s   →  XhYYmZZs (e.g. 1h03m02s)

    The ms tier is the difference vs `format_duration_precise`: telemetry
    per-line durations are routinely sub-second and "0.0s" buckets too
    coarsely for percentile analysis.
    """
    if seconds < 1.0:
        return f"{int(seconds * 1000)}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    s = int(seconds)
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m{s % 60:02d}s"


def format_size(num_bytes: int) -> str:
    """Compact human-readable size: '42 B' / '350 KB' / '4.2 GB'.

    Uses 1024-based units (KiB, MiB, …) but renders unit suffixes as
    KB/MB/GB for readability. Bytes render integer; larger units render
    one decimal.
    """
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    # Anything past TB renders as PB (1024+ TB).
    return f"{size:.1f} PB"
