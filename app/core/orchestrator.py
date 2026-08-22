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
        on_cycle=None,
    ) -> None:
        self.account_id = account_id
        self.status = "idle"
        self._runner = runner
        self._timing = timing or WorkerTiming()
        self._on_state = on_state
        # Called with (account_id, report) after each successful cycle so the
        # caller can log activity and persist stats.
        self._on_cycle = on_cycle
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
            if self._on_cycle is not None:
                try:
                    self._on_cycle(self.account_id, report)
                except Exception:  # noqa: BLE001 — logging must never break the loop
                    logger.exception("on_cycle hook failed for account %s", self.account_id)
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
        """Loop until *stop* is set. A cancel (from :meth:`Orchestrator.stop`)
        interrupts a cycle mid-dispatch so sending stops immediately."""
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
        except asyncio.CancelledError:
            pass  # stopped mid-cycle — fall through to clean up
        finally:
            self._emit("stopped")
            # Close the persistent session cleanly on stop, if the runner has one.
            close = getattr(self._runner, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    logger.exception("closing runner for account %s failed", self.account_id)


GROUP_KEY = 0  # reserved worker/log id for the running group


class Orchestrator:
    """Supervises account workers as isolated asyncio tasks."""

    def __init__(self, worker_factory: Callable[[int], AccountWorker], group_factory=None) -> None:
        self._factory = worker_factory
        self._group_factory = group_factory
        self._tasks: dict[int, tuple[AccountWorker, asyncio.Task, asyncio.Event]] = {}
        # (worker, task, stop_event, member_ids) while a group runs; else None.
        self._group: tuple[AccountWorker, asyncio.Task, asyncio.Event, set[int]] | None = None

    async def start(self, account_id: int) -> None:
        if account_id in self._tasks:
            return
        if self._group is not None and account_id in self._group[3]:
            raise RuntimeError("account is in the running group — stop the group to run it solo")
        worker = self._factory(account_id)
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run_forever(stop))
        self._tasks[account_id] = (worker, task, stop)
        logger.info("started account %s", account_id)

    # -- group (symbiotic) mode -------------------------------------------
    async def start_group(self, member_ids: list[int]) -> None:
        if self._group is not None:
            return
        if self._group_factory is None:
            raise RuntimeError("no group factory configured")
        # A group member can't also run solo — stop any individual workers first.
        for account_id in member_ids:
            await self.stop(account_id)
        worker = self._group_factory(list(member_ids))
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run_forever(stop))
        self._group = (worker, task, stop, set(member_ids))
        logger.info("started group %s", member_ids)

    async def stop_group(self) -> None:
        if self._group is None:
            return
        _worker, task, stop, _members = self._group
        self._group = None
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:  # pragma: no cover
            pass
        logger.info("stopped group")

    @property
    def group_members(self) -> set[int]:
        return set(self._group[3]) if self._group is not None else set()

    @property
    def group_running(self) -> bool:
        return self._group is not None

    def group_status(self) -> str | None:
        return self._group[0].status if self._group is not None else None

    async def stop(self, account_id: int) -> None:
        entry = self._tasks.pop(account_id, None)
        if entry is None:
            return
        _worker, task, stop = entry
        stop.set()
        task.cancel()  # interrupt an in-progress dispatch cycle immediately
        try:
            await task
        except asyncio.CancelledError:  # pragma: no cover
            pass
        logger.info("stopped account %s", account_id)

    async def stop_all(self) -> None:
        await self.stop_group()
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


def build_account_runner(store, secret_box, settings, library, account_id, log_store=None):
    """Construct a session-backed :class:`AccountRunner` for one account from its
    stored config + sealed credentials. Shared by the worker factory and the
    one-shot cleanup action."""
    from .account_config import AccountConfig
    from .account_runner import AccountRunner

    account = store.get_account(account_id)
    config = AccountConfig.from_dict(_load_config(store, account_id))

    def password_provider() -> str:
        # Decrypt lazily at connect time. A changed BOT_SECRET_KEY (or a missing
        # secret) becomes a clear per-account error instead of a startup crash.
        token = store.get_secret(account_id)
        if token is None:
            raise RuntimeError("no stored password — re-enter it on the Configure page")
        try:
            return secret_box.open(token)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "cannot decrypt stored password (BOT_SECRET_KEY changed?) - "
                "re-enter it on the Configure page"
            ) from exc

    def log(message: str) -> None:
        if log_store is not None:
            log_store.append(account_id, message)

    def stat(dispatched: int, missions: int) -> None:
        if dispatched or missions:
            store.add_stat(account_id, dispatched=dispatched, missions=missions)

    runner = AccountRunner(
        base_url=settings.base_url(account.server),
        user_agent=settings.user_agent,
        email=account.email,
        password_provider=password_provider,
        library=library,
        policy=config.dispatch_policy(),
        on_log=log,
        on_stat=stat,
        vehicle_cap=config.max_vehicles,
        transport_enabled=config.transport_enabled,
        transport_only=config.transport_only,
    )
    return runner, config


