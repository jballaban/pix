"""Tests for `pix.errors` — quarantine of CONVERT failures."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from pix import __version__ as PIX_VERSION
from pix.errors import (
    ErrorSidecar,
    errors_dir_for,
    find_orphaned_error_files,
    move_to_errors,
    restore_orphaned_errors,
    restore_stale_errors,
    sidecar_path_for,
)


def test_errors_dir_lives_under_pix(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    assert errors_dir_for(root) == root / ".pix" / "errors"


def test_move_to_errors_relocates_file_and_writes_sidecar(
    tmp_path: Path,
) -> None:
    root = tmp_path / "lib"
    src = tmp_path / "src" / "bad.heic"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"truncated heic bytes")

    dest = move_to_errors(
        source=src,
        library_root=root,
        run_id="2026-05-25_14-52-13",
        line_id="L0042",
        error="Pillow failed to convert ... truncated (14 bytes)",
        failed_at=datetime(2026, 5, 25, 15, 32, 1),
    )

    # File moved to .pix/errors/<run-id>_<line-id>.<ext>.
    assert dest == root / ".pix" / "errors" / "2026-05-25_14-52-13_L0042.heic"
    assert dest.is_file()
    assert dest.read_bytes() == b"truncated heic bytes"
    assert not src.exists()

    # Sidecar written next to the file.
    sidecar = sidecar_path_for(dest)
    assert sidecar.is_file()
    parsed = ErrorSidecar.from_yaml(sidecar.read_text(encoding="utf-8"))
    assert parsed.original_path == str(src)
    assert parsed.failed_at == "2026-05-25T15:32:01"
    assert "truncated" in parsed.error
    assert parsed.run_id == "2026-05-25_14-52-13"
    assert parsed.pix_version == PIX_VERSION


def test_sidecar_round_trip() -> None:
    sidecar = ErrorSidecar(
        original_path="G:/foo/bar.heic",
        failed_at="2026-05-25T15:32:01",
        error="Pillow failed",
        run_id="2026-05-25_14-52-13",
        pix_version="0.1.99",
    )
    yaml_text = sidecar.to_yaml()
    parsed = ErrorSidecar.from_yaml(yaml_text)
    assert parsed == sidecar


def test_sidecar_from_yaml_treats_missing_pix_version_as_empty() -> None:
    """Legacy errorinfo files (pre-v0.1.86) lack pix_version. We accept
    them with an empty string so the restore logic can treat them as
    stale rather than refuse the parse."""
    # YAML auto-detects unquoted ISO timestamps as datetime; the real
    # legacy errorinfo files quote the string (yaml.safe_dump does this
    # automatically for stringy values that look like timestamps).
    legacy_yaml = (
        "original_path: G:/foo/bar.heic\n"
        "failed_at: '2026-05-25T15:32:01'\n"
        "error: Pillow failed\n"
        "run_id: '2026-05-25_14-52-13'\n"
    )
    parsed = ErrorSidecar.from_yaml(legacy_yaml)
    assert parsed.pix_version == ""


def test_sidecar_from_yaml_rejects_missing_fields() -> None:
    with pytest.raises(ValueError, match="original_path"):
        ErrorSidecar.from_yaml(
            "failed_at: x\nerror: y\nrun_id: z\n"
        )
    with pytest.raises(ValueError, match="failed_at"):
        ErrorSidecar.from_yaml(
            "original_path: x\nerror: y\nrun_id: z\n"
        )
    with pytest.raises(ValueError, match="error"):
        ErrorSidecar.from_yaml(
            "original_path: x\nfailed_at: y\nrun_id: z\n"
        )
    with pytest.raises(ValueError, match="run_id"):
        ErrorSidecar.from_yaml(
            "original_path: x\nfailed_at: y\nerror: z\n"
        )


def _quarantine(
    *,
    library_root: Path,
    original_path: Path,
    pix_version: str,
    line_id: str = "L001",
    contents: bytes = b"data",
) -> Path:
    """Helper: create a quarantined entry with a chosen pix_version."""
    original_path.parent.mkdir(parents=True, exist_ok=True)
    original_path.write_bytes(contents)
    dest = move_to_errors(
        source=original_path,
        library_root=library_root,
        run_id="2026-05-25_14-52-13",
        line_id=line_id,
        error="old failure",
    )
    # Rewrite the sidecar with the chosen pix_version (move_to_errors
    # always stamps the current version; we need to simulate older
    # entries).
    sidecar = ErrorSidecar(
        original_path=str(original_path),
        failed_at="2026-05-25T15:32:01",
        error="old failure",
        run_id="2026-05-25_14-52-13",
        pix_version=pix_version,
    )
    sidecar_path_for(dest).write_text(sidecar.to_yaml(), encoding="utf-8")
    return dest


def test_restore_stale_errors_brings_back_old_version_entries(
    tmp_path: Path,
) -> None:
    """Entries written by a different (older) pix version get restored."""
    root = tmp_path / "lib"
    src = tmp_path / "src" / "old.heic"

    quarantined = _quarantine(
        library_root=root,
        original_path=src,
        pix_version="0.1.50",  # older than current
        contents=b"old bytes",
    )
    assert quarantined.is_file()
    assert not src.exists()

    restored, skipped, kept = restore_stale_errors(root)
    assert len(restored) == 1
    assert restored[0].original_path == src
    assert restored[0].sidecar_pix_version == "0.1.50"
    assert skipped == []
    assert kept == 0

    # File back in source, sidecar gone.
    assert src.is_file()
    assert src.read_bytes() == b"old bytes"
    assert not quarantined.is_file()
    assert not sidecar_path_for(quarantined).exists()


def test_restore_stale_errors_leaves_current_version_in_place(
    tmp_path: Path,
) -> None:
    """Entries written by the running pix version stay quarantined."""
    root = tmp_path / "lib"
    src = tmp_path / "src" / "current.heic"

    quarantined = _quarantine(
        library_root=root,
        original_path=src,
        pix_version=PIX_VERSION,
    )

    restored, skipped, kept = restore_stale_errors(root)
    assert restored == []
    assert skipped == []
    assert kept == 1
    assert quarantined.is_file()
    assert not src.exists()


def test_restore_stale_errors_treats_legacy_sidecar_as_stale(
    tmp_path: Path,
) -> None:
    """Sidecars missing pix_version (pre-v0.1.86) get restored too."""
    root = tmp_path / "lib"
    src = tmp_path / "src" / "legacy.heic"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"legacy")
    dest = move_to_errors(
        source=src,
        library_root=root,
        run_id="run1",
        line_id="L001",
        error="legacy failure",
    )
    # Overwrite the sidecar with a legacy (no pix_version) form. YAML
    # requires the timestamp to be quoted so it doesn't auto-parse as
    # a datetime — matches yaml.safe_dump's actual output for the field.
    sidecar_path_for(dest).write_text(
        "original_path: " + str(src) + "\n"
        "failed_at: '2026-05-25T15:32:01'\n"
        "error: legacy failure\n"
        "run_id: run1\n",
        encoding="utf-8",
    )

    restored, _skipped, kept = restore_stale_errors(root)
    assert len(restored) == 1
    assert restored[0].sidecar_pix_version == ""
    assert kept == 0
    assert src.is_file()


def test_restore_stale_errors_skips_when_target_exists(
    tmp_path: Path,
) -> None:
    """If the original_path slot is already occupied, don't overwrite."""
    root = tmp_path / "lib"
    src = tmp_path / "src" / "x.heic"

    _quarantine(
        library_root=root,
        original_path=src,
        pix_version="0.1.50",
        contents=b"quarantined",
    )
    # User restored the original file out-of-band before re-running.
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"manually-restored")

    restored, skipped, _kept = restore_stale_errors(root)
    assert restored == []
    assert len(skipped) == 1
    assert "already exists" in skipped[0].reason
    # Manual restore stays as-is.
    assert src.read_bytes() == b"manually-restored"


