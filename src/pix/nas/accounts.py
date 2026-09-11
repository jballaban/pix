"""Who can sign in, what roles they hold, and the sessions that remember it.

Three things live here that the rest of the app treats as given: the account
store, the built-in administrator, and session tokens.

### Why a file, and why this one is allowed

[§4](../../spec/nas-app.md) rejects a central database *of record* — lose it and
the archive is anonymous. Accounts are not that. They are **configuration**: lose
`users.json` and you lose the logins, not a single photograph or a single
decision about one. Every decision still lives in the `.xmp` beside its file, and
an audience grant names a person who could simply be recreated. So the rule that
kept metadata out of a database does not apply to knowing who `kids` is.

It sits on the share rather than in the container so it survives an image swap,
and it is written temp-then-rename like every other record pix keeps.

### The built-in administrator

`admin` is **hard-coded** and never appears in the store, so there is no way to
lock yourself out by editing a file — and no way to accidentally delete the only
account that can grant access. It is also never an audience: an administrator
sees everything by definition, so sharing *with* admin would mean nothing.

Its password ships as a **hash of a known initial value**, not as plaintext. The
repository has a remote; a password committed to it is a published password, and
a hash is the same convenience without that. The app says so until it is changed.

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

#: The built-in administrator. Hard-coded so it cannot be removed or renamed,
#: and excluded from every audience: an admin sees everything already.
ADMIN: str = "admin"

#: scrypt hash of the initial admin password — the literal string `admin`.
#: Deliberately a hash and not the password: this file has a git remote, and a
#: password in a repository is a published password. Changing it in the app
#: writes a new hash to the store, which then wins.
ADMIN_INITIAL_HASH: str = (
    "64da7a6c950eb208886df87cdff41d8f$"
    "6760ed314f65cecece19ffafebb199a7eeb7d0401ba4b7cb4b5610602cc7b200"
)

#: How long a signed session stays valid. Long enough that a household is not
#: forever logging in, short enough that a forgotten tablet is not forever
#: logged in.
SESSION_DAYS: int = 30

COOKIE: str = "pix2_session"


@dataclass(frozen=True)
class Account:
    """One person who can sign in."""

    name: str
    password: str = ""
    roles: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class Store:
    """The account file's contents.

    `roles` is kept explicitly rather than derived from what users hold, so a
    role can exist before anyone is in it — otherwise creating `tv` would be
    impossible until something had already been shared with it.
    """

    users: dict[str, Account] = field(default_factory=lambda: {})
    roles: list[str] = field(default_factory=lambda: [])
    secret: str = ""

    def grants(self, name: str) -> frozenset[str]:
        """Every name a grant could use to reach this person.

        Themselves plus their roles, because a share names one or the other and
        the access check is not allowed to care which.
        """
        account = self.users.get(name)
        return frozenset({name, *(account.roles if account else ())})

    def audiences(self) -> list[str]:
        """Everything that can be shared with — people and roles, never admin."""
        return sorted({*self.users, *self.roles} - {ADMIN})


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
        roles_raw: object = entry.get("roles")
        users[str(name)] = Account(
            name=str(name),
            password=str(entry.get("password") or ""),
            roles=tuple(str(r) for r in cast("list[Any]", roles_raw or [])),
        )
    raw_roles: object = data.get("roles")
    roles = [str(r) for r in cast("list[Any]", raw_roles or [])]
    return Store(users=users, roles=roles, secret=str(data.get("secret") or ""))


def save(store: Store, path: Path | None = None) -> None:
    """Write the store atomically.

    Temp-then-rename, so a kill mid-write cannot leave a half-parsed file — the
    failure mode there is *nobody can sign in*, which on a NAS in a cupboard is
    a genuinely bad afternoon.
    """
    target = path if path is not None else ACCOUNTS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "users": {name: {"password": a.password, "roles": list(a.roles)}
                  for name, a in sorted(store.users.items())},
        "roles": sorted(set(store.roles)),
        "secret": store.secret,
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

    The admin's stored password wins over the built-in one, so changing it
    actually changes it — but the account itself cannot be removed.
    """
    if name == ADMIN:
        account = store.users.get(ADMIN)
        stored = account.password if account and account.password else ADMIN_INITIAL_HASH
        return auth.verify(stored, password)
    account = store.users.get(name)
    return bool(account and account.password
                and auth.verify(account.password, password))


def admin_password_is_initial(store: Store) -> bool:
    """True while the built-in admin still has its shipped password."""
    account = store.users.get(ADMIN)
    return not (account and account.password)


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
    body = f"{name}|{expires}"
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


def _sign(secret: str, body: str) -> str:
    digest = hmac.new(secret.encode(), body.encode(), "sha256").digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")
