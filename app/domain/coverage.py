"""Vehicle coverage — pick the fewest vehicles that satisfy a mission.

The matching model has two facts, both already encoded in the salvaged data:

* **a role is served by many vehicles** — a requirement (e.g. "firetrucks")
  expands to a *set* of acceptable vehicle type IDs; and
* **a vehicle serves many roles** — a type ID belongs to several classes, so it
  appears in several requirements' type-id sets. An **HLF** (type 30) is in both
  ``firetrucks`` and ``heavy_rescue_vehicles``, so it counts as *both* an LF and
  an RW at once.

Consequence: a mission needing ``1 LF + 1 RW`` is satisfied by a **single HLF**.
Dispatching one vehicle per requirement independently would wastefully send two.

So coverage is a **set-multicover**: choose the fewest vehicles such that, for
every requirement R, the number of chosen vehicles whose type is in
``R.type_ids`` is at least ``R.count`` — where one vehicle contributes to *every*
requirement it qualifies for, simultaneously.

That is NP-hard in general, but a mission has only a handful of requirements and
a small pool of nearby vehicles, so a greedy heuristic (repeatedly take the
vehicle that covers the most still-open demand, nearest first to break ties) is
fast and near-optimal — and gets the HLF case exactly right.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol

from .requirements import Requirement


class _HasType(Protocol):
    id: int
    type_id: int


@dataclass(frozen=True)
class Assignment:
    """A chosen vehicle and which requirements it counts toward."""

    vehicle_id: int
    type_id: int
    covers: tuple[str, ...]  # requirement sources this vehicle satisfies


@dataclass
class CoverageResult:
    assignments: list[Assignment] = field(default_factory=list)
    shortfall: dict[str, int] = field(default_factory=dict)  # source -> still needed

    @property
    def satisfied(self) -> bool:
        return not self.shortfall

    @property
    def vehicle_ids(self) -> list[int]:
        return [a.vehicle_id for a in self.assignments]


def select_coverage(
    requirements: Iterable[Requirement],
    vehicles: Iterable[_HasType],
    distance_of: Callable[[_HasType], float] | None = None,
) -> CoverageResult:
    """Greedily pick the fewest *vehicles* covering all *requirements*.

    *distance_of*, if given, ranks equally-covering candidates nearest-first;
    otherwise ties fall back to a stable order by vehicle id.
    """
    reqs = [r for r in requirements if r.count > 0]
    remaining = {i: r.count for i, r in enumerate(reqs)}
    pool = list(vehicles)
    result = CoverageResult()

    def gain(vehicle) -> list[int]:
        """Indices of still-open requirements this vehicle would reduce."""
        return [
            i
            for i, r in enumerate(reqs)
            if remaining[i] > 0 and vehicle.type_id in r.type_ids
        ]

    while any(v > 0 for v in remaining.values()) and pool:
        # Best = covers the most open requirements; nearest breaks ties.
        best = None
        best_key = None
        for vehicle in pool:
            covered = gain(vehicle)
            if not covered:
                continue
            dist = distance_of(vehicle) if distance_of else 0.0
            key = (-len(covered), dist, vehicle.id)
            if best_key is None or key < best_key:
                best, best_key, best_covered = vehicle, key, covered

        if best is None:
            break  # no remaining vehicle can cover any open requirement

        for i in best_covered:
            remaining[i] -= 1
        result.assignments.append(
            Assignment(
                vehicle_id=best.id,
                type_id=best.type_id,
                covers=tuple(reqs[i].source for i in best_covered),
            )
        )
        pool.remove(best)

    result.shortfall = {
        reqs[i].source: need for i, need in remaining.items() if need > 0
    }
    return result
