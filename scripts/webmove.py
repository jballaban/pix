"""Move top-level definitions out of `pix/nas/web.py` into `pix/nas/webapp/`.

A refactoring tool for splitting the web app, kept in the repo because the
split is done in steps and each step is the same operation:

    uv run python scripts/webmove.py MODULE NAME [NAME ...] [--doc "..."]

It moves each named definition — with the comment block directly above it —
into `src/pix/nas/webapp/MODULE.py` (appending if the module exists), gives a
`_private` name a public one there, imports what the moved code needs, and
leaves web.py importing the moved names back under their old names where it
still uses them. Tests that reached a moved name as `web._x` are rewritten to
reach it in its new home.

It refuses — and changes nothing — when the moved code still needs a name
that stays behind in web.py: that would be an import cycle, and the answer is
to move that name first (or with it).
"""

from __future__ import annotations

import argparse
import ast
import builtins
import io
import re
import symtable
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src/pix/nas/web.py"
PKG = ROOT / "src/pix/nas/webapp"
TESTS = ROOT / "tests"
BUILTINS = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}


#: Public names asked for by `--as`, where dropping the underscore would
#: collide with something already in use.
AS: dict[str, str] = {}


def public(name: str) -> str:
    if name in AS:
        return AS[name]
    return name[1:] if name.startswith("_") and not name.startswith("__") else name


def statements(tree: ast.Module) -> list[tuple[ast.stmt, list[str]]]:
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append((node, [node.name]))
        elif isinstance(node, ast.Assign):
            out.append((node, [t.id for t in node.targets if isinstance(t, ast.Name)]))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.append((node, [node.target.id]))
    return out


def span(node: ast.stmt, lines: list[str]) -> tuple[int, int]:
    """1-based inclusive line span, decorators and the comment block above."""
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    while start > 1 and lines[start - 2].lstrip().startswith("#"):
        start -= 1
    return start, node.end_lineno or node.lineno


def module_tables(src: str) -> dict[tuple[str, int], symtable.SymbolTable]:
    top = symtable.symtable(src, "web", "exec")
    return {(t.get_name(), t.get_lineno()): t for t in top.get_children()}


def table_globals(t: symtable.SymbolTable) -> set[str]:
    out = set()
    for s in t.get_symbols():
        if s.is_referenced() and (s.is_global() or s.is_free()):
            out.add(s.get_name())
    for c in t.get_children():
        out |= table_globals(c)
    return out


