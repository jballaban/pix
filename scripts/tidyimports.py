"""Merge a module's top-level imports into one statement per source.

    uv run python scripts/tidyimports.py FILE [FILE ...]

`webmove.py` writes one `from x import y` line per name, which is correct and
unreadable. This rewrites the block of imports at the top of each file —
stdlib `import`s, then `from` imports, each sorted, `from __future__` first —
leaving everything after the block untouched.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def tidy(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    body = tree.body
    start = 1 if body and isinstance(body[0], ast.Expr) and isinstance(
        getattr(body[0], "value", None), ast.Constant) else 0
    block = []
    for node in body[start:]:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            block.append(node)
        else:
            break
    if not block:
        return False
    first, last = block[0].lineno, block[-1].end_lineno or block[-1].lineno
    lines = src.split("\n")
    # Only a block with nothing but imports and blank lines in it.
    for i in range(first - 1, last):
        s = lines[i].strip()
        if s.startswith("#"):
            return False
    future: list[str] = []
    plain: set[str] = set()
    froms: dict[str, list[str]] = {}
    for node in block:
        if isinstance(node, ast.Import):
            for a in node.names:
                plain.add(a.name + (f" as {a.asname}" if a.asname else ""))
        else:
            mod = "." * node.level + (node.module or "")
            names = [a.name + (f" as {a.asname}" if a.asname else "")
                     for a in node.names]
            if mod == "__future__":
                future += names
                continue
            have = froms.setdefault(mod, [])
            have += [n for n in names if n not in have]
    out: list[str] = []
    if future:
        out.append("from __future__ import " + ", ".join(sorted(set(future))))
        out.append("")
    stdlib_from = {m for m in froms if not m.startswith("pix") and m.split(".")[0]
                   in sys.stdlib_module_names}
    third = {m for m in froms if m not in stdlib_from and not m.startswith("pix")}
    ours = {m for m in froms if m.startswith("pix")}

    def emit(mod: str) -> None:
        names = sorted(froms[mod], key=lambda n: (n.split(" ")[0].lower(), n))
        one = f"from {mod} import " + ", ".join(names)
        if len(one) <= 79:
            out.append(one)
        else:
            out.append(f"from {mod} import (")
            out.extend(f"    {n}," for n in names)
            out.append(")")

    for p in sorted(plain, key=str.lower):
        out.append(f"import {p}")
    for m in sorted(stdlib_from):
        emit(m)
    for group in (third, ours):
        if group and out and out[-1] != "":
            out.append("")
        for m in sorted(group):
            emit(m)
    new = "\n".join(lines[:first - 1] + out + lines[last:])
    if new != src:
        path.write_text(new, encoding="utf-8")
        return True
    return False


if __name__ == "__main__":
    changed = [p for p in map(Path, sys.argv[1:]) if tidy(p)]
    print("tidied:", [p.name for p in changed] or "-")
