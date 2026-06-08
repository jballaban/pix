"""Tests for `pix.metadata_filter` — the stored-metadata allowlist."""

from __future__ import annotations

from pix import dates, events, plan
from pix.metadata_filter import _is_consumed, filter_consumed


def test_drops_noise_keeps_consumed() -> None:
    raw: dict[str, object] = {
        "SourceFile": "G:/pix/x.jpg",
        "EXIF:DateTimeOriginal": "2023:08:15 14:32:05",
        "XMP:EventOverride": "Hawaii",
        "XMP:RegionName": "Alice",  # face region — kept
        "EXIF:MakerNoteCanon": "....big blob....",  # noise — dropped
        "EXIF:ThumbnailImage": "base64....",  # noise — dropped
        "XMP:Rating": "5",  # noise — dropped
    }
    out = filter_consumed(raw)
    assert set(out) == {
        "SourceFile",
        "EXIF:DateTimeOriginal",
        "XMP:EventOverride",
        "XMP:RegionName",
    }


def test_allowlist_covers_every_consumed_constant() -> None:
    """Drift guard: every tag key the runtime reads from cached metadata must
    be kept by the filter. Update `metadata_filter` if this fails."""
    consumed: set[str] = {
        plan.PIX_DATE_AUTO,
        plan.PIX_DATE_AUTO_PREVIOUS,
        plan.PIX_DATE_OVERRIDE,
        plan.PIX_ORIGINAL_PATH,
        plan.PIX_EVENT_AUTO,
        plan.PIX_EVENT_AUTO_PREVIOUS,
        plan.PIX_EVENT_OVERRIDE,
        events.PIX_MERGE_EVENT,
        dates.PIX_MERGE_DATE,
        dates._MTIME_KEY,  # pyright: ignore[reportPrivateUsage]
        dates._ORIGINAL_PATH_KEY,  # pyright: ignore[reportPrivateUsage]
        *dates._PHOTO_DATE_KEYS,  # pyright: ignore[reportPrivateUsage]
        *dates._VIDEO_DATE_KEYS,  # pyright: ignore[reportPrivateUsage]
    }
    missing = {k for k in consumed if not _is_consumed(k)}
    assert not missing, f"allowlist missing consumed keys: {sorted(missing)}"
