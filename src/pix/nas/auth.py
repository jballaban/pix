"""Credential hashing for the app (spec/nas-app.md §8).

Separate from `web.py` so the CLI can generate credentials without importing the
web stack — `pix2 passwd` should work in an environment that has never heard of
FastAPI.

**Hashed, never plaintext.** A credentials file on a share reachable over SMB is
exactly how a reused password leaks. `scrypt` comes from the standard library,
so the container needs no crypto package.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

#: scrypt cost. 16384/8/1 is the interactive-login baseline — roughly 100ms on
#: the desktop, which is right for a login and irrelevant for a household of two.
_N, _R, _P = 16384, 8, 1


def hash_password(password: str, salt: str | None = None) -> str:
    """`salt$hash`. A fresh salt unless one is supplied (for verification)."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(),
                            n=_N, r=_R, p=_P, dklen=32).hex()
    return f"{salt}${digest}"


def verify(stored: str, password: str) -> bool:
    """Constant-time check of `password` against a `salt$hash` pair."""
    salt, sep, _ = stored.partition("$")
    if not sep:
        return False
    return hmac.compare_digest(stored, hash_password(password, salt))


def parse_users(raw: str) -> dict[str, str]:
    """Parse `name:salt$hash;name2:salt$hash` into a mapping.

    Malformed entries are dropped rather than raising: a typo in an environment
    variable should cost that one login, not stop the app from starting.
    """
    users: dict[str, str] = {}
    for pair in (raw or "").split(";"):
        name, sep, digest = pair.partition(":")
        if sep and name.strip() and digest.strip():
            users[name.strip()] = digest.strip()
    return users


def parse_admins(raw: str) -> frozenset[str]:
    """Parse `name;name2` into the set of administrators.

    Separate from `PIX2_USERS` on purpose. Admin is the security boundary —
    it sees every file and is the only role that can change who else can — so
    it should be readable at a glance in the deployment's settings rather than
    encoded as a field inside a credential string.

    **A typo fails closed.** An unrecognised name simply is not an admin, which
    costs that person their privileges; the alternative shapes (a positional
    convention, a flag inside the credential) fail open or silently move admin
    to whoever sorts first.
    """
    return frozenset(n.strip() for n in (raw or "").split(";") if n.strip())
