"""Process-wide configuration, read from the environment.

Only *infrastructure* settings live here (paths, the encryption key, the admin
password, network defaults). Per-account gameplay settings — dispatch radius,
transport toggles, and so on — are stored per account in the database, not
here, so that the web panel can edit them at runtime.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# Repo root = two levels up from this file (app/config.py -> app -> repo).
REPO_ROOT = Path(__file__).resolve().parent.parent


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def server_urls() -> dict[str, str]:
    """Map of server code → base URL, loaded from the salvaged Server.json.

    Covers all 19 regional servers; we only *use* ``de`` for now, but keeping
    the full table means adding another region later is data, not code.
    """
    path = REPO_ROOT / "data" / "reference" / "Server.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    return {e["code"]: e["url"].rstrip("/") for e in entries}


@dataclass(frozen=True)
class Settings:
    """Infrastructure settings resolved from environment variables."""

    # Where SQLite and the salvaged assets live (mounted as a volume in Docker).
    data_dir: Path = field(default_factory=lambda: REPO_ROOT / "data")

    # Master key for AES-GCM credential sealing. Required in production; the
    # app refuses to store credentials without it.
    secret_key: str | None = field(default_factory=lambda: os.environ.get("BOT_SECRET_KEY"))

    # First-run bootstrap password for the web panel (Argon2-hashed on first use).
    admin_password: str | None = field(
        default_factory=lambda: os.environ.get("ADMIN_PASSWORD")
    )

    # Only the German server is supported for now.
    default_server: str = "de"

    # A realistic, stable User-Agent. Each account may override with its own.
    user_agent: str = field(
        default_factory=lambda: os.environ.get(
            "BOT_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        )
    )

    request_timeout_seconds: float = field(
        default_factory=lambda: float(os.environ.get("BOT_HTTP_TIMEOUT", "20"))
    )

    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "info"))

    @property
    def vehicles_dir(self) -> Path:
        """Directory of salvaged German vehicle assets."""
        return self.data_dir / "de" / "vehicles"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "blaulicht.db"

    def base_url(self, server: str | None = None) -> str:
        """Base URL for a server code (defaults to the German server)."""
        return server_urls()[server or self.default_server]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
