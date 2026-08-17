"""Credential sealing — encrypt game passwords at rest.

Game passwords must never sit in the database as plaintext. A :class:`SecretBox`
seals/open them with Fernet (authenticated AES-CBC + HMAC), keyed by a 32-byte
key derived from the process ``BOT_SECRET_KEY`` via stdlib ``scrypt``. The master
key lives only in the environment; the DB holds only ciphertext.

The KDF is stdlib and unit-tested directly. The Fernet round-trip is tested too,
skipped automatically where ``cryptography`` isn't installed (it is a runtime
dependency; the pure-stdlib test suite stays green without it).
"""

from __future__ import annotations

import base64
import hashlib

# Fixed KDF salt: the master key is already high-entropy, and a stable salt lets
# the same key reproduce the same Fernet key across restarts. (Per-secret salts
# would need to be stored alongside each ciphertext; not worth it here.)
_KDF_SALT = b"projekt-blaulicht/secret-kdf/v1"


def derive_key(master_key: str) -> bytes:
    """Derive a urlsafe-base64 Fernet key from the master key (stdlib scrypt)."""
    if not master_key:
        raise ValueError("BOT_SECRET_KEY is required to seal credentials")
    raw = hashlib.scrypt(
        master_key.encode("utf-8"), salt=_KDF_SALT, n=2**14, r=8, p=1, dklen=32
    )
    return base64.urlsafe_b64encode(raw)


class SecretBox:
    """Seals and opens secrets under a key derived from the master key."""

    def __init__(self, master_key: str) -> None:
        # Imported lazily so the pure-stdlib parts of the app (and its tests)
        # don't require `cryptography` unless credentials are actually sealed.
        from cryptography.fernet import Fernet

        self._fernet = Fernet(derive_key(master_key))

    def seal(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def open(self, token: bytes) -> str:
        return self._fernet.decrypt(token).decode("utf-8")
