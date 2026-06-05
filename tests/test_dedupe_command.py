"""Command-level tests for `pix dedupe` console output."""

from __future__ import annotations

from pathlib import Path

import pytest

from pix import dedupe as dedupe_mod
from pix.commands.dedupe import dedupe_library
from pix.convert import VideoProfile
from pix.hash_cache import read_cached_hash, write_cached_hash
from pix.metadata_cache import PerFileCache
from pix.plan import PIX_ORIGINAL_PATH
from pix.vfp_cache import read_cached_fingerprint, write_cached_fingerprint
from pix.video_cache import read_cached_profile, write_cached_profile
from pix.video_fingerprint import VideoFingerprint


def _make_library(tmp_path: Path) -> Path:
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)  # version-less; settings file optional
    return root


def test_noop_dedupe_is_terse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No duplicates → just the no-op line, no 'Plan written' / 'Summary'
    (matches migrate/hash/organize)."""
    root = _make_library(tmp_path)
    a = (root / "a.jpg").resolve()
    b = (root / "b.jpg").resolve()
    a.write_bytes(b"aaaa")
    b.write_bytes(b"bbbb")

    # Seed caches so prereqs pass with no ExifTool / hash compute.
    cache = PerFileCache.for_library(root)
    for f, h in ((a, "h1"), (b, "h2")):  # distinct hashes ⇒ no duplicates
        cache.add(f, {PIX_ORIGINAL_PATH: f"F:/source/{f.name}"})
        st = f.stat()
        write_cached_hash(
            root, f, hash_hex=h, size=st.st_size, mtime_ns=st.st_mtime_ns
        )

    dedupe_library(path=root)
    out = capsys.readouterr().out
    assert "no duplicates found" in out.lower()
    assert "Plan written" not in out
    assert "Summary" not in out


def test_no_prompt_applies_without_prompting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`no_prompt=True` applies the dedupe plan with no `Apply?` prompt.

    If the prompt were reached, `typer.prompt` would block on stdin and
    the test would hang — completing proves the prompt was skipped. Two
    byte-identical-by-hash migrated files (no date/event/override → no
    MERGE line, so no ExifTool needed) leave one survivor."""
    root = _make_library(tmp_path)
    a = (root / "a.jpg").resolve()
    b = (root / "b.jpg").resolve()
    a.write_bytes(b"dup")
    b.write_bytes(b"dup")

    cache = PerFileCache.for_library(root)
    for f in (a, b):
        cache.add(f, {PIX_ORIGINAL_PATH: f"F:/source/{f.name}"})
        st = f.stat()
        write_cached_hash(
            root, f, hash_hex="h", size=st.st_size, mtime_ns=st.st_mtime_ns
        )

    dedupe_library(path=root, no_prompt=True)

    # Keeper is lex-smallest (a.jpg); the loser was removed from the library.
    assert a.exists()
    assert not b.exists()
    out = capsys.readouterr().out
    assert "Removed 1 duplicate(s)" in out


class _MergeBumpsKeeperExif:
    """Fake ExifToolSession: records the merge write and, like a real tag
    write, bumps the keeper file's (size, mtime) so its seeded .hash cache
    entry goes stale — exactly the condition that broke organize."""

    def __init__(self) -> None:
        self.writes: list[Path] = []

    def export_xmp_sidecar(self, file: Path, sidecar_path: Path) -> None:
        sidecar_path.write_text("<xmp/>", encoding="utf-8")

    def write_tags(self, file: Path, tags: dict[str, str]) -> None:
        # Append a byte → size (and mtime) change → (size,mtime) cache key
        # for this file no longer matches; the pre-write .hash is now stale.
        with file.open("ab") as fh:
            fh.write(b"\x00")
        self.writes.append(file)

    def close(self) -> None:
        pass


