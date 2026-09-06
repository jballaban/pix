"""End-to-end `pix export` — config to provisioned delivery tree."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from pix import export_manifest
from pix.commands.export import export_library
from pix.content_hash import compute_content_hash
from pix.hash_cache import write_cached_hash
from pix.metadata_cache import PerFileCache
from pix.plan import PIX_DATE_AUTO, PIX_EVENT_AUTO, PIX_ORIGINAL_PATH
from pix.rating import XMP_RATING


def _library(tmp_path: Path, delivery: Path, *, filter_expr: str = "rating:4,5") -> Path:
    root = tmp_path / "lib"
    pix = root / ".pix"
    pix.mkdir(parents=True)
    filter_line = f"    filter: '{filter_expr}'\n" if filter_expr else ""
    (pix / "pix.yaml").write_text(
        "exports:\n"
        "  general:\n"
        f"    path: '{delivery.as_posix()}'\n"
        f"{filter_line}"
        "    template: '{year}/{event}'\n",
        encoding="utf-8",
    )
    return root


def _seed(
    root: Path,
    rel: str,
    *,
    rating: int | None = 5,
    event: str = "Hawaii",
    content: bytes = b"photo",
) -> Path:
    """Seed a migrated + hashed library file.

    The cached hash is the **real** content hash, as `pix hash` would have
    written it — adoption verifies a delivery copy against it, so a fake
    value here would make the recovery path untestable.
    """
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(content)
    raw: dict[str, object] = {
        "SourceFile": str(f),
        PIX_ORIGINAL_PATH: f"F:/src/{f.name}",
        PIX_DATE_AUTO: "2023-08-15-14:32:05",
        PIX_EVENT_AUTO: event,
    }
    if rating is not None:
        raw[XMP_RATING] = rating
    PerFileCache.for_library(root).add(f, raw)
    st = f.stat()
    write_cached_hash(
        root,
        f,
        hash_hex=compute_content_hash(f),
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
    )
    return f


def test_provisions_selected_files(tmp_path: Path) -> None:
    delivery = tmp_path / "delivery"
    root = _library(tmp_path, delivery)
    _seed(root, "a.jpg", rating=5)
    _seed(root, "b.jpg", rating=2)

    export_library(path=root, no_prompt=True)

    assert (delivery / "2023" / "Hawaii" / "a.jpg").read_bytes() == b"photo"
    assert not (delivery / "2023" / "Hawaii" / "b.jpg").exists()

    manifest = export_manifest.load(root, "general")
    assert manifest is not None
    assert set(manifest.members) == {"2023/Hawaii/a.jpg"}


def test_second_run_is_a_no_op(tmp_path: Path) -> None:
    delivery = tmp_path / "delivery"
    root = _library(tmp_path, delivery)
    _seed(root, "a.jpg")

    export_library(path=root, no_prompt=True)
    written = (delivery / "2023" / "Hawaii" / "a.jpg").stat().st_mtime_ns

    export_library(path=root, no_prompt=True)
    # Untouched — the whole point of the delta reconcile.
    assert (delivery / "2023" / "Hawaii" / "a.jpg").stat().st_mtime_ns == written


def test_dropped_rating_removes_the_member(tmp_path: Path) -> None:
    delivery = tmp_path / "delivery"
    root = _library(tmp_path, delivery)
    f = _seed(root, "a.jpg", rating=5)
    export_library(path=root, no_prompt=True)
    assert (delivery / "2023" / "Hawaii" / "a.jpg").exists()

    # Re-rate below the tier; --no-prompt covers manifest-explained removals.
    cache = PerFileCache.for_library(root)
    cache.add(
        f,
        {
            "SourceFile": str(f),
            PIX_ORIGINAL_PATH: f"F:/src/{f.name}",
            PIX_DATE_AUTO: "2023-08-15-14:32:05",
            PIX_EVENT_AUTO: "Hawaii",
            XMP_RATING: 1,
        },
    )
    export_library(path=root, no_prompt=True)

    assert not (delivery / "2023" / "Hawaii" / "a.jpg").exists()
    manifest = export_manifest.load(root, "general")
    assert manifest is not None and manifest.members == {}


def test_foreign_file_stops_the_run(tmp_path: Path) -> None:
    delivery = tmp_path / "delivery"
    root = _library(tmp_path, delivery)
    _seed(root, "a.jpg")
    export_library(path=root, no_prompt=True)

    # Someone drops their own file into the delivery tree.
    (delivery / "holiday-snap.jpg").write_bytes(b"not ours")

    with pytest.raises(typer.Exit) as exc:
        export_library(path=root, no_prompt=True)
    assert exc.value.exit_code == 1
    # And it is emphatically still there.
    assert (delivery / "holiday-snap.jpg").exists()


def test_modified_member_stops_the_run(tmp_path: Path) -> None:
    delivery = tmp_path / "delivery"
    root = _library(tmp_path, delivery)
    _seed(root, "a.jpg")
    export_library(path=root, no_prompt=True)

    (delivery / "2023" / "Hawaii" / "a.jpg").write_bytes(b"edited on the NAS")

    with pytest.raises(typer.Exit) as exc:
        export_library(path=root, no_prompt=True)
    assert exc.value.exit_code == 1


def test_missing_member_is_re_provisioned(tmp_path: Path) -> None:
    delivery = tmp_path / "delivery"
    root = _library(tmp_path, delivery)
    _seed(root, "a.jpg")
    export_library(path=root, no_prompt=True)

    (delivery / "2023" / "Hawaii" / "a.jpg").unlink()
    export_library(path=root, no_prompt=True)
    assert (delivery / "2023" / "Hawaii" / "a.jpg").exists()


def test_lost_manifest_adopts_instead_of_duplicating(tmp_path: Path) -> None:
    delivery = tmp_path / "delivery"
    root = _library(tmp_path, delivery)
    _seed(root, "a.jpg")
    export_library(path=root, no_prompt=True)

    # Manifest lost (new machine, cleared .pix/local) but the tree is intact.
    export_manifest.discard(root, "general")
    export_library(path=root, no_prompt=True)

    manifest = export_manifest.load(root, "general")
    assert manifest is not None
    assert set(manifest.members) == {"2023/Hawaii/a.jpg"}
    assert list(delivery.rglob("*.jpg")) == [
        delivery / "2023" / "Hawaii" / "a.jpg"
    ]


def test_unknown_distribution_name_errors(tmp_path: Path) -> None:
    delivery = tmp_path / "delivery"
    root = _library(tmp_path, delivery)
    with pytest.raises(typer.Exit) as exc:
        export_library(path=root, name="nope", no_prompt=True)
    assert exc.value.exit_code == 1


def test_no_exports_configured_errors(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    with pytest.raises(typer.Exit) as exc:
        export_library(path=root, no_prompt=True)
    assert exc.value.exit_code == 1


def test_bad_template_fails_before_any_work(tmp_path: Path) -> None:
    delivery = tmp_path / "delivery"
    root = tmp_path / "lib"
    (root / ".pix").mkdir(parents=True)
    (root / ".pix" / "pix.yaml").write_text(
        "exports:\n"
        "  general:\n"
        f"    path: '{delivery.as_posix()}'\n"
        "    template: '{quarter}'\n",
        encoding="utf-8",
    )
    with pytest.raises(typer.Exit) as exc:
        export_library(path=root, no_prompt=True)
    assert exc.value.exit_code == 1
    assert not delivery.exists()


def test_missing_hash_refuses(tmp_path: Path) -> None:
    delivery = tmp_path / "delivery"
    root = _library(tmp_path, delivery)
    f = root / "a.jpg"
    f.write_bytes(b"photo")
    PerFileCache.for_library(root).add(
        f,
        {
            "SourceFile": str(f),
            PIX_ORIGINAL_PATH: "F:/src/a.jpg",
            PIX_DATE_AUTO: "2023-08-15-14:32:05",
            PIX_EVENT_AUTO: "Hawaii",
            XMP_RATING: 5,
        },
    )  # no hash written

    with pytest.raises(typer.Exit) as exc:
        export_library(path=root, no_prompt=True)
    assert exc.value.exit_code == 1


def test_event_rename_moves_rather_than_recopies(tmp_path: Path) -> None:
    delivery = tmp_path / "delivery"
    root = _library(tmp_path, delivery)
    f = _seed(root, "a.jpg", event="Hawaii")
    export_library(path=root, no_prompt=True)

    cache = PerFileCache.for_library(root)
    cache.add(
        f,
        {
            "SourceFile": str(f),
            PIX_ORIGINAL_PATH: f"F:/src/{f.name}",
            PIX_DATE_AUTO: "2023-08-15-14:32:05",
            PIX_EVENT_AUTO: "Maui",
            XMP_RATING: 5,
        },
    )
    export_library(path=root, no_prompt=True)

    assert (delivery / "2023" / "Maui" / "a.jpg").exists()
    assert not (delivery / "2023" / "Hawaii").exists()
    manifest = export_manifest.load(root, "general")
    assert manifest is not None
    assert set(manifest.members) == {"2023/Maui/a.jpg"}
