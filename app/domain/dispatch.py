"""The dispatch planner — decide which vehicles to send to which missions.

Pure, deterministic, and network-free: given the current missions, the fleet,
and the account's buildings, it produces a :class:`DispatchPlan`. The runner
(:mod:`app.core.runner`) does the I/O around it. Keeping the decision here means
the whole policy is unit-tested without a live account.

Per cycle it:

1. keeps only missions within ``radius_km`` of some building, nearest first;
2. resolves each mission's requirements (structured catalogue + class map);
3. applies the **delta** — subtracts vehicles already en route, so a mission
   that's already covered is left alone and a partial one is only topped up;
4. runs the **multi-role coverage** matcher over the still-available pool, so one
   HLF fills an LF + RW at once (see :mod:`app.domain.coverage`);
5. consumes the chosen vehicles from the pool before planning the next mission,
   so two missions never claim the same vehicle.

Vehicles/missions/buildings are duck-typed (the game models qualify): a mission
needs ``id``, ``caption``, ``mission_type_id``, ``position``; a vehicle needs
``id``, ``type_id``, ``is_available``, ``position``; a building needs ``position``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum

from .coverage import select_coverage
from .geo import haversine_km, nearest_km
from .resolver import MissionRequirementResolver


class DispatchOrder(str, Enum):
    """The order in which in-range missions are worked each cycle."""

    NEAREST = "nearest"            # closest to your buildings first (default)
    FARTHEST = "farthest"
    CREDITS_HIGH = "credits_high"  # most cash first
    CREDITS_LOW = "credits_low"    # least cash first
    OLDEST = "oldest"              # longest-open first (by created_at)
    NEWEST = "newest"


class MissionSource(str, Enum):
    """Which missions to work: your own, alliance-shared, or both."""

    BOTH = "both"
    OWN = "own"
    ALLIANCE = "alliance"


@dataclass(frozen=True)
class DispatchPolicy:
    """Per-account dispatch settings (a slice of the account config).

    Cash bounds are separate for own vs alliance missions (0 = no bound).
    Alliance missions additionally have an age window (seconds since shared, or
    since the mission was created if the share time is unavailable).
    """

    radius_km: float = 40.0
    own_credit_min: int = 0
    own_credit_max: int = 0
    alliance_credit_min: int = 0
    alliance_credit_max: int = 0
    alliance_min_age_seconds: int = 0  # skip alliance missions shared too recently
    alliance_max_age_seconds: int = 0  # skip alliance missions shared too long ago
    max_vehicles_per_mission: int | None = None
    order: DispatchOrder = DispatchOrder.NEAREST
    batch_size: int | None = None
    # Only work missions in these game categories (fire, police, …). Empty = all.
    allowed_categories: frozenset[str] = frozenset()
    # Which missions to work, and an optional separate radius for alliance ones.
    mission_source: MissionSource = MissionSource.BOTH
    alliance_radius_km: float | None = None
    # Parasite mode: leech shared/event missions others are already on — contribute
    # something useful if a role is missing, else send a spare vehicle just to
    # claim a participation share. Skips cash/source/category filters + batch/order.
    parasite_mode: bool = False
    # Top-up (symbiont) mode: only work missions ALREADY begun (vehicle_state>=1),
    # own or shared; send the useful vehicles we have (partial ok) and report the
    # rest. No leeching, no fresh missions.
    top_up_mode: bool = False


@dataclass(frozen=True)
class MissionPlan:
    """What to send to one mission this cycle."""

    mission_id: int
    caption: str
    vehicle_ids: tuple[int, ...]
    satisfied: bool
    shortfall: dict[str, int]


@dataclass
class DispatchPlan:
    planned: list[MissionPlan] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)  # (mission_id, reason)

    @property
    def total_dispatches(self) -> int:
        return sum(len(m.vehicle_ids) for m in self.planned)


def assigned_from_vehicles(vehicles) -> dict[int, list[int]]:
    """Map mission id → vehicle type IDs already en route to it (the delta).

    A vehicle carries ``target_type == 'mission'`` and ``target_id`` (the mission
    id) while assigned; those reduce that mission's outstanding requirements so
    the loop tops missions up instead of re-dispatching to them.
    """
    assigned: dict[int, list[int]] = {}
    for vehicle in vehicles:
        if getattr(vehicle, "target_type", None) != "mission":
            continue
        mission_id = getattr(vehicle, "target_id", None)
        type_id = getattr(vehicle, "type_id", None)
        if mission_id is not None and type_id is not None:
            assigned.setdefault(int(mission_id), []).append(int(type_id))
    return assigned


def plan_dispatches(
    missions,
    vehicles,
    buildings,
    resolver: MissionRequirementResolver,
    policy: DispatchPolicy,
    assigned: dict[int, list[int]] | None = None,
    text_engine=None,
    now: float | None = None,
    strikes=None,
) -> DispatchPlan:
    """Plan this cycle's dispatches.

    *assigned* maps mission id → type IDs of vehicles already en route (the
    delta). *text_engine* (a RequirementEngine) parses a mission's live
    ``missing_text`` when the structured catalogue has no requirements. *now*
    (epoch seconds) drives the alliance age-since-shared window.
    """
    plan = DispatchPlan()
    origins = [b.position for b in buildings if getattr(b, "position", None)]
    pool = [v for v in vehicles if getattr(v, "is_available", False)]
    # Vehicles carry no coordinates — a vehicle's position is its building's,
    # used to rank same-role candidates nearest-first.
    building_pos = {
        b.id: b.position
        for b in buildings
        if getattr(b, "position", None) and getattr(b, "id", None) is not None
    }

    # PRIORITY PASS — spread/top-up (missions we're on) and, in parasite mode,
    # leech targets. Runs first, bypassing filters/order/batch.
    handled, priority_batch = _priority_pass(
        plan, missions, pool, resolver, policy, assigned or {}, building_pos, text_engine
    )

    # Gather in-range, category-allowed missions with their distance and the
    # mission type's average credits (used for ordering).
    candidates: list[tuple[float, int, object]] = []  # (distance, credits, mission)
    for mission in missions:
        if mission.id in handled:
            continue  # already actioned in the priority pass
        is_alliance = getattr(mission, "is_alliance", False)
        # Top-up (symbiont) mode: only help missions someone has already begun.
        if policy.top_up_mode and getattr(mission, "vehicle_state", 0) < 1:
            plan.skipped.append((mission.id, "not begun (top-up mode)"))
            continue
        # Source filter: own only / alliance only / both.
        if policy.mission_source is MissionSource.OWN and is_alliance:
            plan.skipped.append((mission.id, "own-only filter"))
            continue
        if policy.mission_source is MissionSource.ALLIANCE and not is_alliance:
            plan.skipped.append((mission.id, "alliance-only filter"))
            continue
        pos = getattr(mission, "position", None)
        if pos is None:
            plan.skipped.append((mission.id, "no position"))
            continue
        # Alliance missions may use their own radius.
        radius = policy.radius_km
        if is_alliance and policy.alliance_radius_km is not None:
            radius = policy.alliance_radius_km
        distance = nearest_km(pos, origins)
        if distance is None or distance > radius:
            plan.skipped.append((mission.id, "out of range"))
            continue
        mtype = (
            resolver.catalog.get(mission.mission_type_id)
            if mission.mission_type_id is not None
            else None
        )
        if policy.allowed_categories:
            cats = set(mtype.categories) if mtype else set()
            if not (cats & policy.allowed_categories):
                plan.skipped.append((mission.id, "category filtered"))
                continue
        # Prefer the mission's own credits; fall back to the type average.
        credits = getattr(mission, "average_credits", 0) or (mtype.average_credits if mtype else 0)
        # Cash bounds and (for alliance) the age window are per-source.
        if is_alliance:
            cmin, cmax = policy.alliance_credit_min, policy.alliance_credit_max
            age = _age_seconds(mission, now)
            if age is not None:
                if policy.alliance_min_age_seconds and age < policy.alliance_min_age_seconds:
                    plan.skipped.append((mission.id, "shared too recently"))
                    continue
                if policy.alliance_max_age_seconds and age > policy.alliance_max_age_seconds:
                    plan.skipped.append((mission.id, "shared too long ago"))
                    continue
        else:
            cmin, cmax = policy.own_credit_min, policy.own_credit_max
        if cmin and credits < cmin:
            plan.skipped.append((mission.id, f"below credit floor ({credits})"))
            continue
        if cmax and credits > cmax:
            plan.skipped.append((mission.id, f"above credit ceiling ({credits})"))
            continue
        candidates.append((distance, credits, mission))

    candidates = _order(candidates, policy.order)

    # Batch limits missions ACTUALLY DISPATCHED per cycle — not the number
    # considered. Missions skipped (no vehicles, already covered, …) don't use a
    # slot; only once `batch_size` missions have been actioned is the rest
    # deferred as "beyond batch size".
    actioned = priority_batch  # priority useful contributions already count
    for _distance, _credits, mission in candidates:
        if policy.batch_size is not None and actioned >= policy.batch_size:
            plan.skipped.append((mission.id, "beyond batch size"))
            continue
        outcome = _plan_one(mission, pool, resolver, policy, assigned, building_pos,
                            text_engine, strikes, now, allow_partial=policy.top_up_mode)
        if isinstance(outcome, str):
            plan.skipped.append((mission.id, outcome))
            continue
        mission_plan, chosen = outcome
        plan.planned.append(mission_plan)
        actioned += 1
        chosen_set = set(chosen)
        pool = [v for v in pool if v.id not in chosen_set]

    return plan


def _age_seconds(mission, now: float | None) -> float | None:
    """Seconds since a mission was shared (or created), or ``None`` if unknown."""
    if now is None:
        return None
    ref = getattr(mission, "alliance_shared_at", None) or getattr(mission, "created_at", None)
    epoch = _to_epoch(ref)
    if epoch is None:
        return None
    return max(0.0, now - epoch)


def _to_epoch(ts) -> float | None:
    """Best-effort parse of a timestamp (epoch number or ISO string) to epoch."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    s = str(ts).strip()
    if not s:
        return None
    try:
        return float(s)  # numeric string (unix epoch)
    except ValueError:
        pass
    try:
        from datetime import datetime

        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, OSError):
        return None


