"""Group ("symbiotic") coordinator — one loop driving many accounts together.

Each cycle it:

1. **shares** every member's own missions to the alliance, so all are visible to
   the whole group;
2. **reads** the union of missions plus every member's fleet (tagged by owner);
3. **plans** globally via :func:`app.domain.group.plan_group_dispatches` — one
   combined crew per mission (no over-send) and a participation token for every
   member (so all earn the reward);
4. **dispatches** each account's own share in one bulk POST; and
5. runs each member's **transport** phase.

It's shaped like an :class:`AccountWorker` runner (``run_cycle`` / ``aclose``), so
the orchestrator supervises it with the same back-off machinery.
"""

from __future__ import annotations

import logging
import time

from ..domain.group import GroupPolicy, plan_group_dispatches

logger = logging.getLogger(__name__)


class GroupCoordinator:
    """Drives a set of member :class:`AccountRunner`s as one symbiotic group."""

    def __init__(self, members, policy: GroupPolicy, *, on_log=None, on_stat=None,
                 dispatch_gap=1.2, share_enabled=True):
        # members: dict[int, AccountRunner]
        self._members = members
        self._policy = policy
        self._on_log = on_log
        self._on_stat = on_stat
        self._gap = dispatch_gap
        self._share_enabled = share_enabled
        self._buildings: dict[int, list] = {}
        self._shared: set[tuple[int, int]] = set()  # (account_id, mission_id) already shared
        import asyncio
        self._sleep = asyncio.sleep

    def _log(self, message: str) -> None:
        logger.info(message)
        if self._on_log is not None:
            try:
                self._on_log(message)
            except Exception:  # noqa: BLE001
                logger.exception("group on_log failed")

    async def _buildings_for(self, aid, member) -> list:
        if aid not in self._buildings:
            self._buildings[aid] = await member.client.fetch_buildings()
        return self._buildings[aid]

    async def run_cycle(self):
        # Connect every member (skip ones that fail to connect this cycle).
        live: dict[int, object] = {}
        for aid, member in self._members.items():
            try:
                await member.connect()
                live[aid] = member
            except Exception as exc:  # noqa: BLE001 — isolate a bad account
                self._log(f"[#{aid}] connect failed: {exc}")
        if not live:
            raise RuntimeError("no group member could connect")

        # Read + share.
        vehicles_by_account: dict[int, list] = {}
        buildings_by_account: dict[int, list] = {}
        missions_by_id: dict[int, object] = {}
        for aid, member in live.items():
            vehicles = await member.client.fetch_vehicles(member.vehicle_cap)
            vehicles_by_account[aid] = vehicles
            buildings_by_account[aid] = await self._buildings_for(aid, member)
            missions = await member.client.fetch_missions()
            for m in missions:
                missions_by_id.setdefault(m.id, m)  # union / dedup
            if self._share_enabled:
                await self._share_own(aid, member, missions)

        missions = list(missions_by_id.values())
        resolver = live[next(iter(live))].resolver
        idle = sum(1 for vs in vehicles_by_account.values() for v in vs if getattr(v, "is_available", False))
        self._log(f"group: {len(live)} account(s), {len(missions)} mission(s), {idle} idle vehicle(s)")

        plan = plan_group_dispatches(
            missions, vehicles_by_account, buildings_by_account, resolver,
            self._policy, now=time.time(),
        )

        # Dispatch each account's share (one bulk POST per mission).
        dispatched = 0
        for aid, mission_plans in plan.by_account.items():
            member = live.get(aid)
            if member is None:
                continue
            veh_by_id = {v.id: v for v in vehicles_by_account.get(aid, [])}
            for mp in mission_plans:
                ids = list(mp.vehicle_ids)
                if not ids:
                    continue
                if await member.client.dispatch_many(mp.mission_id, ids):
                    dispatched += len(ids)
                    names = ", ".join(_name(veh_by_id.get(v), v) for v in ids)
                    self._log(f"[#{aid}] → #{mp.mission_id} {mp.caption!r}: {names}")
                    if self._on_stat is not None:
                        self._on_stat(aid, len(ids), 1)
                else:
                    self._log(f"[#{aid}] batch failed → #{mp.mission_id} {mp.caption!r}")
                await self._sleep(self._gap)

        # Transport phase per member (FMS-5 -> hospital/cell).
        for aid, member in live.items():
            if member.transport_enabled:
                try:
                    await member.handle_transports(vehicles_by_account.get(aid, []))
                except Exception:  # noqa: BLE001
                    logger.exception("group transport for %s failed", aid)

        self._log(
            f"group cycle done: {plan.missions_actioned} mission(s), {dispatched} vehicle(s), "
            f"{len(plan.skipped)} skipped"
        )
        return plan

    async def _share_own(self, aid, member, missions) -> None:
        """Share this member's OWN (not-yet-shared) missions to the alliance."""
        for m in missions:
            if getattr(m, "is_alliance", False):
                continue
            key = (aid, m.id)
            if key in self._shared:
                continue
            if await member.client.share_mission(m.id):
                self._shared.add(key)
                self._log(f"[#{aid}] shared #{m.id} {getattr(m, 'caption', '')!r} to the alliance")
            await self._sleep(self._gap)

    async def aclose(self) -> None:
        for member in self._members.values():
            try:
                await member.aclose()
            except Exception:  # noqa: BLE001
                logger.exception("closing group member failed")


def _name(vehicle, vehicle_id: int) -> str:
    if vehicle is None:
        return f"#{vehicle_id}"
    label = getattr(vehicle, "type_caption", "") or getattr(vehicle, "caption", "")
    return f"{label} (#{vehicle_id})" if label else f"#{vehicle_id}"
