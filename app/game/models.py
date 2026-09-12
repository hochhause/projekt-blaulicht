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


def _as_int(value: Any) -> int | None:
    # mtid is an int for base missions but a variant string ("6/ab") for
    # overlays — tolerate both rather than crash.
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Mission:
    """An open mission from ``/map/missions_json`` (own or alliance-shared)."""

    id: int
    lat: float | None
    lon: float | None
    mission_type_id: int | None  # the int base type ("mtid")
    caption: str
    alliance_id: int | None = None
    alliance_shared_at: str | None = None
    patients_count: int = 0            # patients revealed so far (0 = hidden)
    possible_patients_count: int = 0   # max patients this mission can have
    prisoners_count: int = 0
    possible_prisoners_count: int = 0
    average_credits: int = 0
    created_at: str | None = None  # ISO-ish timestamp, for oldest/newest ordering
    # Revealed "still missing" list from the live feed. A JSON blob like
    # {"vehicles":"1 ELW 1","personnel":"","other":""} once units are on scene.
    missing_text: str = ""
    # Per-patient revealed needs, e.g. ["Wir benötigen: NEF"] — a Notarzt is only
    # requested once the patient is on scene (from patients_prisoners.patients[]).
    patient_missing: tuple[str, ...] = ()
    vehicle_state: int = 0         # 0 = no responders yet, >=1 = someone is on it
    # Raw icon string, e.g. "feuer_rot" — suffix is the status (see `status`
    # below). docs/live-data-model.md: _rot=missing, _gelb=en route, _gruen=done
    # (leaves the feed shortly after). Empty when the feed doesn't carry one.
    icon: str = ""

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Mission":
        pp = d.get("patients_prisoners") or {}
        patients = pp.get("patients") if isinstance(pp, dict) else None
        patient_missing = tuple(
            str(p.get("missing_text"))
            for p in (patients or [])
            if isinstance(p, dict) and p.get("missing_text")
        )
        return cls(
            id=int(d["id"]),
            lat=_as_float(_first(d, "lat", "latitude")),
            lon=_as_float(_first(d, "lng", "long", "longitude")),
            mission_type_id=_as_int(_first(d, "mission_type_id", "mtid")),
            caption=str(_first(d, "caption", "name", default="")),
            alliance_id=_as_int(_first(d, "alliance_id")),
            alliance_shared_at=_first(d, "alliance_shared_at"),
            patients_count=_as_int(_first(d, "patients_count")) or 0,
            possible_patients_count=_as_int(_first(d, "possible_patients_count")) or 0,
            prisoners_count=_as_int(_first(d, "prisoners_count")) or 0,
            possible_prisoners_count=_as_int(_first(d, "possible_prisoners_count")) or 0,
            average_credits=_as_int(_first(d, "average_credits")) or 0,
            created_at=_first(d, "created_at"),
            missing_text=str(_first(d, "missing_text", default="") or ""),
            patient_missing=patient_missing,
            vehicle_state=_as_int(_first(d, "vehicle_state")) or 0,
            icon=str(_first(d, "icon", default="") or ""),
        )

    @property
    def position(self) -> tuple[float, float] | None:
        if self.lat is None or self.lon is None:
            return None
        return (self.lat, self.lon)

    @property
    def is_alliance(self) -> bool:
        """True if this is an alliance-shared (Verbands-) mission."""
        return self.alliance_shared_at is not None

    @property
    def is_event(self) -> bool:
        """True if this is a cooperative event mission — the game tags these
        "[Event]" in the caption (Sasha, 2026-09-12), unlike alliance-sharing
        which is a separate field (`alliance_shared_at`), not a caption
        marker. An event mission can be own OR alliance-shared depending on
        who bought/set it up, so this is an INDEPENDENT flag, not a third
        value of `is_alliance`."""
        return "[Event]" in self.caption

    @property
    def status(self) -> str:
        """The icon-suffix status: "red" (missing something / untouched),
        "yellow" (something on the way), "green" (fully crewed, about to leave
        the feed), or "unknown" if the feed carried no icon at all."""
        if self.icon.endswith("_rot"):
            return "red"
        if self.icon.endswith("_gelb"):
            return "yellow"
        if self.icon.endswith("_gruen"):
            return "green"
        return "unknown"


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
    target_type: str | None = None  # 'mission' when en route to a mission
    target_id: int | None = None    # the mission id it is assigned to
    """The vehicle *type* name (e.g. "NEF", "HLF 20") — the key used for
    training lookups. v2 leaves this empty, so it falls back to ``caption``."""
    # NB: the vehicle API carries NO lat/lon — a vehicle's position is its
    # building's. The dispatch planner resolves that via building_id.

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
            # v2 leaves vehicle_type_caption empty; the vehicle's own caption
            # ("LF 20") is the default type name, so fall back to it for training.
            type_caption=str(
                _first(d, "vehicle_type_caption", "type_caption", default="")
                or _first(d, "caption", default="")
            ),
            target_type=_first(d, "target_type"),
            target_id=_as_int(_first(d, "target_id")),
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
