"""`pix2 import folder` — staging, skip semantics, and resume (spec/nas-app.md §9)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pix.ingest import MANIFEST_DIRNAME
from pix.markers import IMPORT_TMP_SUFFIX
from pix.nas import folder_import as fi
from pix.nas import staging as st


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """A small source tree: nested media plus companions that must not land."""
    src = tmp_path / "src"
    (src / "2015" / "a").mkdir(parents=True)
    (src / "2015" / "a" / "one.jpg").write_bytes(b"one")
    (src / "2015" / "a" / "two.mp4").write_bytes(b"two!!")
    (src / "2015" / "b.heic").write_bytes(b"bee")
    (src / "2015" / "IMG_0001.aae").write_bytes(b"edit")   # companion
    (src / "2015" / ".nomedia").write_bytes(b"")           # bare dotfile
    return src


@pytest.fixture(autouse=True)
def staging_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point IMPORT_ROOT at a temp dir — never the real F:\\pix."""
    root = tmp_path / "staging"
    monkeypatch.setattr(fi, "IMPORT_ROOT", root)
    return root


def test_lands_media_and_ignores_companions(source: Path, staging_root: Path) -> None:
    s = fi.run_folder_import(source, "legacy_2015")

    assert s.landed == 3
    assert s.ignored == 2          # .aae and .nomedia
    assert s.failed == []

    staged = staging_root / "legacy_2015"
    assert (staged / "2015" / "a" / "one.jpg").exists()
    assert (staged / "2015" / "b.heic").exists()
    assert not (staged / "2015" / "IMG_0001.aae").exists()
    assert not (staged / "2015" / ".nomedia").exists()


def test_relative_structure_is_preserved(source: Path, staging_root: Path) -> None:
    """Staging mirrors the source tree; flattening happens at upload, not here."""
    fi.run_folder_import(source, "legacy_2015")
    assert (staging_root / "legacy_2015" / "2015" / "a" / "two.mp4").read_bytes() == b"two!!"


def test_sidecars_live_in_manifest_not_beside_media(source: Path,
                                                    staging_root: Path) -> None:
    """The skip record must survive culling the media (spec/nas-app.md §9)."""
    fi.run_folder_import(source, "legacy_2015")
    media = staging_root / "legacy_2015" / "2015" / "a" / "one.jpg"
    sidecar = media.parent / MANIFEST_DIRNAME / ("one.jpg" + st.SIDECAR_EXT)

    assert sidecar.exists()
    assert not (media.parent / ("one.jpg" + st.SIDECAR_EXT)).exists()

    data = st.read_sidecar(sidecar)
    assert data is not None
    assert data["rel"] == "2015/a/one.jpg"
    assert data["size"] == 3
    assert data["name"] == "legacy_2015"


def test_rerun_skips_everything(source: Path, staging_root: Path) -> None:
    """Idempotent: a second run does no work."""
    fi.run_folder_import(source, "legacy_2015")
    again = fi.run_folder_import(source, "legacy_2015")

    assert again.landed == 0
    assert again.skipped == 3


def test_culled_media_is_not_reimported(source: Path, staging_root: Path) -> None:
    """Deleting media leaves the sidecar, which is a durable 'do not re-import'."""
    fi.run_folder_import(source, "legacy_2015")
    (staging_root / "legacy_2015" / "2015" / "a" / "one.jpg").unlink()

    again = fi.run_folder_import(source, "legacy_2015")

    assert again.skipped == 3
    assert not (staging_root / "legacy_2015" / "2015" / "a" / "one.jpg").exists()


def test_deleting_the_whole_folder_redoes_the_batch(source: Path,
                                                   staging_root: Path) -> None:
    """Removing staging entirely is the deliberate 'redo this batch' gesture."""
    fi.run_folder_import(source, "legacy_2015")
    import shutil
    shutil.rmtree(staging_root / "legacy_2015")

    again = fi.run_folder_import(source, "legacy_2015")
    assert again.landed == 3


def test_straggler_is_adopted_not_relinked(source: Path, staging_root: Path) -> None:
    """A file landed by a cancelled run (no sidecar) is verified, not redone."""
    staged = staging_root / "legacy_2015" / "2015" / "a"
    staged.mkdir(parents=True)
    (staged / "one.jpg").write_bytes(b"one")   # right size, no sidecar

    s = fi.run_folder_import(source, "legacy_2015")

    assert s.adopted == 1
    assert st.sidecar_path(staged / "one.jpg").exists()


def test_changed_source_replaces_a_stale_landing(source: Path,
                                                 staging_root: Path) -> None:
    """A size mismatch means the source changed, so the landing is replaced."""
    staged = staging_root / "legacy_2015" / "2015" / "a"
    staged.mkdir(parents=True)
    (staged / "one.jpg").write_bytes(b"stale-and-longer")

    s = fi.run_folder_import(source, "legacy_2015")

    assert s.adopted == 0
    assert (staged / "one.jpg").read_bytes() == b"one"


def test_accumulates_across_sources(source: Path, staging_root: Path,
                                    tmp_path: Path) -> None:
    """Successive imports append to the same staging folder (spec §9)."""
    other = tmp_path / "other"
    other.mkdir()
    (other / "three.jpg").write_bytes(b"three")

    fi.run_folder_import(source, "legacy_2015")
    s = fi.run_folder_import(other, "legacy_2015")

    assert s.landed == 1
    assert (staging_root / "legacy_2015" / "three.jpg").exists()
    assert (staging_root / "legacy_2015" / "2015" / "a" / "one.jpg").exists()


def test_partial_writes_are_swept(source: Path, staging_root: Path) -> None:
    """A kill mid-write leaves a marker temp, which the next run removes."""
    staged = staging_root / "legacy_2015"
    staged.mkdir(parents=True)
    (staged / ("orphan.jpg" + IMPORT_TMP_SUFFIX)).write_bytes(b"partial")

    fi.run_folder_import(source, "legacy_2015")

    assert not (staged / ("orphan.jpg" + IMPORT_TMP_SUFFIX)).exists()


def test_hardlinks_share_the_inode(source: Path, staging_root: Path) -> None:
    """Staging must not duplicate bytes — there is nowhere to put 2.5TB twice."""
    fi.run_folder_import(source, "legacy_2015")
    src = source / "2015" / "a" / "one.jpg"
    dst = staging_root / "legacy_2015" / "2015" / "a" / "one.jpg"

    if os.name == "nt":
        assert dst.stat().st_nlink > 1
    else:
        assert src.samefile(dst)


def test_missing_source_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(fi.FolderImportError):
        fi.run_folder_import(tmp_path / "nope", "legacy_2015")


def test_skip_key_is_case_and_separator_insensitive() -> None:
    """One file must not look like two because of path spelling."""
    assert st.skip_key("A/B.JPG", 10) == st.skip_key("a\\b.jpg", 10)
    assert st.skip_key("a/b.jpg", 10) != st.skip_key("a/b.jpg", 11)
