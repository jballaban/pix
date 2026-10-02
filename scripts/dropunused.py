"""Remove the imports pyright reports as unused, in the files it names.

    uv run python scripts/dropunused.py [PATH ...]

A text-level pruner keeps an import whose name also appears in a string; the
type checker knows better. This asks it, and takes out exactly what it says
is not accessed — one name from a `from` import, or the whole line.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


def main() -> int:
    targets = sys.argv[1:] or ["src", "tests"]
    out = subprocess.run(["uv", "run", "pyright", "--outputjson", *targets],
                         capture_output=True, text=True)
    report = json.loads(out.stdout)
    unused: dict[str, set[tuple[int, str]]] = defaultdict(set)
    for d in report.get("generalDiagnostics", []):
        if d.get("rule") != "reportUnusedImport":
            continue
        m = re.search(r'Import "([^"]+)" is not accessed', d["message"])
        if m:
            unused[d["file"]].add((d["range"]["start"]["line"] + 1, m.group(1)))
    for file, items in unused.items():
        path = Path(file)
        src = path.read_text(encoding="utf-8")
        lines = src.split("\n")
        tree = ast.parse(src)
        for node in sorted((n for n in ast.walk(tree)
                            if isinstance(n, (ast.Import, ast.ImportFrom))),
                           key=lambda n: -n.lineno):
            lo, hi = node.lineno, node.end_lineno or node.lineno
            drop = {name for line, name in items if lo <= line <= hi}
            if not drop:
                continue
            keep = [a for a in node.names
                    if (a.asname or a.name).split(".")[-1] not in drop
                    and (a.asname or a.name) not in drop]
            indent = lines[lo - 1][:len(lines[lo - 1]) - len(lines[lo - 1].lstrip())]
            if not keep:
                new: list[str] = []
            elif isinstance(node, ast.ImportFrom):
                stmt = ast.unparse(ast.ImportFrom(module=node.module, names=keep,
                                                  level=node.level))
                if len(indent + stmt) > 79:
                    inner = (",\n" + indent + "    ").join(ast.unparse(a) for a in keep)
                    stmt = f"from {node.module} import (\n{indent}    {inner},\n{indent})"
                new = [indent + stmt]
            else:
                new = [indent + ast.unparse(ast.Import(names=keep))]
            lines[lo - 1:hi] = new
        path.write_text("\n".join(lines), encoding="utf-8")
        print("pruned", path.name, sorted(n for _, n in items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
