"""Group ("symbiotic") planner — one plan across MANY accounts.

The single-account planner in :mod:`app.domain.dispatch` treats each account in
isolation. Group mode instead pools **all** member accounts and plans jointly so
that, for every mission:

* the combined fleet crews it once — nobody over-sends (a **global delta** counts
  every account's en-route vehicles), and the minimal combined set is chosen;
* **every** member has at least one vehicle on it (a participation "token"), so —
  because the game pays each participant — everyone earns the reward.

Vehicles are duck-typed exactly as in :mod:`dispatch`. A vehicle's position is
its building's; building ids are game-globally unique, so one merged
``building_id -> position`` map serves every account's vehicles. Ownership
(which account may actually send a vehicle) is tracked separately for routing.

This module is pure and network-free; :class:`app.core.group_coordinator` does
the I/O around it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .coverage import select_coverage
from .dispatch import (
    _ELW_SPECIALIST_TYPE_IDS,
    _SEG_ELW_PREFERRED_PATIENT_THRESHOLD,
    _SEG_ELW_TYPE_ID,
    DispatchOrder,
    _add_water_rescue_escort,
    _distance_fn,
    _fire_crew,
    _fire_personnel_topup,
    _order,
    _outstanding,
    _reserve_vehicle_group,
)
from .geo import nearest_km
from .resolver import _casualties


class RadiusBasis(str, Enum):
    """Whether a mission must be in range of ANY member or ALL members."""

    ANY = "any"
    ALL = "all"


@dataclass(frozen=True)
class GroupPolicy:
    """Shared dispatch settings for a running group."""

    radius_km: float = 40.0
    credit_min: int = 0          # 0 = no floor
    credit_max: int = 0          # 0 = no ceiling
    order: DispatchOrder = DispatchOrder.NEAREST
    batch_size: int | None = None
    allowed_categories: frozenset[str] = frozenset()
    # Guarantee every member contributes ≥1 vehicle to each worked mission so all
    # earn the reward (the point of the mode).
    participation: bool = True
    radius_basis: RadiusBasis = RadiusBasis.ANY
    # Cap participation-token sends per account per cycle (footprint control; None
    # = no cap). Does not limit the crew that actually completes a mission.
    max_token_per_account: int | None = None
    # Allow starting a FRESH mission with only some of its vehicles (the rest
    # follow over later cycles as they free up) — but only if we own the missing
    # types somewhere in the fleet, so we never start what we can't finish.
    partial_starts: bool = False
    # Only share own missions worth at least this many credits to the alliance
    # (0 = share all). Keeps the low-value clutter unshared.
    share_min_credits: int = 0


@dataclass
class GroupMissionPlan:
    """One mission's group plan: which vehicles each account sends to it."""

    mission_id: int
    caption: str
    by_account: dict[int, tuple[int, ...]]  # account_id -> vehicle ids for THIS mission
    satisfied: bool
    shortfall: dict[str, int]


@dataclass
class GroupPlan:
    # Missions in dispatch order — the coordinator sends them one at a time so
    # each is fully staffed by all participants before moving on.
    missions: list[GroupMissionPlan] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)
    shortfalls: dict[str, int] = field(default_factory=dict)  # class -> #missions short
    # mission id -> account ids that ALREADY had a vehicle on it BEFORE this
    # cycle's dispatch (i.e. pre-existing global_assigned participation) — for
    # the coordinator's "dispatched"/"sent to help"/"sent reinforcements" log
    # verb (Sasha, 2026-09-11), mirroring solo dispatch's same distinction.
    participating: dict[int, frozenset[int]] = field(default_factory=dict)

    @property
    def total_dispatches(self) -> int:
        return sum(len(ids) for gmp in self.missions for ids in gmp.by_account.values())

    @property
    def missions_actioned(self) -> int:
        return len(self.missions)


def _note(shortfalls, classes) -> None:
    for cls in classes:
        shortfalls[cls] = shortfalls.get(cls, 0) + 1


