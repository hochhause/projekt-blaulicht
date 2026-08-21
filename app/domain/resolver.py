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


def _casualties(revealed: int, possible_live: int, possible_type: int,
                count_revealed: bool = False) -> int:
    """How many casualties to send transport for.

    The revealed count if any. A count of 0 is ambiguous: on a fresh mission
    nobody has reached yet it means "hidden" (so we pre-stock for the max
    possible), but once someone is on scene it means "none left" — e.g. a shared
    mission whose patients have already been carried off. *count_revealed* (set
    when a unit is already on the mission) makes a 0 authoritative, so we stop
    piling ambulances onto a mission with no patients left.
    """
    if revealed and revealed > 0:
        return int(revealed)
    if count_revealed:
        return 0
    return int(possible_live or possible_type or 0)


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

    def resolve_mission(self, mission) -> Resolution:
        """Full requirement derivation for a *live* mission.

        Combines, by vehicle class:
          * ``requirements`` — fixed class→count;
          * ``chances`` — every entry treated as certain (100%) so we never
            under-send (e.g. an NEF for ``nef: 75``); non-vehicle keys like
            ``patient_transport`` are ignored here (handled by transport);
          * patients → one ambulance each, and prisoners → one police car each,
            using the revealed live count if any, else the mission's max
            possible (``possible_patients_count`` / catalogue ``possible_patient``).
        """
        resolution = Resolution()
        mtid = getattr(mission, "mission_type_id", None)
        mtype = self._catalog.get(mtid) if mtid is not None else None
        if mtype is None:
            return resolution

        counts: dict[str, int] = {}
        for cls, n in mtype.requirements.items():
            counts[cls] = counts.get(cls, 0) + int(n)
        for cls in mtype.chances:
            if cls in self._library.class_map.by_class:
                counts[cls] = counts.get(cls, 0) + 1

        # A unit already on the mission means its casualty counts are revealed,
        # so a 0 is real (patients/prisoners already dealt with) rather than hidden.
        count_revealed = getattr(mission, "vehicle_state", 0) >= 1
        patients = _casualties(
            getattr(mission, "patients_count", 0),
            getattr(mission, "possible_patients_count", 0),
            mtype.possible_patient,
            count_revealed,
        )
        if patients:
            counts["ambulances"] = counts.get("ambulances", 0) + patients
        prisoners = _casualties(
            getattr(mission, "prisoners_count", 0),
            getattr(mission, "possible_prisoners_count", 0),
            mtype.possible_prisoner,
            count_revealed,
        )
        if prisoners:
            counts["police_cars"] = counts.get("police_cars", 0) + prisoners

        # Incident command scales with casualty load and is provided by US —
        # missions never request LNA/OrgL explicitly. Applies in EVERY mode: an
        # LNA above 5 patients, an OrgL above 10.
        if patients > 5:
            counts["kdow_lna"] = max(counts.get("kdow_lna", 0), 1)
        if patients > 10:
            counts["kdow_orgl"] = max(counts.get("kdow_orgl", 0), 1)

        for cls, n in counts.items():
            type_ids = self._library.class_map.by_class.get(cls, ())
            if not type_ids:
                resolution.unmatched.append(cls)
                continue
            resolution.requirements.append(
                Requirement(
                    source=cls,
                    type_ids=frozenset(type_ids),
                    count=int(n),
                    matchless_type_ids=frozenset(type_ids) & self._matchless,
                )
            )
        return resolution

    def resolve_patient_only(self, mission) -> Resolution:
        """Requirements for **patient-only** mode: one RTW (``ambulances``) per
        patient, any NEF / rescue-helicopter the mission calls for, and the
        incident-command units we add by count — an LNA above 5, an OrgL above 10.

        Uses the **exact patients currently on site** (``patients_count``) and
        deliberately does NOT fall back to the possible-max: a mission whose
        patient only appears at the END (e.g. Hitzschlag) reads 0 until then and
        is treated as having no patient at all, so we never park an RTW on it
        waiting. Empty when there's no one on site to move.
        """
        resolution = Resolution()
        mtid = getattr(mission, "mission_type_id", None)
        mtype = self._catalog.get(mtid) if mtid is not None else None
        if mtype is None:
            return resolution
        patients = int(getattr(mission, "patients_count", 0) or 0)
        if patients <= 0:
            return resolution

        counts: dict[str, int] = {"ambulances": patients}
        for cls in ("nef", "rescue_helicopter"):
            n = int(mtype.requirements.get(cls, 0)) + (1 if cls in mtype.chances else 0)
            if n:
                counts[cls] = n
        if patients > 5:
            counts["kdow_lna"] = 1
        if patients > 10:
            counts["kdow_orgl"] = 1

        for cls, n in counts.items():
            type_ids = self._library.class_map.by_class.get(cls, ())
            if not type_ids:
                resolution.unmatched.append(cls)
                continue
            resolution.requirements.append(
                Requirement(
                    source=cls,
                    type_ids=frozenset(type_ids),
                    count=int(n),
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

    @property
    def library(self) -> AssetLibrary:
        """The vehicle asset library (type → crew, class map, …)."""
        return self._library