def test_find_orphaned_error_files_only_returns_sidecar_less_data(
    tmp_path: Path,
) -> None:
    """A data file with no adjacent .errorinfo is an orphan; ones with a
    sidecar, and lone sidecars, are not."""
    root = tmp_path / "lib"
    errors_dir = errors_dir_for(root)
    errors_dir.mkdir(parents=True)

    orphan = errors_dir / "run_L001.mp4"
    orphan.write_bytes(b"orphan")
    # Properly-paired entry: data + sidecar.
    paired = errors_dir / "run_L002.mp4"
    paired.write_bytes(b"paired")
    sidecar_path_for(paired).write_text("original_path: x\n", encoding="utf-8")
    # Lone sidecar (no data) — not a data orphan.
    (errors_dir / "run_L003.mp4.errorinfo").write_text(
        "original_path: y\n", encoding="utf-8"
    )

    assert find_orphaned_error_files(root) == [orphan]


def test_find_orphaned_error_files_empty_when_no_dir(tmp_path: Path) -> None:
    assert find_orphaned_error_files(tmp_path / "lib") == []


def test_restore_orphaned_errors_moves_into_target(tmp_path: Path) -> None:
    """A sidecar-less errors file is moved into the migrate target folder."""
    root = tmp_path / "lib"
    errors_dir = errors_dir_for(root)
    errors_dir.mkdir(parents=True)
    orphan = errors_dir / "2026-05-28_10-55-13_L1042.mp4"
    orphan.write_bytes(b"video bytes")
    target = root / "2014"

    restored, skipped = restore_orphaned_errors(root, target)

    assert skipped == []
    assert len(restored) == 1
    moved = target / "2026-05-28_10-55-13_L1042.mp4"
    assert restored[0].original_path == moved
    assert restored[0].sidecar_pix_version == ""
    assert moved.is_file()
    assert moved.read_bytes() == b"video bytes"
    assert not orphan.exists()  # gone from errors/


