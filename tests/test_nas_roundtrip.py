"""Files that come back (spec/nas-app.md §15, *Round trips*).

Two checks, as early as each import allows: a device import asks of each file
the moment it is down — by stamp, then by content hash — and lets a match go
before it takes a place in staging; a folder import checks the stamp and
leaves the hash to upload. Either way the record stays, so it is never asked
about again.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from pix.nas import folder_import as fi
from pix.nas import identity
from pix.nas import index as ix
from pix.nas import ledger
from pix.nas import roundtrip
from pix.nas import upload as up
from pix.nas.decisions import Decision


def _need(tool: str) -> None:
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} is not installed")


def _jpeg(path: Path, colour: str = "red") -> Path:
    _need("ffmpeg")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c={colour}:s=32x24", "-frames:v", "1", str(path)],
                   check=True, timeout=60)
    return path


def _stamp(path: Path, **tags: str) -> Path:
    _need("exiftool")
    from pix import exiftool_config_path

    subprocess.run(["exiftool", "-config", str(exiftool_config_path()), "-q",
                    "-overwrite_original",
                    *[f"-XMP-pix:{k}={v}" for k, v in tags.items()], str(path)],
                   check=True, timeout=60)
    return path


# --- the stamp -----------------------------------------------------------------

def test_a_stamped_still_says_where_it_came_from(tmp_path: Path) -> None:
    shot = _stamp(_jpeg(tmp_path / "a.jpg"), SourceFile="f/b.mp4", ClipId="k3fa")
    assert roundtrip.returned(shot) == "f/b.mp4"


def test_a_stamped_mp4_is_known(tmp_path: Path) -> None:
    _need("ffmpeg")
    clip = tmp_path / "a.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=32x24:rate=5", "-t", "1", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart+use_metadata_tags",
                    *roundtrip.stamp_args("f/b.mp4", clip_id="k3fa"),
                    str(clip)], check=True, timeout=60)
    assert roundtrip.returned(clip) == "pix"


def test_the_old_librarys_own_tags_are_not_a_stamp(tmp_path: Path) -> None:
    """The seeded library carries pix tags the old pipeline wrote into every
    file; those are originals, and seeding them is the point."""
    shot = _stamp(_jpeg(tmp_path / "a.jpg"), EventAuto="Italy",
                  OriginalPath="G:/pix/2015/a.jpg")
    assert roundtrip.returned(shot) is None
    assert roundtrip.returned(_jpeg(tmp_path / "b.jpg", "blue")) is None


# --- a folder import: stamp at import, hash at upload ----------------------------

@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    staging, share = tmp_path / "staging", tmp_path / "nas"
    master = share / "master"
    master.mkdir(parents=True)
    for module in (fi, up):
        monkeypatch.setattr(module, "IMPORT_ROOT", staging)
    monkeypatch.setattr(up, "MASTER_DIR", master)
    monkeypatch.setattr(ledger, "MASTER_SHARE", share)
    monkeypatch.setattr(ledger, "MASTER_DIR", master)
    db = share / "index.db"
    monkeypatch.setattr(up, "INDEX_DB", db)
    return {"staging": staging, "share": share, "master": master, "db": db,
            "tmp": tmp_path}


def _index_holding(roots: dict[str, Path], name: str, digest: str) -> None:
    """An index with one master in it, whose coded image hashes to `digest`."""
    meta = roots["share"] / "meta" / "init_2026"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / f"{name}.json").write_text(json.dumps({
        "file": name, "folder": "init_2026", "size": 1, "mtime_ns": 1,
        "content_hash": digest, "exif": {}}), encoding="utf-8")
    ix.build(roots["db"], meta_dir=roots["share"] / "meta",
             master_dir=roots["master"])


def _ledger(master_folder: Path) -> list[dict[str, Any]]:
    return list(ledger.iter_entries(master_folder / ".import.jsonl"))


def test_a_folder_import_lets_a_stamped_file_go(
    roots: dict[str, Path]
) -> None:
    src = roots["tmp"] / "phone"
    _stamp(_jpeg(src / "IMG_1.jpg"), SourceFile="f/b.mp4")
    _jpeg(src / "IMG_2.jpg", "blue")

    s = fi.run_folder_import(src, "phone")
    assert (s.returned, s.landed) == (1, 1)

    [u] = up.run_upload()
    assert (u.copied, u.returned) == (1, 1) and u.staging_cleared
    outcomes = {e["rel"].rsplit("/", 1)[-1]: e for e in _ledger(u.master_folder)}
    assert outcomes["IMG_1.jpg"]["outcome"] == "returned"
    assert outcomes["IMG_1.jpg"]["matches"] == "f/b.mp4"
    assert outcomes["IMG_2.jpg"]["outcome"] == "kept"


def test_upload_knows_a_copy_of_a_master_by_its_coded_image(
    roots: dict[str, Path]
) -> None:
    """No stamp — the original pix handed out is untouched — but the same
    coded image, re-tagged and renamed on the way back."""
    src = roots["tmp"] / "phone"
    copy = _stamp(_jpeg(src / "renamed.jpg"), EventAuto="retagged")
    digest = identity.content_hash(copy)
    assert digest is not None
    _index_holding(roots, "IMG_4471.jpg", digest)

    fi.run_folder_import(src, "phone")
    [u] = up.run_upload()
    assert (u.copied, u.returned) == (0, 1) and u.staging_cleared
    [entry] = _ledger(u.master_folder)
    assert entry["outcome"] == "returned"
    assert entry["matches"] == "init_2026/IMG_4471.jpg"


def test_the_record_keeps_a_device_from_offering_it_again(
    roots: dict[str, Path]
) -> None:
    """A device's returned file is uploaded as a ledger line carrying its
    PUID, which is what the next import from that device skips on."""
    staging = roots["staging"] / "gopro"
    media = staging / "DCIM" / "GX1.MP4"
    record = media.parent / ".manifest" / (media.name + ".importinfo")
    record.parent.mkdir(parents=True)
    record.write_text(yaml.safe_dump({
        "serial": "SER", "device_name": "gopro", "puid": "p1",
        "device_path": "/DCIM/GX1.MP4", "original_filename": "GX1.MP4",
        "size": 5, "returned": "f/b.mp4"}), encoding="utf-8")

    [u] = up.run_upload()
    assert u.returned == 1 and u.staging_cleared
    assert ledger.committed_import_ids("SER") == {"SER:p1"}


def test_the_check_a_device_import_is_given(roots: dict[str, Path]) -> None:
    """Stamp first, then hash, against an index read once."""
    plain = _jpeg(roots["tmp"] / "a.jpg")
    digest = identity.content_hash(plain)
    assert digest is not None
    _index_holding(roots, "IMG_1.jpg", digest)
    check = roundtrip.checker(roots["db"])
    assert check(plain) == "init_2026/IMG_1.jpg"
    assert check(_stamp(_jpeg(roots["tmp"] / "b.jpg", "blue"),
                        SourceFile="f/b.mp4")) == "f/b.mp4"
    assert check(_jpeg(roots["tmp"] / "c.jpg", "green")) is None


# --- clips' own files are known by hash ----------------------------------------------

def test_a_clips_files_are_noted_for_the_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pix.nas import derive
    from pix.nas import decisions
    from pix.nas import paths

    master, render, meta = (tmp_path / t for t in ("master", "render", "meta"))
    for module in (derive, ix):
        monkeypatch.setattr(module, "RENDER_DIR", render)
    monkeypatch.setattr(derive, "META_DIR", meta)
    clip = master / "f" / "b.mp4~k3fa"
    (master / "f").mkdir(parents=True)
    decision = Decision(clip_in=1.0, clip_out=1.0)
    decisions.write(clip, decision)
    still = _jpeg(paths.still_path(clip, render, 1.0))

    assert derive._note_clip(clip, decision)  # pyright: ignore[reportPrivateUsage]
    record = json.loads(paths.meta_path(clip, meta).read_text(encoding="utf-8"))
    assert record["clip"] is True
    assert record["content_hash"] == identity.content_hash(still)
    assert not derive._note_clip(clip, decision), "nothing changed"  # pyright: ignore[reportPrivateUsage]


def test_the_index_is_opened_by_the_path_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The share is `\\nas\pix2`, and `as_posix` made that `//nas/pix2` —
    a URI authority SQLite refuses. The refusal was swallowed, so every import
    ran with no hash check; the index is opened by its path as written."""
    import sqlite3

    db = tmp_path / "index.db"
    sqlite3.connect(db).execute(
        "CREATE TABLE files (folder, name, content_hash, render_hash)").connection.commit()
    opened: list[str] = []
    real = sqlite3.connect

    def spy(target: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        opened.append(str(target))
        return real(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    assert roundtrip.known_hashes(db) == {}
    assert opened and opened[0] == f"file:{db}?mode=ro"
