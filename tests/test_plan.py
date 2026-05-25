from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pix.config import Config
from pix.metadata import FileMetadata
from pix.plan import (
    PIX_DATE_AUTO,
    PIX_DATE_OVERRIDE,
    PIX_EVENT_AUTO,
    PIX_EVENT_OVERRIDE,
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
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )

    assert len(plan.lines) == 1
    line = plan.lines[0]
    assert line.action == Action.CONVERT_RENAME_TAG
    assert line.is_first_migrate is True
    assert "→2023-08-15_143205.jpg" in line.details
    assert "original_path init" in line.details
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
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
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
                # Date-only parent → no EventAuto derivation, so the
                # test stays focused on the no-action case.
                PIX_ORIGINAL_PATH: "F:/2023-08/source.jpg",
            },
        )
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
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
                # Date-only parent → no EventAuto derivation, so the
                # test stays focused on the pure-rename case.
                PIX_ORIGINAL_PATH: "F:/2023-08/source.jpg",
            },
        )
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )

    assert len(plan.lines) == 1
    line = plan.lines[0]
    assert line.action == Action.RENAME
    assert line.is_first_migrate is False
    assert "→2023-08-15_143612.jpg" in line.details


def test_plan_m4v_renames_to_mp4_via_extension_alias(tmp_path: Path) -> None:
    """`.m4v` is Apple-branded MP4 — canonical extension is `.mp4` via the
    alias, so a `keep`-policy m4v gets renamed to .mp4 with no conversion."""
    src = tmp_path / "src"
    src.mkdir()
    f = src / "00008-1.m4v"
    f.write_bytes(b"")

    cfg = _config(m4v="keep")
    cache = {
        f.resolve(): _meta(
            str(f),
            **{
                PIX_DATE_AUTO: "2015-06-12-19:30:00",
                PIX_ORIGINAL_PATH: "F:/2015-06/source.m4v",
            },
        )
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )

    assert len(plan.lines) == 1
    line = plan.lines[0]
    # Pure RENAME (no conversion) — bytes don't change.
    assert line.action == Action.RENAME
    assert "→2015-06-12_193000.mp4" in line.details


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
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )

    assert len(plan.lines) == 1
    line = plan.lines[0]
    assert line.action == Action.RENAME_TAG
    assert line.is_first_migrate is True
    assert "→2023-08-15_143612.jpg" in line.details
    assert "original_path init" in line.details


def test_plan_drift_detection_writes_new_date_auto(tmp_path: Path) -> None:
    """If re-derived DateAuto differs from stored, a TAG action is proposed."""
    src = tmp_path / "src"
    src.mkdir()
    f = src / "2020-01-01_000000.jpg"  # canonical name matches OLD stored auto
    f.write_bytes(b"")

    cfg = _config(jpg="keep")
    cache = {
        f.resolve(): _meta(
            str(f),
            **{
                # Stored DateAuto is stale (2020); EXIF says 2023 — drift!
                PIX_DATE_AUTO: "2020-01-01-00:00:00",
                PIX_ORIGINAL_PATH: "F:/2023-08/source.jpg",
                "EXIF:DateTimeOriginal": "2023:08:15 14:32:05",
            },
        )
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )

    assert len(plan.lines) == 1
    line = plan.lines[0]
    # Drift: stored 2020 → re-derived 2023. Needs new auto + rename.
    assert line.action == Action.RENAME_TAG
    assert line.target_filename == "2023-08-15_143205.jpg"
    assert line.pix_writes.get(PIX_DATE_AUTO) == "2023-08-15-14:32:05"
    assert "date_auto 2020-01-01-00:00:00→2023-08-15-14:32:05" in line.details


