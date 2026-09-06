"""pix CLI entry point."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from pix.commands.checkout import run_checkout
from pix.commands.config import show_config
from pix.commands.context_menu import context_menu
from pix.commands.dedupe import dedupe_library
from pix.commands.events import list_events
from pix.commands.export import export_library
from pix.commands.hash import hash_library
from pix.commands.import_ import import_library
from pix.commands.init import init_library
from pix.commands.meta import meta_file
from pix.commands.migrate import migrate_folder
from pix.commands.organize import organize_library
from pix.commands.rotate import rotate_videos
from pix.commands.set import set_override
from pix.commands.sync import sync_library

# Shared help text for the `--no-prompt` confirmation-skip option.
_NO_PROMPT_HELP = (
    "Skip the confirmation prompt and apply the generated plan directly. "
    "The plan is still written to the run folder."
)

app: typer.Typer = typer.Typer(
    name="pix",
    help="Personal media library management at terabyte scale.",
    add_completion=False,
    no_args_is_help=True,
)

# Grouped sub-commands. `tag` collects the operations that write tags to
# specific files (set/clear/rotate/checkout); `info` collects the read-only
# inspectors (meta/events). Registered at the bottom of this module.
tag_app: typer.Typer = typer.Typer(
    name="tag",
    help="Edit tags on specific files: set / clear / rotate / checkout.",
    no_args_is_help=True,
)
info_app: typer.Typer = typer.Typer(
    name="info",
    help="Read-only inspection: config / events / meta.",
    no_args_is_help=True,
)

app.add_typer(tag_app, name="tag")
app.add_typer(info_app, name="info")

# Shared help text for the `--paths-from` list-file option.
_PATHS_FROM_HELP = (
    "Read paths from a file, one per line (UTF-8; blank lines ignored). "
    "Combines with any paths given as arguments. For selections too large "
    "to fit on a command line."
)


def _collect_paths(
    paths: list[Path] | None, paths_from: Path | None
) -> list[Path]:
    """Merge positional paths with the lines of a `--paths-from` list file.

    Windows caps a process command line at 32767 characters, so a large
    Explorer selection (~660 media paths) cannot be splatted onto argv —
    `CreateProcess` fails with "The filename or extension is too long"
    before pix ever starts. The context-menu launcher already snapshots
    the selection to a temp file, so it hands us that file instead.

    Positional paths come first and the file's lines follow; the commands
    dedupe downstream, so an overlap is harmless.
    """
    out: list[Path] = list(paths or [])
    if paths_from is not None:
        try:
            # utf-8-sig: Windows PowerShell's `Set-Content -Encoding UTF8`
            # (what the launcher writes with) emits a BOM, which would
            # otherwise become part of the first path.
            text = paths_from.read_text(encoding="utf-8-sig")
        except OSError as e:
            typer.echo(f"Error: cannot read --paths-from {paths_from}: {e}", err=True)
            raise typer.Exit(code=1)
        out.extend(Path(s) for s in (ln.strip() for ln in text.splitlines()) if s)
    return out



def _force_utf8_output() -> None:
    """Make stdout/stderr UTF-8 so non-ASCII never crashes a run.

    Windows consoles default to cp1252, which can't encode many path
    characters (accents, CJK) or the glyphs our output uses (→, …) —
    echoing one raises UnicodeEncodeError and aborts the command. UTF-8
    encodes everything, so the encode step is always safe even if the
    terminal font can't render a particular glyph. No-op where the
    stream doesn't support reconfigure (already-wrapped, redirected).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def main() -> None:
    """Real entry point. Catches Ctrl-C so the user gets a clean exit
    instead of a multi-frame rich traceback.

    Unix convention: SIGINT exits with code 130. The active line was
    already logged as `Interrupted` to apply.log by the apply loop, so
    the user can tail that file to see where we stopped.
    """
    _force_utf8_output()
    try:
        app()
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        sys.exit(130)


