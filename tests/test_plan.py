from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pix.config import Config
from pix.metadata import FileMetadata
from pix.plan import (
    PIX_CONTENT_HASH,
    PIX_DATE_AUTO,
    PIX_DATE_OVERRIDE,
    PIX_ORIGINAL_PATH,
    Action,
    effective_date,
    generate_plan,
    lookup_policy,
)


def _config(**ext: str) -> Config:
    from pix.config import ExtensionAction
    from typing import cast as _cast

    return Config(
        extensions={k: _cast(ExtensionAction, v) for k, v in ext.items()}
    )


def _meta(path: str, **fields: object) -> FileMetadata:
    return FileMetadata(path=Path(path), raw={"SourceFile": path, **fields})


# --- lookup_policy ---


def test_lookup_policy_by_extension() -> None:
    cfg = _config(heic="convert_to_jpg", jpg="keep")
    assert lookup_policy("IMG_001.HEIC", cfg.extensions) == "convert_to_jpg"
    assert lookup_policy("photo.jpg", cfg.extensions) == "keep"


def test_lookup_policy_by_full_filename() -> None:
    # `_config(**kwargs)` can't express dots in keys; build manually.
    cfg = Config(extensions={"thumbs.db": "delete", "ds_store": "delete"})
    # `thumbs.db` matches the full filename key `thumbs.db` after lower.
    assert lookup_policy("Thumbs.db", cfg.extensions) == "delete"
    # `.DS_Store` matches `ds_store` after stripping leading dot + lower.
    assert lookup_policy(".DS_Store", cfg.extensions) == "delete"


def test_lookup_policy_unknown_returns_none() -> None:
    cfg = _config(jpg="keep")
    assert lookup_policy("weird.xyz", cfg.extensions) is None


# --- effective_date ---


def test_effective_date_no_override_uses_auto() -> None:
    meta = _meta(
        "F:/src/photo.jpg",
        **{PIX_DATE_AUTO: "2023-08-15-14:32:05"},
    )
    assert effective_date(meta) == datetime(2023, 8, 15, 14, 32, 5)


def test_effective_date_override_pins_year() -> None:
    meta = _meta(
        "F:/src/photo.jpg",
        **{
            PIX_DATE_AUTO: "2023-08-15-14:32:05",
            PIX_DATE_OVERRIDE: "2022-*-*-*:*:*",
        },
    )
    assert effective_date(meta) == datetime(2022, 8, 15, 14, 32, 5)


def test_effective_date_override_pins_multiple_fields() -> None:
    meta = _meta(
        "F:/src/photo.jpg",
        **{
            PIX_DATE_AUTO: "2023-08-15-14:32:05",
            PIX_DATE_OVERRIDE: "2020-*-01-*:*:*",
        },
    )
    assert effective_date(meta) == datetime(2020, 8, 1, 14, 32, 5)


def test_effective_date_falls_back_to_derivation_when_no_pix_auto() -> None:
    # No pix:DateAuto stored — derive from EXIF.
    meta = _meta(
        "F:/src/photo.jpg",
        **{"EXIF:DateTimeOriginal": "2023:08:15 14:32:05"},
    )
    assert effective_date(meta) == datetime(2023, 8, 15, 14, 32, 5)


# --- generate_plan ---