def test_dedupe_merge_keeps_keeper_hash_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A MERGE writes tags onto the keeper (bumping its mtime), which would
    invalidate its .hash cache. Since the content hash is metadata-invariant,
    dedupe must re-stamp the keeper's hash so the next `pix organize` (in
    `pix sync`) still finds a valid cached hash. Regression: organize aborted
    with "N file(s) ... lack a cached content hash" after a sync's dedupe.
    """
    root = _make_library(tmp_path)
    a = (root / "a.jpg").resolve()  # keeper (lex-smallest); no event
    b = (root / "b.jpg").resolve()  # loser; folder "Hawaii" yields an event
    a.write_bytes(b"keepkeepkeep")
    b.write_bytes(b"dupdupdup")

    cache = PerFileCache.for_library(root)
    cache.add(a, {PIX_ORIGINAL_PATH: "F:/2023/a.jpg"})
    cache.add(b, {PIX_ORIGINAL_PATH: "F:/Hawaii/b.jpg"})  # event → keeper MERGE
    for f in (a, b):  # same hash ⇒ duplicates
        st = f.stat()
        write_cached_hash(
            root, f, hash_hex="dedupehash",
            size=st.st_size, mtime_ns=st.st_mtime_ns,
        )

    fake = _MergeBumpsKeeperExif()
    monkeypatch.setattr(dedupe_mod, "ExifToolSession", lambda: fake)

    dedupe_library(path=root, no_prompt=True)

    # The merge wrote to the keeper (so its hash key really did go stale)...
    assert fake.writes == [a]
    assert a.exists() and not b.exists()
    # ...yet the keeper's content hash is still cached and valid: re-stamped
    # to the new (size, mtime), value preserved (metadata-invariant).
    assert read_cached_hash(root, a) == "dedupehash"


def test_dedupe_merge_keeps_keeper_video_and_vfp_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A MERGE bumps the keeper's (size, mtime), invalidating its cache keys —
    but the ffprobe profile and perceptual fingerprint are metadata-invariant,
    so dedupe must re-stamp them too. Regression: the keeper's caches were
    wiped by a blanket remove_all and only .hash was restored, so every sync
    re-probed and (expensively) re-fingerprinted merged video keepers.
    """
    root = _make_library(tmp_path)
    a = (root / "a.mp4").resolve()  # keeper (lex-smallest)
    b = (root / "b.mp4").resolve()  # loser; folder "Hawaii" yields an event
    a.write_bytes(b"keepkeepkeep")
    b.write_bytes(b"dupdupdup")

    cache = PerFileCache.for_library(root)
    cache.add(a, {PIX_ORIGINAL_PATH: "F:/2023/a.mp4"})
    cache.add(b, {PIX_ORIGINAL_PATH: "F:/Hawaii/b.mp4"})  # event → keeper MERGE
    profile = VideoProfile(codec="hevc", profile="Main", pix_fmt="yuv420p")
    fingerprint = VideoFingerprint(
        frames=(1, 2, 3, 4, 5, 6), width=1920, height=1080, duration=12.5
    )
    for f in (a, b):  # same hash ⇒ duplicates
        st = f.stat()
        write_cached_hash(
            root, f, hash_hex="dedupehash", size=st.st_size, mtime_ns=st.st_mtime_ns
        )
        write_cached_profile(
            root, f, profile=profile, size=st.st_size, mtime_ns=st.st_mtime_ns
        )
        write_cached_fingerprint(
            root, f, fingerprint=fingerprint,
            size=st.st_size, mtime_ns=st.st_mtime_ns,
        )

    fake = _MergeBumpsKeeperExif()
    monkeypatch.setattr(dedupe_mod, "ExifToolSession", lambda: fake)

    dedupe_library(path=root, no_prompt=True)

    assert fake.writes == [a]  # the merge really bumped the keeper
    # Profile + fingerprint re-stamped to the new (size, mtime), values intact.
    survived_profile = read_cached_profile(root, a)
    assert survived_profile is not None and survived_profile.codec == "hevc"
    survived_fp = read_cached_fingerprint(root, a)
    assert survived_fp is not None and survived_fp.frames == (1, 2, 3, 4, 5, 6)
