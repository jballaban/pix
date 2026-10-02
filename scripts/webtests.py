"""Point tests at the web app's names where they now live.

The companion of `webmove.py`: after a move, a test that reached a name as
`web._x` finds nothing there if web.py no longer uses it. This finds where
each such name lives in `pix.nas.webapp` now, rewrites the reference to
`w_<module>.<name>`, and adds the import.

    uv run python scripts/webtests.py
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def main() -> int:
    sys.path.insert(0, str(ROOT / "src"))
    web = importlib.import_module("pix.nas.web")
    pkg = importlib.import_module("pix.nas.webapp")
    homes: dict[str, str] = {}
    for info in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module(f"pix.nas.webapp.{info.name}")
        src = Path(mod.__file__ or "").read_text(encoding="utf-8")
        for node in ast.parse(src).body:
            names = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            for n in names:
                homes.setdefault(n, info.name)
    changed = []
    for path in sorted(TESTS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        need: set[str] = set()

        def swap(m: re.Match[str]) -> str:
            name = m.group(1)
            if hasattr(web, name):
                return m.group(0)
            pub = name[1:] if name.startswith("_") and not name.startswith("__") else name
            mod = homes.get(pub) or homes.get(name)
            if mod is None:
                return m.group(0)
            need.add(mod)
            return f"w_{mod}.{pub if pub in vars(importlib.import_module('pix.nas.webapp.' + mod)) else name}"

        new = re.sub(r"\bweb\.([A-Za-z_]\w*)\b", swap, text)
        # And any `w_<module>` already used without its import.
        need |= {m for m in re.findall(r"\bw_([a-z_]+)\.", new)
                 if m in set(homes.values())}
        for mod in sorted(need):
            line = f"from pix.nas.webapp import {mod} as w_{mod}\n"
            if line in new:
                continue
            # After the last top-level `from pix.nas…` import, or the last
            # top-level import of any kind.
            lines = new.split("\n")
            tree = ast.parse(new)
            last = max((n.end_lineno or n.lineno) for n in tree.body
                       if isinstance(n, (ast.Import, ast.ImportFrom)))
            lines.insert(last, line.rstrip("\n"))
            new = "\n".join(lines)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(path.name)
    print("tests changed:", changed or "-")
    return 0


if __name__ == "__main__":
    sys.exit(main())