def refs(node: ast.stmt, tables: dict[tuple[str, int], symtable.SymbolTable]) -> set[str]:
    """Names the statement needs from module scope."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        t = tables.get((node.name, node.lineno))
        out = table_globals(t) if t is not None else set()
        # Evaluated (or type-checked) in module scope: decorators, defaults,
        # annotations, bases.
        outer: list[ast.AST] = list(node.decorator_list)
        if isinstance(node, ast.ClassDef):
            outer += node.bases + [k.value for k in node.keywords]
            for item in node.body:
                if isinstance(item, ast.AnnAssign):
                    outer.append(item.annotation)
        else:
            a = node.args
            outer += a.defaults + [d for d in a.kw_defaults if d is not None]
            for arg in a.args + a.kwonlyargs + a.posonlyargs + \
                    [x for x in (a.vararg, a.kwarg) if x]:
                if arg.annotation is not None:
                    outer.append(arg.annotation)
            if node.returns is not None:
                outer.append(node.returns)
        for o in outer:
            out |= {n.id for n in ast.walk(o) if isinstance(n, ast.Name)}
        return out | cast_names(node)
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | \
        cast_names(node)


def cast_names(node: ast.AST) -> set[str]:
    """Names inside `cast("ix.Pick", ...)` — a type written as a string,
    which still has to be importable where it is checked."""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id == "cast" and n.args \
                and isinstance(n.args[0], ast.Constant) \
                and isinstance(n.args[0].value, str):
            out |= set(re.findall(r"\b([A-Za-z_]\w*)\b", n.args[0].value))
    return out


def import_map(tree: ast.Module, src_lines: list[str]) -> dict[str, tuple[str, str, str | None]]:
    """Bound name -> (kind, module, original name) for every top-level import."""
    out: dict[str, tuple[str, str, str | None]] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                out[(a.asname or a.name).split(".")[0]] = ("import", a.name, a.asname)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                out[a.asname or a.name] = ("from", node.module, a.name)
    return out


def rename_tokens(code: str, mapping: dict[str, str]) -> str:
    """Rename NAME tokens (not attributes, not keyword arguments)."""
    if not mapping:
        return code
    toks = list(tokenize.generate_tokens(io.StringIO(code).readline))
    edits = []
    for i, tok in enumerate(toks):
        if tok.type != tokenize.NAME or tok.string not in mapping:
            continue
        prev = next((t for t in reversed(toks[:i])
                     if t.type not in (tokenize.NL, tokenize.COMMENT)), None)
        nxt = next((t for t in toks[i + 1:]
                    if t.type not in (tokenize.NL, tokenize.COMMENT)), None)
        if prev and prev.type == tokenize.OP and prev.string == ".":
            continue
        # f(name=...) inside a call: a keyword, not a reference.
        if nxt and nxt.type == tokenize.OP and nxt.string == "=" and prev \
                and prev.type == tokenize.OP and prev.string in ("(", ","):
            continue
        edits.append((tok.start, tok.end, mapping[tok.string]))
    lines = code.split("\n")
    for (r, c0), (_, c1), new in sorted(edits, reverse=True):
        line = lines[r - 1]
        lines[r - 1] = line[:c0] + new + line[c1:]
    return "\n".join(lines)


def prune_imports(code: str) -> str:
    """Drop the names a module imports and no longer uses."""
    tree = ast.parse(code)
    lines = code.split("\n")
    import_nodes = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    spans = {(n.lineno, n.end_lineno) for n in import_nodes}
    used: set[str] = set()
    for tok in tokenize.generate_tokens(io.StringIO(code).readline):
        if tok.type == tokenize.NAME:
            row = tok.start[0]
            if not any(a <= row <= (b or a) for a, b in spans):
                used.add(tok.string)
    # A string annotation names things too.
    used |= set(re.findall(r"[A-Za-z_]\w*", " ".join(
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and len(n.value) < 80)))
    for node in sorted(import_nodes, key=lambda n: -n.lineno):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        keep = [a for a in node.names
                if (a.asname or a.name).split(".")[0] in used]
        if len(keep) == len(node.names):
            continue
        if isinstance(node, ast.ImportFrom):
            new = ast.ImportFrom(module=node.module, names=keep, level=node.level)
        else:
            new = ast.Import(names=keep)
        text = ast.unparse(new) if keep else None
        if text and len(text) > 79 and isinstance(node, ast.ImportFrom):
            inner = ",\n    ".join(ast.unparse(a) for a in keep)
            text = f"from {node.module} import (\n    {inner},\n)"
        lines[node.lineno - 1:node.end_lineno] = [text] if text else []
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("module")
    ap.add_argument("names", nargs="+")
    ap.add_argument("--doc", default="")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--as", dest="as_", nargs="*", default=[],
                    help="old=new public names, for a name that would collide")
    args = ap.parse_args()
    for pair in args.as_:
        old, _, new = pair.partition("=")
        AS[old] = new

    src = WEB.read_text(encoding="utf-8")
    lines = src.split("\n")
    tree = ast.parse(src)
    tables = module_tables(src)
    stmts = statements(tree)
    defined = {n for _, ns in stmts for n in ns}
    imports = import_map(tree, lines)
    want = set(args.names)
    missing = want - defined
    if missing:
        print("not defined in web.py:", sorted(missing))
        return 1
    moving = [(node, ns) for node, ns in stmts if set(ns) & want]
    moved_names = {n for _, ns in moving for n in ns}
    if moved_names - want:
        print("statements define more than asked:", sorted(moved_names - want))
        return 1

    needed: set[str] = set()
    for node, _ in moving:
        needed |= refs(node, tables)
    needed -= moved_names | BUILTINS
    behind = sorted(n for n in needed if n in defined)
    if behind:
        print(f"refused: the moved code still needs {behind} from web.py")
        return 2

    if args.module in imports or args.module in defined:
        print(f"refused: '{args.module}' is already a name in web.py — "
              "importing the new module would shadow it")
        return 4
    mod = f"pix.nas.webapp.{args.module}"
    # Names that come from other webapp modules arrive under their public
    # names in the new module.
    import_lines: dict[str, str] = {}
    rename: dict[str, str] = {n: public(n) for n in moved_names}
    for n in sorted(needed):
        if n not in imports:
            continue            # a name only in a string, or a builtin
        kind, module, orig = imports[n]
        if kind == "import":
            import_lines[n] = f"import {module}" + (f" as {orig}" if orig else "")
        elif module.startswith("pix.nas.webapp."):
            assert orig is not None
            rename[n] = orig
            import_lines[n] = f"from {module} import {orig}"
        else:
            assert orig is not None
            import_lines[n] = (f"from {module} import {orig}" +
                               (f" as {n}" if n != orig else ""))
    # A public name that is also a local somewhere in the moved code would
    # turn `usual = _usual()` into `usual = usual()`.
    local_names: set[str] = set()
    for node, _ in moving:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            t = tables.get((node.name, node.lineno))
            stack = [t] if t is not None else []
            while stack:
                cur = stack.pop()
                local_names |= {s.get_name() for s in cur.get_symbols()
                                if s.is_local() or s.is_parameter()}
                stack.extend(cur.get_children())
    # A helper the moved code imports from another webapp module keeps its
    # old alias where its public name would collide (`h as _h` beside a
    # local `h`); only the moved names have to be named afresh.
    for n in list(rename):
        p = rename[n]
        if n not in moved_names and p != n and (p in needed or p in local_names):
            kind, module, orig = imports[n]
            import_lines[n] = f"from {module} import {orig} as {n}"
            del rename[n]
    clash = [p for n, p in rename.items()
             if p != n and (p in needed or p in local_names)]
    if clash:
        print("refused: a public name would collide with one in use:", clash)
        return 3

    # The new module's text.
    blocks = []
    cut: list[tuple[int, int]] = []
    for node, _ in moving:
        a, b = span(node, lines)
        cut.append((a, b))
        blocks.append("\n".join(lines[a - 1:b]))
    body = rename_tokens("\n\n\n".join(blocks), rename)
    target = PKG / f"{args.module}.py"
    PKG.mkdir(exist_ok=True)
    init = PKG / "__init__.py"
    if not init.exists():
        init.write_text('"""The web app, a module per concern. `pix.nas.web` assembles it."""\n',
                        encoding="utf-8")
    stdlib = sorted(v for v in import_lines.values() if v.startswith("import "))
    froms = sorted(v for v in import_lines.values() if v.startswith("from "))
    if target.exists():
        old = target.read_text(encoding="utf-8")
        head, _, rest = old.partition("\n\n\n")
        have = set(head.split("\n"))
        add = [l for l in stdlib + froms if l not in have]
        new_text = head + ("\n" + "\n".join(add) if add else "") + "\n\n\n" + rest.rstrip("\n") + "\n\n\n" + body + "\n"
    else:
        import textwrap
        doc = textwrap.fill(args.doc or "Moved out of `pix.nas.web`.", 76)
        new_text = (f'"""{doc}\n"""\n\nfrom __future__ import annotations\n\n'
                    + "\n".join(stdlib) + ("\n" if stdlib else "")
                    + "\n".join(froms) + "\n\n\n" + body + "\n")

    # web.py without them, importing back what it still uses.
    keep = lines[:]
    for a, b in sorted(cut, reverse=True):
        # Two blank lines where it was, so its neighbours stay apart.
        keep[a - 1:b] = ["", ""]
    rest = "\n".join(keep)
    rest = re.sub(r"\n{4,}", "\n\n\n", rest)
    still = {t.string for t in tokenize.generate_tokens(io.StringIO(rest).readline)
             if t.type == tokenize.NAME}
    back = sorted(n for n in moved_names if n in still)
    if back:
        parts = ", ".join(public(n) + (f" as {n}" if public(n) != n else "")
                          for n in back)
        line = f"from {mod} import ({parts})" if len(parts) > 60 else \
            f"from {mod} import {parts}"
        if len(line) > 79:
            inner = ",\n    ".join(public(n) + (f" as {n}" if public(n) != n else "")
                                   for n in back)
            line = f"from {mod} import (\n    {inner},\n)"
        anchor = "from pix.nas import webroots\n"
        assert anchor in rest
        rest = rest.replace(anchor, anchor + line + "\n", 1)

    # Routes register by being imported. A module of them that web.py has no
    # other use for is still imported, and named in `_ROUTES` so the import
    # is a use rather than an unused line.
    has_routes = any(
        isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
        and isinstance(d.func.value, ast.Name) and d.func.value.id == "app"
        for node, _ in moving for d in getattr(node, "decorator_list", []))
    if has_routes:
        reg = f"from pix.nas.webapp import {args.module}"
        if reg + "\n" not in rest:
            anchor = "from pix.nas import webroots\n"
            rest = rest.replace(anchor, anchor + reg + "\n", 1)
        m = re.search(r"^_ROUTES: tuple\[object, \.\.\.\] = \(([^)]*)\)", rest, re.M)
        if m:
            names = [x.strip() for x in m.group(1).split(",") if x.strip()]
            if args.module not in names:
                names.append(args.module)
            rest = (rest[:m.start()] + "_ROUTES: tuple[object, ...] = ("
                    + ", ".join(names) + ("," if len(names) == 1 else "")
                    + ")" + rest[m.end():])
        else:
            rest = (rest.rstrip("\n") + "\n\n\n"
                    "#: The modules whose routes this app serves. Imported for\n"
                    "#: what importing them does — each registers its routes on\n"
                    "#: `app` — and named here so that is plainly a use.\n"
                    f"_ROUTES: tuple[object, ...] = ({args.module},)\n")

    rest = prune_imports(rest)

    # Tests that reached a moved name through `web`.
    alias = f"w_{args.module}"
    test_edits = {}
    for path in TESTS.glob("*.py"):
        t = path.read_text(encoding="utf-8")
        n2 = t
        # Only what web.py no longer has: a name it still imports back is
        # still reachable as `web.x`, and tests that import `web` inside a
        # function have nowhere for a new import to go.
        for n in moved_names - set(back):
            n2 = re.sub(rf"\bweb\.{re.escape(n)}\b", f"{alias}.{public(n)}", n2)
        if n2 != t:
            line = f"from pix.nas.webapp import {args.module} as {alias}\n"
            if line not in n2:
                if "from pix.nas import web\n" in n2:
                    n2 = n2.replace("from pix.nas import web\n",
                                    "from pix.nas import web\n" + line, 1)
                else:
                    n2 = line + n2 if not n2.startswith('"""') else n2
            test_edits[path] = n2

    print(f"moving {len(moved_names)} name(s) to {mod}: "
          f"{sum(b - a + 1 for a, b in cut)} lines")
    print("  imports:", ", ".join(sorted(import_lines.values())) or "-")
    print("  web.py keeps:", back or "-")
    print("  tests changed:", [p.name for p in test_edits] or "-")
    if args.dry:
        return 0
    target.write_text(prune_imports(new_text), encoding="utf-8")
    WEB.write_text(rest, encoding="utf-8")
    for p, t in test_edits.items():
        p.write_text(t, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