def _order(candidates, order: DispatchOrder):
    """Sort (distance, credits, mission) tuples by the chosen strategy."""
    if order is DispatchOrder.CREDITS_HIGH:
        return sorted(candidates, key=lambda t: (-t[1], t[0], t[2].id))
    if order is DispatchOrder.CREDITS_LOW:
        return sorted(candidates, key=lambda t: (t[1], t[0], t[2].id))
    if order is DispatchOrder.FARTHEST:
        return sorted(candidates, key=lambda t: (-t[0], t[2].id))
    if order in (DispatchOrder.OLDEST, DispatchOrder.NEWEST):
        # Sort by created_at (ISO strings sort chronologically). Missing
        # timestamps sort last for oldest / first for newest via the sentinel.
        reverse = order is DispatchOrder.NEWEST
        return sorted(
            candidates,
            key=lambda t: (getattr(t[2], "created_at", None) or "9999", t[2].id),
            reverse=reverse,
        )
    return sorted(candidates, key=lambda t: (t[0], t[2].id))  # NEAREST


def _derive_requirements(mission, resolver, text_engine):
    """Requirements for a mission: structured derivation, else missing_text."""
    reqs = resolver.resolve_mission(mission).requirements or None
    if not reqs and text_engine is not None:
        text = getattr(mission, "missing_text", "") or ""
        if text.strip():
            parsed = text_engine.resolve(re.split(r"[,;\n/]+", text))
            if parsed.requirements:
                reqs = parsed.requirements
    return list(reqs) if reqs else []


