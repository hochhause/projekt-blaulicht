"""Persistence — a small synchronous SQLite store (stdlib ``sqlite3``).

Holds accounts, sealed credentials, per-account config profiles, run state and
stats. Kept synchronous and dependency-free; the web layer calls it from a
thread when needed. The database file lives on the mounted volume.
"""

from .db import Store
from .models import Account

__all__ = ["Store", "Account"]
