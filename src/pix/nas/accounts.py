"""Who can sign in, what roles they hold, and the sessions that remember it.

Three things live here that the rest of the app treats as given: the account
store, who may act as the administrator, and session tokens.

### Why a file, and why this one is allowed

[§4](../../spec/nas-app.md) rejects a central database *of record* — lose it and
the archive is anonymous. Accounts are not that. They are **configuration**: lose
`users.json` and you lose the logins, not a single photograph or a single
decision about one. Every decision still lives in the `.xmp` beside its file, and
an audience grant names a person who could simply be recreated. So the rule that
kept metadata out of a database does not apply to knowing who `kids` is.

It sits on the share rather than in the container so it survives an image swap,
and it is written temp-then-rename like every other record pix keeps.

### Administrator is something you switch on

There is no administrator *account*. Signing out of yourself and into `admin`
and back was the normal way the app was used, and every switch was a chance to
share something as the wrong one. So the people in `ADMINISTRATORS` sign in as
themselves — and see what is shared with them, like anybody — and turn
**Administrator** on from their menu when they mean to: everything visible,
every action. Off again, and they are a household member.

Turning it on asks for **a second password**, the administrator's, separate
from the login. A signed-in tablet left on the kitchen table is a household
member's session, not the archive's keys. The switch is a signed cookie of its
own (`mint_elevated`), bound to the person, short-lived, and signed with the
administrator password's hash mixed in — so changing that password turns the
switch off everywhere at once.

**Hard-coded**, so no edit to a file on a share can make somebody an
administrator, and no deletion can leave nobody able to be one. An
administrator's own login falls back to the shipped password while the store
has none for them — the way back in on a first run — and both passwords ship
as a **hash of a known initial value**, not as plaintext: the repository has a
remote, and a password committed to it is a published password. The app says
so until each is changed.

### Sessions

A signed cookie, because HTTP Basic cannot log out — browsers cache the
credentials and offer no way to clear them, which makes *switch to the admin
account and back* impossible. That is the whole reason this exists.

Basic auth is still **accepted** for scripting, but never *challenged* for:
without a `WWW-Authenticate` header a browser will not cache anything, so the
cookie stays the only thing the browser holds.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pix.markers import SIDECAR_TMP_SUFFIX
from pix.nas import auth
from pix.nas.const import ACCOUNTS_FILE
from pix.nas.decisions import ARCHIVED

#: Who may switch Administrator on. Hard-coded — see the module docstring.
ADMINISTRATORS: frozenset[str] = frozenset({"james"})

#: What the switch is called, and a name no account or group may take: a
#: group called `administrator` would read as a way to make somebody one, and
#: it is not.
ADMINISTRATOR: str = "administrator"

#: The account that was the administrator before it became a switch. Kept
#: reserved so nobody is quietly given its name — and read once, on load, for
#: the password it carried (`load`).
FORMER_ADMIN: str = "admin"

#: Names no account or group may take. The two above; and `archived`, because
#: it is the audience that takes a file out of every view (spec/clips.md §3) —
#: a login called that would be granted exactly the files nobody is meant to
#: see.
RESERVED: frozenset[str] = frozenset({ADMINISTRATOR, FORMER_ADMIN, ARCHIVED})


def canonical(name: str) -> str:
    """The one spelling of a name.

    Names are **case-insensitive**: `Kid`, `kid` and `KID` are one person,
    and typing the wrong one at a login should not fail. Folded at every
    boundary rather than only at sign-in, because a grant is compared
    against a name — a mismatch there reads as *not shared*, which looks
    exactly like a correctly-kept secret rather than like a bug.

    `casefold` and not `lower`: it is the comparison the standard library
    provides for exactly this, and it handles the scripts `lower` gets
    wrong.
    """
    return name.strip().casefold()

#: scrypt hash of the shipped password — the literal string `admin` — for both
#: the Administrator switch and an administrator's first login. Deliberately a
#: hash and not the password: this file has a git remote, and a password in a
#: repository is a published password. Changing either in the app writes a new
#: hash to the store, which then wins.
INITIAL_HASH: str = (
    "64da7a6c950eb208886df87cdff41d8f$"
    "6760ed314f65cecece19ffafebb199a7eeb7d0401ba4b7cb4b5610602cc7b200"
)

#: How long a signed session stays valid. Long enough that a household is not
#: forever logging in, short enough that a forgotten tablet is not forever
#: logged in.
SESSION_DAYS: int = 30

COOKIE: str = "pix2_session"

#: The Administrator switch, as a cookie of its own beside the session.
ELEVATED_COOKIE: str = "pix2_admin"

#: How long Administrator stays on before asking again. A working session,
#: not a month: on is the exception, and one left on is the takeover the
#: second password exists to stop.
ELEVATED_HOURS: int = 12


@dataclass(frozen=True)
class Account:
    """One person who can sign in."""

    name: str
    password: str = ""
    groups: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class Store:
    """The account file's contents.

    `groups` is kept explicitly rather than derived from what people hold, so
    a group can exist before anyone is in it — otherwise creating `tv` would
    be impossible until something had already been shared with it.

    *Group* rather than *role*: a role is something you do, and these are
    simply sets of people — `family`, `parents`, `tv`. Naming them for what
    they are keeps the access model one idea rather than two.
    """

    users: dict[str, Account] = field(default_factory=lambda: {})
    groups: list[str] = field(default_factory=lambda: [])
    secret: str = ""

    #: The audience almost everything ends up with. Named so the grid can
    #: stay quiet about it: if nine files in ten say `family`, printing
    #: `family` on nine thumbnails in ten is noise that tells you nothing.
    #: A setting rather than a hard-coded name, because which audience is
    #: usual is a fact about a household, not about the software.
    usual: str = ""

    #: The administrator password's hash — what turns the switch on. Empty
    #: until it is changed, which means the shipped one (`INITIAL_HASH`).
    elevation: str = ""

    def grants(self, name: str) -> frozenset[str]:
        """Every name a grant could use to reach this person.

        Themselves plus their groups, because a share names one or the other
        and the access check is not allowed to care which.
        """
        who = canonical(name)
        account = self.users.get(who)
        # Never a reserved name, whatever the file says. The page refuses to
        # write one, but `users.json` is a file on a share and can be edited
        # by hand — and a person who answered to `archived` would be granted
        # exactly the files nobody is meant to see.
        return frozenset({who, *(account.groups if account else ())}) - RESERVED

    def audiences(self) -> list[str]:
        """Everything that can be shared with — people and groups, never
        the administrator, which sees everything already."""
        return sorted({*self.users, *self.groups} - RESERVED)


def load(path: Path | None = None) -> Store:
    """Read the account store, or an empty one if it does not exist yet.

    A missing file is the normal first-run state, not an error: the app has to
    come up so somebody can sign in as `admin` and create the accounts.
    """
    target = path if path is not None else ACCOUNTS_FILE
    try:
        raw: object = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Store()
    if not isinstance(raw, dict):
        return Store()
    data = cast("dict[str, Any]", raw)

    users: dict[str, Account] = {}
    raw_users: object = data.get("users")
    for name, value in cast("dict[str, Any]", raw_users or {}).items():
        if not isinstance(value, dict):
            continue
        entry = cast("dict[str, Any]", value)
        # `roles` is the name this field had before; a store written by an
        # older build should keep working rather than quietly losing
        # everybody's memberships.
        raw_groups: object = entry.get("groups", entry.get("roles"))
        who = canonical(str(name))
        users[who] = Account(
            name=who,
            password=str(entry.get("password") or ""),
            groups=tuple(sorted({canonical(str(g)) for g
                                 in cast("list[Any]", raw_groups or [])})),
        )
    raw_all: object = data.get("groups", data.get("roles"))
    groups = sorted({canonical(str(g))
                     for g in cast("list[Any]", raw_all or [])
                     if str(g).strip()})
    # The old built-in account goes, and its password becomes the switch's:
    # it is the password that already meant *administrator* in this house,
    # and dropping it would put the shipped one back. Written out the next
    # time anything is saved.
    former = users.pop(FORMER_ADMIN, None)
    elevation = str(data.get("elevation") or "") or (
        former.password if former else "")
    return Store(users=users, groups=groups,
                 usual=canonical(str(data.get("usual") or "")),
                 secret=str(data.get("secret") or ""),
                 elevation=elevation)


def save(store: Store, path: Path | None = None) -> None:
    """Write the store atomically.

    Temp-then-rename, so a kill mid-write cannot leave a half-parsed file — the
    failure mode there is *nobody can sign in*, which on a NAS in a cupboard is
    a genuinely bad afternoon.
    """
    target = path if path is not None else ACCOUNTS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "users": {name: {"password": a.password, "groups": list(a.groups)}
                  for name, a in sorted(store.users.items())},
        "groups": sorted(set(store.groups)),
        "usual": store.usual,
        "secret": store.secret,
        "elevation": store.elevation,
    }
    tmp = target.with_name(target.name + SIDECAR_TMP_SUFFIX)
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def ensure_secret(store: Store, path: Path | None = None) -> str:
    """The key that signs sessions, minting and persisting one on first use.

    Persisted rather than generated per process, or every restart would sign
    everybody out — which for a container that restarts on a schedule means the
    login is never remembered at all.
    """
    if not store.secret:
        store.secret = secrets.token_hex(32)
        save(store, path)
    return store.secret


# --- authentication ----------------------------------------------------------

def check(store: Store, name: str, password: str) -> bool:
    """Whether these credentials are good.

    An administrator with no stored password signs in with the shipped one,
    so a first run — or a store lost off the share — still has a way in. A
    stored one wins, so changing it actually changes it. Nobody else has a
    fallback: no password, no login.
    """
    name = canonical(name)
    account = store.users.get(name)
    if account and account.password:
        return auth.verify(account.password, password)
    return name in ADMINISTRATORS and auth.verify(INITIAL_HASH, password)


def exists(store: Store, name: str) -> bool:
    """Whether somebody by this name can be signed in at all."""
    who = canonical(name)
    return who in store.users or who in ADMINISTRATORS


def login_is_initial(store: Store, name: str) -> bool:
    """True while an administrator still signs in with the shipped password."""
    who = canonical(name)
    account = store.users.get(who)
    return who in ADMINISTRATORS and not (account and account.password)


def check_elevation(store: Store, password: str) -> bool:
    """Whether this is the administrator password — the switch's, not a
    login's."""
    return auth.verify(store.elevation or INITIAL_HASH, password)


