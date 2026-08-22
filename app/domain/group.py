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
    DispatchOrder,
    _apply_delta,
    _derive_requirements,
    _distance_fn,
    _fire_crew,
    _fire_personnel_topup,
    _order,
)
from .geo import nearest_km


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

    @property
    def total_dispatches(self) -> int:
        return sum(len(ids) for gmp in self.missions for ids in gmp.by_account.values())

    @property
    def missions_actioned(self) -> int:
        return len(self.missions)


def _note(shortfalls, classes) -> None:
    for cls in classes:
        shortfalls[cls] = shortfalls.get(cls, 0) + 1


def plan_group_dispatches(
    missions,
    vehicles_by_account: dict[int, list],
    buildings_by_account: dict[int, list],
    resolver,
    policy: GroupPolicy,
    now: float | None = None,
    text_engine=None,
) -> GroupPlan:
    """Plan one cycle across all member accounts. Returns per-account sends."""
    plan = GroupPlan()
    member_ids = list(vehicles_by_account.keys())

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

    # Combined available pool, tagged by owner for dispatch routing.
    owner_of: dict[int, int] = {}
    pool: list = []
    for aid in member_ids:
        for v in vehicles_by_account.get(aid, []):
            if getattr(v, "is_available", False):
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

    # Candidate missions: in range (per basis), category- and cash-allowed.
    candidates: list[tuple[float, int, object]] = []
    for mission in missions:
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
        mtype = (
            resolver.catalog.get(mission.mission_type_id)
            if getattr(mission, "mission_type_id", None) is not None else None
        )
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
        candidates.append((min(dists), credits, mission))

    candidates = _order(candidates, policy.order)

    token_used: dict[int, int] = {aid: 0 for aid in member_ids}
    actioned = 0
    for _dist, _credits, mission in candidates:
        if policy.batch_size is not None and actioned >= policy.batch_size:
            plan.skipped.append((mission.id, "beyond batch size"))
            continue
        mid = mission.id
        pos = mission.position

        reqs = _apply_delta(_derive_requirements(mission, resolver, text_engine), global_assigned, mid)
        mtype = (
            resolver.catalog.get(mission.mission_type_id)
            if getattr(mission, "mission_type_id", None) is not None else None
        )
        min_fire = int(getattr(mtype, "min_fire_personnel", 0) or 0) if mtype is not None else 0
        fire_needed = max(0, min_fire - _fire_crew(resolver, global_assigned.get(mid, []))) if min_fire else 0

        others_on = bool(participating.get(mid))
        if not reqs and fire_needed <= 0 and not others_on:
            plan.skipped.append((mid, "no requirements found"))
            continue

        # Combined coverage — minimal set from the whole group to crew it. Among
        # equally-covering candidates, take from the account with the MOST spare of
        # that type (balances fleets), then nearest.
        chosen: list[int] = []
        satisfied = True
        shortfall: dict[str, int] = {}
        if reqs:
            cov = select_coverage(reqs, pool, distance_of=_balanced_distance(pos, building_pos, pool, owner_of))
            chosen = list(cov.vehicle_ids)
            satisfied = cov.satisfied
            shortfall = dict(cov.shortfall)
        if fire_needed > 0:
            extra, short = _fire_personnel_topup(chosen, pool, resolver, fire_needed, pos, building_pos)
            chosen += extra
            if short > 0:
                satisfied = False
                shortfall["firefighters"] = short
        if not satisfied:
            _note(plan.shortfalls, shortfall.keys())

        # Participation: make sure every member has a vehicle on this mission.
        if policy.participation:
            covered_owners = {owner_of[i] for i in chosen}
            already = participating.get(mid, set())
            chosen_set = set(chosen)
            for aid in member_ids:
                if aid in covered_owners or aid in already:
                    continue
                if policy.max_token_per_account is not None and token_used[aid] >= policy.max_token_per_account:
                    continue
                spare = _nearest_spare(pool, aid, owner_of, chosen_set, pos, building_pos)
                if spare is not None:
                    chosen.append(spare.id)
                    chosen_set.add(spare.id)
                    token_used[aid] += 1

        if not chosen:
            # Nothing we can add (already covered by others and no free spares).
            plan.skipped.append((mid, "already covered"))
            continue

        # Consume from the pool and split this mission's sends by owner.
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
