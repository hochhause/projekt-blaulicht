"""Primary requirement resolver: mission type → vehicle-type demands.

Joins the two structured data sources — no text parsing:

* :class:`~app.domain.catalog.MissionCatalog` (from ``einsaetze.json``) gives a
  mission type's base requirements as ``{vehicle_class: count}``; and
* :class:`~app.domain.assets.AssetLibrary`'s class map (from ``Vehicle.mscv``)
  turns each class into the set of concrete vehicle type IDs that satisfy it.

The result is a :class:`~app.domain.requirements.Resolution` — the same type the
regex engine produces — so the dispatch matcher consumes both interchangeably.

This is the **primary** path (clean, fast, deterministic). The regex engine in
:mod:`app.domain.requirements` is the **secondary** refinement, used only to add
per-instance scaling (exact crew/water counts, patient/prisoner transports) that
the static catalogue does not carry.
"""

from __future__ import annotations

from .assets import AssetLibrary
from .catalog import MissionCatalog
from .requirements import Requirement, Resolution


class MissionRequirementResolver:
    """Resolve a mission type's base requirements to vehicle-type demands."""

    def __init__(self, catalog: MissionCatalog, library: AssetLibrary) -> None:
        self._catalog = catalog
        self._library = library
        self._matchless = {
            tid for tid, vt in library.types.items() if vt.is_matchless
        }

    def resolve(self, mission_type_id: int) -> Resolution:
        """Resolve base requirements for a mission type.

        Requirement classes that have no known vehicle types in ``Vehicle.mscv``
        are reported in :attr:`Resolution.unmatched` (a data gap to fix), never
        silently dropped. An unknown mission type yields an empty resolution.
        """
        resolution = Resolution()
        requirements = self._catalog.requirements(mission_type_id)

        for vehicle_class, count in requirements.items():
            type_ids = self._library.class_map.by_class.get(vehicle_class, ())
            if not type_ids:
                resolution.unmatched.append(vehicle_class)
                continue
            resolution.requirements.append(
                Requirement(
                    source=vehicle_class,
                    type_ids=frozenset(type_ids),
                    count=int(count),
                    matchless_type_ids=frozenset(type_ids) & self._matchless,
                )
            )
        return resolution

    def knows(self, mission_type_id: int) -> bool:
        """True if the catalogue has this mission type."""
        return self._catalog.get(mission_type_id) is not None

    @property
    def catalog(self) -> MissionCatalog:
        """The underlying mission catalogue (for credits, names, etc.)."""
        return self._catalog
