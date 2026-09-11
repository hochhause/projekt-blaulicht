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

from dataclasses import replace
from math import ceil

from .assets import AssetLibrary
from .catalog import MissionCatalog
from .missingtext import MissingTextResolver, default_resolver, parse_missing_text
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

    def __init__(
        self,
        catalog: MissionCatalog,
        library: AssetLibrary,
        missing_resolver: MissingTextResolver | None = None,
    ) -> None:
        self._catalog = catalog
        self._library = library
        self._matchless = {
            tid for tid, vt in library.types.items() if vt.is_matchless
        }
        # LSSM category-table resolver for a mission's revealed `missing_text`
        # (the authoritative remaining need, docs/live-data-model.md) — defaults
        # to the repo's committed mapping so every caller gets it for free.
        self._missing = missing_resolver if missing_resolver is not None else default_resolver()

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

        Vehicle-class demand comes from, in order:
          1. **``missing_text``** (revealed once ≥1 unit is on scene) — the
             game's own "Am Einsatzort fehlend" category list, the authoritative
             remaining need (docs/live-data-model.md). Replaces the catalogue
             estimate whenever it's present.
          2. **The catalogue** (``requirements`` fixed class→count + ``chances``
             treated as certain) — the fallback for an untouched mission that
             hasn't revealed anything yet.

        Layered on top, always, regardless of which source above applied
        (neither one carries these): patients → one ambulance each, prisoners →
        one police car each (revealed count if any, else the mission's max
        possible), a Notarzt per patient that's asked for one, and incident
        command scaling with casualty load (an LNA above 5 patients, an OrgL
        above 10).
        """
        resolution = Resolution()
        mtid = getattr(mission, "mission_type_id", None)
        mtype = self._catalog.get(mtid) if mtid is not None else None
        if mtype is None:
            return resolution

        vehicle_reqs, vehicle_unmatched = self._vehicle_requirements(mission, mtype)
        resolution.requirements.extend(vehicle_reqs)
        resolution.unmatched.extend(vehicle_unmatched)

        for cls, n in self._casualty_counts(mission, mtype).items():
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

    def _vehicle_requirements(self, mission, mtype) -> tuple[list[Requirement], list[str]]:
        """Base vehicle-class demand: revealed ``missing_text`` categories when
        present, else the catalogue's class/chance estimate.

        Branches on ``revealed``, not "has anything in it": once the game
        hands back a parseable blob, EMPTY ``vehicles``/``personnel`` groups
        are the authoritative "nothing more needed" — not "unknown, guess
        from the catalogue" (docs/live-data-model.md). Conflating the two
        would re-derive a stale catalogue requirement for a mission that's
        already fully crewed on the vehicle side (only a capacity/``other``
        gap remaining, say).
        """
        groups = parse_missing_text(getattr(mission, "missing_text", ""))
        if groups.revealed:
            reqs: list[Requirement] = []
            unmatched: list[str] = []
            if groups.vehicles:
                sub = self._missing.resolve(groups.vehicles)
                reqs.extend(sub.requirements)
                unmatched.extend(sub.unmatched)
            if groups.other:
                # `other` is documented as a CAPACITY dimension (progress
                # bars), but live-verified 2026-08-31: the entries actually
                # seen there ("Feuerlöschpumpe (z. B. LF)", "Schmutzwasserpumpe")
                # are the SAME category-text shape as `vehicles` — already
                # correctly mapped in the LSSM table (Feuerlöschpumpe -> almost
                # the whole Löschfahrzeug set; Schmutzwasserpumpe -> only the
                # two dedicated SW types) — so the same category resolver
                # handles them with no capacity math needed. The numeric
                # water_damage_pump_value / live_current_* fields on the feed
                # entry are a separate, apparently-informational signal for
                # the mission page's progress bars, not something dispatch
                # needs to compute independently once the category text names
                # the right vehicle.
                sub = self._missing.resolve(groups.other)
                reqs.extend(sub.requirements)
                unmatched.extend(sub.unmatched)
            if groups.personnel:
                # `personnel` entries are usually a HEADCOUNT ("12
                # Feuerwehrleute"), unlike `vehicles` entries which already
                # count vehicles — convert via the category's largest
                # per-vehicle crew size (the fewest-vehicles estimate), the
                # same ceil(need/cap) convention assets.ExtendRule.needed
                # uses for a single type, so we never balloon into one
                # vehicle per missing head. BUT live-verified 2026-09-11
                # (mission #4397704222 "Person in Wasser"): the game
                # sometimes puts an actual VEHICLE name under `personnel` by
                # mistake (e.g. "2 GW-Wasserrettung" -> 2 GW-W vehicles, type
                # 64 -- not a headcount of some role) -- resolve_personnel
                # tells the two apart so only genuine headcounts get converted.
                headcount, vehicle = self._missing.resolve_personnel(groups.personnel)
                reqs.extend(self._personnel_to_vehicles(r) for r in headcount.requirements)
                reqs.extend(vehicle.requirements)
                unmatched.extend(headcount.unmatched)
                unmatched.extend(vehicle.unmatched)
            return reqs, unmatched

        counts: dict[str, int] = {}
        for cls, n in mtype.requirements.items():
            counts[cls] = counts.get(cls, 0) + int(n)
        for cls in mtype.chances:
            # NEF (Notarzt) is a per-patient CHANCE — auto-sending one to every
            # mission that might need one drains the NEF fleet. Instead we add a
            # NEF only when a patient on scene actually asks for it (below).
            if cls == "nef":
                continue
            if cls in self._library.class_map.by_class:
                counts[cls] = counts.get(cls, 0) + 1

        reqs = []
        unmatched = []
        for cls, n in counts.items():
            type_ids = self._library.class_map.by_class.get(cls, ())
            if not type_ids:
                unmatched.append(cls)
                continue
            reqs.append(
                Requirement(
                    source=cls,
                    type_ids=frozenset(type_ids),
                    count=int(n),
                    matchless_type_ids=frozenset(type_ids) & self._matchless,
                )
            )
        return reqs, unmatched

    def _personnel_to_vehicles(self, requirement: Requirement) -> Requirement:
        """Convert a ``missing_text`` personnel-group requirement's raw
        HEADCOUNT into a vehicle count, using the largest per-vehicle crew
        size among its eligible types (the fewest-vehicles/best-case
        estimate — coverage doesn't know in advance which type it'll end up
        picking, so this avoids the alternative of demanding one vehicle per
        missing head). Left as-is if none of the eligible types have a known
        crew size (defensive; shouldn't happen with the committed data)."""
        crews = [
            vt.personnel
            for tid in requirement.type_ids
            if (vt := self._library.types.get(tid)) is not None and vt.personnel > 0
        ]
        if not crews:
            return requirement
        vehicles_needed = max(1, ceil(requirement.count / max(crews)))
        return replace(requirement, count=vehicles_needed)

    def _casualty_counts(self, mission, mtype) -> dict[str, int]:
        """NEF / ambulances / police_cars / incident-command driven by revealed
        or possible casualty counts. Never comes from ``missing_text`` (a NEF is
        only ever revealed via ``patients_prisoners[].missing_text``; LNA/OrgL
        are our own incident-command heuristic, never requested by the game) —
        so this always applies on top of :meth:`_vehicle_requirements`."""
        counts: dict[str, int] = {}

        # Notärzte revealed per patient (patients_prisoners[].missing_text =
        # "Wir benötigen: NEF") — the accurate, just-in-time NEF demand.
        nef_needed = sum(
            1 for pm in getattr(mission, "patient_missing", ())
            if "nef" in pm.lower() or "notarzt" in pm.lower()
        )
        if nef_needed:
            counts["nef"] = nef_needed

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
            counts["ambulances"] = patients
        prisoners = _casualties(
            getattr(mission, "prisoners_count", 0),
            getattr(mission, "possible_prisoners_count", 0),
            mtype.possible_prisoner,
            count_revealed,
        )
        if prisoners:
            counts["police_cars"] = prisoners

        # Incident command scales with casualty load and is provided by US —
        # missions never request LNA/OrgL explicitly. Applies in EVERY mode: an
        # LNA above 5 patients, an OrgL above 10.
        if patients > 5:
            counts["kdow_lna"] = max(counts.get("kdow_lna", 0), 1)
        if patients > 10:
            counts["kdow_orgl"] = max(counts.get("kdow_orgl", 0), 1)
        return counts

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
