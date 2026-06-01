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
import typer

from pix import __version__ as PIX_VERSION
from pix.commands.migrate import migrate_folder
from pix.errors import ErrorSidecar, move_to_errors, sidecar_path_for


def _make_library(tmp_path: Path) -> Path:
    """Create an empty library root with a valid .pix/ scaffold."""
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)  # version-less; settings file optional
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


def test_migrate_restores_mirrored_older_version_to_true_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mirrored older-version entry is restored to its ORIGINAL path (via
    its location), not guessed — proving location-as-provenance end to end.

    Unknown extension so migrate fast-fails after the restore, before the
    exiftool-backed metadata phase.
    """
    from pix.errors import errors_path_for, sidecar_path_for

    root = _make_library(tmp_path)
    src = root / "2014" / "old.zzz"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"data")
    dest = move_to_errors(
        source=src, library_root=root, run_id="r", line_id="L1", error="e"
    )
    assert dest == errors_path_for(root, src)
    # Re-stamp the sidecar with an older version so restore is attempted.
    sidecar_path_for(dest).write_text(
        ErrorSidecar(
            original_path=str(src),
            failed_at="2026-05-25T15:32:01",
            error="e",
            run_id="r",
            pix_version="0.1.50",
        ).to_yaml(),
        encoding="utf-8",
    )
    assert not src.exists()

    with pytest.raises(typer.Exit) as exc:
        migrate_folder(root)
    assert exc.value.exit_code == 1  # unknown-extension fast-fail

    assert src.is_file()  # restored to its true source path
    assert not dest.exists()


def test_migrate_reprocesses_orphaned_sidecar_less_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sidecar-less .pix/errors/ file is moved back into the migrate folder
    for another attempt.

    We give it an unknown extension so migrate fast-fails on extension
    validation *after* the orphan restore but *before* the exiftool-backed
    metadata phase — letting us assert the relocation without ffmpeg/exiftool.
    """
    root = _make_library(tmp_path)
    errors_dir = root / ".pix" / "errors"
    errors_dir.mkdir(parents=True)
    orphan = errors_dir / "2026-05-28_10-55-13_L1042.zzz"
    orphan.write_bytes(b"orphan bytes")  # no .errorinfo sidecar

    with pytest.raises(typer.Exit) as exc:
        migrate_folder(root)
    assert exc.value.exit_code == 1  # unknown-extension fast-fail

    # Orphan was relocated out of errors/ into the migrate folder before
    # the extension check tripped.
    assert not orphan.exists()
    assert (root / "2026-05-28_10-55-13_L1042.zzz").is_file()
    err = capsys.readouterr().err
    assert "doesn't handle" in err


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