def elevation_is_initial(store: Store) -> bool:
    """True while the administrator password is still the shipped one."""
    return not store.elevation


# --- sessions ----------------------------------------------------------------

def mint(store: Store, name: str, *, now: float | None = None,
         path: Path | None = None) -> str:
    """A signed session token for `name`.

    Name and expiry in the clear, signed — so the server keeps no session table
    and a restart does not sign anyone out. The signature is what makes the
    contents trustworthy; nothing secret is in them.
    """
    moment = time.time() if now is None else now
    expires = int(moment + SESSION_DAYS * 86400)
    body = f"{canonical(name)}|{expires}"
    return f"{body}|{_sign(ensure_secret(store, path), body)}"


def identify(store: Store, token: str | None, *,
             now: float | None = None) -> str | None:
    """The name a token vouches for, or None if it does not.

    Rejects on a bad signature *or* a passed expiry, and compares the signature
    in constant time. An unparseable token is simply not signed in — a cookie
    from an older format should log someone out, never raise.
    """
    if not token or not store.secret:
        return None
    name, _, rest = token.partition("|")
    expires, _, signature = rest.partition("|")
    if not name or not signature:
        return None
    if not hmac.compare_digest(_sign(store.secret, f"{name}|{expires}"),
                               signature):
        return None
    try:
        if float(expires) < (time.time() if now is None else now):
            return None
    except ValueError:
        return None
    return name