# The version banner now lives in each command (via `pix.banner`) so
# it can be printed as a single line that includes the resolved
# library's schema version when applicable. See `pix.__init__`.


@app.command("init")
def init(
    path: Annotated[
        Path | None,
        typer.Argument(
            help="Path to establish as the library root. Defaults to CWD.",
        ),
    ] = None,
) -> None:
    """Establish a library root, creating .pix/ and a pix.yaml settings file."""
    init_library(path)


@app.command("migrate")
def migrate(
    folder: Annotated[
        Path,
        typer.Argument(
            help=(
                "Folder whose files should be normalized in place. The "
                "library is resolved by walking up from this folder, then "
                "falling back to $PIX_ROOT or CWD."
            ),
        ),
    ],
    no_prompt: Annotated[
        bool, typer.Option("--no-prompt", help=_NO_PROMPT_HELP)
    ] = False,
) -> None:
    """Normalize files in <folder> per the library's policy (in-place, per-file)."""
    migrate_folder(folder=folder, no_prompt=no_prompt)


@app.command("organize")
def organize(
    path: Annotated[
        Path,
        typer.Argument(
            help=(
                "Path inside (or at) the library root. The library is "
                "resolved by walking up from this path. `.` for CWD."
            ),
        ),
    ],
    template: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Folder template, e.g. '{year}/{month}/{event}'. Tokens: "
                "{year}, {month}, {day}, {event}. Levels separated "
                "by `/`. Persisted as the active template on successful "
                "apply. Omit to re-apply the stored default shape."
            ),
        ),
    ] = None,
    no_prompt: Annotated[
        bool, typer.Option("--no-prompt", help=_NO_PROMPT_HELP)
    ] = False,
) -> None:
    """Re-shape the library to match a folder template (library-wide MOVE)."""
    organize_library(path=path, template_str=template, no_prompt=no_prompt)


@app.command("export")
def export(
    path: Annotated[
        Path,
        typer.Argument(
            help=(
                "Path inside (or at) the library root. The library is "
                "resolved by walking up from this path. `.` for CWD."
            ),
        ),
    ],
    name: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Distribution to reconcile, as named under `exports:` in "
                "pix.yaml. Omit to reconcile every distribution."
            ),
        ),
    ] = None,
    no_prompt: Annotated[
        bool, typer.Option("--no-prompt", help=_NO_PROMPT_HELP)
    ] = False,
) -> None:
    """Reconcile a delivery distribution (curated, filtered copy of the library)."""
    export_library(path=path, name=name, no_prompt=no_prompt)


@tag_app.command("set")
def set_(
    tag: Annotated[
        str,
        typer.Argument(help="Tag to set: 'event', 'date', or 'rating'."),
    ],
    value: Annotated[
        str,
        typer.Argument(
            help=(
                'Value to set. For date: a YYYY-MM-DD-HH:MM:SS pattern with '
                "`*` for any unpinned part, e.g. 2022-*-*-*:*:*. For rating: "
                "an integer 0-5 (0 = unrated)."
            ),
        ),
    ],
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            help=(
                "One or more files or folders to set the override on (all "
                "under one library). A folder expands to the taggable media "
                "it contains. Omit when using --paths-from."
            )
        ),
    ] = None,
    no_prompt: Annotated[
        bool, typer.Option("--no-prompt", help=_NO_PROMPT_HELP)
    ] = False,
    paths_from: Annotated[
        Path | None, typer.Option("--paths-from", help=_PATHS_FROM_HELP)
    ] = None,
) -> None:
    """Set a tag override on specific files; run `pix organize` after."""
    set_override(
        tag=tag,
        value=value,
        paths=_collect_paths(paths, paths_from),
        no_prompt=no_prompt,
    )


