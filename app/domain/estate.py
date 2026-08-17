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
        # Real shape: a list of {caption, available, enabled, type_id}. Count an
        # extension as present when it's enabled or available. Bare-id lists are
        # also accepted for forward-compatibility.
        ext_ids: set[int] = set()
        for e in raw_ext:
            if isinstance(e, dict):
                tid = e.get("type_id", e.get("id"))
                if tid is not None and (e.get("enabled", True) or e.get("available", True)):
                    ext_ids.add(int(tid))
            else:
                ext_ids.add(int(e))

        # The buildings API has no "free personnel" field — only personal_count
        # (total hired). Use it for both; refine once assignment data is folded in.
        total = int(building.get("personal_count") or building.get("personnel_total") or 0)
        available = int(building.get("personal_free") or total)

        counts = dict(Counter(int(t) for t in vehicle_type_ids))
        return cls(
            id=int(building["id"]),
            building_type=(
                int(v) if (v := building.get("building_type")) is not None else None
            ),
            level=int(building.get("level") or 0),
            extensions=frozenset(ext_ids),
            vehicle_type_counts=counts,
            personnel_available=available,
            personnel_total=total,
            caption=str(building.get("caption") or building.get("name") or ""),
        )
