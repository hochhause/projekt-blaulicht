"""Group ("symbiotic") coordinator — one loop driving many accounts together.

Each cycle it:

1. **reads** each member's own missions (a first pass, just enough to find
   what's unshared);
2. **shares** every member's own missions to the alliance FIRST — before
   planning — so a mission shared this cycle can already be joined by
   teammates in the SAME cycle instead of only from the next one
   (docs/MODES-TODO.md §5: supersedes the old end-of-cycle share);
3. **re-reads** missions now that sharing has happened, so the union and the
   ``visible_to`` map reflect what just got shared — ``visible_to`` itself is
   kept as a safety net for the (expected, occasional) case the game hasn't
   propagated a share instantly, so a plan never assigns an account to a
   mission it still can't see;
4. **plans** globally via :func:`app.domain.group.plan_group_dispatches` — one
   combined crew per mission (no over-send), a participation token for every
   member (so all earn the reward), and the combined pool excludes any
   vehicle struck out on ITS OWNER's own :class:`~app.domain.strikes.StrikeTracker`
   (each member's, same as solo dispatch — a chronically-uncrewed vehicle
   never gets silently re-selected forever);
5. **dispatches** each account's own share in one bulk POST, retrying a
   failed batch one vehicle at a time to isolate and strike the one that
   actually won't go (mirrors :class:`~app.core.runner.DispatchRunner`); and
6. runs each member's **transport** phase.

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

    def _text_engine(self, resolver):
        """Regex requirement engine (for parsing missing_text), built once."""
        if getattr(self, "_engine", None) is None:
            from ..domain.requirements import RequirementEngine
            self._engine = RequirementEngine(resolver.library)
        return self._engine

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

    async def _safe_fetch_missions(self, aid, member) -> list:
        """fetch_missions(), isolating a transient per-account failure (mirrors
        the connect() isolation below) instead of aborting the whole cycle —
        this account just contributes nothing to the union/visible_to for
        this pass; its vehicles/transport still process normally."""
        try:
            return await member.client.fetch_missions()
        except Exception as exc:  # noqa: BLE001
            self._log(f"[#{aid}] fetch_missions failed: {exc}")
            return []

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

        # Vehicles/buildings don't change from sharing, so this one read of
        # them is the one used for planning too.
        vehicles_by_account: dict[int, list] = {}
        buildings_by_account: dict[int, list] = {}
        for aid, member in live.items():
            vehicles_by_account[aid] = await member.client.fetch_vehicles(member.vehicle_cap)
            buildings_by_account[aid] = await self._buildings_for(aid, member)

        # SHARE FIRST — before planning — so a mission shared this cycle can
        # already be joined by teammates in the SAME cycle (bounded per
        # account so a big backlog doesn't stall the loop). A first read of
        # each member's own missions, just enough to find what's unshared.
        if self._share_enabled:
            for aid, member in live.items():
                first_pass = await self._safe_fetch_missions(aid, member)
                await self._share_own(aid, member, first_pass)

        # RE-FETCH missions now that sharing has happened, so the union and
        # visible_to reflect what just got shared. visible_to remains the
        # SAFETY NET for the mission the game hasn't propagated visibility
        # for instantly — an account still can't be assigned to one it can't
        # (yet) see.
        missions_by_account: dict[int, list] = {}
        missions_by_id: dict[int, object] = {}
        for aid, member in live.items():
            missions = await self._safe_fetch_missions(aid, member)
            missions_by_account[aid] = missions
            for m in missions:
                missions_by_id.setdefault(m.id, m)  # union / dedup

        missions = list(missions_by_id.values())
        # Which accounts can currently SEE each mission (own + shared-to-them) — an
        # account can only dispatch to a mission in its own feed.
        visible_to: dict[int, set[int]] = {}
        for aid, ms in missions_by_account.items():
            for m in ms:
                visible_to.setdefault(m.id, set()).add(aid)
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
            self._policy, now=time.time(), text_engine=self._text_engine(resolver),
            visible_to=visible_to,
            strikes_by_account={aid: member.strikes for aid, member in live.items()},
        )

        # Dispatch MISSION BY MISSION in sort order, so each mission is fully
        # staffed by every participant before we move on (an interruption then
        # leaves completed missions, not half-joined ones). Within a mission the
        # account order is shuffled and gaps jittered so it's less robotic.
        veh_by_id = {v.id: v for vs in vehicles_by_account.values() for v in vs}
        dispatched = 0
        for gmp in plan.missions:
            mission_obj = missions_by_id.get(gmp.mission_id)
            already_participating = plan.participating.get(gmp.mission_id, frozenset())
            senders = [(aid, ids) for aid, ids in gmp.by_account.items() if ids]
            random.shuffle(senders)
            for aid, ids in senders:
                member = live.get(aid)
                if member is None:
                    continue
                ids = list(ids)
                verb = _dispatch_verb(mission_obj, already_participating, aid)
                result = await member.client.dispatch_many(gmp.mission_id, ids)
                if result.ok:
                    # The POST succeeded, but the game may have explicitly
                    # rejected specific vehicles within it (missing crew /
                    # its towing vehicle isn't at the station — live-verified
                    # 2026-09-11) rather than erroring the whole request.
                    strike_now = time.time()
                    succeeded = [v for v in ids if v not in result.rejected]
                    dispatched += len(succeeded)
                    if succeeded:
                        names = ", ".join(_name(veh_by_id.get(v), v) for v in succeeded)
                        self._log(f"[#{aid}] {verb} → #{gmp.mission_id} {gmp.caption!r}: {names}")
                    for vehicle_id, reason in result.rejected.items():
                        name = _name(veh_by_id.get(vehicle_id), vehicle_id)
                        self._log(f"[#{aid}] struck out {name}: game rejected it ({reason})")
                        member.strikes.record_confirmed_failure(str(vehicle_id), strike_now)
                    if self._on_stat is not None and succeeded:
                        self._on_stat(aid, len(succeeded), 1)
                    await self._sleep(self._jittered_gap())
                else:
                    # Batch failed — retry one-by-one so we can isolate and
                    # strike the vehicle that actually won't go (e.g. a
                    # crewless truck), same as solo dispatch
                    # (DispatchRunner.run_cycle) — otherwise a chronically bad
                    # vehicle stays in the combined pool forever, sinking
                    # every good vehicle bundled with it in the failed batch.
                    self._log(f"[#{aid}] batch failed → #{gmp.mission_id} {gmp.caption!r} — retrying individually")
                    strike_now = time.time()
                    sent = 0
                    for vehicle_id in ids:
                        name = _name(veh_by_id.get(vehicle_id), vehicle_id)
                        single = await member.client.dispatch(gmp.mission_id, vehicle_id)
                        if single.ok and vehicle_id not in single.rejected:
                            dispatched += 1
                            sent += 1
                            self._log(f"[#{aid}] {verb} → #{gmp.mission_id} {gmp.caption!r}: {name}")
                        else:
                            reason = single.rejected.get(vehicle_id)
                            self._log(f"[#{aid}] FAILED {name} → #{gmp.mission_id} {gmp.caption!r} ({reason or 'no crew?'})")
                            if reason is not None:
                                member.strikes.record_confirmed_failure(str(vehicle_id), strike_now)
                                self._log(f"[#{aid}] struck out {name}: game rejected it ({reason})")
                            else:
                                member.strikes.record([str(vehicle_id)], strike_now)
                                if member.strikes.is_struck(str(vehicle_id), strike_now):
                                    self._log(f"[#{aid}] struck out {name}: failed to dispatch 3× — skipping 24h")
                        await self._sleep(self._jittered_gap())
                    # One mission-send counted, like the batch-success path —
                    # not once per vehicle (that inflated the dashboard's
                    # "missions" stat on any multi-vehicle partial retry).
                    if self._on_stat is not None and sent:
                        self._on_stat(aid, sent, 1)

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


def _dispatch_verb(mission, participating: frozenset, aid: int) -> str:
    """Same "dispatched"/"sent to help"/"sent reinforcements" distinction as
    solo dispatch's ``runner._dispatch_verb`` (Sasha, 2026-09-11): reinforcing
    if THIS account already had a vehicle on the mission before this cycle
    (``participating``, from ``GroupPlan.participating``), helping if someone
    else (another member or an alliance member) already was, else a genuine
    first-responder send."""
    if aid in participating:
        return "sent reinforcements"
    if getattr(mission, "vehicle_state", 0) >= 1:
        return "sent to help"
    return "dispatched"