def make_cleanup(store, secret_box, settings, log_store=None):
    """Return an async ``cleanup(account_id) -> int`` for the panel's button.

    Builds a fresh, isolated session (works whether or not the account is
    running), recalls surplus vehicles, and closes. Returns how many were pulled.
    """
    from ..domain.assets import AssetLibrary

    library = AssetLibrary.load(settings.vehicles_dir)

    async def cleanup(account_id: int) -> int:
        runner, _config = build_account_runner(store, secret_box, settings, library, account_id, log_store)
        try:
            return await runner.cleanup()
        finally:
            await runner.aclose()

    return cleanup


def default_worker_factory(store, secret_box, settings, log_store=None) -> Callable[[int], AccountWorker]:
    """Build a factory that constructs real, session-backed workers.

    Imports the game layer lazily so importing this module stays ``httpx``-free.
    Each worker gets its own authenticated session and dispatch runner, built
    from the account's stored config and sealed credentials. When *log_store* is
    given, each cycle's activity is logged and stats are persisted.
    """
    from ..domain.assets import AssetLibrary

    # Shared, static reference data — loaded once for all accounts.
    library = AssetLibrary.load(settings.vehicles_dir)

    def factory(account_id: int) -> AccountWorker:
        # A (re)start begins a fresh run: reset the per-run "struggling to field"
        # tally, mirroring the fresh StrikeTracker inside the runner.
        store.clear_shortfalls(account_id)
        runner, config = build_account_runner(store, secret_box, settings, library, account_id, log_store)
        timing = WorkerTiming(poll_seconds=config.poll_seconds, jitter_seconds=config.jitter_seconds)

        def on_state(aid: int, status: str, error: str | None) -> None:
            store.set_state(aid, status, error=error)
            if log_store is not None and error:
                log_store.append(aid, f"error: {error}")

        def on_cycle(aid: int, report) -> None:
            # Persist per-cycle analytics for the dashboard: which classes we
            # couldn't field, and estimated credits earned.
            plan = getattr(report, "plan", None)
            shortfalls = getattr(plan, "shortfalls", None) if plan is not None else None
            if shortfalls:
                store.add_shortfalls(aid, shortfalls)
            credits = int(getattr(report, "credits_est", 0) or 0)
            if credits:
                store.add_stat(aid, 0, 0, credits=credits)

        return AccountWorker(account_id, runner, timing, on_state=on_state, on_cycle=on_cycle)

    return factory


def default_group_factory(store, secret_box, settings, log_store=None):
    """Factory that builds the supervised group worker from the stored group
    config + the given member ids. Wrapped as an :class:`AccountWorker` so the
    orchestrator supervises it with the same back-off loop."""
    import json

    from ..domain.assets import AssetLibrary
    from .account_config import GroupConfig
    from .group_coordinator import GroupCoordinator

    library = AssetLibrary.load(settings.vehicles_dir)

    def factory(member_ids: list[int]) -> AccountWorker:
        raw = store.get_group_config()
        gcfg = GroupConfig.from_dict(json.loads(raw) if raw else {})
        members = {}
        for account_id in member_ids:
            store.clear_shortfalls(account_id)
            runner, _cfg = build_account_runner(store, secret_box, settings, library, account_id, log_store)
            members[account_id] = runner

        def log(message: str) -> None:
            if log_store is not None:
                log_store.append(GROUP_KEY, message)

        def stat(aid: int, dispatched: int, missions: int) -> None:
            if dispatched or missions:
                store.add_stat(aid, dispatched=dispatched, missions=missions)

        coordinator = GroupCoordinator(members, gcfg.group_policy(), on_log=log, on_stat=stat)
        timing = WorkerTiming(poll_seconds=gcfg.poll_seconds, jitter_seconds=gcfg.jitter_seconds)

        def on_state(_aid: int, status: str, error: str | None) -> None:
            # Surface a group cycle failure to the Group log (otherwise it only
            # hits Python logging and the panel looks silent).
            if error and log_store is not None:
                log_store.append(GROUP_KEY, f"group error: {error}")

        return AccountWorker(GROUP_KEY, coordinator, timing, on_state=on_state)

    return factory


def _load_config(store, account_id: int) -> dict:
    import json

    raw = store.get_config(account_id)
    return json.loads(raw) if raw else {}
