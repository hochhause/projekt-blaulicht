"""Personnel/crew reasoning — how many staff a vehicle needs, with trailers.

Crew figures come straight from the salvaged assets (``additional.base.personnel``
per vehicle type). A vehicle that carries a trailer/module (Abrollbehälter,
Anhänger) needs enough crew for the carrier *and* whatever the trailer adds, so
:func:`required_crew` optionally folds in an attached trailer type.

This module only *computes* staffing needs and gaps. Actually assigning
personnel to a vehicle is a write and needs a captured request shape — see
``app/game`` / the reconciliation executor seams.
"""

from __future__ import annotations

from dataclasses import dataclass

from .assets import AssetLibrary, VehicleType


def required_crew(vehicle: VehicleType, trailer: VehicleType | None = None) -> int:
    """Full-staff crew for *vehicle*, plus any *trailer* it carries."""
    total = vehicle.personnel
    if trailer is not None:
        total += trailer.personnel
    return total


@dataclass(frozen=True)
class CrewGap:
    """A staffing shortfall to surface to the operator."""

    building_id: int
    required: int
    available: int

    @property
    def missing(self) -> int:
        return max(0, self.required - self.available)


def building_crew_requirement(
    library: AssetLibrary,
    vehicle_type_counts: dict[int, int],
) -> int:
    """Total crew to fully staff every vehicle stationed at a building.

    *vehicle_type_counts* maps vehicle type ID → how many are stationed there.
    Trailer types are skipped as carriers themselves (they have no own crew);
    their crew, where any, is attributed to the pulling vehicle at assignment
    time, not here.
    """
    total = 0
    for type_id, count in vehicle_type_counts.items():
        vtype = library.types.get(type_id)
        if vtype is None or vtype.is_trailer:
            continue
        total += vtype.personnel * count
    return total


def crew_gap(
    library: AssetLibrary,
    building_id: int,
    vehicle_type_counts: dict[int, int],
    personnel_available: int,
) -> CrewGap:
    """Compute the crew shortfall (if any) for one building."""
    required = building_crew_requirement(library, vehicle_type_counts)
    return CrewGap(building_id=building_id, required=required, available=personnel_available)
