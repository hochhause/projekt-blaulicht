"""Row dataclasses for the store."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Account:
    """A managed game account (credentials live sealed in a separate table)."""

    id: int | None
    label: str
    email: str
    server: str = "de"
    enabled: bool = False
    created_at: str = ""
