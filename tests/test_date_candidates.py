"""Tests for `pix.dates.date_candidates` — the `pix info meta` date explainer."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pix.dates import date_candidates
from pix.metadata import FileMetadata
from pix.plan import PIX_ORIGINAL_PATH


def _meta(path: Path, **fields: object) -> FileMetadata:
    return FileMetadata(path=path, raw={"SourceFile": str(path), **fields})


def test_winner_is_first_parsed_in_priority_order() -> None:
    """Replicates the real 1979 case: zeroed QuickTime clock, a bogus
    filename date that wins, and a folder date that *would* have won."""
    path = Path("G:/pix/1979/New Years at the cottage/10/1979-10-24_045010.mp4")
    meta = _meta(
        path,
        **{
            "QuickTime:CreateDate": "0000:00:00 00:00:00",
            "QuickTime:MediaCreateDate": "0000:00:00 00:00:00",
            PIX_ORIGINAL_PATH: (
                "G:/pix/raw/media/1979/2008-12-31 New Years at the cottage/"
                "1979-10-24-045010000.mp4"
            ),
        },
    )
    cands = date_candidates(meta)

    by_label = {c.label: c for c in cands}
    assert by_label["QuickTime:CreateDate"].note == "unparseable"
    assert by_label["XMP:CreateDate"].note == "absent"

    fn = by_label["filename · OriginalPath"]
    assert fn.parsed == datetime(1979, 10, 24, 4, 50, 10)

    folder = by_label["folder · OriginalPath"]
    assert folder.parsed == datetime(2008, 12, 31, 0, 0, 0)

    # The winner (first parsed in order) is the filename candidate.
    winner = next(c for c in cands if c.parsed is not None)
    assert winner.label == "filename · OriginalPath"


def test_video_uses_quicktime_candidate_set() -> None:
    path = Path("/lib/clip.mp4")
    meta = _meta(path, **{"QuickTime:CreateDate": "2020:01:02 03:04:05"})
    labels = [c.label for c in date_candidates(meta)]
    assert "QuickTime:CreateDate" in labels
    assert "EXIF:DateTimeOriginal" not in labels  # photo-only key absent


def test_photo_uses_exif_candidate_set() -> None:
    path = Path("/lib/photo.jpg")
    meta = _meta(path, **{"EXIF:DateTimeOriginal": "2021:06:07 08:09:10"})
    cands = date_candidates(meta)
    by_label = {c.label: c for c in cands}
    assert by_label["EXIF:DateTimeOriginal"].parsed == datetime(
        2021, 6, 7, 8, 9, 10
    )
    winner = next(c for c in cands if c.parsed is not None)
    assert winner.label == "EXIF:DateTimeOriginal"


def test_original_path_absent_marks_those_candidates_absent() -> None:
    path = Path("/lib/2019-05-05_120000.jpg")
    meta = _meta(path)  # no OriginalPath, no metadata dates
    by_label = {c.label: c for c in date_candidates(meta)}
    assert by_label["filename · OriginalPath"].note == "absent"
    assert by_label["folder · OriginalPath"].note == "absent"
    # Current filename still carries a parseable date.
    assert by_label["filename · current"].parsed == datetime(
        2019, 5, 5, 12, 0, 0
    )
