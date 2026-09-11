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
    """One file, and what it said before the operation."""

    folder: str
    name: str
    decision: Decision | None


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


def record(who: str, summary: str, files: list[Before], *,
           reverts: str | None = None,
           path: Path | None = None) -> Operation:
    """Append one operation to the log and return it.

    Never raises for a logging failure: losing the ability to undo is bad, and
    failing the write the curator actually asked for because the *log* could not
    be written would be worse.
    """
    op = Operation(id=f"{int(time.time())}-{secrets.token_hex(3)}",
                   when=time.time(), who=who, summary=summary,
                   files=tuple(files), reverts=reverts)
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
    out: list[Operation] = []
    for line in reversed(lines):
        if len(out) >= limit:
            break
        op = _from_json(line)
        if op is not None:
            out.append(op)
    return out


def get(op_id: str, *, path: Path | None = None) -> Operation | None:
    """One operation by id."""
    for op in recent(limit=100_000, path=path):
        if op.id == op_id:
            return op
    return None


def undone(ops: list[Operation]) -> set[str]:
    """Which of these have already been reverted."""
    return {op.reverts for op in ops if op.reverts}


# --- serialization -----------------------------------------------------------

def _to_json(op: Operation) -> dict[str, Any]:
    return {
        "id": op.id, "when": round(op.when, 3), "who": op.who,
        "summary": op.summary,
        "reverts": op.reverts,
        "files": [{"folder": b.folder, "name": b.name,
                   "before": _decision_json(b.decision)} for b in op.files],
    }


def _decision_json(decision: Decision | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {"event": decision.event, "date_override": decision.date_override,
            "tags": list(decision.tags), "audience": list(decision.audience)}


def _from_json(line: str) -> Operation | None:
    try:
        raw: object = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    data = cast("dict[str, Any]", raw)
    files: list[Before] = []
    raw_files: object = data.get("files")
    for entry in cast("list[Any]", raw_files or []):
        if not isinstance(entry, dict):
            continue
        item = cast("dict[str, Any]", entry)
        files.append(Before(folder=str(item.get("folder") or ""),
                            name=str(item.get("name") or ""),
                            decision=_decision_from(item.get("before"))))
    try:
        return Operation(id=str(data["id"]), when=float(data.get("when") or 0),
                         who=str(data.get("who") or ""),
                         summary=str(data.get("summary") or ""),
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
    )
