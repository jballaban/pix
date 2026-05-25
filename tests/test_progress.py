"""Tests for the live progress line formatting.

Two pieces of logic worth covering:

- `_truncate_path` — long paths used to push the trailing
  `(Xphase / Yiter)` suffix off the right edge of the terminal,
  costing the user their temporal signal during slow phases. Helper
  trims from the left with an ellipsis prefix and snaps to a
  path-separator boundary so the result is readable.
- Render throttle — plan-gen calls `begin()`/`advance()` at
  thousands-per-second; without a throttle that's tens of thousands
  of stderr writes for nothing visible. 100ms throttle keeps the eye
  happy and the I/O cost down.
"""

from __future__ import annotations

import time
from io import StringIO

from pix.progress import LiveProgress, _truncate_path


class _FakeTTY(StringIO):
    """StringIO that lies about being a TTY so LiveProgress enables itself."""

    def isatty(self) -> bool:  # noqa: D401 — match StringIO signature
        return True


def test_truncate_returns_path_unchanged_when_it_fits() -> None:
    assert _truncate_path("short.jpg", 32) == "short.jpg"
    assert _truncate_path("a/b/c.jpg", 9) == "a/b/c.jpg"  # exactly fits


def test_truncate_snaps_to_separator_on_overflow() -> None:
    """The cut lands at the nearest `\\` or `/` so we don't show a
    half-eaten directory name."""
    long_path = "G:\\very\\long\\path\\to\\some\\Subdir\\image.jpg"
    # Budget of 25 chars would land us inside "Subdir" without the snap;
    # snapping pulls us back to the next separator.
    result = _truncate_path(long_path, 25)
    assert result.startswith("…\\")
    assert result.endswith("\\image.jpg")
    assert len(result) <= 25


def test_truncate_handles_forward_slash() -> None:
    long_path = "a/b/c/d/e/f/g/h/short.jpg"
    result = _truncate_path(long_path, 15)
    assert result.startswith("…/")
    assert len(result) <= 15
    assert result.endswith("short.jpg")


def test_truncate_falls_back_to_char_cut_when_no_separator_in_tail() -> None:
    """A really long bare filename has no separator in the kept tail;
    we still respect the budget by truncating raw chars."""
    long_name = "absurdly_long_unbroken_filename_no_separators_here.jpg"
    result = _truncate_path(long_name, 20)
    assert len(result) == 20
    assert result.startswith("…")
    # The last 19 chars of the source must be preserved.
    assert result[1:] == long_name[-19:]


def test_truncate_max_chars_one_returns_ellipsis_only() -> None:
    assert _truncate_path("x/y/z.jpg", 1) == "…"


def test_truncate_max_chars_zero_returns_empty() -> None:
    assert _truncate_path("x/y/z.jpg", 0) == ""


def test_render_throttle_collapses_rapid_calls() -> None:
    """Many begin()/advance() calls in a tight loop should produce far
    fewer stderr writes than the call count. The 100ms throttle keeps
    the visible progress smooth without paying for every iteration.
    """
    tty = _FakeTTY()
    with LiveProgress(total=100, stream=tty) as progress:
        for i in range(50):
            progress.begin("planning", f"file_{i}.jpg")
            progress.advance()
    rendered = tty.getvalue()
    # 100 calls (50 begin + 50 advance) inside the same 100ms window;
    # we expect roughly the first render plus the forced 100% render
    # from __exit__. Hard upper bound: well under the call count.
    write_count = rendered.count("\r")
    assert write_count < 10, (
        f"throttle leaked: {write_count} writes for 100 calls"
    )
    # Final 100% line still lands.
    assert "100%" in rendered


def test_render_throttle_releases_after_window() -> None:
    """After the 100ms window elapses, the next call paints."""
    tty = _FakeTTY()
    with LiveProgress(total=10, stream=tty) as progress:
        progress.begin("planning", "a.jpg")
        # Force-clear the buffer so we count only post-sleep writes.
        tty.seek(0)
        tty.truncate(0)
        time.sleep(0.12)  # cross the 100ms boundary
        progress.advance()
    rendered = tty.getvalue()
    # At least one render after the sleep, plus the forced 100% line.
    assert rendered.count("\r") >= 2


def test_truncate_user_mtp_path_keeps_filename_and_parent() -> None:
    """Regression-flavored: the long MTP device path from the user's
    library should collapse to something readable that still ends in
    the canonical filename."""
    long_path = (
        "G:\\pix\\raw\\tmp\\mtp\\lola\\"
        "____usb#vid_05ac&pid_12a8&mi_00#a&1045e13b&0&0000#"
        "{6ac27878-a6fa-4155-ba85-f98f491d4f33}\\"
        "Internal Storage\\202408_a\\2024-08-04_090029.jpg"
    )
    result = _truncate_path(long_path, 50)
    assert len(result) <= 50
    assert result.endswith("2024-08-04_090029.jpg")
    assert result.startswith("…")