def _distance_fn(mission_pos, building_pos):
    def distance_of(vehicle) -> float:
        vp = getattr(vehicle, "position", None) or building_pos.get(getattr(vehicle, "building_id", None))
        return haversine_km(mission_pos, vp) if (vp and mission_pos) else float("inf")
    return distance_of


def _abundance_vehicle(pool, mission_pos, building_pos):
    """The nearest instance of whatever vehicle type we hold most of — a spare
    to throw at a leech mission just to claim participation."""
    if not pool:
        return None
    from collections import Counter

    common_type = Counter(v.type_id for v in pool).most_common(1)[0][0]
    candidates = [v for v in pool if v.type_id == common_type]
    return min(candidates, key=_distance_fn(mission_pos, building_pos))


def _priority_pass(plan, missions, pool, resolver, policy, assigned, building_pos, text_engine):
    """Handle spread/top-up and parasite missions BEFORE the normal filtered pass.

    Bypasses cash/source/category filters, ordering and batch. Returns the set of
    handled mission ids and how many count toward the batch (useful contributions
    do; pure leech sends do not).
    """
    handled: set[int] = set()
    batch_used = 0
    assigned = assigned or {}
    for mission in missions:
        mid = mission.id
        on_it = mid in assigned                       # a mission we're already on
        others_on = getattr(mission, "vehicle_state", 0) >= 1
        is_shared = getattr(mission, "is_alliance", False)
        is_parasite = policy.parasite_mode and is_shared and others_on and not on_it
        if not (on_it or is_parasite):
            continue

        pos = getattr(mission, "position", None)
        reqs = _apply_delta(_derive_requirements(mission, resolver, text_engine), assigned, mid)
        chosen: list[int] = []
        if reqs and pos is not None:
            cov = select_coverage(reqs, pool, distance_of=_distance_fn(pos, building_pos))
            if cov.vehicle_ids:
                chosen = list(cov.vehicle_ids)
                plan.planned.append(MissionPlan(mid, getattr(mission, "caption", ""),
                                                tuple(chosen), cov.satisfied, dict(cov.shortfall)))
                batch_used += 1  # useful contribution -> counts to batch
        if not chosen and is_parasite:
            spare = _abundance_vehicle(pool, pos, building_pos)
            if spare is not None:
                chosen = [spare.id]
                plan.planned.append(MissionPlan(mid, getattr(mission, "caption", ""),
                                                (spare.id,), False, {}))  # pure leech, free
        if chosen:
            cset = set(chosen)
            pool[:] = [v for v in pool if v.id not in cset]
            handled.add(mid)
    return handled, batch_used


