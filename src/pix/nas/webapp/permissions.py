"""Who may do what: which fields each action writes, which of them a household
member may write, and the check a page asks before it offers a control.
"""

from __future__ import annotations

from pix.nas.webapp.app import Principal


ACT_WRITES: dict[str, tuple[str, ...]] = {
    "event": ("event",),
    "tags": ("tags",),
    "people": ("people",),
    "date": ("date_override",),
    "access": ("audience",),
    "stack": ("stacked_under",),
    "top": ("stacked_under",),
    "unstack": ("stacked_under",),
    "nostack": ("no_stack", "stacked_under"),
    "delete": ("deleted",),
    "restore": ("deleted",),
    # Takes a copy away and decides nothing, so it writes no field — and is
    # listed anyway, because a table of actions with one missing is a table
    # nobody can read as complete.
    "download": (),
    "purge": (),
    # Opens a page rather than writing anything here; the clips it makes are
    # written through their own routes, which are an administrator's.
    "splice": (),
}


#: What a household member gets.
#:
#: The family curates (§8) — *tagging and ranking are the whole point of the
#: app* — and the whole edit bar being an administrator's is the opposite of
#: that. What they do not get is the two that cannot be taken back by somebody
#: else noticing: **access**, which is the only control that can show a
#: photograph to a person who should not see it, and **purge**, which ends the
#: file. **Restore** is absent because `/history` is, and the deleted are not
#: in anybody else's view to find: deleting is theirs, undeleting is not.
HOUSEHOLD: frozenset[str] = frozenset(ACT_WRITES) - {"access", "purge",
                                                      "restore", "splice"}


#: The decision fields a household member may write, derived rather than
#: listed — a second list is a second thing to forget.
HOUSEHOLD_FIELDS: frozenset[str] = frozenset(
    f for act in HOUSEHOLD for f in ACT_WRITES[act])


def may(user: Principal, act: str) -> bool:
    """Whether this person gets this action."""
    return user.is_admin or act in HOUSEHOLD
