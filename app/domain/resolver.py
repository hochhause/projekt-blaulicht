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

import re
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
        possible), a Notarzt per patient that's asked for one (or a NEF/RTW
        pair proactively — see :meth:`_casualty_counts`), and incident
        command scaling with casualty load (an LNA above 5 possible patients,
        an OrgL above 10 — or an LNA immediately if a patient explicitly asks
        for one, confirmed live 2026-09-21).
        """
        resolution = Resolution()
        mtid = getattr(mission, "mission_type_id", None)
        mtype = self._catalog.get(mtid) if mtid is not None else None
        if mtype is None:
            return resolution

        vehicle_reqs, vehicle_unmatched = self.vehicle_requirements(mission, mtype)
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

    def vehicle_requirements(self, mission, mtype) -> tuple[list[Requirement], list[str]]:
        """Base vehicle-class demand: revealed ``missing_text`` categories when
        present, else the catalogue's class/chance estimate. Public: also
        called from ``dispatch.py`` for the vehicle-class reservation feature
        (Sasha, 2026-09-11), which needs the vehicle-only layer (excluding
        the casualty-count layer :meth:`_casualty_counts` adds) to know what
        an already-open mission still needs.

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
        HEADCOUNT into a vehicle count, using the largest per-vehicle
        PLANNING crew size among its eligible types (the fewest-vehicles/
        best-case estimate — coverage doesn't know in advance which type
        it'll end up picking, so this avoids the alternative of demanding
        one vehicle per missing head). Uses ``planning_personnel`` (halved
        for HLF) rather than nominal capacity, so a real crew shortfall
        (HLF often staffed well under its 9-person cap) doesn't get masked.
        Left as-is if none of the eligible types have a known crew size
        (defensive; shouldn't happen with the committed data)."""
        crews = [
            vt.planning_personnel
            for tid in requirement.type_ids
            if (vt := self._library.types.get(tid)) is not None and vt.planning_personnel > 0
        ]
        if not crews:
            return requirement
        vehicles_needed = max(1, ceil(requirement.count / max(crews)))
        return replace(requirement, count=vehicles_needed)

    def _casualty_counts(self, mission, mtype) -> dict[str, int]:
        """NEF / RTH / ambulances / police_cars / incident-command driven by
        revealed or possible casualty counts. Mostly independent of the
        mission's own ``missing_text`` (NEF/RTH/LNA are revealed per-patient
        via ``patients_prisoners[].missing_text``, a completely separate
        field from ``missing_text`` itself) — so this always applies on top
        of :meth:`vehicle_requirements`. LNA/OrgL are primarily OUR OWN
        incident-command heuristic (by patient-count threshold, below), but
        the game can ALSO explicitly ask for an LNA per-patient (confirmed
        live 2026-09-21, mission #519 "Hüpfburgunfall": "Wir benötigen: LNA")
        — that reveal is honored too, taking the max with the threshold."""
        counts: dict[str, int] = {}

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

        # Notärzte revealed per patient (patients_prisoners[].missing_text =
        # "Wir benötigen: NEF") — the accurate, just-in-time NEF demand.
        # "Wir benötigen: RTH" is the SAME per-patient signal but specifically
        # asks for the helicopter (confirmed live 2026-09-11) — mapped to the
        # dedicated `rescue_helicopter` class (type 31 only), not the broader
        # `nef` class (29/31/74, incl. ground Notarzt vehicles), so a plain
        # ground NEF never gets sent in place of the helicopter actually asked for.
        patient_missing = tuple(pm.lower() for pm in getattr(mission, "patient_missing", ()))
        nef_revealed = sum(
            1 for pm in patient_missing
            if ("nef" in pm or "notarzt" in pm) and "rth" not in pm and "rettungshubschrauber" not in pm
        )
        rth_needed = sum(1 for pm in patient_missing if "rth" in pm or "rettungshubschrauber" in pm)

        # A patient can also ask for a fire crew's "carrying help" getting
        # them out of a building (confirmed live 2026-09-21, "Wir benötigen:
        # Tragehilfe (z.B. durch ein LF) x2") — any LF/HLF satisfies it
        # (Sasha: "just deploy any lf, hlf, or such vehicle"), mapped to the
        # same `firetrucks` class the base catalogue requirement already
        # uses, so a fire truck already being sent for another reason covers
        # this too (coverage.py's max-not-sum multi-role semantics, same
        # precedent as the police_cars/prisoners case above). A trailing
        # "xN" is an explicit count; otherwise each matching entry counts once.
        tragehilfe_needed = 0
        for pm in patient_missing:
            if "tragehilfe" not in pm:
                continue
            count_suffix = re.search(r"x\s*(\d+)\s*$", pm)
            tragehilfe_needed += int(count_suffix.group(1)) if count_suffix else 1
        if tragehilfe_needed:
            counts["firetrucks"] = tragehilfe_needed

        # A patient can also explicitly ask for an LNA (confirmed live
        # 2026-09-21, "Wir benötigen: LNA") — independent of the patient-count
        # threshold below, an incident commander is a single per-SCENE role,
        # not one-per-patient, so this is a plain yes/no signal, not a count.
        lna_revealed = any("lna" in pm for pm in patient_missing)
        if rth_needed:
            # RTH stays strictly reveal-driven (Sasha, 2026-09-16): fast
            # (flying) and expensive, so only send one once a patient
            # actually asks for it — unlike NEF below, no proactive heuristic.
            counts["rescue_helicopter"] = rth_needed

        # NEF generosity (Sasha, 2026-09-16): "a NEF always needs a RTW too,"
        # so this never exceeds `patients` (the ambulance count above). A
        # patient already getting an RTH is excluded from the pool below —
        # the helicopter's own Notarzt already covers them, so they don't
        # also need a ground NEF. Below 5 patients, pair EVERY remaining
        # patient with a NEF — small incidents stay maximally safe, and the
        # absolute NEF cost is low anyway. At 5+ patients (mission scale,
        # counting the RTH-covered ones too), throttle down so a mass-
        # casualty incident doesn't empty the whole regional NEF fleet: take
        # the mission type's own historical NEF-need chance
        # (``mtype.chances["nef"]``, a 0-100 percentage) as the expected
        # count, then send a NEF for half of the remaining (not statistically
        # expected to need one) patients anyway, as a safety margin. Never
        # goes below whatever was explicitly revealed.
        nef_pool = max(patients - rth_needed, 0)
        if not nef_pool:
            heuristic_nef = 0
        elif patients < 5:
            heuristic_nef = nef_pool
        else:
            expected = min(round(patients * mtype.chances.get("nef", 0) / 100), nef_pool)
            remaining = max(nef_pool - expected, 0)
            heuristic_nef = expected + ceil(remaining / 2)
        nef_needed = max(nef_revealed, heuristic_nef)
        if nef_needed:
            counts["nef"] = nef_needed

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
        # LNA above 5 patients, an OrgL above 10. Deliberately NOT conservative
        # here (Sasha, 2026-09-16): threshold against the mission's MAX POSSIBLE
        # patient count, not just the currently-revealed one, so incident
        # command shows up proactively for a scene that COULD turn into a
        # mass-casualty event instead of arriving late once enough patients
        # have already been individually confirmed. EXCEPT when patients are
        # authoritatively confirmed gone (`patients` is 0 here BECAUSE
        # count_revealed made it so, not because nothing's revealed yet) --
        # then the ceiling is stale and re-escalating command staff onto an
        # already-resolved scene makes no sense; matches `patients`' own
        # revealed-zero-is-authoritative rule above.
        if count_revealed and patients == 0:
            max_patients = 0
        else:
            ceiling = int(getattr(mission, "possible_patients_count", 0) or mtype.possible_patient or 0)
            max_patients = max(patients, ceiling)
        if max_patients > 5 or lna_revealed:
            counts["kdow_lna"] = max(counts.get("kdow_lna", 0), 1)
        if max_patients > 10:
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
        # NOT extending the NEF-generosity heuristic here (Sasha's rule,
        # 2026-09-16, applied in _casualty_counts instead) — deliberately.
        # Patient-only governing NEVER allows a partial send (dispatch.py's
        # `allow_partial = (governing == "topup") or (governing == "normal"
        # and policy.partial_starts)` — "patient" is neither), so demanding a
        # NEF here would risk blocking the RTW too whenever NEFs are scarce,
        # the opposite of "safe side". Kept to the original, narrower rule.
        for cls in ("nef", "rescue_helicopter"):
            n = int(mtype.requirements.get(cls, 0)) + (1 if cls in mtype.chances else 0)
            if n:
                counts[cls] = n
        # Not conservative here either (Sasha, 2026-09-16) — threshold against
        # the mission's max possible patient count, matching _casualty_counts,
        # so incident command is sized to the scene's worst case rather than
        # only the patients already confirmed on site.
        max_patients = int(getattr(mission, "possible_patients_count", 0) or mtype.possible_patient or 0)
        max_patients = max(max_patients, patients)
        if max_patients > 5:
            counts["kdow_lna"] = 1
        if max_patients > 10:
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
