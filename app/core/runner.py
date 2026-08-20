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
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from ..domain.dispatch import (
    DispatchPlan,
    DispatchPolicy,
    MissionRequirementResolver,
    assigned_from_vehicles,
    plan_dispatches,
)

logger = logging.getLogger(__name__)


def _vehicle_name(vehicle, vehicle_id: int) -> str:
    """Readable label for a vehicle in the log (type name, else its caption)."""
    if vehicle is None:
        return f"vehicle #{vehicle_id}"
    label = getattr(vehicle, "type_caption", "") or getattr(vehicle, "caption", "")
    return f"{label} (#{vehicle_id})" if label else f"vehicle #{vehicle_id}"


class GameClientLike(Protocol):
    """The slice of the game client the runner needs."""

    async def fetch_missions(self) -> list: ...
    async def fetch_vehicles(self, cap: int = 0) -> list: ...
    async def fetch_buildings(self) -> list: ...
    async def dispatch(self, mission_id: int, vehicle_id: int) -> bool: ...
    async def transport_targets(self, vehicle_id: int) -> list[str]: ...
    async def send_transport(self, action_url: str) -> bool: ...


@dataclass
class CycleReport:
    """Outcome of one dispatch cycle."""

    plan: DispatchPlan
    dispatched: int = 0
    failed: int = 0
    # Estimated credits from the missions we fully crewed this cycle (mission
    # average_credits summed). An estimate — no extra API call — labelled as such.
    credits_est: int = 0

    def summary(self) -> str:
        return (
            f"{len(self.plan.planned)} mission(s) actioned, "
            f"{self.dispatched} vehicle(s) dispatched, "
            f"{self.failed} failed, {len(self.plan.skipped)} skipped"
        )

    @property
    def missions_actioned(self) -> int:
        return len(self.plan.planned)

    def log_lines(self) -> list[str]:
        """Human-readable activity lines for the per-account details view."""
        if not self.plan.planned and self.dispatched == 0:
            return [f"idle - {len(self.plan.skipped)} mission(s) seen, none actionable"]
        lines = [self.summary()]
        for mp in self.plan.planned:
            if mp.satisfied:
                lines.append(f"sent to #{mp.mission_id} {mp.caption!r}: {len(mp.vehicle_ids)} vehicle(s)")
            else:
                missing = ", ".join(f"{cls} x{n}" for cls, n in mp.shortfall.items())
                lines.append(
                    f"partial #{mp.mission_id} {mp.caption!r}: sent {len(mp.vehicle_ids)}, "
                    f"MISSING {missing}"
                )
        return lines


