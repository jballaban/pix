"""Editor invocation and the shared `Apply? [Y/e/n]` prompt.

Both `migrate` and `organize` use the same plan/edit/confirm flow:
generate a plan, show a summary, prompt the user. The prompt accepts
`y` (apply), `e` (open the plan in `$EDITOR` / `%EDITOR%`, fallback
notepad/vi, then re-prompt), or `n` (abort). Editing is opt-in.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import typer


_LINE_ID_RE = re.compile(r"^(L\d+)\s*\|")


def _default_editor() -> str:
    return "notepad" if os.name == "nt" else "vi"


def open_in_editor(path: Path) -> None:
    """Open `path` in the user's editor and block until it closes.

    Resolution: `$EDITOR` env var if set, else `notepad` (Windows) or `vi`
    (POSIX). The editor is expected to be a blocking invocation; users with
    GUI editors that don't block by default (e.g. VS Code) should configure
    `EDITOR="code --wait"` or similar.
    """
    editor = os.environ.get("EDITOR") or _default_editor()
    # Editor may be a command + args separated by whitespace, e.g.
    # `code --wait`. shlex would be more correct on POSIX but for our
    # simple cases str.split() suffices on both platforms.
    cmd = editor.split() + [str(path)]
    subprocess.run(cmd, check=False)


def prompt_apply() -> str:
    """Prompt `Apply? [Y/e/n]` and return one of `'y'`, `'e'`, `'n'`.

    Pressing Enter accepts the default (`y`, apply). Unknown input
    re-prompts. `'e'` is the caller's signal to open the editor; the
    caller loops back to this prompt after the editor closes.
    """
    while True:
        raw = typer.prompt(
            "Apply? [Y/e/n]", default="y", show_default=False
        )
        ans = raw.strip().lower()
        if ans in ("y", "yes"):
            return "y"
        if ans in ("e", "edit"):
            return "e"
        if ans in ("n", "no"):
            return "n"
        typer.echo("Please answer Y (apply), e (edit), or n (abort).")


def parse_kept_line_ids(plan_text: str) -> set[str]:
    """Extract the set of `L###` IDs still present in the (post-edit) plan.

    Comment lines (`# ...`), blank lines, and the summary line are skipped.
    Only lines matching `^L\\d+\\s*\\|` count.
    """
    kept: set[str] = set()
    for raw_line in plan_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _LINE_ID_RE.match(stripped)
        if m is not None:
            kept.add(m.group(1))
    return kept
