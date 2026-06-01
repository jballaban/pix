"""Tests for migrate's remux-repair salvage of damaged video containers
(`pix.apply._repair_video_container`). `remux_repair` is monkeypatched so
these don't shell out to ffmpeg."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

import pix.apply as apply_mod
from pix.apply import apply_plan
from pix.convert import ConvertFailed
from pix.exiftool_session import ExifToolSession, TagWriteFailed
from pix.plan import Action, Plan, PlanLine

_repair_video_container = apply_mod._repair_video_container  # pyright: ignore[reportPrivateUsage]
_repair_image = apply_mod._repair_image  # pyright: ignore[reportPrivateUsage]


def _always_remuxable(p: Path) -> bool:
    return True


def _line(src: Path) -> PlanLine:
    return PlanLine(
        line_id="L001",
        action=Action.TAG,
        rel_path=src.name,
        details="",
        abs_path=src,
    )


def test_repair_remuxes_and_swaps_conserving_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    staging = tmp_path / "staging"
    src = tmp_path / "lib" / "v.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"DAMAGED")

    def fake_remux(s: Path, d: Path) -> None:
        d.write_bytes(b"CLEAN")

    monkeypatch.setattr(apply_mod, "remux_repair", fake_remux)

    assert _repair_video_container(_line(src), run_dir, staging) is True
    # Clean container swapped into place; damaged original conserved.
    assert src.read_bytes() == b"CLEAN"
    captured = run_dir / "data" / "L001_v.mp4.damaged"
    assert captured.exists() and captured.read_bytes() == b"DAMAGED"


def test_repair_skips_non_video(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    src = tmp_path / "lib" / "photo.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"img")

    # No monkeypatch needed — it must bail before touching ffmpeg.
    assert _repair_video_container(_line(src), run_dir, tmp_path / "s") is False
    assert src.read_bytes() == b"img"  # untouched


def test_repair_returns_false_and_leaves_original_when_ffmpeg_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    src = tmp_path / "lib" / "v.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"DAMAGED")

    def boom(s: Path, d: Path) -> None:
        raise ConvertFailed("too damaged to salvage")

    monkeypatch.setattr(apply_mod, "remux_repair", boom)

    assert _repair_video_container(_line(src), run_dir, tmp_path / "s") is False
    # Original left in place for the caller to quarantine.
    assert src.exists() and src.read_bytes() == b"DAMAGED"
    assert not (run_dir / "data" / "L001_v.mp4.damaged").exists()


class _StubExif(ExifToolSession):
    """Records metadata-copy calls; spawns no subprocess."""

    def __init__(self) -> None:
        self.copied: list[tuple[Path, Path]] = []

    def copy_metadata_and_write_tags(
        self, source: Path, dest: Path, tags: dict[str, str]
    ) -> None:
        self.copied.append((source, dest))


def test_repair_image_reencodes_copies_metadata_and_swaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    staging = tmp_path / "staging"
    src = tmp_path / "lib" / "photo.jpg"  # content is actually PNG/odd
    src.parent.mkdir(parents=True)
    src.write_bytes(b"NOT-REALLY-JPEG")

    def fake_to_jpg(s: Path, d: Path) -> None:
        d.write_bytes(b"CLEANJPG")

    monkeypatch.setattr(apply_mod, "convert_to_jpg", fake_to_jpg)
    stub = _StubExif()

    ln = PlanLine(
        line_id="L001", action=Action.TAG, rel_path="photo.jpg",
        details="", abs_path=src,
    )
    assert _repair_image(ln, run_dir, staging, stub) is True
    # Clean JPEG swapped in; EXIF copied onto it; original conserved.
    assert src.read_bytes() == b"CLEANJPG"
    assert stub.copied and stub.copied[0][0] == src
    captured = run_dir / "data" / "L001_photo.jpg.original"
    assert captured.exists() and captured.read_bytes() == b"NOT-REALLY-JPEG"


# --- Salvage carve-out for Insta360 360 media (apply-loop integration) ---


class _FailWriteExif(ExifToolSession):
    """Stub session: captures sidecars fine but every tag write fails to
    persist (the `0 image files updated` case). No subprocess spawned."""

    def __init__(self) -> None:
        pass

    def export_xmp_sidecar(self, file: Path, sidecar_path: Path) -> None:
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_text("<xmp/>", encoding="utf-8")

    def write_tags(self, file: Path, tags: dict[str, str]) -> None:
        raise TagWriteFailed(Path(file), "0 image files updated")

    def close(self) -> None:
        pass


def _tag_line(src: Path, run_dir: Path) -> PlanLine:
    return PlanLine(
        line_id="L001",
        action=Action.TAG,
        rel_path=src.name,
        details="original_path init",
        abs_path=src,
        pix_writes={"XMP:OriginalPath": str(src)},
        sidecar_path=run_dir / "data" / f"L001_{src.name}.xmp",
    )


def _run_apply(
    src: Path, root: Path, run_dir: Path, staging: Path
) -> tuple[int, list[tuple[PlanLine, str]]]:
    plan = Plan(
        source=root,
        run_id=run_dir.name,
        generated_at=datetime.now(),
        lines=[_tag_line(src, run_dir)],
    )
    return apply_plan(
        plan,
        run_dir / "plan.txt",
        run_dir,
        {"L001"},
        staging_dir=staging,
    )


def test_apply_insv_tagwrite_failure_quarantines_without_salvage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .insv whose tag write doesn't persist is quarantined untouched —
    never remuxed/re-encoded (which would strip the Insta360 trailer)."""
    root = tmp_path / "lib"
    run_dir = root / ".pix" / "runs" / "r"
    run_dir.mkdir(parents=True)
    staging = root / ".pix" / "staging"
    insv = root / "VID_20230516_164835_00_010.insv"
    insv.write_bytes(b"BROKEN-INSV")

    remux_calls: list[tuple[Path, Path]] = []

    def rec_remux(s: Path, d: Path) -> None:
        remux_calls.append((s, d))

    monkeypatch.setattr(apply_mod, "remux_repair", rec_remux)
    # If the carve-out were missing, the loop would consult these — make
    # them say "yes, salvageable" so a regression would actually remux.
    monkeypatch.setattr(apply_mod, "is_remuxable_video", _always_remuxable)
    monkeypatch.setattr(apply_mod, "ExifToolSession", _FailWriteExif)

    # Stub the quarantine move — the real mirrored-path layout blows past
    # Windows' 260-char limit under deep pytest tmp dirs. We only care that
    # the file was routed to quarantine (not salvaged), not the exact path.
    quarantined: list[Path] = []

    def fake_move(*, source: Path, library_root: Path, run_id: str,
                  line_id: str, error: str) -> Path:
        quarantined.append(source)
        source.unlink()
        return library_root / ".pix" / "errors" / source.name

    monkeypatch.setattr(apply_mod, "move_to_errors", fake_move)

    completed, failures = _run_apply(insv, root, run_dir, staging)

    assert completed == 0
    assert len(failures) == 1
    assert remux_calls == []  # carve-out: never salvaged
    assert quarantined == [insv]  # routed straight to quarantine
    assert not insv.exists()