def _smart_efficiency(mission, mtype, credits, resolver, assigned, text_engine, avg_fire_crew) -> float:
    """Credits per vehicle needed -- same SMART-order heuristic as solo
    dispatch (Sasha, 2026-09-11: "the sharing can work on a much faster
    clock speed... a lot of the settings seem missing from group mode, such
    as smart planning"). Reuses `_outstanding`, the SAME requirement
    derivation the real dispatch step (`action()`) uses, so the estimate
    matches what will actually be sent -- only decides PRIORITY order
    (which mission group mode works first), not the goal itself; the
    combined-pool dispatch loop still tries every candidate in this order
    and crews everything it can, so the outcome is "the most total credits
    this GROUP's combined fleet can earn right now."""
    outstanding = _outstanding(mission, resolver, text_engine, assigned)
    vehicles_needed = sum(r.count for r in outstanding)
    min_fire = int(getattr(mtype, "min_fire_personnel", 0) or 0) if mtype is not None else 0
    if min_fire:
        already = _fire_crew(resolver, assigned.get(mission.id, []))
        fire_needed = max(0, min_fire - already)
        vehicles_needed += -(-fire_needed // max(1, int(avg_fire_crew)))  # ceil division
    return credits / max(1, vehicles_needed)


def plan_group_dispatches(
    missions,
    vehicles_by_account: dict[int, list],
    buildings_by_account: dict[int, list],
    resolver,
    policy: GroupPolicy,
    now: float | None = None,
    text_engine=None,
    visible_to: dict[int, set[int]] | None = None,
    strikes_by_account: dict[int, object] | None = None,
) -> GroupPlan:
    """Plan one cycle across all member accounts. Returns per-account sends.

    *visible_to* maps mission id -> the accounts whose feed currently contains it.
    An account can only be assigned to dispatch to a mission it can actually see
    (its own + alliance-shared) — otherwise the POST is a silent no-op. ``None``
    means every member sees every mission (used in tests).

    *strikes_by_account* maps account id -> that account's
    :class:`~app.domain.strikes.StrikeTracker` — a vehicle struck out on ITS
    OWNER's tracker (repeatedly failed to dispatch, e.g. no crew) is excluded
    from the combined pool, same as solo dispatch. ``None`` (or a missing
    entry, or *now* unset) skips this filtering entirely."""
    plan = GroupPlan()
    member_ids = list(vehicles_by_account.keys())
    member_set = set(member_ids)

    # Average firetruck crew size, for SMART order's rough min_fire_personnel
    # estimate below (same as solo dispatch's avg_fire_crew).
    _fire_types = [
        vt for tid, vt in resolver.library.types.items()
        if tid in resolver.library.class_map.by_class.get("firetrucks", ())
    ]
    avg_fire_crew = (sum(vt.planning_personnel for vt in _fire_types) / len(_fire_types)) if _fire_types else 6.0

    def _visible(mid: int) -> set[int]:
        return member_set if visible_to is None else visible_to.get(mid, set())

    # Per-account radius origins, and one MERGED building_id->pos map (ids are
    # game-global, so a vehicle's building resolves regardless of owner).
    origins = {
        aid: [b.position for b in buildings_by_account.get(aid, []) if getattr(b, "position", None)]
        for aid in member_ids
    }
    building_pos: dict[int, tuple] = {}
    for aid in member_ids:
        for b in buildings_by_account.get(aid, []):
            if getattr(b, "position", None) and getattr(b, "id", None) is not None:
                building_pos[b.id] = b.position

    def _usable(v, aid) -> bool:
        if not getattr(v, "is_available", False):
            return False
        if strikes_by_account is not None and now is not None:
            tracker = strikes_by_account.get(aid)
            if tracker is not None and tracker.is_struck(str(v.id), now):
                return False
        return True

    # Combined available pool, tagged by owner for dispatch routing.
    owner_of: dict[int, int] = {}
    pool: list = []
    for aid in member_ids:
        for v in vehicles_by_account.get(aid, []):
            if _usable(v, aid):
                owner_of[v.id] = aid
                pool.append(v)

    # Global delta (union of everyone's en-route vehicles) + who's already on each.
    global_assigned: dict[int, list[int]] = {}
    participating: dict[int, set[int]] = {}
    for aid in member_ids:
        for v in vehicles_by_account.get(aid, []):
            if getattr(v, "target_type", None) != "mission":
                continue
            mid = getattr(v, "target_id", None)
            if mid is None:
                continue
            mid = int(mid)
            tid = getattr(v, "type_id", None)
            if tid is not None:
                global_assigned.setdefault(mid, []).append(int(tid))
            participating.setdefault(mid, set()).add(aid)

    plan.participating = {mid: frozenset(aids) for mid, aids in participating.items()}

    # Types we own ANYWHERE (any state) — a mission is only partial-startable if
    # every missing type is owned somewhere, so we never begin what we can't finish.
    owned_type_ids = {
        getattr(v, "type_id", None) for vs in vehicles_by_account.values() for v in vs
    }

    # Vehicle-class reservation (Sasha, 2026-09-11) — same rule as solo dispatch
    # (see _reserve_vehicle_group's docstring): any mission that already has
    # SOME response and still mentions a vehicle category reserves the WHOLE
    # type-id group from fresh sends, group-wide. Built as one upfront pass
    # over EVERY mission before either dispatch pass runs (not inline per
    # mission), so a fresh mission processed earlier in policy order can't
    # "beat" an already-open mission evaluated later in the same cycle.
    # Simplification vs. solo mode: not range/scope-filtered here (group
    # mode's own "begun" pass already bypasses those filters by design, and
    # over-reserving is the user's explicitly stated preference for this
    # feature — a mission we can't actually reach only ever contributes if
    # it's already revealed something, which is a narrow edge case).
    reserved_type_ids: set[int] = set()
    for mission in missions:
        rtype = (
            resolver.catalog.get(mission.mission_type_id)
            if getattr(mission, "mission_type_id", None) is not None else None
        )
        _reserve_vehicle_group(reserved_type_ids, mission, rtype, resolver, global_assigned,
                                patient_governed=False)

    def _class_owned(cls: str) -> bool:
        key = "firetrucks" if cls == "firefighters" else cls
        return any(t in owned_type_ids for t in resolver.library.class_map.by_class.get(key, ()))

    token_used: dict[int, int] = {aid: 0 for aid in member_ids}

    def action(mission, *, is_begun: bool) -> str | None:
        """Plan one mission; return a skip reason or None if actioned. Consumes
        from *pool*. Begun (already-started) missions partial-fill; fresh ones
        need a full crew unless partial_starts is on and we own the missing types."""
        nonlocal pool
        mid = mission.id
        pos = getattr(mission, "position", None)
        # Only accounts that can SEE this mission (own it, or it's shared to them)
        # may be assigned to it — otherwise the dispatch POST is a silent no-op.
        vis = _visible(mid)
        elig = [v for v in pool if owner_of.get(v.id) in vis]
        reqs = _outstanding(mission, resolver, text_engine, global_assigned)
        mtype = (
            resolver.catalog.get(mission.mission_type_id)
            if getattr(mission, "mission_type_id", None) is not None else None
        )
        min_fire = int(getattr(mtype, "min_fire_personnel", 0) or 0) if mtype is not None else 0
        fire_needed = max(0, min_fire - _fire_crew(resolver, global_assigned.get(mid, []))) if min_fire else 0

        others_on = bool(participating.get(mid))
        if not reqs and fire_needed <= 0 and not others_on:
            return "no requirements found"

        # A genuinely FRESH send (not begun by us, nobody else there either)
        # can't use a reserved class — see the reservation pre-scan above.
        # Applies to both the coverage/escort candidate pool and the
        # participation-token pool, so a "spare" pick doesn't spend a
        # reserved vehicle either.
        is_fresh_send = not is_begun and getattr(mission, "vehicle_state", 0) == 0
        call_pool = (
            [v for v in elig if v.type_id not in reserved_type_ids]
            if is_fresh_send and reserved_type_ids else elig
        )
        token_pool = (
            [v for v in pool if v.type_id not in reserved_type_ids]
            if is_fresh_send and reserved_type_ids else pool
        )

        chosen: list[int] = []
        satisfied = True
        shortfall: dict[str, int] = {}
        if reqs and pos is not None:
            # ELW specialist/preference tiebreak (Sasha, 2026-09-11): SEG-ELW
            # and ELW2-Drohne are deprioritized for a plain ELW slot unless
            # the mission's reconciled patient count is heavy enough that
            # SEG-ELW's automatic patient handling makes it the better pick.
            count_revealed = getattr(mission, "vehicle_state", 0) >= 1
            patient_load = _casualties(
                int(getattr(mission, "patients_count", 0) or 0),
                int(getattr(mission, "possible_patients_count", 0) or 0),
                getattr(mtype, "possible_patient", 0) if mtype is not None else 0,
                count_revealed,
            )
            preferred = (
                frozenset({_SEG_ELW_TYPE_ID})
                if patient_load >= _SEG_ELW_PREFERRED_PATIENT_THRESHOLD else frozenset()
            )
            cov = select_coverage(
                reqs, call_pool, distance_of=_balanced_distance(pos, building_pos, call_pool, owner_of),
                specialist_type_ids=_ELW_SPECIALIST_TYPE_IDS, preferred_type_ids=preferred,
            )
            chosen = list(cov.vehicle_ids)
            satisfied = cov.satisfied
            shortfall = dict(cov.shortfall)
        if fire_needed > 0 and pos is not None:
            extra, short = _fire_personnel_topup(chosen, call_pool, resolver, fire_needed, pos, building_pos)
            chosen += extra
            if short > 0:
                satisfied = False
                shortfall["firefighters"] = short

        # A FRESH mission that we can't fully crew: only start it if partial-starts
        # is enabled AND we own the missing types (so it can be finished later).
        # (A begun mission always gets whatever we can add toward completing it.)
        if not is_begun and not satisfied and not others_on:
            if not (policy.partial_starts and shortfall and all(_class_owned(c) for c in shortfall)):
                _note(plan.shortfalls, shortfall.keys())
                return "not enough vehicles"
        if not satisfied:
            _note(plan.shortfalls, shortfall.keys())

        # MZB water-rescue escort (Sasha, 2026-09-11): opportunistic, never a
        # hard requirement — placed after every gating check above so it can
        # never cause a mission to be skipped or marked unsatisfied.
        chosen = _add_water_rescue_escort(chosen, call_pool, resolver, pos, building_pos)

        # Participation: make sure every member has a vehicle on this mission.
        if policy.participation:
            covered_owners = {owner_of[i] for i in chosen}
            already = participating.get(mid, set())
            chosen_set = set(chosen)
            for aid in member_ids:
                if aid not in vis:  # can't send to a mission it can't see
                    continue
                if aid in covered_owners or aid in already:
                    continue
                if policy.max_token_per_account is not None and token_used[aid] >= policy.max_token_per_account:
                    continue
                spare = _nearest_spare(token_pool, aid, owner_of, chosen_set, pos, building_pos)
                if spare is not None:
                    chosen.append(spare.id)
                    chosen_set.add(spare.id)
                    token_used[aid] += 1

        if not chosen:
            return "already covered"

        chosen_set = set(chosen)
        pool = [v for v in pool if v.id not in chosen_set]
        per_owner: dict[int, list[int]] = {}
        for vid in chosen:
            per_owner.setdefault(owner_of[vid], []).append(vid)
        plan.missions.append(GroupMissionPlan(
            mission_id=mid,
            caption=getattr(mission, "caption", ""),
            by_account={aid: tuple(ids) for aid, ids in per_owner.items()},
            satisfied=satisfied,
            shortfall=dict(shortfall),
        ))
        return None

    # PASS 1 — FINISH WHAT'S STARTED. Missions the group already has vehicles on
    # get first claim on the free pool (so their needed types aren't spent on new
    # missions), bypassing radius/cash/category/batch. Ordered by the sort order.
    begun = [m for m in missions if m.id in global_assigned]
    begun_c = []
    for mission in begun:
        pos = getattr(mission, "position", None)
        dists = [nearest_km(pos, origins[aid]) for aid in member_ids
                 if origins.get(aid) and pos is not None]
        dists = [d for d in dists if d is not None]
        mtype = resolver.catalog.get(getattr(mission, "mission_type_id", None))
        credits = getattr(mission, "average_credits", 0) or (mtype.average_credits if mtype else 0)
        dist = min(dists) if dists else 0.0
        if policy.order is DispatchOrder.SMART:
            efficiency = _smart_efficiency(mission, mtype, credits, resolver, global_assigned,
                                            text_engine, avg_fire_crew)
            begun_c.append((dist, credits, mission, "begun", efficiency))
        else:
            begun_c.append((dist, credits, mission))
    for candidate in _order(begun_c, policy.order):
        mission = candidate[2]
        reason = action(mission, is_begun=True)
        if reason:
            plan.skipped.append((mission.id, reason))

    # PASS 2 — FRESH missions in range, filtered, ordered and batched.
    fresh_c = []
    for mission in missions:
        if mission.id in global_assigned:
            continue  # handled in pass 1
        pos = getattr(mission, "position", None)
        if pos is None:
            plan.skipped.append((mission.id, "no position"))
            continue
        dists = [d for aid in member_ids if origins.get(aid)
                 for d in (nearest_km(pos, origins[aid]),) if d is not None]
        if not dists:
            plan.skipped.append((mission.id, "out of range"))
            continue
        reach = min(dists) if policy.radius_basis is RadiusBasis.ANY else max(dists)
        if reach > policy.radius_km:
            plan.skipped.append((mission.id, "out of range"))
            continue
        mtype = resolver.catalog.get(getattr(mission, "mission_type_id", None))
        if policy.allowed_categories:
            cats = set(mtype.categories) if mtype else set()
            if not (cats & policy.allowed_categories):
                plan.skipped.append((mission.id, "category filtered"))
                continue
        credits = getattr(mission, "average_credits", 0) or (mtype.average_credits if mtype else 0)
        if policy.credit_min and credits < policy.credit_min:
            plan.skipped.append((mission.id, f"below credit floor ({credits})"))
            continue
        if policy.credit_max and credits > policy.credit_max:
            plan.skipped.append((mission.id, f"above credit ceiling ({credits})"))
            continue
        dist = min(dists)
        if policy.order is DispatchOrder.SMART:
            efficiency = _smart_efficiency(mission, mtype, credits, resolver, global_assigned,
                                            text_engine, avg_fire_crew)
            fresh_c.append((dist, credits, mission, "fresh", efficiency))
        else:
            fresh_c.append((dist, credits, mission))

    actioned = 0
    for candidate in _order(fresh_c, policy.order):
        mission = candidate[2]
        if policy.batch_size is not None and actioned >= policy.batch_size:
            plan.skipped.append((mission.id, "beyond batch size"))
            continue
        reason = action(mission, is_begun=False)
        if reason:
            plan.skipped.append((mission.id, reason))
        else:
            actioned += 1

    return plan


def _balanced_distance(mission_pos, building_pos, pool, owner_of):
    """Coverage ranking key for group mode: prefer the account holding the MOST
    spare of a vehicle's type (to level fleets), then the nearest vehicle."""
    dist = _distance_fn(mission_pos, building_pos)
    spare: dict[tuple[int, int], int] = {}
    for v in pool:
        spare[(owner_of.get(v.id), v.type_id)] = spare.get((owner_of.get(v.id), v.type_id), 0) + 1

    def key(vehicle):
        n = spare.get((owner_of.get(vehicle.id), vehicle.type_id), 0)
        return (-n, dist(vehicle))  # most-spare first, then nearest

    return key


def _nearest_spare(pool, account_id, owner_of, chosen_set, mission_pos, building_pos):
    """Nearest still-free vehicle owned by *account_id* — a participation token."""
    dist = _distance_fn(mission_pos, building_pos)
    cands = [v for v in pool if owner_of.get(v.id) == account_id and v.id not in chosen_set]
    if not cands:
        return None
    return min(cands, key=dist)
