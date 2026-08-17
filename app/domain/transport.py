"""Transport routing — send patients to hospitals and prisoners to cells.

After a mission, transporting vehicles carry **patients** (to a Krankenhaus with
a free bed and, if the patient needs one, the right specialty/Fachrichtung) or
**prisoners** (to a building with a free cell). This module picks the best
target for each: nearest suitable, within distance, capacity, and — for alliance
buildings — an optional tax ceiling.

Pure and testable. Detecting the requests (which vehicle carries what, and any
required specialty) is a read-layer concern with provisional field names; the
game client already has the confirmed GET routes to *perform* a transport, and
:mod:`app.core.runner` wires routing to those. Here we only decide *where*.

Targets passed in are already the right kind for the request (hospitals for
patients, cells for prisoners) — the building→target extraction (which buildings
are hospitals, their beds and departments) stays in the read layer so this stays
free of unverified building-type assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from .geo import haversine_km

LatLon = tuple[float, float]


class TransportKind(str, Enum):
    PATIENT = "patient"
    PRISONER = "prisoner"


@dataclass(frozen=True)
class TransportRequest:
    """A vehicle needing to unload a patient or prisoner."""

    vehicle_id: int
    kind: TransportKind
    origin: LatLon
    required_specialty: str | None = None  # patients only; None = any hospital


@dataclass(frozen=True)
class TransportTarget:
    """A candidate destination — a hospital (patients) or cell block (prisoners)."""

    building_id: int
    position: LatLon
    free_capacity: int
    specialties: frozenset[str] = frozenset()  # hospital departments
    tax: int = 0  # alliance fee %, 0 for own buildings
    is_own: bool = True


@dataclass(frozen=True)
class TransportPolicy:
    max_distance_km: float = 100.0
    max_tax: int | None = None  # None = ignore fees; int = accept fee up to this %
    prefer_own: bool = True
    allow_alliance: bool = True


@dataclass(frozen=True)
class TransportDecision:
    request_vehicle_id: int
    kind: TransportKind
    target_building_id: int
    distance_km: float
    tax: int


def _eligible(request: TransportRequest, target: TransportTarget, policy: TransportPolicy) -> bool:
    if target.free_capacity <= 0:
        return False
    if not target.is_own and not policy.allow_alliance:
        return False
    if policy.max_tax is not None and target.tax > policy.max_tax:
        return False
    if request.required_specialty and request.required_specialty not in target.specialties:
        return False
    return haversine_km(request.origin, target.position) <= policy.max_distance_km


def route_transport(
    request: TransportRequest,
    targets: list[TransportTarget],
    policy: TransportPolicy,
) -> TransportDecision | None:
    """Pick the best target for one request, or ``None`` if none is suitable.

    Ranking: own buildings before alliance (when ``prefer_own``), then nearest,
    then cheapest tax.
    """
    best: TransportTarget | None = None
    best_key = None
    best_distance = 0.0
    for target in targets:
        if not _eligible(request, target, policy):
            continue
        distance = haversine_km(request.origin, target.position)
        own_rank = 0 if (target.is_own and policy.prefer_own) else 1
        key = (own_rank, distance, target.tax, target.building_id)
        if best_key is None or key < best_key:
            best, best_key, best_distance = target, key, distance

    if best is None:
        return None
    return TransportDecision(
        request_vehicle_id=request.vehicle_id,
        kind=request.kind,
        target_building_id=best.building_id,
        distance_km=best_distance,
        tax=best.tax,
    )


def route_transports(
    requests: list[TransportRequest],
    targets: list[TransportTarget],
    policy: TransportPolicy,
) -> list[tuple[TransportRequest, TransportDecision | None]]:
    """Route several requests, consuming capacity so two don't overfill a target.

    A ``None`` decision means no suitable target remained (the operator/executor
    handles that as a release — a capture-pending write).
    """
    remaining = {t.building_id: t.free_capacity for t in targets}
    results: list[tuple[TransportRequest, TransportDecision | None]] = []
    for request in requests:
        live = [replace(t, free_capacity=remaining[t.building_id]) for t in targets]
        decision = route_transport(request, live, policy)
        if decision is not None:
            remaining[decision.target_building_id] -= 1
        results.append((request, decision))
    return results