def _plan_one(mission, pool, resolver, policy, assigned, building_pos=None,
              text_engine=None, strikes=None, now=None, allow_partial=False):
    """Return a ``(MissionPlan, chosen_ids)`` pair, or a skip-reason string.

    *allow_partial* (top-up/symbiont mode) sends whatever useful vehicles we
    have to an already-begun mission and reports the rest, instead of holding
    out for a full crew. It never strikes — the mode is about helping, not
    giving up on a class.
    """
    building_pos = building_pos or {}

    requirements = _derive_requirements(mission, resolver, text_engine) or None
    if not requirements:
        return "no requirements found"

    # Strike-out: skip a mission needing a vehicle class that's out of supply.
    # Skipped in top-up mode — there we contribute what we can and report the gap.
    if not allow_partial and strikes is not None and now is not None:
        struck = {r.source for r in requirements if strikes.is_struck(r.source, now)}
        if struck:
            return f"needs struck-out vehicle(s): {', '.join(sorted(struck))}"

    # Credit gating already happened during candidate collection.
    requirements = _apply_delta(requirements, assigned, mission.id)
    if not requirements:
        return "already covered"

    coverage = select_coverage(requirements, pool, distance_of=_distance_fn(mission.position, building_pos))

    # Only dispatch missions we can FULLY crew — a partial send just ties up
    # vehicles on a job that won't complete. A shortfall strikes the missing
    # class(es) so we eventually stop trying missions we can't fill.
    if not coverage.satisfied:
        if allow_partial:
            # Top-up: send the useful vehicles we do have, report the shortfall.
            if not coverage.vehicle_ids:
                return "nothing useful to add"
            plan = MissionPlan(
                mission_id=mission.id,
                caption=getattr(mission, "caption", ""),
                vehicle_ids=tuple(coverage.vehicle_ids),
                satisfied=False,
                shortfall=dict(coverage.shortfall),
            )
            return plan, coverage.vehicle_ids
        if strikes is not None and now is not None:
            strikes.record(coverage.shortfall.keys(), now)
        needed = ", ".join(f"{cls} x{n}" for cls, n in coverage.shortfall.items())
        return f"not enough vehicles: short {needed}"

    if (
        policy.max_vehicles_per_mission is not None
        and len(coverage.vehicle_ids) > policy.max_vehicles_per_mission
    ):
        return f"needs {len(coverage.vehicle_ids)} > cap {policy.max_vehicles_per_mission}"

    mission_plan = MissionPlan(
        mission_id=mission.id,
        caption=getattr(mission, "caption", ""),
        vehicle_ids=tuple(coverage.vehicle_ids),
        satisfied=True,
        shortfall={},
    )
    return mission_plan, coverage.vehicle_ids


def _apply_delta(requirements, assigned, mission_id):
    """Reduce requirement counts by vehicles already en route to this mission.

    A multi-role vehicle already assigned (e.g. an HLF) reduces *every*
    requirement it qualifies for — matching how one counts toward both an LF and
    an RW.
    """
    if not assigned:
        return list(requirements)
    types = assigned.get(mission_id, [])
    if not types:
        return list(requirements)

    reduced = []
    for requirement in requirements:
        covered = sum(1 for t in types if t in requirement.type_ids)
        new_count = max(0, requirement.count - covered)
        if new_count > 0:
            reduced.append(replace(requirement, count=new_count))
    return reduced