def mint_elevated(store: Store, name: str, *, now: float | None = None,
                  path: Path | None = None) -> str:
    """A signed token saying this person switched Administrator on.

    Signed over the administrator password's hash as well as the session
    secret, so changing that password turns every switch off at once — the
    answer to *somebody else knows it now*.
    """
    moment = time.time() if now is None else now
    expires = int(moment + ELEVATED_HOURS * 3600)
    body = f"{canonical(name)}|{expires}"
    secret = ensure_secret(store, path)
    return f"{body}|{_sign(secret, _elevated_body(store, body))}"


def elevated(store: Store, token: str | None, name: str, *,
             now: float | None = None) -> bool:
    """Whether `token` is a live Administrator switch for `name`.

    For *this* person, who must still be one of `ADMINISTRATORS`: a switch
    minted for somebody else, or for somebody since taken off the list,
    turns nothing on.
    """
    who = canonical(name)
    if not token or not store.secret or who not in ADMINISTRATORS:
        return False
    owner, _, rest = token.partition("|")
    expires, _, signature = rest.partition("|")
    if owner != who or not signature:
        return False
    body = f"{owner}|{expires}"
    if not hmac.compare_digest(
            _sign(store.secret, _elevated_body(store, body)), signature):
        return False
    try:
        return float(expires) >= (time.time() if now is None else now)
    except ValueError:
        return False


def _elevated_body(store: Store, body: str) -> str:
    # Prefixed, so a session token can never be read as a switch: the two are
    # signed with the same secret and must not be interchangeable.
    return f"elevated|{body}|{store.elevation or INITIAL_HASH}"


def _sign(secret: str, body: str) -> str:
    digest = hmac.new(secret.encode(), body.encode(), "sha256").digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")