@tag_app.command("clear")
def clear_(
    tag: Annotated[
        str,
        typer.Argument(help="Tag to clear: 'event', 'date', or 'rating'."),
    ],
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            help=(
                "One or more files or folders to clear. A folder expands to "
                "the taggable media it contains. Omit when using --paths-from."
            )
        ),
    ] = None,
    no_prompt: Annotated[
        bool, typer.Option("--no-prompt", help=_NO_PROMPT_HELP)
    ] = False,
    paths_from: Annotated[
        Path | None, typer.Option("--paths-from", help=_PATHS_FROM_HELP)
    ] = None,
) -> None:
    """Clear a tag: blank the event (forces 'no event', even if auto-derived),
    revert a date override to the auto date, or remove a rating (→ unrated)."""
    set_override(
        tag=tag,
        value="",
        paths=_collect_paths(paths, paths_from),
        no_prompt=no_prompt,
        clear=True,
    )


@tag_app.command("rotate")
def rotate(
    degrees: Annotated[
        int,
        typer.Argument(help="Clockwise rotation to add: 90, 180, or 270."),
    ],
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            help=(
                "Video files or folders to rotate (all under one library). "
                "Folders expand to the videos inside; non-videos are skipped. "
                "Omit when using --paths-from."
            )
        ),
    ] = None,
    no_prompt: Annotated[
        bool, typer.Option("--no-prompt", help=_NO_PROMPT_HELP)
    ] = False,
    paths_from: Annotated[
        Path | None, typer.Option("--paths-from", help=_PATHS_FROM_HELP)
    ] = None,
) -> None:
    """Losslessly rotate videos by tagging orientation (no re-encode)."""
    rotate_videos(
        degrees=degrees,
        paths=_collect_paths(paths, paths_from),
        no_prompt=no_prompt,
    )


@app.command("context-menu")
def context_menu_(
    action: Annotated[
        str,
        typer.Argument(
            help=(
                "install, uninstall, or status (default). Manages the Windows "
                "Explorer 'Pix' right-click menu (Event/Date > Set/Clear, plus "
                "Info on files) for files/folders."
            )
        ),
    ] = "status",
) -> None:
    """Manage the Windows Explorer "Pix" right-click menu."""
    context_menu(action=action)


@info_app.command("config")
def config(
    path: Annotated[
        Path | None,
        typer.Argument(
            help=(
                "A file/folder inside the library (resolves the root). "
                "Defaults to CWD / PIX_ROOT."
            )
        ),
    ] = None,
) -> None:
    """Show the library's resolved pix.yaml settings and export distributions."""
    show_config(path)


@info_app.command("events")
def events(
    path: Annotated[
        Path | None,
        typer.Argument(
            help=(
                "A file/folder inside the library (resolves the root). "
                "Defaults to CWD / PIX_ROOT."
            )
        ),
    ] = None,
) -> None:
    """List the library's unique event names, one per line (read-only)."""
    list_events(path)


@info_app.command("meta")
def meta(
    path: Annotated[
        Path,
        typer.Argument(
            help="File to inspect (read-only; shows date sources + tags)."
        ),
    ],
) -> None:
    """Show pix's date candidates and notable tags for one file."""
    meta_file(path)


@tag_app.command("checkout")
def checkout(
    path: Annotated[
        Path | None,
        typer.Argument(
            help=(
                "Folder to scope the checkout to (like `pix migrate "
                "<folder>`). Resolves the library root and bounds the "
                "file set. `.` for CWD. Required when starting a checkout; "
                "omit it (and the template) for --commit / --reset / status."
            ),
        ),
    ] = None,
    template: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Folder template, e.g. '{year}/{event}'. Tokens: {year}, "
                "{month}, {day}, {event}. Required when starting."
            ),
        ),
    ] = None,
    commit: Annotated[
        bool,
        typer.Option("--commit", help="Apply the open checkout's tag edits."),
    ] = False,
    reset: Annotated[
        bool,
        typer.Option("--reset", help="Discard the open checkout."),
    ] = False,
) -> None:
    """Edit tags by shuffling a hard-link workspace (scoped to <path>)."""
    run_checkout(path, template, commit=commit, reset=reset)


