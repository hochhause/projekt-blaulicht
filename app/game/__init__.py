"""Game I/O layer — authenticated session and the ten-endpoint client.

Requires ``httpx`` (see pyproject). Kept separate from ``app.domain`` so the
pure logic there stays importable without any network dependency.
"""

from .client import GameClient
from .models import Building, Mission, Vehicle
from .session import GameSession, LoginResult, LoginStatus

__all__ = [
    "GameClient",
    "GameSession",
    "LoginResult",
    "LoginStatus",
    "Mission",
    "Vehicle",
    "Building",
]
