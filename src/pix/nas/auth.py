"""Credential hashing for the app (spec/nas-app.md §8).

Just the hashing. Who exists and what they may see lives in `accounts`; this
is separate so `pix2 passwd` can mint a hash without importing the web stack,
in an environment that has never heard of FastAPI.

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
