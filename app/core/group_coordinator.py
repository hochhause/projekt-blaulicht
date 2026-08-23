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
import random
import time

from ..domain.group import GroupPolicy, plan_group_dispatches

logger = logging.getLogger(__name__)


class GroupCoordinator:
    """Drives a set of member :class:`AccountRunner`s as one symbiotic group."""

    def __init__(self, members, policy: GroupPolicy, *, on_log=None, on_stat=None,
                 dispatch_gap=1.2, share_enabled=True, share_cap=15):
        # members: dict[int, AccountRunner]
        self._members = members
        self._policy = policy
        self._on_log = on_log
        self._on_stat = on_stat
        self._gap = dispatch_gap
        self._share_enabled = share_enabled
        self._share_cap = share_cap  # own missions shared per account per cycle
        self._buildings: dict[int, list] = {}
        self._shared: set[tuple[int, int]] = set()  # (account_id, mission_id) already shared
        import asyncio
        self._sleep = asyncio.sleep

    def _jittered_gap(self) -> float:
        """A human-ish gap: the base gap plus up to 100% jitter."""
        return random.uniform(self._gap, self._gap * 2.0)

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

        # Read (no sharing yet — sharing is bounded and runs at the end so a big
        # backlog of own missions can't make the cycle look dead).
        vehicles_by_account: dict[int, list] = {}
        buildings_by_account: dict[int, list] = {}
        missions_by_account: dict[int, list] = {}
        missions_by_id: dict[int, object] = {}
        for aid, member in live.items():
            vehicles_by_account[aid] = await member.client.fetch_vehicles(member.vehicle_cap)
            buildings_by_account[aid] = await self._buildings_for(aid, member)
            missions = await member.client.fetch_missions()
            missions_by_account[aid] = missions
            for m in missions:
                missions_by_id.setdefault(m.id, m)  # union / dedup

        missions = list(missions_by_id.values())
        resolver = live[next(iter(live))].resolver
        idle = sum(1 for vs in vehicles_by_account.values() for v in vs if getattr(v, "is_available", False))
        self._log(f"group: {len(live)} account(s), {len(missions)} mission(s), {idle} idle vehicle(s)")

        # PRIO 1 — transport (Sprechwünsche) first, per member.
        for aid, member in live.items():
            if member.transport_enabled:
                try:
                    await member.handle_transports(vehicles_by_account.get(aid, []))
                except Exception:  # noqa: BLE001
                    logger.exception("group transport for %s failed", aid)

        plan = plan_group_dispatches(
            missions, vehicles_by_account, buildings_by_account, resolver,
            self._policy, now=time.time(),
        )

        # Dispatch MISSION BY MISSION in sort order, so each mission is fully
        # staffed by every participant before we move on (an interruption then
        # leaves completed missions, not half-joined ones). Within a mission the
        # account order is shuffled and gaps jittered so it's less robotic.
        veh_by_id = {v.id: v for vs in vehicles_by_account.values() for v in vs}
        dispatched = 0
        for gmp in plan.missions:
            senders = [(aid, ids) for aid, ids in gmp.by_account.items() if ids]
            random.shuffle(senders)
            for aid, ids in senders:
                member = live.get(aid)
                if member is None:
                    continue
                ids = list(ids)
                if await member.client.dispatch_many(gmp.mission_id, ids):
                    dispatched += len(ids)
                    names = ", ".join(_name(veh_by_id.get(v), v) for v in ids)
                    self._log(f"[#{aid}] → #{gmp.mission_id} {gmp.caption!r}: {names}")
                    if self._on_stat is not None:
                        self._on_stat(aid, len(ids), 1)
                else:
                    self._log(f"[#{aid}] batch failed → #{gmp.mission_id} {gmp.caption!r}")
                await self._sleep(self._jittered_gap())

        # Share each member's own missions so others can join them next cycle —
        # bounded per cycle so a backlog trickles instead of stalling the loop.
        if self._share_enabled:
            for aid, member in live.items():
                await self._share_own(aid, member, missions_by_account.get(aid, []))

        self._log(
            f"group cycle done: {plan.missions_actioned} mission(s), {dispatched} vehicle(s), "
            f"{len(plan.skipped)} skipped"
        )
        return plan

    async def _share_own(self, aid, member, missions) -> None:
        """Share this member's OWN (not-yet-shared) missions to the alliance,
        capped per cycle so a big backlog doesn't block the loop. Missions below
        the policy's share_min_credits are left unshared."""
        floor = getattr(self._policy, "share_min_credits", 0) or 0
        own = [
            m for m in missions
            if not getattr(m, "is_alliance", False)
            and (aid, m.id) not in self._shared
            and (floor <= 0 or int(getattr(m, "average_credits", 0) or 0) >= floor)
        ]
        if not own:
            return
        batch = own[: self._share_cap]
        self._log(f"[#{aid}] sharing {len(batch)} of {len(own)} own mission(s) to the alliance")
        for m in batch:
            if await member.client.share_mission(m.id):
                self._shared.add((aid, m.id))
            await self._sleep(self._jittered_gap())

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
