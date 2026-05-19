"""Tests for the apply loop.

Split into two layers:
- Unit-level: RENAME and DELETE handlers with the real apply path but
  with no TAG actions, so ExifTool isn't actually invoked.
- Integration (`needs_exiftool` marker): full apply including TAG writes
  + XMP sidecar export; skipped when `exiftool` isn't on PATH.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pytest

from pix.apply import apply_plan
from pix.plan import (
    PIX_DATE_AUTO,
    Action,
    Plan,
    PlanLine,
)


needs_exiftool = pytest.mark.skipif(
    shutil.which("exiftool") is None,
    reason="exiftool not installed on PATH",
)


def _make_plan(
    source: Path, lines: list[PlanLine], run_id: str = "test-run"
) -> Plan:
    return Plan(
        source=source,
        run_id=run_id,
        generated_at=datetime(2026, 5, 18, 19, 0, 0),
        lines=lines,
    )


def test_apply_delete_moves_file_into_run_dir(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    junk = src / "Thumbs.db"
    junk.write_bytes(b"junk")

    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    plan = _make_plan(
        src,
        [
            PlanLine(
                line_id="L001",
                action=Action.DELETE,
                rel_path="Thumbs.db",
                details="extension policy: delete",
                abs_path=junk.resolve(),
            )
        ],
    )
    plan_path = run_dir / "plan.txt"
    plan_path.write_text(plan.to_text(), encoding="utf-8")

    completed, skipped = apply_plan(
        plan=plan,
        plan_path=plan_path,
        run_dir=run_dir,
        kept_line_ids={"L001"},
    )
    assert (completed, skipped) == (1, 0)
    assert not junk.exists()
    assert (run_dir / "L001_Thumbs.db").exists()

    # Plan was updated with completion annotation.
    updated = plan_path.read_text(encoding="utf-8")
    assert "Completed]" in updated


def test_apply_rename_within_same_folder(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    old = src / "DSC_0042.JPG"
    old.write_bytes(b"img")

    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    plan = _make_plan(
        src,
        [
            PlanLine(
                line_id="L001",
                action=Action.RENAME,
                rel_path="DSC_0042.JPG",
                details="→2023-08-15_143612.jpg",
                abs_path=old.resolve(),
                target_filename="2023-08-15_143612.jpg",
            )
        ],
    )
    plan_path = run_dir / "plan.txt"
    plan_path.write_text(plan.to_text(), encoding="utf-8")

    completed, _ = apply_plan(
        plan=plan,
        plan_path=plan_path,
        run_dir=run_dir,
        kept_line_ids={"L001"},
    )
    assert completed == 1
    assert not old.exists()
    assert (src / "2023-08-15_143612.jpg").exists()
    assert (src / "2023-08-15_143612.jpg").read_bytes() == b"img"


def test_apply_rename_case_only_difference(tmp_path: Path) -> None:
    """Case-only renames must work on case-insensitive filesystems (NTFS).

    A direct `os.rename("FOO.JPG", "FOO.jpg")` on NTFS can silently no-op
    because the OS sees them as the same file. The apply layer goes through
    a temp name to force the case to change.
    """
    src = tmp_path / "src"
    src.mkdir()
    old = src / "DSC_0042.JPG"
    old.write_bytes(b"img")

    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    plan = _make_plan(
        src,
        [
            PlanLine(
                line_id="L001",
                action=Action.RENAME,
                rel_path="DSC_0042.JPG",
                details="→DSC_0042.jpg",
                abs_path=old.resolve(),
                target_filename="DSC_0042.jpg",
            )
        ],
    )
    plan_path = run_dir / "plan.txt"
    plan_path.write_text(plan.to_text(), encoding="utf-8")

    apply_plan(
        plan=plan,
        plan_path=plan_path,
        run_dir=run_dir,
        kept_line_ids={"L001"},
    )

    # Reading via Path.exists() would succeed regardless of case on NTFS,
    # so verify by listing the directory: the actual on-disk name must be
    # the lowercase form.
    on_disk = [p.name for p in src.iterdir()]
    assert on_disk == ["DSC_0042.jpg"]


def test_apply_skips_lines_not_in_kept_set(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    keep = src / "Thumbs.db"
    keep.write_bytes(b"")
    skip = src / "Desktop.ini"
    skip.write_bytes(b"")

    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    plan = _make_plan(
        src,
        [
            PlanLine(
                line_id="L001",
                action=Action.DELETE,
                rel_path="Thumbs.db",
                details="extension policy: delete",
                abs_path=keep.resolve(),
            ),
            PlanLine(
                line_id="L002",
                action=Action.DELETE,
                rel_path="Desktop.ini",
                details="extension policy: delete",
                abs_path=skip.resolve(),
            ),
        ],
    )
    plan_path = run_dir / "plan.txt"
    plan_path.write_text(plan.to_text(), encoding="utf-8")

    completed, _ = apply_plan(
        plan=plan,
        plan_path=plan_path,
        run_dir=run_dir,
        kept_line_ids={"L001"},  # only L001 survived editor edit
    )
    assert completed == 1
    assert not keep.exists()
    assert skip.exists()  # untouched


@needs_exiftool
def test_apply_tag_writes_pix_field_and_creates_sidecar(
    tmp_path: Path,
) -> None:
    # Use a tiny but ExifTool-readable JPEG so writes succeed.
    src = tmp_path / "src"
    src.mkdir()
    jpg = src / "2023-08-15_143205.jpg"
    jpg.write_bytes(_minimal_jpeg())

    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    plan = _make_plan(
        src,
        [
            PlanLine(
                line_id="L001",
                action=Action.TAG,
                rel_path="2023-08-15_143205.jpg",
                details="date_auto null→2023-08-15-14:32:05; original_path init",
                abs_path=jpg.resolve(),
                is_first_migrate=True,
                pix_writes={PIX_DATE_AUTO: "2023-08-15-14:32:05"},
                needs_original_path=True,
            )
        ],
    )
    plan_path = run_dir / "plan.txt"
    plan_path.write_text(plan.to_text(), encoding="utf-8")

    completed, _ = apply_plan(
        plan=plan,
        plan_path=plan_path,
        run_dir=run_dir,
        kept_line_ids={"L001"},
    )
    assert completed == 1
    assert jpg.exists()  # in-place TAG; file stays

    # Sidecar captured prior XMP.
    sidecar = run_dir / "L001_2023-08-15_143205.jpg.xmp"
    assert sidecar.exists()

    # Verify the pix:* fields actually landed on the file by re-reading.
    from pix.metadata import build_cache

    cache = build_cache(src)
    meta = cache[jpg.resolve()]
    assert meta.get_str(PIX_DATE_AUTO) == "2023-08-15-14:32:05"
    assert meta.get_str("XMP:OriginalPath") == str(jpg.resolve())


@needs_exiftool
def test_apply_convert_png_to_jpg_end_to_end(tmp_path: Path) -> None:
    """CONVERT renames PNG to canonical JPG with pix:* fields written."""
    from PIL import Image

    src = tmp_path / "src"
    src.mkdir()
    png = src / "IMG_001.png"
    Image.new("RGB", (50, 50), color="red").save(png, "PNG")

    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    staging = tmp_path / "staging"

    plan = _make_plan(
        src,
        [
            PlanLine(
                line_id="L001",
                action=Action.CONVERT_RENAME_TAG,
                rel_path="IMG_001.png",
                details="→2023-08-15_143205.jpg; original_path init; "
                "date_auto null→2023-08-15-14:32:05",
                abs_path=png.resolve(),
                is_first_migrate=True,
                target_filename="2023-08-15_143205.jpg",
                pix_writes={PIX_DATE_AUTO: "2023-08-15-14:32:05"},
                needs_original_path=True,
            )
        ],
    )
    plan_path = run_dir / "plan.txt"
    plan_path.write_text(plan.to_text(), encoding="utf-8")

    completed, _ = apply_plan(
        plan=plan,
        plan_path=plan_path,
        run_dir=run_dir,
        kept_line_ids={"L001"},
        staging_dir=staging,
    )
    assert completed == 1

    # Source folder now has the canonical JPG, no PNG.
    on_disk = sorted(p.name for p in src.iterdir())
    assert on_disk == ["2023-08-15_143205.jpg"]

    # Original captured to runs/.
    assert (run_dir / "L001_IMG_001.png").exists()

    # Converted file has pix:* fields.
    from pix.metadata import build_cache

    cache = build_cache(src)
    converted = cache[(src / "2023-08-15_143205.jpg").resolve()]
    assert converted.get_str(PIX_DATE_AUTO) == "2023-08-15-14:32:05"
    assert "IMG_001.png" in (converted.get_str("XMP:OriginalPath") or "")


def _minimal_jpeg() -> bytes:
    """A minimal JPEG file ExifTool can read+write to."""
    # SOI + APP0 (JFIF) + DQT + SOF0 + DHT + SOS + EOI
    # Reasonable byte sequence borrowed from a 1x1 JPEG.
    return bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000"
        "ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d"
        "1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d3832"
        "3c2e333432"
        "ffc0000b08000100010101110000"
        "ffc40014000100000000000000000000000000000003"
        "ffc40014100100000000000000000000000000000000"
        "ffda0008010100003f00fb"
        "ffd9"
    )