@app.command("import")
def import_(
    path: Annotated[
        Path,
        typer.Argument(
            help=(
                "Path inside (or at) the library root, used to resolve the "
                "library. `.` for CWD. Landed files go under "
                ".pix/local/import/<device>/."
            ),
        ),
    ],
    device: Annotated[
        str | None,
        typer.Option(
            "--device",
            help=(
                "Select the connected device by serial or (friendly/model) "
                "name substring. Required when more than one device is present."
            ),
        ),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            help=(
                "Assign the selected device's friendly folder name without "
                "prompting. Persisted to .pix/devices.yaml and reused on future "
                "runs (also renames a device already named)."
            ),
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Enumerate and report new-vs-already-imported counts; download nothing.",
        ),
    ] = False,
) -> None:
    """Pull new photos/videos off a connected phone into .pix/local/import/ (verified)."""
    import_library(path=path, device=device, name=name, dry_run=dry_run)


@app.command("hash")
def hash_(
    path: Annotated[
        Path,
        typer.Argument(
            help=(
                "Path inside (or at) the library root. The library is "
                "resolved by walking up from this path. Hash operates on "
                "every file under the library; subfolder scope is not "
                "supported in v1."
            ),
        ),
    ],
    no_prompt: Annotated[
        bool, typer.Option("--no-prompt", help=_NO_PROMPT_HELP)
    ] = False,
) -> None:
    """Populate the per-file content-hash cache for every stale/missing entry."""
    hash_library(path=path, no_prompt=no_prompt)


@app.command("dedupe")
def dedupe(
    path: Annotated[
        Path | None,
        typer.Argument(
            help=(
                "Path inside (or at) the library root. The library is "
                "resolved by walking up from this path. `.` for CWD. "
                "Omitted with --commit (the review folder names the library)."
            ),
        ),
    ] = None,
    no_prompt: Annotated[
        bool, typer.Option("--no-prompt", help=_NO_PROMPT_HELP)
    ] = False,
    min_distance: Annotated[
        int,
        typer.Option(
            "--min",
            help="Min perceptual distance for video matches (default 0).",
        ),
    ] = 0,
    max_distance: Annotated[
        int,
        typer.Option(
            "--max",
            help="Max perceptual distance for video matches (default 30).",
        ),
    ] = 30,
    checkout: Annotated[
        Path | None,
        typer.Option(
            "--checkout",
            help=(
                "Review mode: write a montage + manifest per video duplicate "
                "group into this folder instead of deleting. Delete a montage "
                "to skip that group, then run --commit."
            ),
        ),
    ] = None,
    commit: Annotated[
        Path | None,
        typer.Option(
            "--commit",
            help=(
                "Apply the groups whose montage still exists in this review "
                "folder (from a prior --checkout)."
            ),
        ),
    ] = None,
    videos_only: Annotated[
        bool,
        typer.Option(
            "--videos-only",
            help=(
                "Only the perceptual video pass; skip exact image dedupe. "
                "Matches what --checkout/--commit operate on."
            ),
        ),
    ] = False,
) -> None:
    """Remove duplicates: images by content hash, videos by perceptual match."""
    dedupe_library(
        path=path,
        no_prompt=no_prompt,
        min_distance=min_distance,
        max_distance=max_distance,
        checkout=checkout,
        commit=commit,
        videos_only=videos_only,
    )


@app.command("sync")
def sync(
    path: Annotated[
        Path,
        typer.Argument(
            help=(
                "Path to run the pipeline over. Scopes `migrate` to this "
                "folder and resolves the library root for the (library-wide) "
                "hash / dedupe / organize steps. `.` for CWD."
            ),
        ),
    ],
    template: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Folder template for the organize step (e.g. "
                "'{year}/{month}/{event}'). Omit to use the library's "
                "stored template."
            ),
        ),
    ] = None,
) -> None:
    """Do everything: migrate → hash → dedupe → organize, non-interactively.

    Each step auto-applies its plan (no prompts); plans are still written.
    Stops at the first step that errors so a failure isn't buried.
    """
    sync_library(path=path, template_str=template)
