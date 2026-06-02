"""Tests for the hybrid CPU/GPU CONVERT routing (spec/migrate.md → Convert
concurrency).

Covers:
- `_route_encoder` content preference (4K→GPU, else CPU).
- `convert_to_mp4`'s encoder branch building the right ffmpeg command
  (libx265 vs hevc_nvenc) and re-muxing already-HEVC sources regardless.
- `nvenc_available` two-stage detection (listed + functional probe), cached.
- `_StagingPrefetcher._acquire` slot routing + HD→GPU overflow, and that 4K
  never overflows to a CPU slot.
- The x265 fallback when a GPU encode raises `ConvertFailed`.
"""

from __future__ import annotations

# This module white-box-tests internal routing helpers (_route_encoder,
# _StagingPrefetcher, its slot semaphores) that have no public surface.
# pyright: reportPrivateUsage=false

import threading
from pathlib import Path
from typing import Any

import pytest

from pix import apply, convert
from pix.apply import _ActiveEncode, _route_encoder, _StagingPrefetcher
from pix.convert import (
    ConvertFailed,
    ENCODER_NVENC,
    ENCODER_X265,
    VideoProfile,
    convert_to_mp4,
    nvenc_available,
)
from pix.plan import Action, PlanLine


def _passthrough(name: str) -> str:
    """Stand-in for `convert._require_tool` — returns the tool name as-is."""
    return name


def _profile(codec: str = "h264", height: int = 1080) -> VideoProfile:
    return VideoProfile(
        codec=codec, profile="High", pix_fmt="yuv420p",
        width=height * 16 // 9, height=height,
    )


# --- _route_encoder -------------------------------------------------------

@pytest.mark.parametrize(
    "height,nvenc,expected",
    [
        (2160, True, ENCODER_NVENC),   # 4K + GPU available → GPU
        (2160, False, ENCODER_X265),   # 4K but no GPU → CPU
        (3000, True, ENCODER_NVENC),   # above-4K → GPU
        (1080, True, ENCODER_X265),    # HD + GPU → CPU (preference)
        (1080, False, ENCODER_X265),   # HD + no GPU → CPU
        (2159, True, ENCODER_X265),    # just under the 4K threshold → CPU
    ],
)
def test_route_encoder(height: int, nvenc: bool, expected: str) -> None:
    assert _route_encoder(_profile(height=height), nvenc) == expected


# --- convert_to_mp4 encoder branch ---------------------------------------

