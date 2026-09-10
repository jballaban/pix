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
  `,`, `;` or `|` — same limitation the braced form has always had for `,`.
- **Value buckets** — `{rating:1,2|3,4,5}` splits one tag's values into
  `|`-separated *groups*, each rendering a single folder (`1_2`, `3_4_5`).
  Buckets are **checkout-only** (see spec/tag-editing.md → Value buckets):
  they exist so a coarse pass can see and edit one folder per group without
  flattening the finer values inside it, which only the folder-shuffle UI
  needs. organize, export templates, and bare filters all reject `|`.

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

# Separates the value *groups* of a bucketed `{tag:a,b|c,d}` level. Checkout
# only — every other consumer rejects it.
BUCKET_SEPARATOR: str = "|"


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


@dataclass(frozen=True)
class Bucket:
    """One `|`-separated value group of a bucketed `{tag:a,b|c,d}` level.

    Carries the values twice on purpose: `values` keeps them **as typed**
    and in order, because checkout writes `values[0]` when a file is
    dragged into this bucket's folder (and writing a case-folded event
    name would corrupt it); `folded` is the case-insensitive membership
    set, matching how every other filter compares.
    """

    values: tuple[str, ...]
    folded: frozenset[str]

    def accepts(self, value: str | None) -> bool:
        return accepts(self.folded, value)


def _parse_items(tag: str, spec: str) -> tuple[str, ...]:
    """Split the `v1,v2` half of a clause into trimmed values, in order.

    The shared half of `parse_values` and `parse_buckets`. Raises
    `FilterError` on an empty list, an empty item (`3,,4`, a trailing
    comma), a `!` negation prefix, or a stray `|` (buckets are parsed by
    `parse_buckets`, and every non-checkout consumer rejects them).
    """
    if not spec.strip():
        raise FilterError(
            f"filter on {{{tag}}} lists no values — write `{tag}:value` "
            f"(or drop the `:` to enumerate every value)"
        )

    items: list[str] = []
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
        if BUCKET_SEPARATOR in value:
            raise FilterError(
                f"value buckets (`{BUCKET_SEPARATOR}`) are only supported in "
                f"`pix tag checkout` templates — a bucket renders one folder "
                f"per group, which only the folder-shuffle UI can reverse. "
                f"Write `{tag}:a,b` here."
            )
        items.append(value)
    return tuple(items)


def parse_values(tag: str, spec: str) -> frozenset[str]:
    """Parse the `v1,v2` half of a clause into a case-folded inclusion list.

    Shared by the braced (template) and bare (export `filter:`) spellings,
    so the two can't drift. Raises `FilterError` on an empty list, an empty
    item (`3,,4`, a trailing comma), or a `!` negation prefix.
    """
    return frozenset(v.casefold() for v in _parse_items(tag, spec))


def parse_buckets(tag: str, spec: str) -> tuple[Bucket, ...]:
    """Parse a bucketed value spec — `1,2|3,4,5` — into ordered groups.

    Only called for a spec that contains `|`, and only by
    `organize.parse_template` when the caller allows buckets (checkout).
    Beyond the per-value rules of `_parse_items`, rejects:

    - an empty group (`1,2|`, `|3`, `1||2`);
    - `null` inside a group — the workspace root already means "untagged"
      (checkout's stop-at-the-first-gap rule), and "assign the group's
      first value" would mean *clear the tag* for a null-leading group,
      which checkout deliberately can't do yet;
    - a value claimed by two groups, which would give the file two homes.
    """
    groups: list[Bucket] = []
    claimed: dict[str, int] = {}
    parts = spec.split(BUCKET_SEPARATOR)
    for index, part in enumerate(parts):
        if not part.strip():
            raise FilterError(
                f"empty bucket in the filter on {{{tag}}} — check for a "
                f"doubled, leading, or trailing `{BUCKET_SEPARATOR}` in "
                f"{spec!r}"
            )
        items = _parse_items(tag, part)
        folded: set[str] = set()
        for value in items:
            key = value.casefold()
            if key == NULL_KEYWORD:
                raise FilterError(
                    f"`{NULL_KEYWORD}` can't go in a bucket on {{{tag}}} — "
                    f"untagged files already rest at the checkout root, and "
                    f"dropping a file into a bucket assigns its first value, "
                    f"which can't be `{NULL_KEYWORD}`."
                )
            if key in claimed and claimed[key] != index:
                raise FilterError(
                    f"value {value!r} appears in two buckets on {{{tag}}} in "
                    f"{spec!r} — a file with that value would have two "
                    f"folders to live in. Buckets must not overlap."
                )
            claimed[key] = index
            folded.add(key)
        groups.append(Bucket(values=items, folded=frozenset(folded)))

    if len(groups) < 2:
        raise FilterError(
            f"a bucketed filter on {{{tag}}} needs at least two groups "
            f"separated by `{BUCKET_SEPARATOR}` (got {spec!r})"
        )
    return tuple(groups)


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

    if BUCKET_SEPARATOR in raw:
        raise FilterError(
            f"a filter selects files, it doesn't group them, so "
            f"`{BUCKET_SEPARATOR}` has no meaning here. List every value you "
            f"want (`rating:1,2,3`); value buckets are a "
            f"`pix tag checkout` template feature."
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
