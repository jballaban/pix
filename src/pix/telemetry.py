"""Per-line telemetry: timing records and end-of-run summary.

Always-on (no flag) — overhead is two `monotonic()` calls plus one
optional `stat()` per line. Cheap enough that even a 60k-line run pays
a few milliseconds of bookkeeping total.

Each command's apply loop maintains a `list[LineRecord]` and calls
`write_summary(log, records)` after the per-line transition log.
The summary compresses N lines into a per-action stats block plus the
top-10 slowest entries, which is what a human or assistant reads first
to spot hotspots.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import IO

from pix.duration import format_duration_compact, format_size


@dataclass
class LineRecord:
    """One completed (or failed) apply line, for the end-of-run summary."""

    line_id: str
    action: str
    duration_seconds: float
    rel_path: str
    size_bytes: int | None = None
    failed: bool = False


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Return the `pct` (0..1) percentile from a sorted list of values.

    Nearest-rank by index; fine for telemetry-grade summaries. Empty
    inputs return 0.0 so the caller doesn't have to guard.
    """
    if not sorted_values:
        return 0.0
    idx = int(pct * (len(sorted_values) - 1))
    return sorted_values[idx]


def write_summary(log: IO[str], records: list[LineRecord]) -> None:
    """Append the per-action stats + top-10 slowest block to `log`.

    Layout:

        === Summary ===
        ACTION             N lines | p50=X p95=Y max=Z | total T[, F failed]
        ...

        Top 10 slowest:
          L<id> ACTION duration — rel/path (size)
          ...
    """
    if not records:
        return

    by_action: dict[str, list[LineRecord]] = defaultdict(list)
    for r in records:
        by_action[r.action].append(r)

    log.write("\n=== Summary ===\n")
    for action in sorted(by_action.keys()):
        group = by_action[action]
        durs = sorted(r.duration_seconds for r in group)
        total = sum(durs)
        p50 = _percentile(durs, 0.5)
        p95 = _percentile(durs, 0.95)
        mx = durs[-1] if durs else 0.0
        failed = sum(1 for r in group if r.failed)
        failed_part = f", {failed} failed" if failed else ""
        log.write(
            f"{action:<18} {len(group):>6} lines | "
            f"p50={format_duration_compact(p50):<8} "
            f"p95={format_duration_compact(p95):<8} "
            f"max={format_duration_compact(mx):<8} | "
            f"total {format_duration_compact(total)}"
            f"{failed_part}\n"
        )

    top = sorted(records, key=lambda r: -r.duration_seconds)[:10]
    log.write("\nTop 10 slowest:\n")
    for r in top:
        size_part = (
            f" ({format_size(r.size_bytes)})"
            if r.size_bytes is not None
            else ""
        )
        log.write(
            f"  {r.line_id} {r.action} "
            f"{format_duration_compact(r.duration_seconds)} — "
            f"{r.rel_path}{size_part}\n"
        )
    log.flush()