def test_plan_drift_with_override_writes_date_auto_previous(
    tmp_path: Path,
) -> None:
    """Drift detected while override pins → DateAutoPrevious is written."""
    src = tmp_path / "src"
    src.mkdir()
    # File is at 2022-... canonical because override pins year=2022.
    f = src / "2022-08-15_143205.jpg"
    f.write_bytes(b"")

    cfg = _config(jpg="keep")
    cache = {
        f.resolve(): _meta(
            str(f),
            **{
                PIX_DATE_AUTO: "2023-08-15-14:32:05",  # stored
                PIX_DATE_OVERRIDE: "2022-*-*-*:*:*",  # pins year
                PIX_ORIGINAL_PATH: "F:/2023-08/source.jpg",
                # New EXIF says 2024 → re-derive will produce drift.
                "EXIF:DateTimeOriginal": "2024:08:15 14:32:05",
            },
        )
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )

    assert len(plan.lines) == 1
    line = plan.lines[0]
    # Stored auto changes from 2023 → 2024. Override pins year=2022 so
    # effective year stays 2022; filename doesn't change.
    assert line.action == Action.TAG
    assert line.target_filename is None  # no rename: effective date unchanged
    assert line.pix_writes.get(PIX_DATE_AUTO) == "2024-08-15-14:32:05"
    # Previous = stored DateAuto (the value about to be replaced).
    assert (
        line.pix_writes.get("XMP:DateAutoPrevious")
        == "2023-08-15-14:32:05"
    )
    assert "date_auto_previous→2023-08-15-14:32:05" in line.details


def test_plan_drift_without_override_does_not_write_previous(
    tmp_path: Path,
) -> None:
    """Drift without an override → no Previous (change is visible to user)."""
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
                # No DateOverride.
                PIX_ORIGINAL_PATH: "F:/2023-08/source.jpg",
                "EXIF:DateTimeOriginal": "2024:08:15 14:32:05",
            },
        )
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )

    assert len(plan.lines) == 1
    line = plan.lines[0]
    assert line.action == Action.RENAME_TAG  # new auto changes filename
    assert "XMP:DateAutoPrevious" not in line.pix_writes


def test_plan_all_wildcard_override_is_treated_as_no_override(
    tmp_path: Path,
) -> None:
    """A `*-*-*-*:*:*` override pins nothing → no Previous on drift."""
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
                PIX_DATE_OVERRIDE: "*-*-*-*:*:*",  # all wildcard
                PIX_ORIGINAL_PATH: "F:/2023-08/source.jpg",
                "EXIF:DateTimeOriginal": "2024:08:15 14:32:05",
            },
        )
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )
    line = plan.lines[0]
    assert "XMP:DateAutoPrevious" not in line.pix_writes


def test_plan_no_drift_no_action_when_stored_matches_derivation(
    tmp_path: Path,
) -> None:
    """When re-derivation produces the same value as stored, no plan line."""
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
                PIX_ORIGINAL_PATH: "F:/2023-08/source.jpg",
                # Filename pattern would derive the same value — no drift.
            },
        )
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )
    assert plan.lines == []


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
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )
    assert [ln.line_id for ln in plan.lines] == ["L001", "L002", "L003"]


def test_plan_collision_suffixes_competing_rename_targets(
    tmp_path: Path,
) -> None:
    """Two files deriving to the same canonical name get _001 on the second."""
    src = tmp_path / "src"
    src.mkdir()
    a = src / "burst-a.jpg"
    b = src / "burst-b.jpg"
    a.write_bytes(b"")
    b.write_bytes(b"")

    cfg = _config(jpg="keep")
    cache = {
        a.resolve(): _meta(
            str(a),
            **{"EXIF:DateTimeOriginal": "2023:08:15 14:32:05"},
        ),
        b.resolve(): _meta(
            str(b),
            **{"EXIF:DateTimeOriginal": "2023:08:15 14:32:05"},
        ),
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )

    assert len(plan.lines) == 2
    targets = sorted(ln.target_filename for ln in plan.lines if ln.target_filename)
    assert targets == ["2023-08-15_143205.jpg", "2023-08-15_143205_001.jpg"]
    # Lex-first source ("burst-a.jpg") gets the bare slot.
    burst_a_line = next(ln for ln in plan.lines if ln.abs_path == a.resolve())
    assert burst_a_line.target_filename == "2023-08-15_143205.jpg"


