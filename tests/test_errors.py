"""Tests for `pix.errors` — quarantine of CONVERT failures."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from pix.errors import (
    ErrorSidecar,
    errors_dir_for,
    move_to_errors,
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


def test_sidecar_round_trip() -> None:
    sidecar = ErrorSidecar(
        original_path="G:/foo/bar.heic",
        failed_at="2026-05-25T15:32:01",
        error="Pillow failed",
        run_id="2026-05-25_14-52-13",
    )
    yaml_text = sidecar.to_yaml()
    parsed = ErrorSidecar.from_yaml(yaml_text)
    assert parsed == sidecar


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
