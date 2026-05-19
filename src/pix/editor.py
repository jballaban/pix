"""Editor invocation for the migrate plan review step.

Per spec/migrate.md → Workflow step 4: the CLI opens plan.txt in the user's
configured editor (`$EDITOR` / `%EDITOR%`, falling back to notepad on Windows,
vi on POSIX). The editor invocation blocks until the editor closes; the
user's edits are then visible to the apply step.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


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
