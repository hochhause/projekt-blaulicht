"""Persistent, human-esque session runner for one account.

The naive approach logs in and re-fetches the mission catalogue every cycle —
loud and bot-like. This holds **one** authenticated session per account: it
connects once, caches the mission catalogue for the life of the session, and
each cycle only polls missions + vehicles. If a cycle fails (e.g. the session
cookie expired), the session is dropped so the next cycle reconnects — no
retry-hammering, and the worker's back-off spaces reconnects out.

This module is game-coupled (imports the session/client, hence ``httpx``); the
orchestrator imports it lazily so its own supervision core stays dependency-free.
"""

from __future__ import annotations

import logging

from ..domain.catalog import MissionCatalog
from ..domain.dispatch import DispatchPolicy
from ..domain.resolver import MissionRequirementResolver
from ..game.client import GameClient
from ..game.session import GameSession
from .runner import DispatchRunner, RunnerTiming

logger = logging.getLogger(__name__)


class AccountRunner:
    """Owns a persistent session and runs dispatch cycles over it."""

    def __init__(
        self,
        *,
        base_url: str,
        user_agent: str,
        email: str,
        password: str,
        library,
        policy: DispatchPolicy,
        timeout: float = 20.0,
        on_log=None,
        on_stat=None,
        timing: RunnerTiming | None = None,
    ) -> None:
        self._base_url = base_url
        self._user_agent = user_agent
        self._email = email
        self._password = password
        self._library = library
        self._policy = policy
        self._timeout = timeout
        self._on_log = on_log
        self._on_stat = on_stat
        self._timing = timing
        self._session: GameSession | None = None
        self._dispatch: DispatchRunner | None = None

    def _log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)

    async def _connect(self) -> None:
        self._log("connecting to game…")
        session = GameSession(self._base_url, user_agent=self._user_agent, timeout=self._timeout)
        result = await session.login(self._email, self._password)
        if not result.ok:
            await session.aclose()
            raise RuntimeError(f"login {result.status.value}: {result.detail}".rstrip(": "))

        client = GameClient(session)
        einsaetze = await client.fetch_einsaetze()  # once per session, not per cycle
        resolver = MissionRequirementResolver(MissionCatalog.from_payload(einsaetze), self._library)
        self._session = session
        self._dispatch = DispatchRunner(
            client, resolver, self._policy,
            on_log=self._on_log, on_stat=self._on_stat, timing=self._timing,
        )
        self._log(f"connected — {len(resolver.catalog)} mission types loaded")

    async def run_cycle(self):
        """Run one cycle, connecting first if needed. Drops the session on any
        failure so the next cycle reconnects cleanly."""
        if self._session is None:
            await self._connect()
        try:
            return await self._dispatch.run_cycle()
        except Exception:
            await self.aclose()  # force a fresh connect next cycle (handles expiry)
            raise

    async def aclose(self) -> None:
        if self._session is not None:
            try:
                await self._session.aclose()
            except Exception:  # noqa: BLE001
                logger.exception("closing session failed")
        self._session = None
        self._dispatch = None
