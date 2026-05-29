"""Command-level tests for `pix migrate`'s `.pix/errors/` console reporting.

A file quarantined by the *current* pix version is deliberately not retried
(the same code would fail again). The mechanics live in `pix.errors` and are
covered by `test_errors.py`; here we pin the migrate-side behavior that the
user can actually see: the console must say *why* a quarantined file is being
left alone, instead of silently printing "Nothing to do."
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pix import __version__ as PIX_VERSION
from pix.commands.migrate import migrate_folder
from pix.config import DEFAULT_CONFIG_YAML
from pix.errors import ErrorSidecar, move_to_errors, sidecar_path_for
from pix.schema import SCHEMA_VERSION


def _make_library(tmp_path: Path) -> Path:
    """Create an empty library root with a valid .pix/ scaffold."""
    root = tmp_path / "lib"
    pix = root / ".pix"
    pix.mkdir(parents=True)
    (pix / "config.yaml").write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    (pix / "state.yaml").write_text(
        f"schema_version: {SCHEMA_VERSION}\n", encoding="utf-8"
    )
    return root


def test_migrate_reports_quarantined_file_kept_this_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file quarantined under the running version is announced, not hidden.

    The library has no other source files, so migrate short-circuits at
    "Nothing to do." — without the console note the user would have no clue
    a quarantined file exists or why it isn't being retried.
    """
    root = _make_library(tmp_path)
    bad = root / "raw" / "bad.heic"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"truncated")
    # move_to_errors stamps the current pix version → restore leaves it put.
    move_to_errors(
        source=bad,
        library_root=root,
        run_id="2026-05-25_14-52-13",
        line_id="L001",
        error="Pillow failed to convert",
    )

    migrate_folder(root)

    captured = capsys.readouterr()
    assert "Nothing to do." in captured.out
    assert "remain quarantined" in captured.err
    assert PIX_VERSION in captured.err
    assert ".errorinfo" in captured.err


def test_migrate_reports_unrestorable_errorinfo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lone .errorinfo (data file gone) is flagged for attention on console."""
    root = _make_library(tmp_path)
    errors_dir = root / ".pix" / "errors"
    errors_dir.mkdir(parents=True)
    # Sidecar with no adjacent data file → restore can't proceed.
    sidecar = ErrorSidecar(
        original_path=str(root / "raw" / "gone.heic"),
        failed_at="2026-05-25T15:32:01",
        error="old failure",
        run_id="2026-05-25_14-52-13",
        pix_version="0.1.50",  # older, so restore is attempted (then skipped)
    )
    (errors_dir / "2026-05-25_14-52-13_L001.heic.errorinfo").write_text(
        sidecar.to_yaml(), encoding="utf-8"
    )

    migrate_folder(root)

    err = capsys.readouterr().err
    assert "Could not restore" in err
    assert "needs attention" in err
    assert "missing" in err  # the per-entry reason


def test_migrate_silent_when_errors_dir_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No quarantine chatter when there's nothing in .pix/errors/."""
    root = _make_library(tmp_path)
    migrate_folder(root)
    captured = capsys.readouterr()
    assert "Nothing to do." in captured.out
    assert "quarantined" not in captured.err
    assert "Could not restore" not in captured.err


def test_migrate_reports_restored_older_version_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file quarantined by an older version is restored — and announced.

    We point original_path at a location the source walk won't re-ingest as
    a convertible file: the restore itself is what we're asserting, surfaced
    on the console so the user understands why prior-failed work reappeared.
    """
    root = _make_library(tmp_path)
    errors_dir = root / ".pix" / "errors"
    errors_dir.mkdir(parents=True)
    data = errors_dir / "2026-05-25_14-52-13_L001.heic"
    data.write_bytes(b"old bytes")
    # Restore target outside the library so migrate won't try to convert it.
    target = tmp_path / "outside" / "old.heic"
    sidecar = ErrorSidecar(
        original_path=str(target),
        failed_at="2026-05-25T15:32:01",
        error="old failure",
        run_id="2026-05-25_14-52-13",
        pix_version="0.1.50",  # older than current → restored
    )
    sidecar_path_for(data).write_text(sidecar.to_yaml(), encoding="utf-8")

    migrate_folder(root)

    out = capsys.readouterr().out
    assert "Restored 1 file(s) from .pix/errors/" in out
    assert target.is_file()  # actually moved back