def test_plan_collision_already_at_canonical_name_keeps_bare_slot(
    tmp_path: Path,
) -> None:
    """A file already at the canonical bare name wins it; competitors get suffixes."""
    src = tmp_path / "src"
    src.mkdir()
    occupant = src / "2023-08-15_143205.jpg"
    challenger = src / "IMG_20230815_143205.jpg"
    occupant.write_bytes(b"")
    challenger.write_bytes(b"")

    cfg = _config(jpg="keep")
    cache = {
        occupant.resolve(): _meta(
            str(occupant),
            **{"EXIF:DateTimeOriginal": "2023:08:15 14:32:05"},
        ),
        challenger.resolve(): _meta(
            str(challenger),
            **{"EXIF:DateTimeOriginal": "2023:08:15 14:32:05"},
        ),
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )

    challenger_line = next(
        ln for ln in plan.lines if ln.abs_path == challenger.resolve()
    )
    # Occupant was already canonical → keeps bare slot. Challenger gets _001.
    assert challenger_line.target_filename == "2023-08-15_143205_001.jpg"
    assert f"→2023-08-15_143205_001.jpg" in challenger_line.details


def test_plan_collision_three_way(tmp_path: Path) -> None:
    """Three files colliding produce bare, _001, _002."""
    src = tmp_path / "src"
    src.mkdir()
    files: list[Path] = []
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        p = src / name
        p.write_bytes(b"")
        files.append(p)

    cfg = _config(jpg="keep")
    cache = {
        f.resolve(): _meta(
            str(f), **{"EXIF:DateTimeOriginal": "2023:08:15 14:32:05"}
        )
        for f in files
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )

    targets = sorted(ln.target_filename for ln in plan.lines if ln.target_filename)
    assert targets == [
        "2023-08-15_143205.jpg",
        "2023-08-15_143205_001.jpg",
        "2023-08-15_143205_002.jpg",
    ]


def test_plan_drops_noop_rename_from_collision_reshuffle(
    tmp_path: Path,
) -> None:
    """Regression: a folder containing the bare-name file plus suffixed
    siblings must not produce no-op RENAMEs on a re-migrate.

    File `2003-09-01_000000.mp4` already at canonical → no plan line.
    Files `_001.mp4`, `_002.mp4` collide on the same canonical; the
    bare file wins, and collision resolution assigns suffixes `_001`,
    `_002` to the others — but those match each file's current
    suffix. Plan should be empty.
    """
    src = tmp_path / "src"
    src.mkdir()
    bare = src / "2003-09-01_000000.mp4"
    one = src / "2003-09-01_000000_001.mp4"
    two = src / "2003-09-01_000000_002.mp4"
    for p in (bare, one, two):
        p.write_bytes(b"")

    cfg = _config(mp4="keep")
    cache = {
        p.resolve(): _meta(
            str(p),
            **{
                "QuickTime:CreateDate": "2003:09:01 00:00:00",
                PIX_ORIGINAL_PATH: str(p),  # already-migrated marker
                PIX_DATE_AUTO: "2003-09-01-00:00:00",
                # Match what derive_event_auto would produce from the
                # folder name, so no event-drift TAG line is queued.
                PIX_EVENT_AUTO: "src",
            },
        )
        for p in (bare, one, two)
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )

    assert plan.lines == []


def test_plan_demotes_rename_tag_to_tag_when_target_matches_current(
    tmp_path: Path,
) -> None:
    """Same collision-reshuffle case but with a pending TAG write.

    The file's current name already equals the collision-resolved
    target, so the rename is a no-op — but a tag write still needs to
    happen (first-migrate marker missing). Plan should demote the line
    from RENAME+TAG to TAG and strip the rename arrow from details.
    """
    src = tmp_path / "src"
    src.mkdir()
    bare = src / "2003-09-01_000000.mp4"
    one = src / "2003-09-01_000000_001.mp4"
    for p in (bare, one):
        p.write_bytes(b"")

    cfg = _config(mp4="keep")
    # Both files are first-migrate (no pix:OriginalPath) → tag writes
    # will be queued for both. With the bare-name file present, the
    # `_001` file's rename target lands on its own name.
    cache = {
        p.resolve(): _meta(
            str(p),
            **{"QuickTime:CreateDate": "2003:09:01 00:00:00"},
        )
        for p in (bare, one)
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )

    one_line = next(
        ln for ln in plan.lines if ln.abs_path == one.resolve()
    )
    assert one_line.action == Action.TAG
    assert one_line.target_filename is None
    # The rename-target arrow must be gone; tag-change arrows
    # (date_auto null→…, event_auto null→…) are fine and stay.
    assert "→2003-09-01_000000_001.mp4" not in one_line.details
    assert "original_path init" in one_line.details


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
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
        now=datetime(2026, 5, 18, 17, 0, 0),
    )

    text = plan.to_text()
    assert "# Migration plan:" in text
    assert "# Run ID: 2026-05-18_17-00-00" in text
    assert "first time will have their source path stored" in text
    assert "L001 | CONVERT+RENAME+TAG" in text
    assert "# Summary:" in text
    assert "1 CONVERT" in text


