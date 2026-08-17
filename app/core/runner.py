"""The per-account dispatch runner — one cycle of poll → plan → dispatch.

This is the I/O shell around the pure planner in
:mod:`app.domain.dispatch`. It is intentionally decoupled from the concrete
game client (and therefore from ``httpx``): it talks to any object satisfying
:class:`GameClientLike`, so a cycle can be unit-tested with a fake client and no
network. The infinite loop, jitter and back-off live here; the decision logic
does not.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from ..domain.dispatch import DispatchPlan, DispatchPolicy, MissionRequirementResolver, plan_dispatches

logger = logging.getLogger(__name__)


class GameClientLike(Protocol):
    """The slice of the game client the runner needs."""

    async def fetch_missions(self) -> list: ...
    async def fetch_vehicles(self) -> list: ...
    async def fetch_buildings(self) -> list: ...
    async def dispatch(self, mission_id: int, vehicle_id: int) -> bool: ...


@dataclass
class CycleReport:
    """Outcome of one dispatch cycle."""

    plan: DispatchPlan
    dispatched: int = 0
    failed: int = 0

    def summary(self) -> str:
        return (
            f"{len(self.plan.planned)} mission(s) actioned, "
            f"{self.dispatched} vehicle(s) dispatched, "
            f"{self.failed} failed, {len(self.plan.skipped)} skipped"
        )


@dataclass
class RunnerTiming:
    """Timing between actions. Injectable so tests run instantly."""

    between_dispatches: float = 0.8  # human-ish gap between individual sends
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)


class DispatchRunner:
    """Runs dispatch cycles for a single account."""

    def __init__(
        self,
        client: GameClientLike,
        resolver: MissionRequirementResolver,
        policy: DispatchPolicy,
        *,
        buildings_cache: list | None = None,
        timing: RunnerTiming | None = None,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._policy = policy
        # Buildings change rarely; the runner may be handed a cached list to
        # avoid re-fetching them every cycle.
        self._buildings = buildings_cache
        self._timing = timing or RunnerTiming()

    async def _buildings_for_cycle(self) -> list:
        if self._buildings is None:
            self._buildings = await self._client.fetch_buildings()
        return self._buildings

    async def run_cycle(self) -> CycleReport:
        """Run exactly one poll → plan → dispatch cycle."""
        missions = await self._client.fetch_missions()
        vehicles = await self._client.fetch_vehicles()
        buildings = await self._buildings_for_cycle()

        plan = plan_dispatches(missions, vehicles, buildings, self._resolver, self._policy)
        report = CycleReport(plan=plan)

        for mission_plan in plan.planned:
            for vehicle_id in mission_plan.vehicle_ids:
                ok = await self._client.dispatch(mission_plan.mission_id, vehicle_id)
                if ok:
                    report.dispatched += 1
                else:
                    report.failed += 1
                    logger.warning(
                        "dispatch failed: mission %s <- vehicle %s",
                        mission_plan.mission_id,
                        vehicle_id,
                    )
                await self._timing.sleep(self._timing.between_dispatches)

        logger.info("cycle done: %s", report.summary())
        return report
