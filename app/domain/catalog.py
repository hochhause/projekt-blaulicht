"""The mission-type catalogue, parsed from ``/einsaetze.json``.

``einsaetze.json`` is a public array of every mission type the game can throw,
each carrying a **structured** ``requirements`` map of vehicle-class → count.
Those class names are the same ones used by ``Vehicle.mscv`` (``firetrucks``,
``police_cars``, …), so joining the two resolves a mission's base requirements
with no text parsing at all — see :mod:`app.domain.resolver`.

Verified shape (2026-08-16), one entry::

    {
      "id": "1",                       # NB: string
      "name": "Brennender Abfallcontainer",
      "average_credits": 170,
      "requirements": {"firetrucks": 1},
      "prerequisites": {"main_building": 0, "fire_stations": 1},
      "additional": {"expansion_missions_ids": [2, 3, ...], "filter_id": "..."},
      "mission_categories": ["fire"],
      "base_mission_id": 1
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MissionType:
    """One mission type and its requirements.

    Vehicle needs come from three places (see :meth:`resolve_mission`):
    ``requirements`` (fixed class→count), ``chances`` (probabilistic extras,
    which we treat as certain so we never under-send), and ``possible_patient``
    / ``possible_prisoner`` (per-casualty transport vehicles).
    """

    id: int
    name: str
    average_credits: int
    requirements: dict[str, int]
    chances: dict[str, int] = field(default_factory=dict)
    possible_patient: int = 0
    possible_prisoner: int = 0
    # Minimum firefighters the mission needs (additional.average_min_fire_personnel).
    # Some missions state their need ONLY as personnel (no vehicle class) — e.g.
    # "3 Feuerwehrleute mind. benötigt" — satisfied by fire vehicles whose combined
    # crew reaches this (one HLF ≈ 9).
    min_fire_personnel: int = 0
    categories: tuple[str, ...] = ()
    expansion_ids: tuple[int, ...] = ()

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "MissionType":
        def _int_map(raw) -> dict[str, int]:
            return {
                str(k): int(v)
                for k, v in (raw or {}).items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            }

        additional = d.get("additional") or {}
        expansions = additional.get("expansion_missions_ids") or []
        return cls(
            id=int(d["id"]),
            name=str(d.get("name", "")),
            average_credits=int(d.get("average_credits") or 0),
            requirements=_int_map(d.get("requirements")),
            chances=_int_map(d.get("chances")),
            possible_patient=int(additional.get("possible_patient") or 0),
            possible_prisoner=int(additional.get("possible_prisoner") or 0),
            min_fire_personnel=int(additional.get("average_min_fire_personnel") or 0),
            categories=tuple(d.get("mission_categories") or ()),
            expansion_ids=tuple(int(x) for x in expansions),
        )


@dataclass(frozen=True)
class MissionCatalog:
    """All mission types, indexed by integer id."""

    types: dict[int, MissionType] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Any) -> "MissionCatalog":
        """Build from the raw ``/einsaetze.json`` payload.

        Accepts the documented array form, or an id-keyed object (in case a
        caller pre-indexed it), so the catalogue is robust to either.
        """
        if isinstance(payload, dict):
            entries = [v for v in payload.values() if isinstance(v, dict)]
        elif isinstance(payload, list):
            entries = [e for e in payload if isinstance(e, dict)]
        else:
            entries = []

        types: dict[int, MissionType] = {}
        for entry in entries:
            try:
                mtype = MissionType.from_json(entry)
            except (KeyError, TypeError, ValueError):
                continue  # skip malformed entries rather than fail the whole load
            types[mtype.id] = mtype
        return cls(types)

    def get(self, mission_type_id: int) -> MissionType | None:
        return self.types.get(mission_type_id)

    def requirements(self, mission_type_id: int) -> dict[str, int]:
        """Base class→count requirements for a mission type ({} if unknown)."""
        mtype = self.types.get(mission_type_id)
        return dict(mtype.requirements) if mtype else {}

    def __len__(self) -> int:
        return len(self.types)
