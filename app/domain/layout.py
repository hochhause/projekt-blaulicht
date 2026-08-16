"""Declarative building **layouts** — the desired state of your estate.

A layout says what a building *should* look like, e.g. "every fire station is
level 20, has the rescue-service extension, and holds 4 LF + 1 DLK + 1 ELW".
The reconciler (:mod:`app.domain.reconcile`) diffs this against the actual
estate and produces the actions needed to close the gap.

Two levels of specification, most-specific wins:

* **by building type** — a default applied to every building of that type
  (keyed by the game's numeric ``building_type``); and
* **by building id** — an override for one specific building.

Vehicles are specified by **vehicle type ID** (the canonical, unambiguous key —
a class name like "firetrucks" maps to many types, so it can't express "4 LF").
A future web UI will offer a name picker over these IDs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BuildingLayout:
    """Desired state for a building (or a whole building type)."""

    target_level: int | None = None
    extensions: frozenset[int] = frozenset()
    vehicles: dict[int, int] = field(default_factory=dict)  # vehicle type ID -> count

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BuildingLayout":
        level = d.get("level", d.get("target_level"))
        extensions = frozenset(int(x) for x in (d.get("extensions") or []))
        vehicles = {
            int(type_id): int(count)
            for type_id, count in (d.get("vehicles") or {}).items()
        }
        return cls(
            target_level=int(level) if level is not None else None,
            extensions=extensions,
            vehicles=vehicles,
        )


@dataclass(frozen=True)
class EstateLayout:
    """The full desired estate: per-type defaults + per-building overrides."""

    by_type: dict[int, BuildingLayout] = field(default_factory=dict)
    by_building: dict[int, BuildingLayout] = field(default_factory=dict)

    def for_building(self, building_id: int, building_type: int | None) -> BuildingLayout | None:
        """The layout that applies to a building — its override, else its
        type default, else ``None`` (unmanaged)."""
        if building_id in self.by_building:
            return self.by_building[building_id]
        if building_type is not None:
            return self.by_type.get(building_type)
        return None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EstateLayout":
        """Parse a layout config.

        Shape::

            {
              "by_type":     { "0": {"level": 20, "extensions": [0],
                                     "vehicles": {"0": 4, "2": 1}} },
              "by_building": { "12345": {"vehicles": {"29": 1}} }
            }

        Type/building/vehicle keys are numeric IDs (strings accepted).
        """
        by_type = {
            int(k): BuildingLayout.from_dict(v) for k, v in (d.get("by_type") or {}).items()
        }
        by_building = {
            int(k): BuildingLayout.from_dict(v) for k, v in (d.get("by_building") or {}).items()
        }
        return cls(by_type=by_type, by_building=by_building)
