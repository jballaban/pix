"""Tests for `pix.metadata_filter` — the stored-metadata allowlist."""

from __future__ import annotations

from pix import dates, events, plan, rating
from pix.metadata_filter import _is_consumed, consumed_read_args, filter_consumed


def test_drops_noise_keeps_consumed() -> None:
    raw: dict[str, object] = {
        "SourceFile": "G:/pix/x.jpg",
        "EXIF:DateTimeOriginal": "2023:08:15 14:32:05",
        "XMP:EventOverride": "Hawaii",
        "XMP:RegionName": "Alice",  # face region — kept
        "XMP:Rating": "5",  # rating tag — kept
        "EXIF:MakerNoteCanon": "....big blob....",  # noise — dropped
        "EXIF:ThumbnailImage": "base64....",  # noise — dropped
    }
    out = filter_consumed(raw)
    assert set(out) == {
        "SourceFile",
        "EXIF:DateTimeOriginal",
        "XMP:EventOverride",
        "XMP:RegionName",
        "XMP:Rating",
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
        rating.XMP_RATING,
        dates._MTIME_KEY,  # pyright: ignore[reportPrivateUsage]
        dates._ORIGINAL_PATH_KEY,  # pyright: ignore[reportPrivateUsage]
        *dates._PHOTO_DATE_KEYS,  # pyright: ignore[reportPrivateUsage]
        *dates._VIDEO_DATE_KEYS,  # pyright: ignore[reportPrivateUsage]
    }
    missing = {k for k in consumed if not _is_consumed(k)}
    assert not missing, f"allowlist missing consumed keys: {sorted(missing)}"


def test_read_args_cover_consumed_non_xmp_keys() -> None:
    """The ExifTool read allowlist must request `-XMP:all` (covers every
    consumed XMP/pix/region tag) plus every consumed non-XMP key — otherwise
    a cache fill could miss a tag pix needs."""
    args = consumed_read_args()
    assert "-XMP:all" in args  # covers every consumed XMP/pix/region tag
    needed = {
        dates._MTIME_KEY,  # pyright: ignore[reportPrivateUsage]
        *dates._PHOTO_DATE_KEYS,  # pyright: ignore[reportPrivateUsage]
        *dates._VIDEO_DATE_KEYS,  # pyright: ignore[reportPrivateUsage]
    }
    for key in needed:
        if key.startswith("XMP:"):
            continue  # covered by -XMP:all
        assert f"-{key}" in args, f"read allowlist missing -{key}"
    # XMP keys are covered by -XMP:all, never requested individually.
    assert all(a == "-XMP:all" or not a.startswith("-XMP:") for a in args)