def _capture_cmd(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Patch ffmpeg lookup + subprocess.run; capture the argv handed to run."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *_a: Any, **_k: Any):
        calls.append(cmd)

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        return _P()

    monkeypatch.setattr(convert, "_require_tool", _passthrough)
    monkeypatch.setattr(convert.subprocess, "run", fake_run)
    return calls


def test_convert_to_mp4_x265_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture_cmd(monkeypatch)
    convert_to_mp4(
        tmp_path / "in.avi", tmp_path / "out.mp4",
        encoder=ENCODER_X265, profile=_profile(codec="h264"),
    )
    cmd = calls[0]
    assert "libx265" in cmd and "hevc_nvenc" not in cmd
    assert "-crf" in cmd


def test_convert_to_mp4_nvenc_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture_cmd(monkeypatch)
    convert_to_mp4(
        tmp_path / "in.mp4", tmp_path / "out.mp4",
        encoder=ENCODER_NVENC, profile=_profile(codec="h264", height=2160),
    )
    cmd = calls[0]
    assert "hevc_nvenc" in cmd and "libx265" not in cmd
    assert "cuda" in cmd  # full GPU decode pipeline
    assert "-cq" in cmd


def test_convert_to_mp4_hevc_source_remuxes_regardless_of_encoder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An already-HEVC source is copied (`-c copy`) — no encoder runs even
    when ENCODER_NVENC is requested."""
    calls = _capture_cmd(monkeypatch)
    convert_to_mp4(
        tmp_path / "in.mp4", tmp_path / "out.mp4",
        encoder=ENCODER_NVENC, profile=_profile(codec="hevc"),
    )
    cmd = calls[0]
    assert "copy" in cmd
    assert "hevc_nvenc" not in cmd and "libx265" not in cmd


# --- geometry probe parsing ----------------------------------------------

def test_probe_geometry_parses_key_value_regardless_of_field_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ffprobe emits stream fields in the file's natural order (width/height
    *before* pix_fmt), not the requested order — key=value parsing must map
    by name, not position. (Regression: positional parsing read 720x0.)"""
    out = (
        "codec_name=h264\nprofile=High\nwidth=3840\nheight=2160\n"
        "pix_fmt=yuv420p\n"
    )

    def fake_run(*_a: Any, **_k: Any):
        class _P:
            stdout = out
            stderr = ""
            returncode = 0

        return _P()

    monkeypatch.setattr(convert, "_require_tool", _passthrough)
    monkeypatch.setattr(convert.subprocess, "run", fake_run)
    p = convert.probe_video_profile_with_geometry(tmp_path / "x.mp4")
    assert (p.codec, p.profile, p.pix_fmt, p.width, p.height) == (
        "h264", "High", "yuv420p", 3840, 2160,
    )


# --- nvenc_available ------------------------------------------------------

def _nvenc_fake_run(listed: bool, functional: bool):
    def runner(cmd: list[str], *_a: Any, **_k: Any):
        is_listing = "-encoders" in cmd

        class _P:
            stdout = ("... hevc_nvenc ..." if listed else "...") if is_listing else ""
            stderr = ""
            returncode = 0 if (is_listing or functional) else 1

        return _P()

    return runner


@pytest.mark.parametrize(
    "listed,functional,expected",
    [(True, True, True), (False, True, False), (True, False, False)],
)
def test_nvenc_available(
    monkeypatch: pytest.MonkeyPatch,
    listed: bool,
    functional: bool,
    expected: bool,
) -> None:
    nvenc_available.cache_clear()
    monkeypatch.setattr(convert, "_require_tool", _passthrough)
    monkeypatch.setattr(convert.subprocess, "run", _nvenc_fake_run(listed, functional))
    try:
        assert nvenc_available() is expected
    finally:
        nvenc_available.cache_clear()


# --- prefetcher slot routing + overflow ----------------------------------

def _prefetcher(nvenc: bool, x265: int = 1, gpu: int = 1) -> _StagingPrefetcher:
    # No lines → __init__ submits nothing; we exercise the slot logic directly.
    return _StagingPrefetcher([], x265_workers=x265, nvenc_workers=gpu, nvenc=nvenc)


def test_acquire_4k_takes_gpu_slot() -> None:
    p = _prefetcher(nvenc=True)
    assert p._acquire(ENCODER_NVENC) == ENCODER_NVENC
    p._release(ENCODER_NVENC)


def test_acquire_hd_prefers_cpu() -> None:
    p = _prefetcher(nvenc=True)
    assert p._acquire(ENCODER_X265) == ENCODER_X265
    p._release(ENCODER_X265)


def test_acquire_hd_overflows_to_gpu_when_cpu_busy() -> None:
    p = _prefetcher(nvenc=True, x265=1, gpu=1)
    first = p._acquire(ENCODER_X265)          # takes the only CPU slot
    assert first == ENCODER_X265
    overflow = p._acquire(ENCODER_X265)        # CPU full → spills to GPU
    assert overflow == ENCODER_NVENC
    p._release(first)
    p._release(overflow)


def test_acquire_hd_blocks_for_cpu_when_no_gpu() -> None:
    """Without a GPU, an HD acquire must take a CPU slot (never overflow)."""
    p = _prefetcher(nvenc=False, x265=1, gpu=1)
    held = p._acquire(ENCODER_X265)
    assert held == ENCODER_X265
    # Second acquire would block (no GPU to overflow to); confirm it can't be
    # taken without releasing first, via a short-lived thread.
    got: list[str] = []
    t = threading.Thread(target=lambda: got.append(p._acquire(ENCODER_X265)))
    t.start()
    t.join(timeout=0.3)
    assert t.is_alive()          # still blocked — no overflow happened
    p._release(held)             # free the CPU slot
    t.join(timeout=1.0)
    assert got == [ENCODER_X265]
    p._release(got[0])


# --- x265 fallback on GPU failure ----------------------------------------

def test_pooled_mp4_falls_back_to_x265_on_nvenc_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 4K clip routes to NVENC; if that raises ConvertFailed, the pool
    retries on x265 and succeeds."""
    used: list[str] = []

    def fake_convert(src: Path, dst: Path, *, encoder: str = ENCODER_X265,
                     profile: VideoProfile | None = None) -> None:
        used.append(encoder)
        if encoder == ENCODER_NVENC:
            raise ConvertFailed("nvenc can't do this pixel format")
        # x265 succeeds

    def fake_probe(_src: Path) -> VideoProfile:
        return _profile(codec="h264", height=2160)

    monkeypatch.setattr(apply, "probe_video_profile_with_geometry", fake_probe)
    monkeypatch.setattr(apply, "convert_to_mp4", fake_convert)

    p = _prefetcher(nvenc=True)
    ln = PlanLine(
        line_id="L001", action=Action.CONVERT_RENAME_TAG,
        rel_path="clip.mp4", details="", abs_path=tmp_path / "clip.mp4",
        staging_path=tmp_path / "stg.mp4", target_path=tmp_path / "t.mp4",
    )
    # register active state (normally done in _run) so _label is a no-op-safe
    p._active[ln.line_id] = _ActiveEncode(0.0)
    p._encode_mp4(ln)
    assert used == [ENCODER_NVENC, ENCODER_X265]
