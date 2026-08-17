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

from dataclasses import dataclass, field, replace
from enum import Enum

from .coverage import select_coverage
from .geo import haversine_km, nearest_km
from .resolver import MissionRequirementResolver


class DispatchOrder(str, Enum):
    """The order in which in-range missions are worked each cycle."""

    NEAREST = "nearest"          # closest to your buildings first (default)
    FARTHEST = "farthest"
    CREDITS_HIGH = "credits_high"  # best-paying mission types first
    CREDITS_LOW = "credits_low"


@dataclass(frozen=True)
class DispatchPolicy:
    """Per-account dispatch settings (a slice of the account config)."""

    radius_km: float = 40.0
    credit_floor: int = 0
    max_vehicles_per_mission: int | None = None
    order: DispatchOrder = DispatchOrder.NEAREST
    batch_size: int | None = None
    # Only work missions in these game categories (fire, police, …). Empty = all.
    allowed_categories: frozenset[str] = frozenset()


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


def plan_dispatches(
    missions,
    vehicles,
    buildings,
    resolver: MissionRequirementResolver,
    policy: DispatchPolicy,
    assigned: dict[int, list[int]] | None = None,
) -> DispatchPlan:
    """Plan this cycle's dispatches.

    *assigned* optionally maps mission id → type IDs of vehicles already en route
    to it (the delta); those reduce the mission's outstanding requirements.
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

    # Gather in-range, category-allowed missions with their distance and the
    # mission type's average credits (used for ordering).
    candidates: list[tuple[float, int, object]] = []  # (distance, credits, mission)
    for mission in missions:
        pos = getattr(mission, "position", None)
        if pos is None:
            plan.skipped.append((mission.id, "no position"))
            continue
        distance = nearest_km(pos, origins)
        if distance is None or distance > policy.radius_km:
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
        credits = mtype.average_credits if mtype else 0
        candidates.append((distance, credits, mission))

    candidates = _order(candidates, policy.order)

    # Only work a stack of `batch_size` per cycle (the rest wait for the next).
    if policy.batch_size is not None and len(candidates) > policy.batch_size:
        for _d, _c, mission in candidates[policy.batch_size:]:
            plan.skipped.append((mission.id, "beyond batch size"))
        candidates = candidates[: policy.batch_size]

    for _distance, _credits, mission in candidates:
        outcome = _plan_one(mission, pool, resolver, policy, assigned, building_pos)
        if isinstance(outcome, str):
            plan.skipped.append((mission.id, outcome))
            continue
        mission_plan, chosen = outcome
        plan.planned.append(mission_plan)
        chosen_set = set(chosen)
        pool = [v for v in pool if v.id not in chosen_set]

    return plan


def _order(candidates, order: DispatchOrder):
    """Sort (distance, credits, mission) tuples by the chosen strategy."""
    if order is DispatchOrder.CREDITS_HIGH:
        return sorted(candidates, key=lambda t: (-t[1], t[0], t[2].id))
    if order is DispatchOrder.CREDITS_LOW:
        return sorted(candidates, key=lambda t: (t[1], t[0], t[2].id))
    if order is DispatchOrder.FARTHEST:
        return sorted(candidates, key=lambda t: (-t[0], t[2].id))
    return sorted(candidates, key=lambda t: (t[0], t[2].id))  # NEAREST


def _plan_one(mission, pool, resolver, policy, assigned, building_pos=None):
    """Return a ``(MissionPlan, chosen_ids)`` pair, or a skip-reason string."""
    building_pos = building_pos or {}
    if mission.mission_type_id is None:
        return "unknown mission type"

    resolution = resolver.resolve(mission.mission_type_id)
    if not resolution.requirements:
        return "no known requirements"

    mtype = resolver.catalog.get(mission.mission_type_id)
    if policy.credit_floor and mtype and mtype.average_credits < policy.credit_floor:
        return f"below credit floor ({mtype.average_credits} < {policy.credit_floor})"

    requirements = _apply_delta(resolution.requirements, assigned, mission.id)
    if not requirements:
        return "already covered"

    pos = mission.position

    def distance_of(vehicle) -> float:
        # Prefer the vehicle's own position if present, else its building's.
        vp = getattr(vehicle, "position", None) or building_pos.get(
            getattr(vehicle, "building_id", None)
        )
        return haversine_km(pos, vp) if vp else float("inf")

    coverage = select_coverage(requirements, pool, distance_of=distance_of)
    if not coverage.vehicle_ids:
        return "no available vehicles for requirements"
    if (
        policy.max_vehicles_per_mission is not None
        and len(coverage.vehicle_ids) > policy.max_vehicles_per_mission
    ):
        return f"needs {len(coverage.vehicle_ids)} > cap {policy.max_vehicles_per_mission}"

    mission_plan = MissionPlan(
        mission_id=mission.id,
        caption=getattr(mission, "caption", ""),
        vehicle_ids=tuple(coverage.vehicle_ids),
        satisfied=coverage.satisfied,
        shortfall=coverage.shortfall,
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
