"""Admin authentication for the panel — one password, in-memory sessions.

The panel is single-operator and localhost-bound, so this is deliberately small:
the admin password is hashed with stdlib ``scrypt`` (no argon2 dependency) and
stored in the DB; sessions are random tokens held in memory. Restarting the
process logs everyone out, which is fine for a personal tool.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_SETTING_KEY = "admin_password_hash"


def hash_password(password: str) -> str:
    """Return a ``salt$hash`` string (both base64)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_b64, hash_b64 = stored.split("$", 1)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(digest, expected)


class AdminAuth:
    """Password check + session tokens for the panel."""

    COOKIE = "blaulicht_session"

    def __init__(self, store) -> None:
        self._store = store
        self._sessions: set[str] = set()

    def bootstrap(self, initial_password: str | None) -> None:
        """Set the admin password from env on first run, if not already set."""
        if self._store.get_setting(_SETTING_KEY) is None and initial_password:
            self._store.set_setting(_SETTING_KEY, hash_password(initial_password))

    def is_configured(self) -> bool:
        return self._store.get_setting(_SETTING_KEY) is not None

    def check(self, password: str) -> bool:
        stored = self._store.get_setting(_SETTING_KEY)
        return bool(stored) and verify_password(password, stored)

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions.add(token)
        return token

    def valid(self, token: str | None) -> bool:
        return bool(token) and token in self._sessions

    def revoke(self, token: str | None) -> None:
        self._sessions.discard(token or "")