def test_apply_mp4_tagwrite_failure_does_attempt_salvage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: a regular .mp4 in the same situation DOES go through the
    remux salvage path — proving the carve-out is specific to 360 media."""
    root = tmp_path / "lib"
    run_dir = root / ".pix" / "runs" / "r"
    run_dir.mkdir(parents=True)
    staging = root / ".pix" / "staging"
    mp4 = root / "2023-05-16_164835.mp4"
    mp4.write_bytes(b"BROKEN-MP4")

    remux_calls: list[tuple[Path, Path]] = []

    def fake_remux(s: Path, d: Path) -> None:
        remux_calls.append((s, d))
        d.write_bytes(b"CLEAN")

    monkeypatch.setattr(apply_mod, "remux_repair", fake_remux)
    monkeypatch.setattr(apply_mod, "is_remuxable_video", _always_remuxable)
    monkeypatch.setattr(apply_mod, "ExifToolSession", _FailWriteExif)

    _run_apply(mp4, root, run_dir, staging)

    assert len(remux_calls) == 1  # salvage was attempted for the mp4


def test_quarantine_uses_passed_library_root_for_relocated_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a relocated run folder (config runs_dir on another volume), the
    quarantine errors-tree must use the explicit library_root, not one
    derived by walking up from the run folder (which would land on the
    wrong drive)."""
    lib = tmp_path / "lib"
    lib.mkdir()
    # Run dir deliberately NOT under lib/.pix/runs — simulates runs_dir
    # pointed at another location/volume.
    run_dir = tmp_path / "elsewhere" / "runs" / "r"
    run_dir.mkdir(parents=True)
    staging = tmp_path / "staging"
    insv = lib / "VID_x_00_001.insv"  # 360 → carve-out → quarantine on fail
    insv.write_bytes(b"x")

    monkeypatch.setattr(apply_mod, "is_remuxable_video", _always_remuxable)
    monkeypatch.setattr(apply_mod, "ExifToolSession", _FailWriteExif)

    seen: dict[str, Path] = {}

    def fake_move(*, source: Path, library_root: Path, run_id: str,
                  line_id: str, error: str) -> Path:
        seen["root"] = library_root
        source.unlink()
        return library_root / ".pix" / "errors" / source.name

    monkeypatch.setattr(apply_mod, "move_to_errors", fake_move)

    plan = Plan(
        source=lib, run_id="r", generated_at=datetime.now(),
        lines=[_tag_line(insv, run_dir)],
    )
    apply_plan(
        plan, run_dir / "plan.txt", run_dir, {"L001"},
        staging_dir=staging, library_root=lib,
    )
    # Must be the passed library root, not run_dir.parent.parent.parent.
    assert seen["root"] == lib
