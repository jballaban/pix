"""`pix upload` — staging to master, and the one destructive step (spec §9)."""

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


def _corrupting(real: object):
    """Wrap the copy so the landed bytes differ from what was hashed."""
    def wrapped(src: Path, dst: Path) -> str:
        digest = real(src, dst)  # type: ignore[operator]
        data = bytearray(dst.read_bytes())
        data[0] ^= 0x01          # one bit, same length
        dst.write_bytes(bytes(data))
        return digest
    return wrapped


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
    monkeypatch.setattr(up, "_copy_hashing", _corrupting(up._copy_hashing))

    [s] = up.run_upload()

    assert s.verified is False
    assert s.staging_cleared is False
    assert s.failed and "read-back" in s.failed[0]
    assert (roots["staging"] / "legacy").is_dir()


def test_a_corrupt_copy_is_removed_from_master(staged: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """A known-bad file must never be left in the archive."""
    monkeypatch.setattr(up, "_copy_hashing", _corrupting(up._copy_hashing))

    [s] = up.run_upload()

    present = [p for p in s.master_folder.iterdir() if p.suffix != ".jsonl"]
    assert present == []


def test_a_corrupt_copy_is_not_recorded_in_the_ledger(
    staged: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ledger is the archive's record; it must only hold verified files."""
    monkeypatch.setattr(up, "_copy_hashing", _corrupting(up._copy_hashing))

    [s] = up.run_upload()

    assert _entries(s.master_folder) == []


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
    real = up._copy_hashing

    def spy(src: Path, dst: Path) -> str:
        seen.append(dst.name)
        return real(src, dst)

    monkeypatch.setattr(up, "_copy_hashing", spy)
    up.run_upload()

    assert seen and all(n.endswith(EXPORT_TMP_SUFFIX) for n in seen)


def test_ledger_records_a_hash_for_every_file(staged: Path) -> None:
    """Size catches truncation; only a hash catches a flipped bit."""
    [s] = up.run_upload()
    entries = [e for e in _entries(s.master_folder) if e.get("outcome") == "kept"]

    assert entries
    for e in entries:
        assert isinstance(e.get("blake3"), str) and len(str(e["blake3"])) == 64


def test_hash_matches_the_source_bytes(staged: Path) -> None:
    [s] = up.run_upload()
    entry = next(e for e in _entries(s.master_folder)
                 if str(e.get("file", "")).endswith("_a_one.jpg"))

    assert entry["blake3"] == up._digest(staged / "a" / "one.jpg")


def test_orphan_temps_are_swept(staged: Path, roots: dict[str, Path]) -> None:
    """A killed run leaves marker temps in master; nothing else removes them."""
    staging = roots["staging"] / "legacy"
    target = up._resolve_target(staging, "legacy")
    target.mkdir(parents=True)
    orphan = target / ("stale.jpg" + EXPORT_TMP_SUFFIX)
    orphan.write_bytes(b"partial")

    up.run_upload()

    assert not orphan.exists()


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


# --- device imports, and never clearing what was not uploaded -----------------
#
# A GoPro import was deleted whole: its records were in the device importer's
# shape, upload read them as nothing, counted zero files, found zero failures
# and cleared the folder.

import yaml  # noqa: E402


def _device_file(staging: Path, rel: str, data: bytes, *, puid: str,
                 serial: str = "C3441324567890") -> Path:
    """Land one file the way `importer` does: the media at its device path,
    its YAML record in a `.manifest/` beside it."""
    media = staging / rel
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(data)
    record = media.parent / ".manifest" / (media.name + ".importinfo")
    record.parent.mkdir(exist_ok=True)
    record.write_text(yaml.safe_dump({
        "serial": serial, "friendly": "HERO12 Black", "device_name": "gopro",
        "imported_at": "20260928", "puid": puid,
        "device_path": "/" + rel, "original_filename": media.name,
        "size": len(data), "capture_date": "2026-09-27 14:00:00",
    }, sort_keys=False), encoding="utf-8")
    return media


def test_a_device_import_uploads(roots: dict[str, Path]) -> None:
    staging = roots["staging"] / "gopro"
    _device_file(staging, "DCIM/100GOPRO/GX010001.MP4", b"clip one", puid="o1")
    _device_file(staging, "DCIM/100GOPRO/GX010002.MP4", b"clip two!", puid="o2")

    [s] = up.run_upload()

    assert (s.copied, s.failed, s.unaccounted) == (2, [], [])
    assert (s.master_folder / "DCIM_100GOPRO_GX010001.MP4").read_bytes() == \
        b"clip one"
    assert s.staging_cleared and not staging.exists()


def test_a_device_upload_is_what_the_next_import_skips(
    roots: dict[str, Path]
) -> None:
    """The committed half of the skip check: the header names the serial and
    every line carries the PUID."""
    staging = roots["staging"] / "gopro"
    _device_file(staging, "DCIM/100GOPRO/GX010001.MP4", b"clip one", puid="o1")
    [s] = up.run_upload()

    header = ledger.read_header(s.master_folder / ".import.jsonl")
    assert header is not None
    assert (header.source, header.serial, header.name) == (
        "device", "C3441324567890", "gopro")
    [entry] = _entries(s.master_folder)
    assert entry["puid"] == "o1" and entry["outcome"] == "kept"
    assert entry["device_path"] == "/DCIM/100GOPRO/GX010001.MP4"
    assert ledger.committed_import_ids("C3441324567890") == {
        "C3441324567890:o1"}


def test_a_culled_device_file_is_recorded_and_never_fetched_again(
    roots: dict[str, Path]
) -> None:
    staging = roots["staging"] / "gopro"
    _device_file(staging, "DCIM/100GOPRO/GX010001.MP4", b"keep", puid="o1")
    _device_file(staging, "DCIM/100GOPRO/GX010002.MP4", b"cull", puid="o2")
    (staging / "DCIM/100GOPRO/GX010002.MP4").unlink()

    [s] = up.run_upload()

    assert (s.copied, s.culled) == (1, 1)
    assert ledger.committed_import_ids("C3441324567890") == {
        "C3441324567890:o1", "C3441324567890:o2"}


def test_staging_is_never_cleared_while_it_holds_files_nobody_uploaded(
    roots: dict[str, Path]
) -> None:
    """The deletion itself: nothing failed, because nothing was tried."""
    staging = roots["staging"] / "gopro"
    staging.mkdir(parents=True)
    (staging / "GX010001.MP4").write_bytes(b"no record at all")

    [s] = up.run_upload()

    assert s.copied == 0
    assert s.unaccounted == ["GX010001.MP4: no import record"]
    assert not s.staging_cleared
    assert (staging / "GX010001.MP4").read_bytes() == b"no record at all"


def test_a_record_nobody_can_read_keeps_its_folder(
    roots: dict[str, Path]
) -> None:
    staging = roots["staging"] / "odd"
    media = staging / "x.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"x")
    record = staging / ".manifest" / "x.mp4.importinfo"
    record.parent.mkdir()
    record.write_text("something: else\n", encoding="utf-8")

    [s] = up.run_upload()

    assert not s.staging_cleared and media.is_file()
    assert any("not understood" in u for u in s.unaccounted)
    assert any("no import record" in u for u in s.unaccounted)


def test_a_partial_download_does_not_hold_staging_back(
    roots: dict[str, Path]
) -> None:
    """What an interrupted import leaves is not media anybody is waiting for."""
    from pix.markers import IMPORT_TMP_SUFFIX

    staging = roots["staging"] / "gopro"
    _device_file(staging, "DCIM/100GOPRO/GX010001.MP4", b"clip", puid="o1")
    (staging / "DCIM/100GOPRO" / ("GX010003.MP4" + IMPORT_TMP_SUFFIX)
     ).write_bytes(b"part")

    [s] = up.run_upload()

    assert s.unaccounted == [] and s.staging_cleared


def test_two_devices_in_one_folder_are_refused(roots: dict[str, Path]) -> None:
    """One header describes one source; mixing them would lose track of a
    phone's files for its next import."""
    staging = roots["staging"] / "mixed"
    _device_file(staging, "A/one.jpg", b"1", puid="o1", serial="AAA")
    _device_file(staging, "B/two.jpg", b"2", puid="o2", serial="BBB")

    [s] = up.run_upload()

    assert s.failed and not s.staging_cleared
    assert (staging / "A/one.jpg").is_file()
