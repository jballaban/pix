"""Command-level tests for `pix organize`, esp. the bare/no-template form.

`pix organize <path>` with no template re-applies the stored
`organize.template` (spec/organize.md → Active template persistence).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from pix.commands.organize import organize_library
from pix.hash_cache import write_cached_hash
from pix.metadata_cache import PerFileCache
from pix.plan import PIX_DATE_AUTO, PIX_EVENT_AUTO, PIX_ORIGINAL_PATH


def _seed(
    root: Path,
    cache: PerFileCache,
    rel: str,
    *,
    date_auto: str,
    event: str,
    hash_hex: str,
) -> Path:
    """Create a migrated + hashed file at `rel` with the given effective date
    and event, so organize can plan its move with no ExifTool."""
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x")
    cache.add(
        f,
        {
            "SourceFile": str(f),
            PIX_ORIGINAL_PATH: f"F:/src/{f.name}",
            PIX_DATE_AUTO: date_auto,
            PIX_EVENT_AUTO: event,
        },
    )
    st = f.stat()
    write_cached_hash(
        root, f, hash_hex=hash_hex, size=st.st_size, mtime_ns=st.st_mtime_ns
    )
    return f


def test_scoped_organize(tmp_path: Path) -> None:
    """`pix organize <subfolder>` reorganizes just that subtree, leaves files
    elsewhere untouched, and still suffixes around a file already sitting at
    the target name (the destination-folder augmentation). Single-level
    template keeps the (cache-mirrored) paths under the Windows MAX_PATH."""
    root = _make_library(tmp_path, template="{event}")
    cache = PerFileCache.for_library(root)
    canon = "2023-05-10_120000.jpg"

    # In scope: should move src/ -> Hawaii/. Larger hash, so it yields the bare
    # name to the occupant and takes the _001 suffix.
    incoming = _seed(
        root, cache, "src/a.jpg",
        date_auto="2023-05-10-12:00:00", event="Hawaii", hash_hex="bbbb",
    )
    # Already at the destination under the canonical name (smaller hash → keeps
    # the bare name). Correctly placed → no move of its own.
    occupant = _seed(
        root, cache, f"Hawaii/{canon}",
        date_auto="2023-05-10-12:00:00", event="Hawaii", hash_hex="aaaa",
    )
    # Out of scope: would move (event Aruba) but must not be touched.
    outside = _seed(
        root, cache, "keep/b.jpg",
        date_auto="2024-01-01-00:00:00", event="Aruba", hash_hex="cccc",
    )

    organize_library(
        path=root / "src", template_str="{event}", no_prompt=True
    )

    # Incoming moved and got the _001 suffix (occupant kept the bare name).
    assert not incoming.exists()
    assert occupant.is_file()
    assert (root / "Hawaii" / "2023-05-10_120000_001.jpg").is_file()
    # Out-of-scope file untouched; its destination never created.
    assert outside.is_file()
    assert not (root / "Aruba").exists()


def _make_library(tmp_path: Path, *, template: str | None = None) -> Path:
    """Create an empty library root with a valid .pix/ and optional template."""
    root = tmp_path / "lib"
    pix = root / ".pix"
    pix.mkdir(parents=True)
    if template is not None:
        (pix / "pix.yaml").write_text(
            f'organize:\n  template: "{template}"\n', encoding="utf-8"
        )
    return root


def test_bare_organize_errors_when_no_stored_template(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _make_library(tmp_path, template=None)
    with pytest.raises(typer.Exit) as exc:
        organize_library(path=root, template_str=None)
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "no template given and none stored" in err


def test_bare_organize_uses_stored_template(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With a stored template, bare organize falls back to it and proceeds.

    The library is empty, so it short-circuits at "nothing to organize"
    — which proves the template fallback resolved (no required-template
    error, no parse error).
    """
    root = _make_library(tmp_path, template="{year}/{event}")
    organize_library(path=root, template_str=None)  # no raise
    out = capsys.readouterr().out
    assert "empty" in out.lower()


def test_noop_organize_is_terse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A library already in the target shape prints just the no-op line —
    no 'Plan written' / 'Summary' noise (matches migrate/hash)."""
    root = _make_library(tmp_path, template="{year}/{event}")
    # File already at its canonical target for {year}/{event}.
    media = (root / "2023" / "Hawaii" / "2023-08-15_143205.jpg").resolve()
    media.parent.mkdir(parents=True)
    media.write_bytes(b"data")

    # Seed both caches so plan-gen needs neither ExifTool nor a hash compute.
    PerFileCache.for_library(root).add(
        media,
        {
            PIX_ORIGINAL_PATH: "F:/source/x.jpg",
            PIX_DATE_AUTO: "2023-08-15-14:32:05",
            PIX_EVENT_AUTO: "Hawaii",
        },
    )
    st = media.stat()
    write_cached_hash(
        root, media, hash_hex="h", size=st.st_size, mtime_ns=st.st_mtime_ns
    )

    organize_library(path=root, template_str="{year}/{event}")
    out = capsys.readouterr().out
    assert "nothing to do" in out.lower()
    assert "Plan written" not in out
    assert "Summary" not in out
