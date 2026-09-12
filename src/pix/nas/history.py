"""What was changed, and how to put it back (spec/nas-app.md §8).

A bulk edit can touch several hundred files from one click. *I just gave the
children access to three hundred photographs* has no other cure — the decisions
are correct, individually, in three hundred separate sidecars, and nothing in
the archive remembers that they used to say something else. So this does.

### Cherry-picked revert, not an undo stack

A stack assumes one writer. This app is explicitly last-write-wins with several
curators (§8), so *undo my last operation* can quietly overwrite what somebody
else did after you. Reverting a **named** operation says exactly which files it
touches and what it puts back, and stays truthful when two people are working.

A revert is itself recorded, so it can be reverted in turn — which is the redo
nobody has to build.

### Why a log is allowed here

[§4](../../spec/nas-app.md) keeps metadata out of a central file because losing
it would leave the archive anonymous. This is not that. Every decision still
lives in the `.xmp` beside its file; losing `operations.jsonl` costs the
*ability to undo*, not a single fact about a single photograph. It is
append-only, so a crash mid-write costs at most the last line.

The previous values are recorded in full rather than as a diff. A diff has to be
interpreted against whatever the file says now, and *now* may already have moved
— the whole reason you are reverting.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pix.nas.const import OPERATIONS_FILE
from pix.nas.decisions import Decision

#: How many operations the history page offers. Older ones stay in the file —
#: it is the record — but a list nobody scrolls is not a safety net.
RECENT: int = 200


@dataclass(frozen=True)
class Before:
    """One file, what it said before the operation, and what the operation did
    to it.

    **Both**, because the previous value alone cannot be safely undone: it says
    what the file used to be, not which parts of that this operation is
    answerable for. Writing all of it back undoes every later edit to the same
    file, including ones to fields this never touched.

    Per file rather than per operation, because a revert does something
    different to each one — it restores each file's own previous value — and
    reverting a revert has to work like reverting anything else.
    """

    folder: str
    name: str
    decision: Decision | None
    #: The fields this operation set on this file, and the values it set them
    #: to. Empty for anything recorded before this was kept, which is why those
    #: cannot be put back: without knowing what an operation set, there is no
    #: way to tell whether it is still in effect.
    did: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class Operation:
    """One bulk edit, with everything needed to undo it."""

    id: str
    when: float
    who: str
    summary: str
    files: tuple[Before, ...] = field(default_factory=tuple)
    #: Set when this operation undid another, so the page can say so and a
    #: reverted operation is not offered for reverting twice.
    reverts: str | None = None
    #: How many files it touched. Usually `len(files)`, but a purge records
    #: none — there is nothing to put back — and still did something to a
    #: number of them.
    n: int = 0

    def revertable(self) -> bool:
        """Whether anything here knows what it did well enough to undo it."""
        return any(f.did for f in self.files)


def record(who: str, summary: str, files: list[Before], *,
           reverts: str | None = None, batch: str | None = None,
           count: int | None = None,
           path: Path | None = None) -> Operation:
    """Append one operation to the log and return it.

    Never raises for a logging failure: losing the ability to undo is bad, and
    failing the write the curator actually asked for because the *log* could not
    be written would be worse.

    `batch` makes several appends **one operation**. A bulk edit is chunked
    into requests of a hundred so that each one stays short, and that is a fact
    about the transport which had been leaking into the log: naming an event
    across four hundred files read as four identical entries, and putting it
    back meant reverting each of them. Sharing an id groups them on the way out
    without giving up the append-only write, where a crash costs one line.

    `count` is for operations that touch files they cannot offer back — a purge
    has no previous value to record, but it still did something to a number of
    files.
    """
    op = Operation(id=batch or f"{int(time.time())}-{secrets.token_hex(3)}",
                   when=time.time(), who=who, summary=summary,
                   files=tuple(files), reverts=reverts,
                   n=len(files) if count is None else count)
    target = path if path is not None else OPERATIONS_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_to_json(op), separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass
    return op


def recent(limit: int = RECENT, *, path: Path | None = None) -> list[Operation]:
    """The most recent operations, newest first.

    A malformed line is skipped rather than fatal: the log is a convenience,
    and one bad line should cost one entry rather than the whole history.
    """
    target = path if path is not None else OPERATIONS_FILE
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    # Grouped by id rather than by adjacency: two curators working at once
    # interleave their lines, and a gesture is still one gesture when somebody
    # else's landed in the middle of it.
    merged: dict[str, dict[str, Any]] = {}
    for line in lines:
        data = _parse(line)
        if data is None:
            continue
        first = merged.get(data["id"])
        if first is None:
            merged[data["id"]] = data
            continue
        first["files"].extend(data["files"])
        first["n"] += data["n"]

    out: list[Operation] = []
    for data in reversed(list(merged.values())):
        if len(out) >= limit:
            break
        op = _build(data)
        if op is not None:
            out.append(op)
    return out


def get(op_id: str, *, path: Path | None = None) -> Operation | None:
    """One operation by id."""
    for op in recent(limit=100_000, path=path):
        if op.id == op_id:
            return op
    return None


# --- putting one operation back ----------------------------------------------
#
# A revert is the **inverse of what the operation did**, applied only where that
# is still what the file says.
#
# Writing the recorded previous value back wholesale was simpler and wrong in
# two ways at once. Set a date on a file and then an event, revert the date, and
# the event went too — it was never this operation's to undo. Set an event
# twice and revert the first, and it "succeeded", quietly replacing the second
# curator's answer with a value nobody currently held.
#
# So each operation carries what it set, and each file is checked against it.
# Still in effect, and it is put back. Moved on, and it is left alone and
# counted — because at three hundred files some will always have moved, and
# refusing the whole operation for one of them would make revert useless
# exactly when it matters most.

#: The scalar decisions: one value, replaced outright.
_SCALARS: tuple[str, ...] = ("event", "date_override", "deleted")

#: The multi-valued ones, as (whole-list key, add key, remove key, attribute).
_LISTS: tuple[tuple[str, str, str, str], ...] = (
    ("tags", "add_tags", "remove_tags", "tags"),
    ("audience", "add_audience", "remove_audience", "audience"),
)


def in_effect(did: dict[str, Any], current: Decision | None) -> bool:
    """Whether `current` still shows what this operation did.

    Not *is it unchanged* — only the parts this operation is answerable for.
    Another field having moved since is none of its business, and is exactly
    the case the old wholesale revert got wrong.
    """
    if not did:
        return False
    now = current or Decision()
    for name in _SCALARS:
        if name in did and getattr(now, name) != _norm(did[name], name):
            return False
    for whole, add, remove, attr in _LISTS:
        have: tuple[str, ...] = getattr(now, attr)
        if whole in did:
            if tuple(sorted(did[whole] or ())) != tuple(sorted(have)):
                return False
        if any(v not in have for v in did.get(add) or ()):
            return False
        if any(v in have for v in did.get(remove) or ()):
            return False
    return True


def undo(did: dict[str, Any], before: Decision | None) -> dict[str, Any]:
    """The arguments to `decisions.change` that put this operation back.

    Scoped to what it touched, and for the multi-valued fields to the exact
    values it touched: *gave family access* is undone by taking `family` away,
    not by restoring the whole list — somebody may have added `james` since,
    and that grant is not this operation's to remove.

    A value the file **already had** is not taken away again. Adding a tag to a
    file that carried it did nothing, and undoing nothing by removing it would
    be this operation destroying a decision it never made.
    """
    was = before or Decision()
    out: dict[str, Any] = {}
    for name in _SCALARS:
        if name in did:
            out[name] = getattr(was, name)
    for whole, add, remove, attr in _LISTS:
        had: tuple[str, ...] = getattr(was, attr)
        if whole in did:
            out[whole] = list(had)
            continue
        put_back = [v for v in did.get(remove) or () if v in had]
        take_away = [v for v in did.get(add) or () if v not in had]
        if put_back:
            out[f"add_{attr}"] = put_back
        if take_away:
            out[f"remove_{attr}"] = take_away
    return out


def _norm(value: object, name: str) -> object:
    """A stored value as the decision would hold it."""
    if name == "deleted":
        return bool(value)
    return str(value) if value else None


def undone(ops: list[Operation]) -> set[str]:
    """Which of these have already been reverted."""
    return {op.reverts for op in ops if op.reverts}


# --- serialization -----------------------------------------------------------

def _to_json(op: Operation) -> dict[str, Any]:
    return {
        "id": op.id, "when": round(op.when, 3), "who": op.who,
        "summary": op.summary, "n": op.n,
        "reverts": op.reverts,
        "files": [{"folder": b.folder, "name": b.name,
                   "before": _decision_json(b.decision), "did": b.did}
                  for b in op.files],
    }


def _decision_json(decision: Decision | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {"event": decision.event, "date_override": decision.date_override,
            "tags": list(decision.tags), "audience": list(decision.audience),
            "deleted": decision.deleted}


def _render(summary: str, n: int) -> str:
    """Fill in how many files an operation touched.

    Stored with a `{n}` where the count goes, because a bulk edit arrives as
    several requests and only the reader knows the total. Anything written
    without a placeholder — a purge, a revert — comes back as it was written,
    which is also what makes every line recorded before this still read.
    """
    if "{n}" not in summary:
        return summary
    return summary.replace("{n}", f"{n:,} file" + ("s" if n != 1 else ""))


def _parse(line: str) -> dict[str, Any] | None:
    """One log line as plain data, ready to be merged with its siblings."""
    try:
        raw: object = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    data = cast("dict[str, Any]", raw)
    if not data.get("id"):
        return None
    raw_files: object = data.get("files")
    kept: list[dict[str, Any]] = [
        cast("dict[str, Any]", f) for f in cast("list[Any]", raw_files or [])
        if isinstance(f, dict)]
    data["files"] = kept
    # Absent in anything written before operations counted their own files.
    data["n"] = int(data.get("n") or len(kept))
    return data


def _build(data: dict[str, Any]) -> Operation | None:
    files: list[Before] = []
    for entry in cast("list[Any]", data.get("files") or []):
        item = cast("dict[str, Any]", entry)
        did = item.get("did")
        files.append(Before(folder=str(item.get("folder") or ""),
                            name=str(item.get("name") or ""),
                            decision=_decision_from(item.get("before")),
                            did=cast("dict[str, Any]",
                                     did if isinstance(did, dict) else {})))
    try:
        n = int(data.get("n") or len(files))
        return Operation(id=str(data["id"]), when=float(data.get("when") or 0),
                         who=str(data.get("who") or ""),
                         summary=_render(str(data.get("summary") or ""), n),
                         n=n,
                         files=tuple(files),
                         reverts=(str(data["reverts"])
                                  if data.get("reverts") else None))
    except (KeyError, TypeError, ValueError):
        return None


def _decision_from(raw: object) -> Decision | None:
    if not isinstance(raw, dict):
        return None
    d = cast("dict[str, Any]", raw)
    return Decision(
        event=(str(d["event"]) if d.get("event") else None),
        date_override=(str(d["date_override"])
                       if d.get("date_override") else None),
        tags=tuple(str(t) for t in cast("list[Any]", d.get("tags") or [])),
        audience=tuple(str(a) for a in cast("list[Any]", d.get("audience") or [])),
        # Absent in anything written before deletion existed, which reads as
        # not deleted — which is what those files were.
        deleted=bool(d.get("deleted")),
    )
