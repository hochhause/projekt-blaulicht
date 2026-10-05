"""Group ("symbiotic") coordinator — one loop driving many accounts together.

Each cycle it:

1. **connects** every member once (sessions persist across cycles after that —
   this is per-account isolation, not concurrency: login only ever happens
   ONCE per member for the group's whole lifetime, so there's no per-cycle
   cost to keeping it sequential and unstaggered);
2. **reads** each member's vehicles/buildings and first-pass own missions
   CONCURRENTLY — one coroutine per account, each launched with its own
   small random lead-in delay so the group's accounts don't all hit the
   server in a literal simultaneous burst (Sasha, 2026-09-12: "overlapping
   but individually jittered", not a synchronized gather);
3. **shares** every member's own missions to the alliance FIRST — before
   planning, also concurrently — so a mission shared this cycle can already
   be joined by teammates in the SAME cycle instead of only from the next
   one (docs/MODES-TODO.md §5: supersedes the old end-of-cycle share);
4. **re-reads** missions (concurrently) now that sharing has happened, so
   the union and the ``visible_to`` map reflect what just got shared —
   ``visible_to`` itself is kept as a safety net for the (expected,
   occasional) case the game hasn't propagated a share instantly, so a plan
   never assigns an account to a mission it still can't see;
5. runs each member's **transport** phase, concurrently;
6. **plans** globally via :func:`app.domain.group.plan_group_dispatches` — one
   combined crew per mission (no over-send), a participation token for every
   member (so all earn the reward), and the combined pool excludes any
   vehicle struck out on ITS OWNER's own :class:`~app.domain.strikes.StrikeTracker`
   (each member's, same as solo dispatch — a chronically-uncrewed vehicle
   never gets silently re-selected forever);
7. **dispatches** — each account now runs its OWN independent async worker
   through its share of the plan (sequential within an account, own
   delay/jitter, but every account's worker runs CONCURRENTLY with every
   other's — Sasha, 2026-09-12: "everyone with their own delay and jitter"),
   retrying a failed batch one vehicle at a time to isolate and strike the
   one that actually won't go (mirrors :class:`~app.core.runner.DispatchRunner`);
   and
8. **backfills** — any vehicle that failed to make it (confirmed-rejected or
   plain-failed) gets ONE substitute attempt from whichever member still has
   a free vehicle of the same type and can see the mission ("if a vehicle
   could not be sent out another account covers it if possible").

Trade-off note on step 7 (Sasha, 2026-09-12, explicitly chosen over the old
"one mission fully staffed by everyone before moving on" guarantee): running
every account's dispatch worker concurrently means an interruption mid-cycle
can leave a mission half-joined rather than cleanly finished. Nothing is
actually lost by that — the next cycle's fresh plan re-derives whatever's
still outstanding and tops it up — it's only delayed by one cycle (typically
well under a minute), in exchange for real wall-clock speed.

It's shaped like an :class:`AccountWorker` runner (``run_cycle`` / ``aclose``), so
the orchestrator supervises it with the same back-off machinery.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from ..domain.dispatch import _distance_fn
from ..domain.group import GroupPolicy, merge_building_positions, plan_group_dispatches

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
        self._sleep = asyncio.sleep
        # Ambulance/NEF sticky memory (MissionRequirementResolver.
        # effective_count_revealed) pooled across ALL members, not kept
        # per-account: a shared mission's casualty count being authoritatively
        # revealed should count for the whole group, regardless of which
        # member's resolver `plan_group_dispatches` happens to use that cycle
        # (see the `live[next(iter(live))].resolver` pick below) — otherwise a
        # member reconnecting (or simply not being the one picked that cycle)
        # would silently lose a reveal another member already witnessed.
        # Delegates to AccountRunner.adopt_ever_revealed (mirrors the public
        # `strikes` property's existing precedent for sharing a member's
        # per-account state with the group, rather than poking its private
        # attribute directly) — that method also repoints an already-built
        # resolver's own reference, not just this attribute, so it stays
        # correct even for a member that somehow connected before being
        # handed to the group. Falls back to the plain attribute for a test
        # double that doesn't model the full AccountRunner surface.
        self._ever_revealed: set[int] = set()
        for member in self._members.values():
            adopt = getattr(member, "adopt_ever_revealed", None)
            if adopt is not None:
                adopt(self._ever_revealed)
            else:
                self._ever_revealed |= getattr(member, "_ever_revealed", None) or set()
                member._ever_revealed = self._ever_revealed

    def _text_engine(self, resolver):
        """Regex requirement engine (for parsing missing_text), built once."""
        if getattr(self, "_engine", None) is None:
            from ..domain.requirements import RequirementEngine
            self._engine = RequirementEngine(resolver.library)
        return self._engine

    def _jittered_gap(self) -> float:
        """A human-ish gap: the base gap plus up to 100% jitter."""
        return random.uniform(self._gap, self._gap * 2.0)

    def _lead_jitter(self) -> float:
        """Small random delay before a concurrent per-account task actually
        fires its request — reuses the same ``dispatch_gap`` dial so several
        accounts' reads/shares/sends overlap in wall time instead of landing
        at the exact same instant (a more obviously-scripted signature) or,
        at the other extreme, running strictly one after another."""
        return random.uniform(0, self._gap)

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

    async def _fetch_fleet(self, aid, member) -> tuple[list, list]:
        """Vehicles + buildings for one account, each isolated the same way
        ``_safe_fetch_missions`` is — a flaky account contributes nothing to
        the pool/positions for this cycle rather than crashing the group."""
        try:
            vehicles = await member.client.fetch_vehicles(member.vehicle_cap)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[#{aid}] fetch_vehicles failed: {exc}")
            vehicles = []
        try:
            buildings = await self._buildings_for(aid, member)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[#{aid}] fetch_buildings failed: {exc}")
            buildings = []
        return vehicles, buildings

    async def _handle_transport_safe(self, aid, member, vehicles) -> None:
        if not member.transport_enabled:
            return
        try:
            await member.handle_transports(vehicles)
        except Exception:  # noqa: BLE001
            logger.exception("group transport for %s failed", aid)

    async def _gather_jittered(self, aid_coro_pairs):
        """Run one coroutine per account CONCURRENTLY, each preceded by its
        own small random delay (``_lead_jitter``). Every coroutine passed in
        is expected to already isolate its own per-account failures (same
        convention as ``_safe_fetch_missions``/``_fetch_fleet``/
        ``_handle_transport_safe``), so this helper adds concurrency and
        stagger only, no error handling of its own. Returns
        ``{account_id: result}`` (``result`` is ``None`` for a coroutine that
        returns nothing, e.g. the share phase)."""
        async def _lead_in(aid, coro):
            await self._sleep(self._lead_jitter())
            return aid, await coro

        pairs = list(aid_coro_pairs)
        if not pairs:
            return {}
        results = await asyncio.gather(*(_lead_in(aid, coro) for aid, coro in pairs))
        return dict(results)

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
        # them is the one used for planning too. Concurrent across members.
        fleets = await self._gather_jittered(
            (aid, self._fetch_fleet(aid, member)) for aid, member in live.items()
        )
        vehicles_by_account: dict[int, list] = {aid: v for aid, (v, _b) in fleets.items()}
        buildings_by_account: dict[int, list] = {aid: b for aid, (_v, b) in fleets.items()}
        building_pos = merge_building_positions(buildings_by_account)

        # SHARE FIRST — before planning — so a mission shared this cycle can
        # already be joined by teammates in the SAME cycle (bounded per
        # account so a big backlog doesn't stall the loop). A first read of
        # each member's own missions, just enough to find what's unshared.
        # Both the read and the share itself run one coroutine per account,
        # concurrently.
        if self._share_enabled:
            first_pass = await self._gather_jittered(
                (aid, self._safe_fetch_missions(aid, member)) for aid, member in live.items()
            )
            await self._gather_jittered(
                (aid, self._share_own(aid, member, first_pass.get(aid, [])))
                for aid, member in live.items()
            )

        # RE-FETCH missions now that sharing has happened, so the union and
        # visible_to reflect what just got shared. visible_to remains the
        # SAFETY NET for the mission the game hasn't propagated visibility
        # for instantly — an account still can't be assigned to one it can't
        # (yet) see. Concurrent across members.
        missions_by_account = await self._gather_jittered(
            (aid, self._safe_fetch_missions(aid, member)) for aid, member in live.items()
        )
        missions_by_id: dict[int, object] = {}
        for missions in missions_by_account.values():
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

        # PRIO 1 — transport (Sprechwünsche) first, per member. Concurrent.
        await self._gather_jittered(
            (aid, self._handle_transport_safe(aid, member, vehicles_by_account.get(aid, [])))
            for aid, member in live.items()
        )

        plan = plan_group_dispatches(
            missions, vehicles_by_account, buildings_by_account, resolver,
            self._policy, now=time.time(), text_engine=self._text_engine(resolver),
            visible_to=visible_to,
            strikes_by_account={aid: member.strikes for aid, member in live.items()},
        )

        # DISPATCH — one independent async worker per account, each working
        # through its OWN share of the plan (in the plan's priority order)
        # sequentially with its own delay/jitter; every account's worker runs
        # CONCURRENTLY with every other's (see module docstring, step 7, for
        # the trade-off this makes against the old strictly-sequential loop).
        veh_by_id = {v.id: v for vs in vehicles_by_account.values() for v in vs}
        per_account_assignments: dict[int, list] = {aid: [] for aid in live}
        for gmp in plan.missions:
            for aid, ids in gmp.by_account.items():
                if aid in per_account_assignments and ids:
                    per_account_assignments[aid].append(gmp)

        async def _run_account(aid):
            # A last-resort net for a genuinely unexpected bug — the EXPECTED
            # failure mode (a transient network error from one mission's
            # dispatch call) is now caught per-mission inside
            # _dispatch_account itself (code review, 2026-09-12: this used to
            # catch here and discard every vehicle the account had already
            # legitimately sent earlier in its own loop, opening a
            # double-dispatch window in _backfill).
            try:
                return await self._dispatch_account(
                    aid, live[aid], per_account_assignments[aid], missions_by_id, plan, veh_by_id,
                )
            except Exception:  # noqa: BLE001 — isolate one account's crash from the rest
                logger.exception("group dispatch for %s failed", aid)
                return 0, [], set()

        active = [aid for aid in live if per_account_assignments[aid]]
        results = list(await asyncio.gather(*(_run_account(aid) for aid in active))) if active else []
        dispatched = sum(r[0] for r in results)
        failures: list[tuple[int, int | None]] = [f for r in results for f in r[1]]
        consumed_ids: set[int] = set()
        for r in results:
            consumed_ids |= r[2]

        # BACKFILL — "if a vehicle could not be sent out another account
        # covers it if possible" (Sasha, 2026-09-12): a single substitute
        # attempt per failed vehicle, from whatever's genuinely still free.
        if failures:
            dispatched += await self._backfill(
                failures, missions_by_id, live, vehicles_by_account, consumed_ids, building_pos, visible_to,
            )

        self._log(
            f"group cycle done: {plan.missions_actioned} mission(s), {dispatched} vehicle(s), "
            f"{len(plan.skipped)} skipped"
        )
        return plan

    async def _dispatch_account(self, aid, member, assignments, missions_by_id, plan, veh_by_id):
        """Send this account's share of every mission in *assignments* (plan
        order) sequentially, with its own gap/jitter between sends — runs
        concurrently with every other account's identical loop (see
        run_cycle). Returns ``(dispatched_count, failures, consumed_ids)``,
        where *failures* is a list of ``(mission_id, type_id)`` for every
        vehicle that didn't make it (confirmed-rejected or plain-failed),
        feeding the end-of-cycle cross-account backfill pass."""
        dispatched = 0
        failures: list[tuple[int, int | None]] = []
        consumed: set[int] = set()
        await self._sleep(self._lead_jitter())  # independent lead-in, not a synchronized burst
        for gmp in assignments:
            ids = list(gmp.by_account.get(aid, ()))
            if not ids:
                continue
            consumed.update(ids)
            mission_obj = missions_by_id.get(gmp.mission_id)
            already_participating = plan.participating.get(gmp.mission_id, frozenset())
            verb = _dispatch_verb(mission_obj, already_participating, aid)
            try:
                result = await member.client.dispatch_many(gmp.mission_id, ids)
            except Exception as exc:  # noqa: BLE001 — isolate THIS mission's transient
                # failure from the rest of this account's queue (code review,
                # 2026-09-12): a bare exception here used to propagate all the
                # way out to _run_account's catch-all, which discarded every
                # vehicle this account had already genuinely dispatched
                # earlier in the SAME loop — losing them from `consumed` and
                # opening a window for _backfill to re-dispatch an
                # already-busy vehicle to a second, unrelated mission.
                self._log(f"[#{aid}] dispatch → #{gmp.mission_id} {gmp.caption!r} failed: {exc}")
                continue
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
                    self._log(
                        f"[#{aid}] {verb} {len(succeeded)} → #{gmp.mission_id} {gmp.caption!r}: {names}"
                    )
                for vehicle_id, reason in result.rejected.items():
                    name = _name(veh_by_id.get(vehicle_id), vehicle_id)
                    self._log(f"[#{aid}] struck out {name}: game rejected it ({reason})")
                    member.strikes.record_confirmed_failure(str(vehicle_id), strike_now)
                    failures.append((gmp.mission_id, _type_of(veh_by_id.get(vehicle_id))))
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
                    try:
                        single = await member.client.dispatch(gmp.mission_id, vehicle_id)
                    except Exception as exc:  # noqa: BLE001 — isolate this one vehicle's
                        # transient failure from the rest of the retry loop, same
                        # reasoning as the batch try/except above.
                        self._log(f"[#{aid}] dispatch {name} → #{gmp.mission_id} {gmp.caption!r} failed: {exc}")
                        await self._sleep(self._jittered_gap())
                        continue
                    if single.ok and vehicle_id not in single.rejected:
                        dispatched += 1
                        sent += 1
                        self._log(f"[#{aid}] {verb} → #{gmp.mission_id} {gmp.caption!r}: {name}")
                    else:
                        reason = single.rejected.get(vehicle_id)
                        self._log(f"[#{aid}] FAILED {name} → #{gmp.mission_id} {gmp.caption!r} ({reason or 'no crew?'})")
                        failures.append((gmp.mission_id, _type_of(veh_by_id.get(vehicle_id))))
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
        return dispatched, failures, consumed

    async def _backfill(self, failures, missions_by_id, live, vehicles_by_account, consumed_ids,
                         building_pos, visible_to) -> int:
        """End-of-cycle pass (Sasha, 2026-09-12: "if a vehicle could not be
        sent out another account covers it if possible"): for every vehicle
        that failed to dispatch this cycle, try ONE substitute of the SAME
        type from whichever member still has a free one AND can actually see
        the mission (code review, 2026-09-12: a substitute from an account
        outside ``visible_to[mission_id]`` would be a silent no-op at best —
        the mission isn't even in that account's own feed — mirroring the
        same guard ``plan_group_dispatches`` applies via its own ``_visible``
        helper). Deliberately narrow otherwise — a straight type-for-type
        swap against the pool as it stands after the main dispatch pass, not
        a full replan (no ELW/MZB/participation-token logic here):
        reconciling a second full coverage pass with vehicles that were
        CHOSEN but never actually left the station is a materially bigger,
        riskier change than what was asked for. One substitute attempt per
        failure — a substitute that ALSO fails is just logged (and picked up
        fresh by the next cycle's full replan), no further retry chaining
        within this cycle."""
        now = time.time()
        consumed = set(consumed_ids)
        dispatched = 0
        for mission_id, type_id in failures:
            if type_id is None:
                continue
            mission_obj = missions_by_id.get(mission_id)
            if mission_obj is None:
                continue
            vis = visible_to.get(mission_id, set())
            candidates = []
            for aid, member in live.items():
                if aid not in vis:
                    continue
                for v in vehicles_by_account.get(aid, []):
                    if v.id in consumed or v.type_id != type_id:
                        continue
                    if not getattr(v, "is_available", False):
                        continue
                    if member.strikes.is_struck(str(v.id), now):
                        continue
                    candidates.append((aid, member, v))
            if not candidates:
                continue
            pos = getattr(mission_obj, "position", None)
            if pos is not None:
                dist = _distance_fn(pos, building_pos)
                candidates.sort(key=lambda c: dist(c[2]))
            aid, member, vehicle = candidates[0]
            consumed.add(vehicle.id)
            name = _name(vehicle, vehicle.id)
            try:
                result = await member.client.dispatch(mission_id, vehicle.id)
            except Exception as exc:  # noqa: BLE001 — one candidate's transient
                # failure shouldn't abort backfill for the REST of this
                # cycle's failures.
                self._log(f"[#{aid}] backfill dispatch → #{mission_id} ({name}) failed: {exc}")
                await self._sleep(self._jittered_gap())
                continue
            if result.ok and vehicle.id not in result.rejected:
                dispatched += 1
                self._log(f"[#{aid}] covered a failed send → #{mission_id}: {name}")
                if self._on_stat is not None:
                    self._on_stat(aid, 1, 0)
            else:
                reason = result.rejected.get(vehicle.id) if result.ok else None
                self._log(f"[#{aid}] backfill FAILED for #{mission_id} ({name}): {reason or 'no crew?'}")
                if reason is not None:
                    member.strikes.record_confirmed_failure(str(vehicle.id), now)
            await self._sleep(self._jittered_gap())
        return dispatched

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


def _type_of(vehicle) -> int | None:
    return getattr(vehicle, "type_id", None) if vehicle is not None else None


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
