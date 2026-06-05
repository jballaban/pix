"""pix CLI entry point."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from pix.commands.checkout import run_checkout
from pix.commands.context_menu import context_menu
from pix.commands.dedupe import dedupe_library
from pix.commands.hash import hash_library
from pix.commands.init import init_library
from pix.commands.meta import meta_file
from pix.commands.migrate import migrate_folder
from pix.commands.organize import organize_library
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


@app.command("set")
def set_(
    tag: Annotated[
        str,
        typer.Argument(help="Tag to override: 'event' or 'date'."),
    ],
    value: Annotated[
        str,
        typer.Argument(
            help=(
                'Override value. Empty string "" clears the override. For '
                "date: a YYYY-MM-DD-HH:MM:SS pattern with `*` for any "
                "unpinned part, e.g. 2022-*-*-*:*:*"
            ),
        ),
    ],
    paths: Annotated[
        list[Path],
        typer.Argument(
            help=(
                "One or more files or folders to set the override on (all "
                "under one library). A folder expands to the taggable media "
                "it contains."
            )
        ),
    ],
    no_prompt: Annotated[
        bool, typer.Option("--no-prompt", help=_NO_PROMPT_HELP)
    ] = False,
) -> None:
    """Set a tag override on specific files; run `pix organize` after."""
    set_override(tag=tag, value=value, paths=paths, no_prompt=no_prompt)


@app.command("clear")
def clear_(
    tag: Annotated[
        str,
        typer.Argument(help="Tag whose override to remove: 'event' or 'date'."),
    ],
    paths: Annotated[
        list[Path],
        typer.Argument(
            help=(
                "One or more files or folders to clear the override on. A "
                "folder expands to the taggable media it contains."
            )
        ),
    ],
    no_prompt: Annotated[
        bool, typer.Option("--no-prompt", help=_NO_PROMPT_HELP)
    ] = False,
) -> None:
    """Remove a tag override from specific files (the inverse of `pix set`)."""
    set_override(tag=tag, value="", paths=paths, no_prompt=no_prompt, clear=True)


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


@app.command("meta")
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


@app.command("checkout")
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
