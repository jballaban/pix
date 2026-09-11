"""`pix2 upload` — staging to master, and the one destructive step (spec §9)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pix.markers import EXPORT_TMP_SUFFIX
from pix.nas import folder_import as fi
from pix.nas import ledger
from pix.nas import upload as up


@pytest.fixture(autouse=True)
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect staging and the share; never the real `G:` or `\\\\nas`."""
    staging = tmp_path / "staging"
    share = tmp_path / "nas"
    master = share / "master"
    master.mkdir(parents=True)

    monkeypatch.setattr(fi, "IMPORT_ROOT", staging)
    monkeypatch.setattr(up, "IMPORT_ROOT", staging)
    monkeypatch.setattr(up, "MASTER_DIR", master)
    monkeypatch.setattr(ledger, "MASTER_SHARE", share)
    monkeypatch.setattr(ledger, "MASTER_DIR", master)
    return {"staging": staging, "master": master, "tmp": tmp_path}


@pytest.fixture
def staged(roots: dict[str, Path]) -> Path:
    """A source imported into staging, ready to upload."""
    src = roots["tmp"] / "src"
    (src / "a").mkdir(parents=True)
    (src / "a" / "one.jpg").write_bytes(b"one")
    (src / "two.mp4").write_bytes(b"two!!")
    fi.run_folder_import(src, "legacy")
    return src


def tag_of(src: Path) -> str:
    from pix.nas.staging import source_tag
    return source_tag(src)


def _entries(master_folder: Path) -> list[dict[str, object]]:
    return list(ledger.iter_entries(master_folder / ".import.jsonl"))


def test_flatten_carries_provenance() -> None:
    """A file pulled out of master still says where it came from (spec §3)."""
    assert up.flatten("2015/a/one.jpg") == "2015_a_one.jpg"


def test_uploads_and_flattens(staged: Path, roots: dict[str, Path]) -> None:
    [s] = up.run_upload()

    assert s.copied == 2
    assert s.failed == []
    t = tag_of(staged)
    assert (s.master_folder / f"{t}_a_one.jpg").read_bytes() == b"one"
    assert (s.master_folder / f"{t}_two.mp4").read_bytes() == b"two!!"
    assert s.master_folder.name.startswith("legacy_")


def test_staging_is_cleared_only_after_verification(staged: Path,
                                                    roots: dict[str, Path]) -> None:
    """The one destructive step, gated on every file being present at size."""
    [s] = up.run_upload()

    assert s.verified is True
    assert s.staging_cleared is True
    assert not (roots["staging"] / "legacy").exists()


def test_staging_survives_a_failed_verification(staged: Path, roots: dict[str, Path],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """If master does not hold what we sent, staging must not be destroyed."""
    monkeypatch.setattr(up, "_verify", lambda items, target: False)

    [s] = up.run_upload()

    assert s.verified is False
    assert s.staging_cleared is False
    assert (roots["staging"] / "legacy").is_dir()


def test_ledger_has_a_header_then_entries(staged: Path) -> None:
    [s] = up.run_upload()
    raw = (s.master_folder / ".import.jsonl").read_text(encoding="utf-8").splitlines()

    header = json.loads(raw[0])
    assert header["name"] == "legacy"
    assert header["source"] == "folder"
    assert header["source_roots"]          # what makes the registry derivable

    entries = [json.loads(line) for line in raw[1:]]
    t = tag_of(staged)
    assert {e["file"] for e in entries} == {f"{t}_a_one.jpg", f"{t}_two.mp4"}
    assert all(e["root"] for e in entries)  # per-entry, for mixed-source batches


def test_culled_files_are_recorded_not_copied(staged: Path,
                                              roots: dict[str, Path]) -> None:
    """Deleting staged media before upload is a durable 'never import again'."""
    (roots["staging"] / "legacy" / tag_of(staged) / "a" / "one.jpg").unlink()

    [s] = up.run_upload()

    assert s.culled == 1
    assert s.copied == 1
    assert not (s.master_folder / f"{tag_of(staged)}_a_one.jpg").exists()
    culled = [e for e in _entries(s.master_folder) if e.get("outcome") == "culled"]
    assert len(culled) == 1


def test_culled_file_is_not_reimported(staged: Path, roots: dict[str, Path]) -> None:
    """The whole point of recording a cull: it survives staging being cleared."""
    (roots["staging"] / "legacy" / tag_of(staged) / "a" / "one.jpg").unlink()
    up.run_upload()

    again = fi.run_folder_import(staged, "legacy")

    assert again.skipped == 2      # the culled one and the uploaded one
    assert again.landed == 0


def test_uploaded_files_are_not_restaged(staged: Path) -> None:
    """Committed half doing its job after staging is gone."""
    up.run_upload()
    again = fi.run_folder_import(staged, "legacy")

    assert again.landed == 0
    assert again.skipped == 2


def test_resume_continues_into_the_same_master_folder(staged: Path,
                                                      roots: dict[str, Path]) -> None:
    """A folder name embeds a timestamp, so a resume must not start a second one."""
    staging = roots["staging"] / "legacy"
    target = up._resolve_target(staging, "legacy")
    target.mkdir(parents=True)

    [s] = up.run_upload()

    assert s.master_folder == target
    assert len(list(roots["master"].iterdir())) == 1


def test_resume_adds_no_duplicate_ledger_lines(staged: Path,
                                               roots: dict[str, Path]) -> None:
    """Re-running over a partially uploaded folder must not double-record."""
    staging = roots["staging"] / "legacy"
    target = up._resolve_target(staging, "legacy")
    up._upload_one(staging, echo=lambda _: None)

    # Re-stage the same source and upload again into the same target.
    fi.run_folder_import(staged, "legacy_again")
    (roots["staging"] / "legacy_again" / ".upload-target").write_text(
        target.name, encoding="utf-8")
    up._upload_one(roots["staging"] / "legacy_again", echo=lambda _: None)

    files = [e["file"] for e in _entries(target)]
    assert len(files) == len(set(files))


def test_partial_copies_use_a_marker_temp(staged: Path, roots: dict[str, Path],
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """A killed copy must leave something no name-and-size check would accept."""
    seen: list[str] = []
    real = up.shutil.copy2

    def spy(src: object, dst: object, *a: object, **k: object) -> object:
        seen.append(Path(str(dst)).name)
        return real(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(up.shutil, "copy2", spy)
    up.run_upload()

    assert seen and all(n.endswith(EXPORT_TMP_SUFFIX) for n in seen)


def test_nothing_staged_is_not_an_error(roots: dict[str, Path]) -> None:
    assert up.run_upload() == []


def test_flatten_collisions_are_disambiguated() -> None:
    """`a/b_c.jpg` and `a_b/c.jpg` both flatten to one name."""
    used: dict[str, str] = {}
    first = up._unique(up.flatten("a/b_c.jpg"), "a/b_c.jpg", used)
    second = up._unique(up.flatten("a_b/c.jpg"), "a_b/c.jpg", used)

    assert first == "a_b_c.jpg"
    assert second != first
    assert second.endswith(".jpg")