@dataclass
class RunnerTiming:
    """Timing between actions. Injectable so tests run instantly."""

    # Human-ish gap between individual sends: a random value in this range.
    between_dispatches: float = 0.9
    between_dispatches_max: float = 2.2
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)

    def dispatch_gap(self) -> float:
        lo = self.between_dispatches
        hi = max(lo, self.between_dispatches_max)
        return random.uniform(lo, hi)


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
        on_log: Callable[[str], None] | None = None,
        on_stat: Callable[[int, int], None] | None = None,
        text_engine=None,
        strikes=None,
        vehicle_cap: int = 0,
        transport_enabled: bool = True,
        transport_only: bool = False,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._policy = policy
        self._text_engine = text_engine
        self._strikes = strikes
        self._vehicle_cap = vehicle_cap
        self._transport_enabled = transport_enabled
        self._transport_only = transport_only
        # Buildings change rarely; the runner may be handed a cached list to
        # avoid re-fetching them every cycle.
        self._buildings = buildings_cache
        self._timing = timing or RunnerTiming()
        # Real-time activity sink (the panel's per-account log). Called as the
        # cycle progresses, so a long or in-progress cycle still shows what it
        # is doing rather than nothing until it finishes.
        self._on_log = on_log
        # Called (dispatched, missions) after each mission so the panel counters
        # tick up during the cycle, not only at its end.
        self._on_stat = on_stat

    def _log(self, message: str) -> None:
        logger.info(message)
        if self._on_log is not None:
            try:
                self._on_log(message)
            except Exception:  # noqa: BLE001 — logging must never break the cycle
                logger.exception("on_log hook failed")

    def _record_dispatch_failure(self, vehicle, vehicle_id: int, now: float, report) -> None:
        """A vehicle that LOOKED dispatchable (FMS 1/2, in the fleet) failed to
        send — most often it has no crew. Strike the specific vehicle; after 3
        such failures in 24h the planner drops it from the pool. Also surface it
        on the dashboard's "struggling to field" list (trouble dispatching)."""
        label = (
            getattr(vehicle, "type_caption", "")
            or getattr(vehicle, "caption", "")
            or f"vehicle #{vehicle_id}"
        )
        tally = report.plan.shortfalls
        tally[label] = tally.get(label, 0) + 1
        if self._strikes is None:
            return
        self._strikes.record([str(vehicle_id)], now)
        if self._strikes.is_struck(str(vehicle_id), now):
            name = _vehicle_name(vehicle, vehicle_id)
            self._log(
                f"struck out {name}: failed to dispatch 3× — likely no crew; "
                f"skipping this vehicle for 24h"
            )

    async def _buildings_for_cycle(self) -> list:
        if self._buildings is None:
            self._buildings = await self._client.fetch_buildings()
        return self._buildings

    async def _handle_transports(self, vehicles) -> int:
        """Unload patients/prisoners from vehicles requesting it (FMS 5).

        Runs before dispatch each cycle. Picks the game's first offered
        hospital/cell (already filtered to free capacity + the vehicle's own
        in-game distance/price/own-alliance settings).
        """
        pending = [v for v in vehicles if getattr(v, "fms", None) == 5]
        if not pending:
            return 0
        self._log(f"transport: {len(pending)} vehicle(s) awaiting a bed/cell")
        done = 0
        for v in pending:
            name = _vehicle_name(v, v.id)
            targets = await self._client.transport_targets(v.id)
            if not targets:
                self._log(f"  no free target for {name} — all full / out of range")
                continue
            if await self._client.send_transport(targets[0]):
                done += 1
                self._log(f"  transported {name}")
            await self._timing.sleep(self._timing.dispatch_gap())
        return done

    async def run_cycle(self) -> CycleReport:
        """Run one cycle: transport (Sprechwünsche) first, then poll → plan →
        dispatch (skipped in transport-only mode)."""
        vehicles = await self._client.fetch_vehicles(self._vehicle_cap)

        if self._transport_enabled:
            await self._handle_transports(vehicles)
        if self._transport_only:
            self._log("transport-only mode: dispatch skipped")
            return CycleReport(plan=DispatchPlan())

        missions = await self._client.fetch_missions()
        buildings = await self._buildings_for_cycle()

        idle = sum(1 for v in vehicles if getattr(v, "is_available", False))
        self._log(f"polled {len(missions)} mission(s), {idle} idle vehicle(s)")

        # Subtract vehicles already en route so missions are topped up, not
        # re-dispatched (avoids piling vehicles onto an already-covered mission).
        now = time.time()
        assigned = assigned_from_vehicles(vehicles)
        plan = plan_dispatches(
            missions, vehicles, buildings, self._resolver, self._policy,
            assigned=assigned, text_engine=self._text_engine, now=now,
            strikes=self._strikes,
        )
        report = CycleReport(plan=plan)

        # Show why missions were (or weren't) actionable — the key diagnostic.
        reasons = Counter(reason for _mid, reason in plan.skipped)
        if reasons:
            breakdown = "; ".join(f"{n}x {r}" for r, n in reasons.most_common(6))
            self._log(f"plan: {len(plan.planned)} actionable, {len(plan.skipped)} skipped ({breakdown})")
        else:
            self._log(f"plan: {len(plan.planned)} actionable")

        # Log shortfalls / strike-outs prominently — these matter more than
        # successes (they're why a mission didn't get handled).
        by_id = {m.id: m for m in missions}
        shortfalls = [
            (mid, r) for mid, r in plan.skipped
            if r.startswith("not enough vehicles") or r.startswith("needs struck-out")
        ]
        for mid, reason in shortfalls[:5]:
            mm = by_id.get(mid)
            cap = getattr(mm, "caption", "") if mm is not None else ""
            self._log(f"  short #{mid} {cap!r}: {reason}")
        if len(shortfalls) > 5:
            self._log(f"  … and {len(shortfalls) - 5} more short/struck missions")

        # Diagnose unresolved missions: show a few with their live requirement
        # text so we can see whether it's empty or just not parsing.
        unresolved = [mid for mid, r in plan.skipped if r == "no requirements found"]
        if unresolved:
            catalog = self._resolver.catalog
            for mid in unresolved[:3]:
                mm = by_id.get(mid)
                if mm is None:
                    continue
                mtid = getattr(mm, "mission_type_id", None)
                mtype = catalog.get(mtid) if mtid is not None else None
                reqs = mtype.requirements if mtype is not None else None
                text = (getattr(mm, "missing_text", "") or "").strip() or "(empty)"
                self._log(
                    f"  unresolved #{mid} {getattr(mm, 'caption', '')!r}: mtid={mtid} "
                    f"in_catalog={mtype is not None} catalog_reqs={reqs} "
                    f"missing_text={text[:100]!r}"
                )

        veh_by_id = {v.id: v for v in vehicles}

        for mission_plan in plan.planned:
            sent = 0
            for vehicle_id in mission_plan.vehicle_ids:
                name = _vehicle_name(veh_by_id.get(vehicle_id), vehicle_id)
                ok = await self._client.dispatch(mission_plan.mission_id, vehicle_id)
                if ok:
                    report.dispatched += 1
                    sent += 1
                    self._log(
                        f"dispatched {name} → #{mission_plan.mission_id} {mission_plan.caption!r}"
                    )
                else:
                    report.failed += 1
                    self._log(
                        f"FAILED {name} → #{mission_plan.mission_id} {mission_plan.caption!r} (no crew?)"
                    )
                    self._record_dispatch_failure(veh_by_id.get(vehicle_id), vehicle_id, now, report)
                await self._timing.sleep(self._timing.dispatch_gap())
            if not mission_plan.satisfied:
                self._log(f"#{mission_plan.mission_id} still MISSING {mission_plan.shortfall}")
            # Estimate earned credits: a fully-crewed mission we actually sent to.
            if sent and mission_plan.satisfied:
                mm = by_id.get(mission_plan.mission_id)
                report.credits_est += int(getattr(mm, "average_credits", 0) or 0)
            # Tick the counters per mission, not only at cycle end.
            if self._on_stat is not None and sent:
                self._on_stat(sent, 1)

        self._log(f"cycle done: {report.summary()}")
        return report