def test_plan_first_migrate_convert(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    heic = src / "IMG_001.HEIC"
    heic.write_bytes(b"")

    cfg = _config(heic="convert_to_jpg")
    cache = {
        heic.resolve(): _meta(
            str(heic),
            **{"EXIF:DateTimeOriginal": "2023:08:15 14:32:05"},
        )
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
    )

    assert len(plan.lines) == 1
    line = plan.lines[0]
    assert line.action == Action.CONVERT_RENAME_TAG
    assert line.is_first_migrate is True
    assert "→2023-08-15_143205.jpg" in line.details
    assert "original_path init" in line.details
    assert "content_hash compute" in line.details
    assert "date_auto null→2023-08-15-14:32:05" in line.details


def test_plan_delete_for_junk(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    junk = src / "Thumbs.db"
    junk.write_bytes(b"")

    cfg = Config(extensions={"thumbs.db": "delete"})
    cache = {junk.resolve(): _meta(str(junk))}

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
    )

    assert len(plan.lines) == 1
    assert plan.lines[0].action == Action.DELETE
    assert "extension policy: delete" in plan.lines[0].details


def test_plan_already_canonical_keeps_file_with_no_line(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    f = src / "2023-08-15_143205.jpg"
    f.write_bytes(b"")

    cfg = _config(jpg="keep")
    cache = {
        f.resolve(): _meta(
            str(f),
            **{
                PIX_DATE_AUTO: "2023-08-15-14:32:05",
                PIX_ORIGINAL_PATH: "F:/old/source.jpg",
                PIX_CONTENT_HASH: "deadbeef",
            },
        )
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
    )
    assert plan.lines == []


def test_plan_pure_rename_for_non_canonical_name(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    f = src / "DSC_0042.JPG"
    f.write_bytes(b"")

    cfg = _config(jpg="keep")
    cache = {
        f.resolve(): _meta(
            str(f),
            **{
                PIX_DATE_AUTO: "2023-08-15-14:36:12",
                PIX_ORIGINAL_PATH: "F:/old/source.jpg",
                PIX_CONTENT_HASH: "deadbeef",
            },
        )
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
    )

    assert len(plan.lines) == 1
    line = plan.lines[0]
    assert line.action == Action.RENAME
    assert line.is_first_migrate is False
    assert "→2023-08-15_143612.jpg" in line.details


def test_plan_rename_plus_tag_for_first_migrate_canonical_format(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    f = src / "DSC_0042.JPG"
    f.write_bytes(b"")

    cfg = _config(jpg="keep")
    cache = {
        f.resolve(): _meta(
            str(f),
            **{"EXIF:DateTimeOriginal": "2023:08:15 14:36:12"},
        )
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
    )

    assert len(plan.lines) == 1
    line = plan.lines[0]
    assert line.action == Action.RENAME_TAG
    assert line.is_first_migrate is True
    assert "→2023-08-15_143612.jpg" in line.details
    assert "original_path init" in line.details
    assert "content_hash compute" in line.details


def test_plan_tag_only_for_missing_hash_on_already_migrated(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    f = src / "2021-12-25_090015.jpg"
    f.write_bytes(b"")

    cfg = _config(jpg="keep")
    cache = {
        f.resolve(): _meta(
            str(f),
            **{
                PIX_DATE_AUTO: "2021-12-25-09:00:15",
                PIX_ORIGINAL_PATH: "F:/old/source.jpg",
                # PIX_CONTENT_HASH intentionally missing.
            },
        )
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
    )

    assert len(plan.lines) == 1
    line = plan.lines[0]
    assert line.action == Action.TAG
    assert "content_hash compute" in line.details
    assert "→" not in line.details  # no rename


def test_plan_line_ids_are_sequential(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    files = ["a.jpg", "b.jpg", "c.jpg"]
    cache: dict[Path, FileMetadata] = {}
    for name in files:
        p = src / name
        p.write_bytes(b"")
        cache[p.resolve()] = _meta(
            str(p), **{"EXIF:DateTimeOriginal": "2023:08:15 14:36:12"}
        )

    cfg = _config(jpg="keep")
    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
    )
    assert [ln.line_id for ln in plan.lines] == ["L001", "L002", "L003"]


def test_plan_to_text_includes_header_and_summary(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    f = src / "IMG_001.HEIC"
    f.write_bytes(b"")

    cache = {
        f.resolve(): _meta(
            str(f),
            **{"EXIF:DateTimeOriginal": "2023:08:15 14:32:05"},
        )
    }
    cfg = _config(heic="convert_to_jpg")
    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="2026-05-18_17-00-00",
        now=datetime(2026, 5, 18, 17, 0, 0),
    )

    text = plan.to_text()
    assert "# Migration plan:" in text
    assert "# Run ID: 2026-05-18_17-00-00" in text
    assert "first time will have their source path stored" in text
    assert "L001 | CONVERT+RENAME+TAG" in text
    assert "# Summary:" in text
    assert "1 CONVERT" in text
