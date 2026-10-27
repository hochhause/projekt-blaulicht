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

from ..domain.alarms import AlarmTracker
from ..domain.catalog import MissionCatalog
from ..domain.dispatch import DispatchPolicy, RecallCadence
from ..domain.requirements import RequirementEngine
from ..domain.resolver import MissionRequirementResolver
from ..domain.strikes import StrikeTracker
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
        password_provider,
        library,
        policy: DispatchPolicy,
        timeout: float = 20.0,
        on_log=None,
        on_stat=None,
        timing: RunnerTiming | None = None,
        vehicle_cap: int = 0,
        transport_enabled: bool = True,
        transport_only: bool = False,
    ) -> None:
        self._base_url = base_url
        self._user_agent = user_agent
        self._email = email
        self._vehicle_cap = vehicle_cap
        self._transport_enabled = transport_enabled
        self._transport_only = transport_only
        # A callable returning the plaintext password — decryption is deferred
        # to connect time so a bad key surfaces as an account error, not a
        # startup crash.
        self._password_provider = password_provider
        self._library = library
        self._policy = policy
        self._timeout = timeout
        self._on_log = on_log
        self._on_stat = on_stat
        self._timing = timing
        self._session: GameSession | None = None
        self._dispatch: DispatchRunner | None = None
        self._client = None
        self._resolver = None
        # Strikes are keyed by VEHICLE id now: a vehicle that repeatedly fails to
        # dispatch (looked free but wouldn't send — usually no crew) is struck out
        # and dropped from the pool. The runner logs the exclusion (it has the
        # vehicle's name). Persists across reconnects; a real account restart makes
        # a fresh AccountRunner -> fresh tracker.
        self._strikes = StrikeTracker()
        # Real-alarm (initial send / reinforcement, never parasite/S5) counter
        # per mission (see app.domain.alarms.AlarmTracker) -- owned HERE for
        # the same reason as _strikes above: _connect() rebuilds the
        # DispatchRunner on every reconnect, so this must live above that or
        # a transient reconnect would silently reset a mission's in-progress
        # tally mid-close. In-memory only (Sasha, 2026-10-27: "doesn't need
        # to survive a restart") -- a real account restart makes a fresh
        # AccountRunner -> fresh tracker, same as StrikeTracker.
        self._alarms = AlarmTracker()
        # D4's automatic-recall cadence state (see RecallCadence) -- owned
        # HERE for the same reconnect-survival reason as _strikes/_alarms
        # above: a reconnect must not reset "have we already fired on this
        # green mission" or "when did we last check it", or the 15-minute/
        # once-on-green cadence would silently restart on every transient
        # failure. In-memory only (Sasha, 2026-10-27: "doesn't need to
        # survive a restart") -- a real account restart makes a fresh
        # AccountRunner -> fresh cadence state.
        self._recall_cadence = RecallCadence()
        # Mission ids whose casualty count has ever been authoritatively
        # revealed (see MissionRequirementResolver.effective_count_revealed) —
        # owned here, not by the resolver, and handed to each newly-built
        # resolver in _connect() below, for the SAME reason as _strikes above:
        # _connect() is re-run on any transient failure (not just a confirmed
        # logout), which would otherwise silently wipe this memory mid-mission
        # and reopen the ambulance/NEF over-dispatch bug it exists to prevent.
        self._ever_revealed: set[int] = set()

    def _log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)

    async def _connect(self) -> None:
        self._log("connecting to game…")
        password = self._password_provider()  # may raise (e.g. bad key) -> account error
        session = GameSession(self._base_url, user_agent=self._user_agent, timeout=self._timeout)
        result = await session.login(self._email, password)
        if not result.ok:
            await session.aclose()
            raise RuntimeError(f"login {result.status.value}: {result.detail}".rstrip(": "))

        client = GameClient(session)
        einsaetze = await client.fetch_einsaetze()  # once per session, not per cycle
        resolver = MissionRequirementResolver(
            MissionCatalog.from_payload(einsaetze), self._library,
            ever_revealed=self._ever_revealed,
        )
        # Regex fallback for missions the structured catalogue doesn't cover.
        text_engine = RequirementEngine(self._library)
        self._session = session
        self._client = client
        self._resolver = resolver
        self._dispatch = DispatchRunner(
            client, resolver, self._policy,
            on_log=self._on_log, on_stat=self._on_stat, timing=self._timing,
            text_engine=text_engine, strikes=self._strikes, alarm_tracker=self._alarms,
            recall_cadence=self._recall_cadence, vehicle_cap=self._vehicle_cap,
            transport_enabled=self._transport_enabled, transport_only=self._transport_only,
        )
        self._log(f"connected — {len(resolver.catalog)} mission types loaded")

    async def connect(self) -> None:
        """Ensure the session is up (used by the group coordinator)."""
        if self._session is None:
            await self._connect()

    @property
    def client(self):
        """The authenticated GameClient (after connect)."""
        return self._client

    @property
    def resolver(self):
        """The mission resolver (after connect) — the catalogue is public, so any
        member's resolver serves the whole group."""
        return self._resolver

    @property
    def transport_enabled(self) -> bool:
        return self._transport_enabled

    @property
    def vehicle_cap(self) -> int:
        return self._vehicle_cap

    @property
    def strikes(self) -> StrikeTracker:
        """This account's strike tracker — used by the group coordinator so a
        chronically-uncrewed vehicle is excluded from the combined pool the
        same way it would be in solo dispatch."""
        return self._strikes

    def adopt_ever_revealed(self, shared: set[int]) -> None:
        """Point this account's ambulance/NEF sticky memory (see
        MissionRequirementResolver.effective_count_revealed) at an externally
        pooled set — used by GroupCoordinator so a reveal any group member
        witnesses counts for the whole group, regardless of which member's
        resolver gets used on a later cycle (mirrors the `strikes` property
        above: a public, named way for the group coordinator to share a
        member's per-account state, instead of reaching past the underscore).
        Folds in whatever this account already held (never discards a real
        reveal) and, if a resolver has ALREADY been built (this account was
        connected before being handed to the group — not how the real group
        factory works today, but not relied upon here either), repoints its
        live reference too: swapping only this attribute would otherwise
        leave that resolver writing into the old, now-orphaned set until its
        next reconnect, silently defeating the pooling for that member.
        """
        shared |= self._ever_revealed
        self._ever_revealed = shared
        if self._resolver is not None:
            self._resolver._ever_revealed = shared

    async def handle_transports(self, vehicles) -> int:
        """Run the FMS-5 transport phase over an already-fetched fleet."""
        if self._dispatch is None:
            return 0
        return await self._dispatch._handle_transports(vehicles)

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

    async def cleanup(self) -> int:
        """Connect if needed, run one surplus-recall pass, return how many were
        recalled. Used by the panel's 'Cleanup' button (one-shot)."""
        if self._session is None:
            await self._connect()
        try:
            return await self._dispatch.cleanup()
        except Exception:
            await self.aclose()  # drop a broken session so a later action reconnects
            raise

    async def aclose(self) -> None:
        if self._session is not None:
            try:
                await self._session.aclose()
            except Exception:  # noqa: BLE001
                logger.exception("closing session failed")
        self._session = None
        self._dispatch = None
        self._client = None
        self._resolver = None
