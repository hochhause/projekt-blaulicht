"""Typed views over the game's JSON entities.

The exact JSON field names below are drawn from ShiftCaptain's userscript and
common knowledge of the leitstellenspiel API, but they have **not** yet been
re-verified against a live capture on this account. Every parser is therefore
defensive: it accepts a few known aliases per field and tolerates missing keys,
so a small upstream naming difference degrades gracefully instead of crashing.
Tighten these once a real response is in hand (see ``docs`` / Phase 1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# FMS (Funkmeldesystem) radio-status codes. A vehicle is dispatchable when it
# is free — status 1 (free, on the radio) or 2 (free, at the station).
FMS_AVAILABLE = {1, 2}


def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present, non-None value among *keys*."""
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return default


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Mission:
    """An open mission from ``/map/missions_json``."""

    id: int
    lat: float | None
    lon: float | None
    mission_type_id: int | None
    caption: str

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Mission":
        return cls(
            id=int(d["id"]),
            lat=_as_float(_first(d, "lat", "latitude")),
            lon=_as_float(_first(d, "lng", "long", "longitude")),
            mission_type_id=(
                int(v) if (v := _first(d, "mission_type_id", "mtid")) is not None else None
            ),
            caption=str(_first(d, "caption", "name", default="")),
        )

    @property
    def position(self) -> tuple[float, float] | None:
        if self.lat is None or self.lon is None:
            return None
        return (self.lat, self.lon)


@dataclass(frozen=True)
class Vehicle:
    """A vehicle from ``/api/vehicles``."""

    id: int
    type_id: int
    building_id: int | None
    fms: int | None
    lat: float | None
    lon: float | None
    caption: str
    type_caption: str = ""
    """The vehicle *type* name (e.g. "NEF", "HLF 20") — the key used for
    training lookups. Distinct from ``caption``, the user's custom vehicle name."""

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Vehicle":
        return cls(
            id=int(d["id"]),
            type_id=int(_first(d, "vehicle_type", "vehicle_type_id", default=-1)),
            building_id=(
                int(v) if (v := _first(d, "building_id")) is not None else None
            ),
            fms=(int(v) if (v := _first(d, "fms_real", "fms_show", "fms")) is not None else None),
            lat=_as_float(_first(d, "latitude", "lat")),
            lon=_as_float(_first(d, "longitude", "lng", "long")),
            caption=str(_first(d, "caption", default="")),
            type_caption=str(_first(d, "vehicle_type_caption", "type_caption", default="")),
        )

    @property
    def is_available(self) -> bool:
        """True if the vehicle is free to be dispatched (FMS 1 or 2)."""
        return self.fms in FMS_AVAILABLE

    @property
    def position(self) -> tuple[float, float] | None:
        if self.lat is None or self.lon is None:
            return None
        return (self.lat, self.lon)


@dataclass(frozen=True)
class Building:
    """A building from ``/api/buildings`` — used as a radius origin."""

    id: int
    lat: float | None
    lon: float | None
    building_type: int | None
    caption: str

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Building":
        return cls(
            id=int(d["id"]),
            lat=_as_float(_first(d, "latitude", "lat")),
            lon=_as_float(_first(d, "longitude", "lng", "long")),
            building_type=(
                int(v) if (v := _first(d, "building_type", "building_type_id")) is not None else None
            ),
            caption=str(_first(d, "caption", "name", default="")),
        )

    @property
    def position(self) -> tuple[float, float] | None:
        if self.lat is None or self.lon is None:
            return None
        return (self.lat, self.lon)
