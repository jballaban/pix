"""Implementation of `pix info meta <path>` — read-only single-file inspector.

Prints what pix "sees" for one file so the user can decide how to act
on it without re-deriving by hand:

- Effective values pix currently uses (date / event / canonical name).
- Every date candidate in priority order, with the winner marked — so
  it's obvious which source won and what the alternatives would yield.
- Notable raw tags: the `pix:*` namespace, camera make/model/comment,
  and anything mentioning MTP.

Read-only: reads the live file via ExifTool (no cache, no library root,
no lock), so it reflects on-disk truth and is exempt from the checkout
freeze.
"""

from __future__ import annotations

from pathlib import Path

import typer

from pix import banner
from pix.dates import date_candidates
from pix.events import effective_event
from pix.metadata import (
    ExifToolFailed,
    ExifToolNotFound,
    FileMetadata,
    read_metadata_batched,
)
from pix.plan import (
    PIX_DATE_AUTO,
    PIX_DATE_AUTO_PREVIOUS,
    PIX_DATE_OVERRIDE,
    PIX_EVENT_AUTO,
    PIX_EVENT_AUTO_PREVIOUS,
    PIX_EVENT_OVERRIDE,
    PIX_ORIGINAL_PATH,
    canonical_extension,
    effective_date,
)

# pix:* fields shown in their own block, in (raw-key, display-label) order.
_PIX_FIELDS: tuple[tuple[str, str], ...] = (
    (PIX_DATE_AUTO, "pix:DateAuto"),
    (PIX_DATE_OVERRIDE, "pix:DateOverride"),
    (PIX_DATE_AUTO_PREVIOUS, "pix:DateAutoPrevious"),
    (PIX_EVENT_AUTO, "pix:EventAuto"),
    (PIX_EVENT_OVERRIDE, "pix:EventOverride"),
    (PIX_EVENT_AUTO_PREVIOUS, "pix:EventAutoPrevious"),
    (PIX_ORIGINAL_PATH, "pix:OriginalPath"),
)


def meta_file(path: Path) -> None:
    """Inspect one file's date sources and notable tags."""
    banner()
    path = path.resolve()
    if not path.is_file():
        typer.echo(f"Error: {path} is not a file.", err=True)
        raise typer.Exit(code=1)

    try:
        result = read_metadata_batched([path], cache=None)
    except ExifToolNotFound as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    except ExifToolFailed as e:
        typer.echo(f"Error: exiftool failed.\n{e}", err=True)
        raise typer.Exit(code=1) from e

    meta = result.get(path)
    if meta is None:
        # ExifTool may echo SourceFile with different separators/case;
        # for a single-file read the lone value is ours.
        meta = next(iter(result.values()), None) or FileMetadata(
            path=path, raw={"SourceFile": str(path)}
        )

    kind = "video" if path.suffix.lower().lstrip(".") in _VIDEO_EXTS else "photo"
    typer.echo(f"File: {path}  ({kind})")

    _print_effective(meta, path)
    _print_candidates(meta)
    _print_pix_fields(meta)
    _print_notable(meta)


_VIDEO_EXTS = {"mp4", "mov", "m4v", "3gp", "mkv", "wmv", "webm", "avi"}


def _print_effective(meta: FileMetadata, path: Path) -> None:
    typer.echo("")
    typer.echo("== Effective (what pix uses) ==")
    eff = effective_date(meta)
    override = meta.get_str(PIX_DATE_OVERRIDE)
    src = "pix:DateAuto + override" if override else "pix:DateAuto / derived"
    if eff is not None:
        typer.echo(f"  date  : {eff.strftime('%Y-%m-%d %H:%M:%S')}   [{src}]")
        ext = canonical_extension(path.suffix)
        typer.echo(
            f"  name  : {eff.strftime('%Y-%m-%d_%H%M%S')}.{ext}"
        )
    else:
        typer.echo("  date  : (none — would land in null/)")
    event = effective_event(meta)
    ev_src = "pix:EventOverride" if meta.get_str(PIX_EVENT_OVERRIDE) else "pix:EventAuto"
    typer.echo(f"  event : {event if event is not None else '(none)'}   [{ev_src}]")


def _print_candidates(meta: FileMetadata) -> None:
    typer.echo("")
    typer.echo("== Date candidates (priority order — first match wins) ==")
    cands = date_candidates(meta)
    winner = next(
        (i for i, c in enumerate(cands) if c.parsed is not None), None
    )
    label_w = max((len(c.label) for c in cands), default=0)
    detail_w = min(
        max((len(c.detail) for c in cands), default=0), 48
    )
    for i, c in enumerate(cands):
        mark = "USED →" if i == winner else "      "
        detail = c.detail if len(c.detail) <= detail_w else c.detail[: detail_w - 1] + "…"
        if c.parsed is not None:
            outcome = f"{c.parsed.strftime('%Y-%m-%d %H:%M:%S')}"
        else:
            outcome = f"({c.note})"
        typer.echo(
            f"  {mark} {c.label.ljust(label_w)}  {detail.ljust(detail_w)}  {outcome}"
        )


def _print_pix_fields(meta: FileMetadata) -> None:
    typer.echo("")
    typer.echo("== pix:* fields ==")
    width = max(len(label) for _, label in _PIX_FIELDS)
    for key, label in _PIX_FIELDS:
        value = meta.get_str(key)
        typer.echo(f"  {label.ljust(width)}  {value if value else '(absent)'}")


def _print_notable(meta: FileMetadata) -> None:
    """Surface camera make/model/comment and anything mentioning MTP."""
    notable: list[tuple[str, str]] = []
    seen: set[str] = set()
    for key, value in meta.raw.items():
        if key == "SourceFile" or not isinstance(value, str):
            continue
        tail = key.split(":")[-1].lower()
        is_mtp = "mtp" in key.lower() or "mtp" in value.lower()
        is_id = tail in {"usercomment", "comment", "make", "model", "software"}
        if (is_mtp or is_id) and key not in seen:
            seen.add(key)
            notable.append((key, value))
    if not notable:
        return
    typer.echo("")
    typer.echo("== Other notable tags ==")
    width = max(len(k) for k, _ in notable)
    for key, value in notable:
        typer.echo(f"  {key.ljust(width)}  {value}")
