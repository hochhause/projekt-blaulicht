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

from .coverage import select_coverage
from .geo import haversine_km, nearest_km
from .resolver import MissionRequirementResolver


@dataclass(frozen=True)
class DispatchPolicy:
    """Per-account dispatch settings (a slice of the account config)."""

    radius_km: float = 40.0
    credit_floor: int = 0
    max_vehicles_per_mission: int | None = None


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

    # In-range missions, nearest building first.
    candidates: list[tuple[float, object]] = []
    for mission in missions:
        pos = getattr(mission, "position", None)
        if pos is None:
            plan.skipped.append((mission.id, "no position"))
            continue
        distance = nearest_km(pos, origins)
        if distance is None or distance > policy.radius_km:
            plan.skipped.append((mission.id, "out of range"))
            continue
        candidates.append((distance, mission))
    candidates.sort(key=lambda t: (t[0], t[1].id))

    for _, mission in candidates:
        outcome = _plan_one(mission, pool, resolver, policy, assigned)
        if isinstance(outcome, str):
            plan.skipped.append((mission.id, outcome))
            continue
        mission_plan, chosen = outcome
        plan.planned.append(mission_plan)
        chosen_set = set(chosen)
        pool = [v for v in pool if v.id not in chosen_set]

    return plan


def _plan_one(mission, pool, resolver, policy, assigned):
    """Return a ``(MissionPlan, chosen_ids)`` pair, or a skip-reason string."""
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
        vp = getattr(vehicle, "position", None)
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
