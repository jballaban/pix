"""The edit engine: a decision as a change to a sidecar, what one request is
allowed to write, how far a change reaches through a stack, and the line
History records for it.
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast, Sequence

from fastapi import HTTPException, status
from pydantic import BaseModel

from pix.nas import clips, decisions, history, index as ix, webroots
from pix.nas.decisions import Decision, Unset
from pix.nas.webapp.app import db, Principal, write_lock
from pix.nas.webapp.permissions import HOUSEHOLD_FIELDS
from pix.nas.webapp.vocab import KIND_WORDS, META_READERS


def writable(user: Principal, change: Change) -> None:
    """Refuse a decision that touches a field this person may not write.

    **Checked here rather than trusted from the page.** The bar renders only
    the actions `may` allows, but a bar is a suggestion and this is the rule:
    a household member posting `audience` directly gets a 403 whatever their
    browser was showing them.
    """
    if user.is_admin:
        return
    sent = set(recorded(change))
    # Half-name writes are not in the log's vocabulary — it records the name
    # each file ended up with, not the half that was asked for — so they have
    # to be named here or they would reach the archive unchecked. A field this
    # cannot see is a field nobody is refusing.
    if not isinstance(change.event_head, decisions.Unset):
        sent.add("event")
    if not isinstance(change.event_leaf, decisions.Unset):
        sent.add("event")
    # `add_tags` and the rest name the field they edit; one prefix strip puts
    # them back with it rather than needing their own list.
    touched = {f.removeprefix("add_").removeprefix("remove_") for f in sent}
    refused = sorted(touched - HOUSEHOLD_FIELDS)
    if refused:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"not yours to change: {', '.join(refused)}")


def one_kind(conn: sqlite3.Connection | None, change: Change,
              targets: Sequence[Target]) -> None:
    """Refuse a stack that would mix photographs with video — or hold video
    at all.

    **Video does not stack yet** (spec/clips.md §4). Whether one clip can
    speak for another is a question with no answer so far, and a stack is a
    fold: nothing should be taken out of the grid on a rule nobody has made.
    Stacks of video made before this are left alone and can still be taken
    apart — that write names no top, and returns before any of this.

    A stack says *these are the same shot, and this one speaks for the rest*.
    A photograph and a clip are not the same shot whatever else they share —
    same second, same camera, same name — and neither can stand in for the
    other, so folding one behind the other hides a thing nothing on screen
    represents.

    Here as well as in the page, because this is the scripting surface: a rule
    only the page holds is one the next client does not.

    Checked after the expansion, like the permission check above it and for
    the same reason: `_behind` brings in the rest of a stack, and those come
    along whether or not the request named them.

    One query rather than one per file, with the file being deferred to
    counted among them — which makes *is more than one kind of thing in play*
    a single question.
    """
    top = change.stacked_under
    if conn is None or isinstance(top, Unset) or not top:
        return
    folder, _, name = str(top).rpartition("/")
    keys = [f"{t.folder}{chr(10)}{t.name}" for t in targets]
    keys.append(f"{folder}{chr(10)}{name}")
    rows = conn.execute(
        "SELECT files.kind AS kind, COUNT(*) AS n FROM files "
        "JOIN json_each(:keys) "
        "  ON json_each.value = files.folder || char(10) || files.name "
        "GROUP BY files.kind", {"keys": json.dumps(keys)}).fetchall()
    if len(rows) < 2:
        if rows and rows[0]["kind"] == "video":
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "video cannot be stacked yet — whether one clip can speak "
                "for another has not been decided")
        return
    counts = " and ".join(
        f'{r["n"]} {KIND_WORDS.get(str(r["kind"]), str(r["kind"]))}'
        for r in sorted(rows, key=lambda r: -int(r["n"])))
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        f"a stack is one shot, and this one would be {counts} — "
        f"none of them can speak for the rest")


def clip_rules(conn: sqlite3.Connection | None, change: Change,
                targets: Sequence[Target]) -> list[Target]:
    """What binning and restoring mean where clips are involved
    (spec/clips.md §5).

    **A source cannot be binned while it has clips.** A clip is the source's
    bytes plus a range, and cannot outlive it — and *I only want the clips*
    is exactly why somebody would bin the source, so the one gesture would
    destroy what it was meant to keep. The answer is to hide it. Clips binned
    in the same request count as binned, so taking a whole video and its
    clips out at once is still one gesture.

    **Restoring a clip restores its source**, because it cannot exist without
    it — and is refused if it would come back over footage another clip has
    taken since.
    """
    out = list(targets)
    if conn is None or isinstance(change.deleted, Unset):
        return out
    named = {(t.folder, t.name) for t in targets}
    if change.deleted:
        for t in targets:
            if clips.source_of(t.name) is not None:
                continue
            live = [c for c in ix.clips_of(conn, t.folder, t.name)
                    if not c["deleted"] and (t.folder, c["name"]) not in named]
            if live:
                n = len(live)
                them = "it" if n == 1 else "them"
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"{t.name} has {n} clip{'s' if n != 1 else ''} cut from "
                    f"it — archive it instead, or bin {them} first")
        return out
    for t in targets:
        source = clips.source_of(t.name)
        row = ix.one(conn, t.folder, t.name) if source else None
        if source is None or row is None or row["clip_of"] is None:
            continue
        siblings = [(float(c["clip_in"]), float(c["clip_out"]))
                    for c in ix.clips_of(conn, t.folder, source)
                    if not c["deleted"] and c["name"] != t.name]
        try:
            clips.check(float(row["clip_in"]), float(row["clip_out"]),
                        siblings=siblings, duration=None)
        except clips.ClipError as e:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"{t.name} cannot come back: {e}") from e
        parent = ix.one(conn, t.folder, source)
        if (parent is not None and parent["deleted"]
                and (t.folder, source) not in named):
            out.append(Target(folder=t.folder, name=source))
            named.add((t.folder, source))
    return out


def records_for(targets: Sequence[Target]
                 ) -> dict[tuple[str, str], dict[str, Any]]:
    """The probed facts for a whole batch, fetched together.

    Read-only and independent, so they overlap safely; one that fails is
    simply absent, and the refresh that wanted it falls back to fetching its
    own.
    """
    if len(targets) < 2:
        return {}
    def one(t: Target) -> tuple[str, str, dict[str, Any] | None]:
        return t.folder, t.name, ix.record_of(webroots.META_DIR, t.folder, t.name)

    out: dict[tuple[str, str], dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=META_READERS) as pool:
        for folder, name, record in pool.map(one, targets):
            if record is not None:
                out[(folder, name)] = record
    return out


class DecideBody(BaseModel):
    """A curation decision about one master file.

    Every field is optional *and* nullable, and the two mean different things:
    omitting `event` leaves it alone, sending `null` clears it. Without that
    distinction a one-field UI gesture — tier this photo — would silently erase
    whatever else had been decided about it, so the wire format has to carry it.
    """

    folder: str
    name: str
    event: str | None = None
    #: Half a name each: set the event and keep whatever sub-event each file
    #: has, or set the sub-event and keep each file's event. One request, a
    #: different answer per file, which is why neither can be worked out by
    #: the caller.
    event_head: str | None = None
    event_leaf: str | None = None
    date_override: str | None = None
    tags: list[str] | None = None
    add_tags: list[str] = []
    remove_tags: list[str] = []
    people: list[str] | None = None
    add_people: list[str] = []
    remove_people: list[str] = []
    audience: list[str] | None = None
    add_audience: list[str] = []
    remove_audience: list[str] = []
    deleted: bool | None = None
    stacked_under: str | None = None
    no_stack: bool | None = None


class Target(BaseModel):
    """One file in a selection."""

    folder: str
    name: str


class DecideBulkBody(BaseModel):
    """One decision applied across a selection of files.

    The primitive both bulk gestures need. *Finishing an event* writes
    `tier: "none"` to everything left unpromoted (§8) — a few hundred files from
    one click. *Naming a range* writes `event` across every file the curator
    selected, which is what makes event assignment cheap: the range is evaluated
    once, here, and never stored as a rule.

    The selection is **explicit file names, not a query.** The client already has
    them, and sending them means what gets written is what the curator saw — a
    query re-evaluated server-side could pick up a file someone else just moved
    into the event.
    """

    files: list[Target]
    #: Ties the chunks of one gesture together in the log. Chunking is a fact
    #: about the transport, and without this it read as several edits.
    batch: str | None = None
    event: str | None = None
    #: Half a name each: set the event and keep whatever sub-event each file
    #: has, or set the sub-event and keep each file's event. One request, a
    #: different answer per file, which is why neither can be worked out by
    #: the caller.
    event_head: str | None = None
    event_leaf: str | None = None
    date_override: str | None = None
    tags: list[str] | None = None
    add_tags: list[str] = []
    remove_tags: list[str] = []
    people: list[str] | None = None
    add_people: list[str] = []
    remove_people: list[str] = []
    audience: list[str] | None = None
    add_audience: list[str] = []
    remove_audience: list[str] = []
    deleted: bool | None = None
    stacked_under: str | None = None
    no_stack: bool | None = None


@dataclass(frozen=True)
class Change:
    """One decision edit, with "leave it alone" distinct from "clear it"."""

    event: str | None | Unset = decisions.UNSET
    event_head: str | None | Unset = decisions.UNSET
    event_leaf: str | None | Unset = decisions.UNSET
    date_override: str | None | Unset = decisions.UNSET
    tags: Sequence[str] | None | Unset = decisions.UNSET
    add_tags: Sequence[str] = field(default_factory=tuple)
    remove_tags: Sequence[str] = field(default_factory=tuple)
    people: Sequence[str] | None | Unset = decisions.UNSET
    add_people: Sequence[str] = field(default_factory=tuple)
    remove_people: Sequence[str] = field(default_factory=tuple)
    audience: Sequence[str] | None | Unset = decisions.UNSET
    add_audience: Sequence[str] = field(default_factory=tuple)
    remove_audience: Sequence[str] = field(default_factory=tuple)
    deleted: bool | Unset = decisions.UNSET
    stacked_under: str | None | Unset = decisions.UNSET
    no_stack: bool | Unset = decisions.UNSET
    #: A clip's range. Never read from a decide request — a range is edited
    #: through the clip routes, which check it against its siblings — but
    #: carried here so those routes write through the same path as every
    #: other decision, and are logged and reverted the same way.
    clip_in: float | None | Unset = decisions.UNSET
    clip_out: float | None | Unset = decisions.UNSET


def change_from(body: DecideBody | DecideBulkBody) -> Change:
    """Which decision fields the request actually sent.

    Omitted and `null` mean different things, so a field nobody sent becomes
    `UNSET` and is left exactly as it was. Tags additionally distinguish
    *replace* from *add* and *remove*: applying a tag to a selection of 200
    files has to add to what each one already carries.
    """
    sent = body.model_fields_set
    def got(name: str) -> Any:
        return getattr(body, name) if name in sent else decisions.UNSET
    return Change(event=got("event"),
                   event_head=got("event_head"), event_leaf=got("event_leaf"),
                   date_override=got("date_override"), tags=got("tags"),
                   add_tags=tuple(body.add_tags),
                   remove_tags=tuple(body.remove_tags),
                   people=got("people"),
                   add_people=tuple(body.add_people),
                   remove_people=tuple(body.remove_people),
                   audience=got("audience"),
                   add_audience=tuple(body.add_audience),
                   remove_audience=tuple(body.remove_audience),
                   deleted=flag(got("deleted")),
                   stacked_under=got("stacked_under"),
                   no_stack=flag(got("no_stack")))


def did(change: Change, base: dict[str, Any],
         decision: Decision) -> dict[str, Any]:
    """What the operation did to *this* file.

    A half-name write resolves differently per file — *Sicily > Taormina*
    and *Sicily > Catania* both become *Family Trip > something* — so the
    log records the name each one ended up with rather than the half that was
    asked for. Reverting compares what it recorded against what the file says
    now, and a batch-wide *event_head* would match neither of them.
    """
    if (isinstance(change.event_head, Unset)
            and isinstance(change.event_leaf, Unset)):
        return base
    return {**base, "event": decision.event}


def recorded(change: Change) -> dict[str, Any]:
    """What an operation did, in the shape the log keeps it.

    Only the fields it actually sent — `UNSET` means *left alone*, and a log
    that could not tell that apart from *set to nothing* would revert fields
    the operation never touched.
    """
    out: dict[str, Any] = {}
    for name in ("event", "date_override", "tags", "people", "audience",
                 "deleted",
                 "stacked_under", "no_stack", "clip_in", "clip_out"):
        value: Any = getattr(change, name)
        if isinstance(value, Unset):
            continue
        out[name] = ([str(v) for v in cast("Sequence[str]", value)]
                     if isinstance(value, (list, tuple)) else value)
    for name in ("add_tags", "remove_tags", "add_people", "remove_people",
                 "add_audience", "remove_audience"):
        many = cast("Sequence[str]", getattr(change, name))
        if many:
            out[name] = [str(v) for v in many]
    return out


def flag(value: Any) -> bool | Unset:
    """A boolean decision, where `null` means *leave it alone*.

    For every other field `null` clears it, because every other field has a
    cleared state distinct from any value it could hold. A flag does not:
    there is no third thing between deleted and not, so rather than invent
    one, sending nothing and sending null say the same thing.

    `UNSET` has to survive untouched, not be coerced — `bool(UNSET)` is `True`,
    which turned every edit of any field into a deletion.
    """
    if isinstance(value, Unset) or value is None:
        return decisions.UNSET
    return bool(value)


def decide(folder: str, name: str, change: Change,
            *, conn: sqlite3.Connection | None = None,
            record: dict[str, Any] | None = None,
            commit: bool = True,
            creating: bool = False
            ) -> tuple[Decision | None, Decision, bool]:
    """Write one decision to master, then bring its index row up to date.

    **Sidecar first, index follows** (§4). If the sidecar write fails nothing
    happened; if the index update fails the decision still stands and a
    `pix2 index` catches up — drift is only ever "the index is behind", never
    "the record is wrong".

    The lock is taken per file, not per batch. Finishing a large event would
    otherwise hold it for a minute and stall every other curator's clicks, and
    there is nothing to gain: the files are disjoint, and where they are not,
    last-write-wins is the stated policy anyway (§8).

    `conn` lets a batch reuse one index connection; alone, it opens and closes
    its own.

    **`commit=False` puts the row in the caller's transaction.** Every row of
    a bulk edit was its own commit, and a commit is an fsync — to an index
    that lives on the share, measured at 8.7ms a row against 0.2ms when a
    batch shares one. `record` is the same idea for the meta file the refresh
    would otherwise fetch per row.

    The sidecar write stays per file and outside any of that. It is the
    record; the index is a projection of it, and a projection that rolls back
    is behind, which is the one direction drift is allowed to go.
    """
    media = master_file(folder, name, creating=creating)
    # The event this file is showing, which for most of the library is in its
    # own tags and not in a sidecar — a half-name write keeps the half it is
    # not replacing, and that half has to be the one on screen.
    #
    # Read only where a half-name write is actually in play: the meta is an
    # SMB round trip, and no other field needs it.
    half = not (isinstance(change.event_head, Unset)
                and isinstance(change.event_leaf, Unset))
    inherited = ix.inherited_event(
        record if record is not None
        else ix.record_of(webroots.META_DIR, folder, name)) if half else None
    with write_lock:
        try:
            was, decision = decisions.change(
                media, event=change.event,
                event_head=change.event_head, event_leaf=change.event_leaf,
                inherited_event=inherited,
                date_override=change.date_override, tags=change.tags,
                add_tags=change.add_tags, remove_tags=change.remove_tags,
                people=change.people,
                add_people=change.add_people,
                remove_people=change.remove_people,
                audience=change.audience,
                add_audience=change.add_audience,
                remove_audience=change.remove_audience,
                deleted=change.deleted,
                stacked_under=change.stacked_under,
                no_stack=change.no_stack,
                clip_in=change.clip_in, clip_out=change.clip_out)
        except decisions.DecisionError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
        except OSError as e:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"could not write the sidecar: {e}") from e

        # Nothing was written, so there is nothing for the row to catch up
        # with. The index is exactly as right as it was a moment ago, which is
        # the only promise it makes.
        if (decision == was if was is not None else decision.is_empty()):
            return was, decision, True
        indexed = False
        own = conn is None and webroots.DB_PATH.is_file()
        if own:
            conn = ix.open_rw(webroots.DB_PATH)
        if conn is not None:
            try:
                # Passed rather than left to the index's own constants, so the
                # path the decision was written to and the path the row is
                # rebuilt from are the same one.
                # The decision is the one just written, so the refresh
                # does not go back to the share to read it again.
                indexed = ix.refresh(conn, folder, name,
                                     meta_dir=webroots.META_DIR, master_dir=webroots.MASTER_DIR,
                                     decision=decision, record=record,
                                     commit=commit)
            except sqlite3.Error:
                indexed = False
            finally:
                if own:
                    conn.close()
    return was, decision, indexed


def behind(conn: sqlite3.Connection | None, view: ix.Filters,
            files: Sequence[Target], *, members: bool = True
            ) -> list[Target]:
    """The selection, plus whatever a folded view is hiding behind it.

    A stack shows one photograph and hides the rest, and the whole point of
    that is to work as though there is one file — so a decision made about
    what is on screen is a decision about all of them. Tag the stack and every
    take carries the tag; share it and every take is shared, so that taking it
    apart later leaves what somebody thought they had shared.

    **Both kinds, and this is what it got wrong.** It followed only the app's
    *guesses* and never a stack somebody had actually made, while claiming in
    this very docstring to be following "exactly the rule a real stack
    follows". It was not. The library therefore cascaded the grouping it had
    proposed and not the one that had been confirmed — which is the wrong way
    round, a stack you made being the stronger statement of the two. Sharing a
    stack shared one photograph of it, and unstacking months later produced
    files nobody could see.

    **Resolved before the first write, not after.** Refusing a guess writes
    `no_stack` to the photograph that speaks for it, and the index answers by
    recomputing that group — which would leave the others grouped behind a new
    leader, still unanswered, ready to be offered again tomorrow. Asked first,
    the refusal reaches all of them. The same hazard applies to a real stack:
    the write that moves a member out is the write that changes who its
    members are.

    Not when the view is already opened — inside a stack, or grouped by one.
    There the members are on screen and in the selection already, so following
    them again would be a second write to a file the curator can see they
    picked.

    **`members=False` for a write that moves a stack about**, because that one
    already has a cascade of its own and it runs afterwards: `_cascade` sends
    the members where the file that spoke for them went, and knows not to
    point one at itself. Following them here as well applied the head's new
    `stacked_under` to each of them — so promoting a take put the new top
    behind itself, and the whole stack disappeared from every listing at once.
    A guessed group is still followed, because nothing else follows it.
    """
    if conn is None:
        return list(files)
    # `within` opens one stack and `unfold` opens every stack in the view;
    # either way what is behind is in front of the curator already.
    opened = bool(view.within) or view.unfold
    if opened:
        return list(files)
    out = list(files)
    seen = {(t.folder, t.name) for t in files}
    for target in files:
        key = f"{target.folder}/{target.name}"
        # A confirmed stack always. A guessed one only where a guess is
        # behaving as a stack for this viewer, because where it is not, those
        # files are on screen in their own right and were not picked.
        hidden = list(ix.members(conn, key)) if members else []
        if view.folds_guesses:
            hidden += ix.proposed(conn, key)
        for folder, name in hidden:
            if (folder, name) in seen:
                continue
            seen.add((folder, name))
            out.append(Target(folder=folder, name=name))
    return out


def only_mine(user: Principal, conn: sqlite3.Connection | None,
          targets: Sequence[Target]) -> list[Target]:
    """Drop the ones this person may not write to, without saying which.

    **Silently**, which is the deliberate part. A cascade reaches files the
    curator never named — the rest of a stack — and a household member can be
    shown a stack whose other takes were never shared with them. Refusing the
    whole edit would make an ordinary tag fail for a reason they cannot see;
    naming what was skipped would tell them a photograph is there. So their
    decision lands on the files that are theirs and stops at the ones that are
    not, which is the same answer the grid already gives them.

    An admin has no scope and pays nothing for this.
    """
    if user.scope is None or not targets:
        return list(targets)
    look = conn if conn is not None else db()
    try:
        mine = ix.matching(look, ix.Filters(viewer=user.scope, unfold=True),
                           [(t.folder, t.name) for t in targets])
    finally:
        if conn is None:
            look.close()
    return [t for t in targets if (t.folder, t.name) in mine]


def cascade(conn: sqlite3.Connection | None,
             done: list[tuple[str, str]],
             top: str | None) -> list[history.Before]:
    """A stack's members follow the file that speaks for them.

    One rule, and it answers both directions. Stacked behind something else,
    and they go with it — stacks are flat, and the alternative is not a deeper
    stack but a stranded one, with the members a level down where no listing
    reaches them. Taken out of its stack, and they come out too: *unstack this*
    said of the file that speaks means the stack, not the one photograph, and
    leaving the others deferring to a file that defers to nobody would leave a
    stack nobody asked to keep.

    A file that speaks for nobody has nothing to cascade, which is why taking
    one photograph out of a stack takes only that one.

    Their previous values come back so the whole thing reverts as one gesture.
    Nothing recurses: this runs on every stacking write, so there is never more
    than one level to follow.
    """
    if conn is None:
        return []
    moved: list[history.Before] = []
    # Never the file being stacked *onto*: it is the one that speaks now, and
    # pointing it at itself hides it from every listing at once — a file behind
    # itself is behind something, so nothing shows it, and everything deferring
    # to it goes with it. Promoting one photograph out of a stack did exactly
    # that, and the whole stack vanished.
    written: set[str] = {f"{f}/{n}" for f, n in done}
    if top:
        written.add(top)
    for folder, name in done:
        for m_folder, m_name in ix.members(conn, f"{folder}/{name}"):
            if f"{m_folder}/{m_name}" in written:
                continue
            try:
                was, _, _ = decide(m_folder, m_name,
                                    Change(stacked_under=top), conn=conn)
            except HTTPException:
                continue
            moved.append(history.Before(m_folder, m_name, was,
                                        did={"stacked_under": top}))
    return moved


def promote(conn: sqlite3.Connection, top: str) -> list[history.Before]:
    """Take the file that is about to speak out of whatever it was behind.

    A top is a file nothing is behind. Stacking onto one that is itself stacked
    leaves a ring — it defers to the file now deferring to it — and a ring
    shows nowhere, because every file in it is behind something.
    """
    folder, _, name = top.partition("/")
    if not folder or not name:
        return []
    try:
        media = master_file(folder, name)
    except HTTPException:
        return []
    current = decisions.read(media)
    if current is None or not current.stacked_under:
        return []
    was, _, _ = decide(folder, name, Change(stacked_under=None), conn=conn)
    return [history.Before(folder, name, was, did={"stacked_under": None})]


def summary(change: Change) -> str:
    """What an operation did, in the words a person would use.

    Read months later off a list, so it says the value and the count — "gave
    family access to 312 files" is a thing you can recognise as the mistake
    you are looking for; "bulk edit" is not.

    The count is left as `{n}` for the log to fill in. A bulk edit arrives as
    several requests and no single one of them knows the total; only the reader,
    once it has put them back together, does.
    """
    files = "{n}"
    # Archiving is not giving somebody access, though it is written as an
    # audience — and *gave archived access to 3* is not how anybody says it.
    if list(change.add_audience) == [decisions.ARCHIVED]:
        return f"archived {files}"
    if list(change.remove_audience) == [decisions.ARCHIVED]:
        return f"took {files} out of the archive"
    if change.add_audience:
        return f"gave {', '.join(change.add_audience)} access to {files}"
    if change.remove_audience:
        return f"took {', '.join(change.remove_audience)} access "\
               f"from {files}"
    if change.add_people:
        return f"put {', '.join(change.add_people)} in {files}"
    if change.remove_people:
        return f"took {', '.join(change.remove_people)} out of {files}"
    if change.add_tags:
        return f"tagged {files} {', '.join(change.add_tags)}"
    if change.remove_tags:
        return f"untagged {', '.join(change.remove_tags)} on {files}"
    if not isinstance(change.no_stack, Unset):
        return (f"said {files} are not a stack" if change.no_stack
                else f"let {files} be suggested again")
    if not isinstance(change.stacked_under, Unset):
        return (f"stacked {files} under {change.stacked_under.rpartition('/')[2]}"
                if change.stacked_under else f"unstacked {files}")
    if not isinstance(change.deleted, Unset):
        return (f"deleted {files}" if change.deleted
                else f"restored {files}")
    if not isinstance(change.event, Unset):
        return (f"set the event on {files} to {change.event}"
                if change.event else f"cleared the event on {files}")
    if not isinstance(change.date_override, Unset):
        return (f"dated {files} {change.date_override}"
                if change.date_override
                else f"cleared the date override on {files}")
    return f"changed {files}"


def master_file(folder: str, name: str, *, creating: bool = False) -> Path:
    """Resolve a master path from untrusted URL/body components.

    Same guard as `_serve`, and needed more here because this one writes: `..`
    in either component would otherwise drop an `.xmp` anywhere on the share.
    A sidecar is refused as a target too — decisions are about media, and
    `a.jpg.xmp.xmp` is nobody's intent.

    **A clip is a path with no file at it** (spec/clips.md §2). It is found by
    its sidecar, beside a source that is there — or, while it is being made,
    by the source alone, which is what `creating` allows and nothing else
    does: a decision about a clip that does not exist is not a way to invent
    one.
    """
    target = (webroots.MASTER_DIR / folder / name).resolve()
    if webroots.MASTER_DIR.resolve() not in target.parents:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad path")
    if name.lower().endswith(".xmp"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "that is a sidecar, not a file")
    if not target.is_file() and not is_clip_path(target, creating=creating):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not in master")
    return target


def is_clip_path(target: Path, *, creating: bool = False) -> bool:
    """Whether `target` names a clip whose source is in master."""
    source = clips.source_of(target.name)
    if source is None or not (target.parent / source).is_file():
        return False
    return creating or decisions.sidecar_path(target).is_file()
