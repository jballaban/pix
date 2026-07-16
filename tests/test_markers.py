"""The transient-marker naming convention: every pix-authored marker must be
covered by the single sync-exclude glob `*.__*`, and ordinary media files must
not be. Guards `pix.markers` against drift (e.g. a new marker that forgets the
`.__` convention would silently escape the sync rule)."""

from __future__ import annotations

from fnmatch import fnmatch

from pix import markers


def test_all_pix_markers_match_the_single_glob() -> None:
    """Every specific pix marker constant embeds the shared infix, so the one
    sync rule (`PIX_MARKER_GLOB`) covers them all."""
    for const in (
        markers.RENAME_SUFFIX,
        markers.CONVERT_INFIX,
        markers.ORGANIZE_TMP_SUFFIX,
        markers.ROTATE_INFIX,
    ):
        assert markers.MARKER_INFIX in const, const


def test_constructed_marker_names_match_glob() -> None:
    """The actual on-disk names built at each call site match `PIX_MARKER_GLOB`."""
    names = [
        f"IMG_1234.JPG{markers.RENAME_SUFFIX}",              # apply case-rename
        f"IMG_1234.HEIC{markers.CONVERT_INFIX}jpg",          # migrate CONVERT
        f"L001{markers.ORGANIZE_TMP_SUFFIX}",                # organize park
        f"clip.__rot__.mp4",                                 # rotate remux temp
    ]
    for name in names:
        assert fnmatch(name, markers.PIX_MARKER_GLOB), name
        assert markers.is_pix_marker(name), name


def test_exiftool_temp_is_the_documented_exception() -> None:
    """ExifTool's temp is externally named: it does NOT match the pix glob and
    needs its own rule."""
    name = f"IMG_1234.jpg{markers.EXIFTOOL_TMP_SUFFIX}"
    assert not fnmatch(name, markers.PIX_MARKER_GLOB), name
    assert not markers.is_pix_marker(name), name
    assert fnmatch(name, markers.EXIFTOOL_TMP_GLOB), name


def test_ordinary_media_names_are_not_matched() -> None:
    """A canonical media filename must never be caught by the exclude glob."""
    for name in ("2023-08-15_143205.jpg", "IMG_1234.HEIC", "VID_0001.mp4"):
        assert not fnmatch(name, markers.PIX_MARKER_GLOB), name
        assert not markers.is_pix_marker(name), name