def test_restore_orphaned_errors_skips_collision(tmp_path: Path) -> None:
    """Never clobber an existing file in the target folder."""
    root = tmp_path / "lib"
    errors_dir = errors_dir_for(root)
    errors_dir.mkdir(parents=True)
    orphan = errors_dir / "run_L001.mp4"
    orphan.write_bytes(b"orphan")
    target = root / "2014"
    target.mkdir(parents=True)
    (target / "run_L001.mp4").write_bytes(b"already here")

    restored, skipped = restore_orphaned_errors(root, target)

    assert restored == []
    assert len(skipped) == 1
    assert "already exists" in skipped[0].reason
    assert orphan.exists()  # left in place
    assert (target / "run_L001.mp4").read_bytes() == b"already here"


def test_restore_orphaned_errors_leaves_paired_entries_alone(
    tmp_path: Path,
) -> None:
    """A data file that still has its sidecar is not an orphan — untouched."""
    root = tmp_path / "lib"
    errors_dir = errors_dir_for(root)
    errors_dir.mkdir(parents=True)
    paired = errors_dir / "run_L002.mp4"
    paired.write_bytes(b"paired")
    sidecar_path_for(paired).write_text("original_path: x\n", encoding="utf-8")

    restored, skipped = restore_orphaned_errors(root, root / "src")

    assert restored == []
    assert skipped == []
    assert paired.exists()


def test_move_to_errors_opaque_name_avoids_collisions(
    tmp_path: Path,
) -> None:
    """Two sources with the same filename get different opaque names."""
    root = tmp_path / "lib"
    a = tmp_path / "a" / "IMG_0001.heic"
    b = tmp_path / "b" / "IMG_0001.heic"
    a.parent.mkdir(parents=True)
    b.parent.mkdir(parents=True)
    a.write_bytes(b"A")
    b.write_bytes(b"B")

    da = move_to_errors(
        source=a, library_root=root,
        run_id="run1", line_id="L001", error="err",
    )
    db = move_to_errors(
        source=b, library_root=root,
        run_id="run1", line_id="L002", error="err",
    )
    assert da != db
    assert da.read_bytes() == b"A"
    assert db.read_bytes() == b"B"
