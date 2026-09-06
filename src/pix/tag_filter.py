"""The shared tag-filter grammar — `{tag:val1,val2}` and `tag:val1,val2`.

One grammar, two spellings of the same thing (spec/tags.md → Template
grammar):

- **Braced, inside a template** — `{rating:3,4,5}` filters *and* produces a
  folder level. Files that don't match render to the `(filtered)` sentinel
  (see `pix.special_folders`), because organize must account for every file.
- **Bare, standalone** — `rating:3,4,5` in an [export](../../spec/export.md)
  distribution's `filter:` key selects files without shaping the output;
  non-matching files simply don't appear (no `(filtered)` folder). Keeping
  `filter:` separate from `template:` is what lets a distribution select
  `rating:5` without materializing a `5/` folder.

The value-list grammar is identical in both spellings, and lives here so the
two can never drift. Rules:

- `tag:v1,v2` — the file's effective value must be one of the listed values.
- `null` — the reserved keyword for "no effective value" (an untagged file).
  It renders as the `(null)` folder, but the user *types* the bare word.
- Values compare case-insensitively; whitespace around them is trimmed.
- A bare filter ANDs its clauses, separated by `;`:
  `rating:4,5; event:beach trip`. Semicolon (not whitespace) is the
  separator so event names may contain spaces. Values still can't contain
  `,` or `;` — same limitation the braced form has always had for `,`.

Negation (`!`) is specified in spec/tags.md but **deliberately not
implemented yet** — every list is an inclusion list. `parse` rejects `!`
with a pointed message rather than silently treating it as a literal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# The tag vocabulary shared by every template-consuming op (organize,
# checkout, export). `date`/`time` are deliberately absent — they're
# rejected with their own message in `pix.organize.parse_template`.
ALLOWED_TOKENS: frozenset[str] = frozenset(
    {"year", "month", "day", "event", "rating"}
)

# The reserved query-language word for "no effective value". Renders to
# `special_folders.NULL_FOLDER`; the user types the bare keyword.
NULL_KEYWORD: str = "null"

# Separates the clauses of a bare filter expression (see module docstring).
CLAUSE_SEPARATOR: str = ";"


class FilterError(Exception):
    """A malformed filter expression or `{tag:...}` value list."""


def accepts(values: frozenset[str], value: str | None) -> bool:
    """Does an effective tag `value` fall inside an inclusion list?

    `None` (untagged) matches only when the list names `null`.
    """
    if value is None:
        return NULL_KEYWORD in values
    return value.casefold() in values


@dataclass(frozen=True)
class Clause:
    """One `tag:v1,v2` term — a tag and its inclusion list."""

    tag: str
    values: frozenset[str]

    def accepts(self, value: str | None) -> bool:
        return accepts(self.values, value)


@dataclass(frozen=True)
class Filter:
    """A parsed bare filter expression: clauses ANDed together.

    No clauses means "match everything" (an absent or empty `filter:`).
    """

    raw: str
    clauses: tuple[Clause, ...]

    def matches(self, values: Mapping[str, str | None]) -> bool:
        """Test a file's effective values (as `organize.compute_values`)."""
        return all(c.accepts(values.get(c.tag)) for c in self.clauses)


def parse_values(tag: str, spec: str) -> frozenset[str]:
    """Parse the `v1,v2` half of a clause into a case-folded inclusion list.

    Shared by the braced (template) and bare (export `filter:`) spellings,
    so the two can't drift. Raises `FilterError` on an empty list, an empty
    item (`3,,4`, a trailing comma), or a `!` negation prefix.
    """
    if not spec.strip():
        raise FilterError(
            f"filter on {{{tag}}} lists no values — write `{tag}:value` "
            f"(or drop the `:` to enumerate every value)"
        )

    values: set[str] = set()
    for item in spec.split(","):
        value = item.strip()
        if not value:
            raise FilterError(
                f"empty value in the filter on {{{tag}}} — check for a "
                f"doubled or trailing `,` in {spec!r}"
            )
        if value.startswith("!"):
            raise FilterError(
                f"negation (`{value}`) isn't supported yet — filters are "
                f"inclusion lists for now. List the values you want."
            )
        values.add(value.casefold())
    return frozenset(values)


def parse_tag(tag_spec: str) -> str:
    """Normalize and validate a tag name from either spelling."""
    tag = tag_spec.strip().lower()
    if not tag:
        raise FilterError("filter clause names no tag — write `tag:value`")
    if tag not in ALLOWED_TOKENS:
        raise FilterError(
            f"unknown tag {tag!r} in filter; valid tags: "
            f"{sorted(ALLOWED_TOKENS)}"
        )
    return tag


def parse(expr: str) -> Filter:
    """Parse a bare filter expression: `rating:4,5; event:beach trip`.

    Empty (or whitespace-only) yields a filter that matches everything.
    Raises `FilterError` on braces (that's template syntax), a clause with
    no `:`, an unknown tag, a bad value list, or a repeated tag.
    """
    raw = expr.strip()
    if not raw:
        return Filter(raw=expr, clauses=())

    if "{" in raw or "}" in raw:
        raise FilterError(
            "filter expressions are unbraced — `{}` is template syntax that "
            f"produces a folder. Write `rating:4,5`, not {raw!r}."
        )

    clauses: list[Clause] = []
    seen: set[str] = set()
    for clause_str in raw.split(CLAUSE_SEPARATOR):
        if not clause_str.strip():
            raise FilterError(
                f"empty clause in filter {raw!r} — check for a doubled or "
                f"trailing `{CLAUSE_SEPARATOR}`"
            )
        tag_spec, sep, value_spec = clause_str.partition(":")
        if not sep:
            raise FilterError(
                f"filter clause {clause_str.strip()!r} has no `:` — write "
                f"`tag:value` (clauses are separated by "
                f"`{CLAUSE_SEPARATOR}` and ANDed together)"
            )
        tag = parse_tag(tag_spec)
        if tag in seen:
            raise FilterError(
                f"tag {tag!r} appears in more than one clause of {raw!r}; "
                f"list its values together as `{tag}:a,b`"
            )
        seen.add(tag)
        clauses.append(Clause(tag=tag, values=parse_values(tag, value_spec)))

    return Filter(raw=expr, clauses=tuple(clauses))