# --- EventAuto derivation in plan-gen ---


def test_plan_first_migrate_derives_event_from_parent_folder(
    tmp_path: Path,
) -> None:
    """First migrate: parent folder `2023-08-Hawaii` → EventAuto='Hawaii'."""
    src = tmp_path / "2023-08-Hawaii"
    src.mkdir()
    f = src / "IMG_001.jpg"
    f.write_bytes(b"")

    cfg = _config(jpg="keep")
    cache = {
        f.resolve(): _meta(
            str(f), **{"EXIF:DateTimeOriginal": "2023:08:15 14:32:05"}
        )
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )

    assert len(plan.lines) == 1
    line = plan.lines[0]
    assert line.pix_writes[PIX_EVENT_AUTO] == "Hawaii"
    assert "event_auto null→Hawaii" in line.details


def test_plan_first_migrate_skips_event_when_folder_is_date_only(
    tmp_path: Path,
) -> None:
    """Date-only parent (`2023-08`) → no EventAuto written."""
    src = tmp_path / "2023-08"
    src.mkdir()
    f = src / "IMG_001.jpg"
    f.write_bytes(b"")

    cfg = _config(jpg="keep")
    cache = {
        f.resolve(): _meta(
            str(f), **{"EXIF:DateTimeOriginal": "2023:08:15 14:32:05"}
        )
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )

    assert len(plan.lines) == 1
    line = plan.lines[0]
    assert PIX_EVENT_AUTO not in line.pix_writes
    assert "event_auto" not in line.details


def test_plan_re_migrate_writes_event_drift(tmp_path: Path) -> None:
    """File moved to a new event folder updates pix:EventAuto on re-migrate."""
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
                # File originally lived under a `Wedding` folder; now
                # OriginalPath points at a `Birthday` folder (user moved it).
                PIX_ORIGINAL_PATH: "F:/source/2023-08-Birthday/img.jpg",
                PIX_EVENT_AUTO: "Wedding",
            },
        )
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )

    assert len(plan.lines) == 1
    line = plan.lines[0]
    assert line.action == Action.TAG
    assert line.pix_writes[PIX_EVENT_AUTO] == "Birthday"
    assert "event_auto Wedding→Birthday" in line.details


def test_plan_re_migrate_writes_event_previous_when_override_set(
    tmp_path: Path,
) -> None:
    """Drift while EventOverride is active → writes pix:EventAutoPrevious."""
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
                PIX_ORIGINAL_PATH: "F:/source/2023-08-Birthday/img.jpg",
                PIX_EVENT_AUTO: "Wedding",
                PIX_EVENT_OVERRIDE: "Reception",
            },
        )
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )

    assert len(plan.lines) == 1
    line = plan.lines[0]
    from pix.plan import PIX_EVENT_AUTO_PREVIOUS

    assert line.pix_writes[PIX_EVENT_AUTO] == "Birthday"
    assert line.pix_writes[PIX_EVENT_AUTO_PREVIOUS] == "Wedding"


def test_plan_re_migrate_no_action_when_event_matches(
    tmp_path: Path,
) -> None:
    """Stored EventAuto matches re-derived → no plan line for event alone."""
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
                PIX_ORIGINAL_PATH: "F:/source/2023-08-Hawaii/img.jpg",
                PIX_EVENT_AUTO: "Hawaii",
            },
        )
    }

    plan = generate_plan(
        source=src.resolve(),
        cache=cache,
        config=cfg,
        run_id="test-run",
        run_dir=tmp_path / "runs",
        staging_dir=tmp_path / "staging",
    )

    assert plan.lines == []
