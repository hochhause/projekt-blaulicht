"""Multi-account orchestrator — one supervised runner loop per account.

The :class:`Orchestrator` owns an asyncio task per enabled account and keeps them
**isolated**: a failing cycle is caught and retried with back-off, and one
account crashing never touches the others. It is built around an injected
``worker_factory`` so the supervision logic is unit-tested with fakes and needs
no ``httpx``; :func:`default_worker_factory` builds the real, session-backed
worker.

An :class:`AccountWorker` wraps one account's :class:`DispatchRunner`: it runs a
cycle, records state via an optional callback, then waits ``poll_seconds`` plus
jitter — with the wait interruptible so a stop is prompt.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

StateCallback = Callable[[int, str, str | None], None]


@dataclass
class WorkerTiming:
    poll_seconds: float = 45.0
    jitter_seconds: float = 10.0
    max_backoff_seconds: float = 600.0
    # Injectable for deterministic tests.
    jitter: Callable[[float], float] = field(default=lambda j: random.uniform(0.0, j))


class _RunnerLike:
    async def run_cycle(self): ...  # structural hint only


class AccountWorker:
    """Runs one account's dispatch loop, with error isolation and back-off."""

    def __init__(
        self,
        account_id: int,
        runner: _RunnerLike,
        timing: WorkerTiming | None = None,
        on_state: StateCallback | None = None,
    ) -> None:
        self.account_id = account_id
        self.status = "idle"
        self._runner = runner
        self._timing = timing or WorkerTiming()
        self._on_state = on_state
        self._consecutive_errors = 0

    def _emit(self, status: str, error: str | None = None) -> None:
        self.status = status
        if self._on_state:
            self._on_state(self.account_id, status, error)

    async def run_once(self):
        """Run a single cycle; never raises. Returns the report or ``None``."""
        try:
            report = await self._runner.run_cycle()
            self._consecutive_errors = 0
            self._emit("idle")
            return report
        except Exception as exc:  # noqa: BLE001 — isolation boundary
            self._consecutive_errors += 1
            logger.exception("account %s cycle failed", self.account_id)
            self._emit("error", str(exc))
            return None

    def _next_delay(self) -> float:
        base = self._timing.poll_seconds + self._timing.jitter(self._timing.jitter_seconds)
        if self._consecutive_errors:
            # Exponential back-off after errors, capped.
            base = min(base * (2**self._consecutive_errors), self._timing.max_backoff_seconds)
        return base

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Loop until *stop* is set. The inter-cycle wait is interruptible."""
        self._emit("running")
        try:
            while not stop.is_set():
                await self.run_once()
                if stop.is_set():
                    break
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._next_delay())
                    break  # stop was set during the wait
                except asyncio.TimeoutError:
                    continue  # normal: slept the full delay
        finally:
            self._emit("stopped")


class Orchestrator:
    """Supervises account workers as isolated asyncio tasks."""

    def __init__(self, worker_factory: Callable[[int], AccountWorker]) -> None:
        self._factory = worker_factory
        self._tasks: dict[int, tuple[AccountWorker, asyncio.Task, asyncio.Event]] = {}

    async def start(self, account_id: int) -> None:
        if account_id in self._tasks:
            return
        worker = self._factory(account_id)
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run_forever(stop))
        self._tasks[account_id] = (worker, task, stop)
        logger.info("started account %s", account_id)

    async def stop(self, account_id: int) -> None:
        entry = self._tasks.pop(account_id, None)
        if entry is None:
            return
        _worker, task, stop = entry
        stop.set()
        try:
            await task
        except asyncio.CancelledError:  # pragma: no cover
            pass
        logger.info("stopped account %s", account_id)

    async def stop_all(self) -> None:
        for account_id in list(self._tasks):
            await self.stop(account_id)

    async def reload(self, enabled_ids: list[int]) -> None:
        """Reconcile running workers with the set of enabled accounts."""
        wanted = set(enabled_ids)
        for account_id in list(self._tasks):
            if account_id not in wanted:
                await self.stop(account_id)
        for account_id in wanted:
            await self.start(account_id)

    @property
    def running(self) -> list[int]:
        return list(self._tasks)

    def status(self) -> dict[int, str]:
        return {aid: worker.status for aid, (worker, _t, _s) in self._tasks.items()}


def default_worker_factory(store, secret_box, settings) -> Callable[[int], AccountWorker]:
    """Build a factory that constructs real, session-backed workers.

    Imports the game layer lazily so importing this module stays ``httpx``-free.
    Each worker gets its own authenticated session and dispatch runner, built
    from the account's stored config and sealed credentials.
    """
    from ..domain.assets import AssetLibrary
    from ..domain.catalog import MissionCatalog
    from ..domain.resolver import MissionRequirementResolver
    from ..game.client import GameClient
    from ..game.session import GameSession
    from .account_config import AccountConfig
    from .runner import DispatchRunner

    # Shared, static reference data — loaded once for all accounts.
    library = AssetLibrary.load(settings.vehicles_dir)

    def factory(account_id: int) -> AccountWorker:
        account = store.get_account(account_id)
        config = AccountConfig.from_dict(_load_config(store, account_id))
        password = secret_box.open(store.get_secret(account_id))

        async def build_and_run_cycle():
            # A fresh session per cycle keeps this simple and stateless; a
            # longer-lived session is a later optimisation.
            base = settings.base_url(account.server)
            async with GameSession(base, user_agent=settings.user_agent) as session:
                result = await session.login(account.email, password)
                if not result.ok:
                    raise RuntimeError(f"login failed: {result.status.value}")
                client = GameClient(session)
                einsaetze = await client.fetch_einsaetze()
                catalog = MissionCatalog.from_payload(einsaetze)
                resolver = MissionRequirementResolver(catalog, library)
                runner = DispatchRunner(client, resolver, config.dispatch_policy())
                return await runner.run_cycle()

        # Adapt the per-cycle builder to the runner-like interface the worker
        # expects (an object with async run_cycle()).
        cycle_runner = type("_CycleRunner", (), {"run_cycle": staticmethod(build_and_run_cycle)})()
        timing = WorkerTiming(poll_seconds=config.poll_seconds, jitter_seconds=config.jitter_seconds)

        def on_state(aid: int, status: str, error: str | None) -> None:
            store.set_state(aid, status, error=error)

        return AccountWorker(account_id, cycle_runner, timing, on_state=on_state)

    return factory


def _load_config(store, account_id: int) -> dict:
    import json

    raw = store.get_config(account_id)
    return json.loads(raw) if raw else {}
