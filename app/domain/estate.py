"""Actual state of a building — the read side of reconciliation.

Assembled from the game's read APIs (``/api/buildings/{id}`` for level,
extensions and personnel; ``/api/buildings/{id}/vehicles`` for the fleet
stationed there). The field names in :meth:`BuildingState.from_api` are
**provisional** until verified against a live response; the reconciliation logic
itself operates on this typed model, not the raw JSON, so it is unaffected by
naming and is fully unit-tested with constructed states.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class BuildingState:
    """A building as it currently is."""

    id: int
    building_type: int | None
    level: int
    extensions: frozenset[int]
    vehicle_type_counts: dict[int, int]
    personnel_available: int
    personnel_total: int
    caption: str = ""

    @classmethod
    def from_api(
        cls,
        building: dict[str, Any],
        vehicle_type_ids: Iterable[int] = (),
    ) -> "BuildingState":
        """Build from a ``/api/buildings/{id}`` object plus the type IDs of the
        vehicles stationed there. Field names are provisional (see module doc)."""
        raw_ext = building.get("extensions") or []
        # Extensions may arrive as a list of ids, or of objects with an "id"
        # and an "available"/"enabled" flag — accept both.
        ext_ids: set[int] = set()
        for e in raw_ext:
            if isinstance(e, dict):
                if e.get("available", True) and "id" in e:
                    ext_ids.add(int(e["id"]))
            else:
                ext_ids.add(int(e))

        counts = dict(Counter(int(t) for t in vehicle_type_ids))
        return cls(
            id=int(building["id"]),
            building_type=(
                int(v) if (v := building.get("building_type")) is not None else None
            ),
            level=int(building.get("level") or 0),
            extensions=frozenset(ext_ids),
            vehicle_type_counts=counts,
            personnel_available=int(building.get("personal_free") or building.get("personnel_available") or 0),
            personnel_total=int(building.get("personal_count") or building.get("personnel_total") or 0),
            caption=str(building.get("caption") or building.get("name") or ""),
        )
