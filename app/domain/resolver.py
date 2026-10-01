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

import json
import re
from dataclasses import replace
from functools import lru_cache
from math import ceil
from pathlib import Path

from .assets import AssetLibrary
from .catalog import MissionCatalog
from .missingtext import MissingTextResolver, default_resolver, parse_missing_text
from .requirements import Requirement, Resolution

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CAPACITY_TABLE_PATH = _REPO_ROOT / "data" / "reference" / "vehicle-water-pump.json"

# Sonderlöschmittel (foam/powder) resupply: SLF (type 167) is the only
# vehicle worth tracking for this (Sasha, 2026-09-22: "is the only one
# holding close to that much" — the "foam" values other types carry in
# vehicle-water-pump.json, 150-750L, are each vehicle's own consumption NEED
# ("Sonderloeschmittelbedarf" per the user's own CSV column), not a
# meaningful supply to redistribute). Deliberately NOT derived from the
# capacity table's "foam" keys generically — that would wrongly include
# every ordinary LF's tiny Bedarf value.
_SONDERLOESCHMITTEL_TYPE_IDS = frozenset({167})

# Water/pump eligible types: exactly the set Sasha listed (2026-09-22) —
# ordinary Löschfahrzeuge/Tanklöschfahrzeuge plus GTLF (type 121, a dedicated
# water tanker with no pump). Deliberately excludes the OTHER exotic
# dedicated tankers/AB-modules also present in vehicle-water-pump.json
# (PTLF, AB-Wasser/Schaum, etc., up to 20000L/15000 L/min) — Sasha's own
# table didn't include them, and letting them into the eligible set would
# badly skew the "fewest vehicles" ceil-division estimate for a typical
# fleet that doesn't own one (same reasoning as excluding the small
# Sonderlöschmittel trailer above, mirrored at the other end of the scale).
_WATER_PUMP_TYPE_IDS = frozenset({
    0, 1, 6, 7, 8, 9, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 30, 37, 87, 88, 89, 90, 121,
})

# A raw `other`/`personnel`-group chunk boundary, same convention as
# missingtext.py's _SPLIT_RE (duplicated rather than imported — keeps modules
# decoupled per this project's own precedent of avoiding cross-module private
# access) — including its "<count>x " variant, confirmed live 2026-10-01.
_OTHER_CHUNK_SPLIT_RE = re.compile(r",\s*(?=\d+x?\s)")

# Betreuungshelfer/Verpflegungshelfer ("Nx Betreuungshelfer"/"Nx
# Verpflegungshelfer", confirmed live 2026-10-01, mission #4420164223 "Brand
# in Mehrfamilienhaus") — a per-vehicle-type capacity confirmed directly from
# each vehicle's own .mscv extend-rule tiers (130 GW-Bt: 1-3->1, 4-6->2, ...,
# i.e. ceil(N/3); 131 Bt-Kombi: 1-9->1, 10-18->2, ..., i.e. ceil(N/9)).
# Neither vehicle has any crew/personnel data at all (capacities={} in
# AssetLibrary), so this is NOT the same concept _personnel_to_vehicles
# converts via (crew size) and gets its own small table instead. The two
# types' real capacities differ (3 vs 9) — using the SMALLER (3) as the
# shared divisor for both, so the estimate is never wrong regardless of
# which type coverage.py ends up picking (same safety-first reasoning as
# Sonderlöschmittel's single-type scoping above, applied here via MIN
# instead since excluding either type outright isn't an option — both are
# genuinely eligible, just at different scales).
_CARE_HELPER_TYPE_IDS = frozenset({130, 131})
_CARE_HELPER_CAPACITY = 3  # min(GW-Bt's 3, Bt-Kombi's 9)
_CARE_HELPER_PATTERN = re.compile(r"^(\d+)x?\s+(betreuungshelfer|verpflegungshelfer)\s*$", re.IGNORECASE)

# Polizeipferde ("N Polizeipferde", found 2026-10-01 via a systematic .mscv
# sweep) — registered in the LSSM text table (vehiclesByRequirement) with a
# "factors" field ({134: 2, 135: 4, 136: 2}) that MissingTextResolver's
# _build_table has always silently ignored (it only reads "vehicles"/
# "texts"), treating the count as a literal vehicle count like every other
# vehiclesByRequirement entry. Each vehicle's own .mscv extend-rule tiers
# independently confirm the SAME divisors (134/136: ceil(N/2); 135: ceil(N/4))
# -- two independent sources agreeing is strong enough to act on without a
# live capture, unlike the still-open "min." Ausbildung-prefix question
# below. Using the smaller divisor (2) across all three, same safety-first
# reasoning as Betreuungshelfer. Not live-confirmed which raw group ("vehicles"
# or "other") this text actually arrives in, so intercepted in both.
_POLIZEIPFERD_TYPE_IDS = frozenset({134, 135, 136})
_POLIZEIPFERD_CAPACITY = 2  # min(134's 2, 135's 4, 136's 2)
_POLIZEIPFERD_PATTERN = re.compile(r"^(\d+)\s+polizeipferde?\s*$", re.IGNORECASE)

# Capacity-phrased `other`-group entries — a raw liter/rate NUMBER, not a
# vehicle count (confirmed live: "Uns fehlt: N Sonderlöschmittel" / "N l.
# Wasser" / "N l/min Pumpenleistung") — need real ceil-division vehicle math
# against data/reference/vehicle-water-pump.json instead of the plain
# category-lookup convention every other `other`-group entry uses (confirmed
# NOT needed for Feuerlöschpumpe/Schmutzwasserpumpe, 2026-08-31 — those ARE
# plain category text despite `other`'s general "capacity" framing).
_CAPACITY_PATTERNS = (
    (re.compile(r"^(\d+)\s*sonderlöschmittel", re.IGNORECASE), "foam"),
    # "N l. Schaum" — confirmed directly in SLF's (type 167) and AB-Wasser/
    # Schaum's (type 170) own .mscv extend-rule text as an alternate phrasing
    # for the exact same foam need "N Sonderlöschmittel" already covers for
    # the same type 167 — not live-confirmed in a real mission yet, but
    # authoritative straight from the vehicle's own definition, and strictly
    # additive (same type/divisor, just a second way to spell the same thing).
    (re.compile(r"^(\d+)\s*l\.?\s*schaum", re.IGNORECASE), "foam"),
    (re.compile(r"^(\d+)\s*l\.?\s*wasser", re.IGNORECASE), "water"),
    (re.compile(r"^(\d+)\s*l\s*/\s*min\s*pumpenleistung", re.IGNORECASE), "pump"),
)

# Polizisten ("N Polizisten", a headcount in the `personnel` group) — the
# FuStW's (type 32) own .mscv extend-rule tiers confirm ceil(N/2) (1-2->1,
# 3-4->2, ...), but EVERY type in the broader "Polizisten" staff-table entry
# (type 32 plus 16 other police vehicles) has personnel=0/planning_personnel=0
# in AssetLibrary — the SAME shape of bug as Betreuungshelfer: with zero
# known crews, _personnel_to_vehicles' defensive fallback returns the
# headcount UNCONVERTED, so "5 Polizisten" would request 5 literal vehicles
# instead of ceil(5/2)=3. Keeps the same (already-established) broad
# eligible-type set, just supplies the confirmed-safe divisor it was missing.
_POLIZISTEN_TYPE_IDS = frozenset({32, 35, 50, 51, 52, 61, 72, 79, 80, 81, 82, 94, 95, 98, 103, 156, 184})
_POLIZISTEN_CAPACITY = 2
_POLIZISTEN_PATTERN = re.compile(r"^(\d+)x?\s+polizisten\s*$", re.IGNORECASE)


@lru_cache(maxsize=1)
def _default_capacity_table() -> dict[int, dict[str, int]]:
    """Per-vehicle-type water/pump/foam capacity, from the repo's committed
    (and user-cross-checked, 2026-09-22) reference data. Cached: static
    reference data, identical for every account and every mission."""
    raw = json.loads(_CAPACITY_TABLE_PATH.read_text(encoding="utf-8"))
    return {int(tid): caps for tid, caps in raw.items()}


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
        capacity_table: dict[int, dict[str, int]] | None = None,
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
        # Water/pump/foam capacity per vehicle type, for the capacity-phrased
        # `other`-group entries (see _CAPACITY_PATTERNS) — same default-unless-
        # injected pattern as `missing_resolver` above.
        self._capacity = capacity_table if capacity_table is not None else _default_capacity_table()

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
                # Polizeipferde needs ceil-division against each vehicle's
                # real horse capacity (see _POLIZEIPFERD_CAPACITY), not a
                # literal vehicle count — split it out first, same pattern
                # as the `other`-group capacity entries below. Not
                # live-confirmed which group this text actually arrives in,
                # so intercepted here AND in `other`.
                horse_reqs, remaining_vehicles = self._split_polizeipferd_entries(groups.vehicles)
                reqs.extend(horse_reqs)
                if remaining_vehicles:
                    sub = self._missing.resolve(remaining_vehicles)
                    reqs.extend(sub.requirements)
                    unmatched.extend(sub.unmatched)
            if groups.other:
                # `other` is a CAPACITY dimension (progress bars) in general,
                # but most entries actually seen there ("Feuerlöschpumpe (z.
                # B. LF)", "Schmutzwasserpumpe", live-verified 2026-08-31) are
                # plain CATEGORY text — the same shape as `vehicles`, already
                # correctly mapped in the LSSM table, no capacity math needed.
                # Sonderlöschmittel/Wasser/Pumpenleistung entries ARE genuine
                # liter/rate NUMBERS though (confirmed live 2026-09-21/22) —
                # split those out for ceil-division vehicle math first, then
                # run whatever's left through the normal category resolver.
                capacity_reqs, remaining_other = self._split_capacity_entries(groups.other)
                reqs.extend(capacity_reqs)
                horse_reqs, remaining_other = self._split_polizeipferd_entries(remaining_other)
                reqs.extend(horse_reqs)
                if remaining_other:
                    sub = self._missing.resolve(remaining_other)
                    reqs.extend(sub.requirements)
                    unmatched.extend(sub.unmatched)
            if groups.personnel:
                # Betreuungshelfer/Verpflegungshelfer ("Nx Betreuungshelfer",
                # confirmed live 2026-10-01, mission #4420164223 "Brand in
                # Mehrfamilienhaus") get their OWN conversion first — their
                # per-vehicle capacity (GW-Bt holds 3, Bt-Kombi holds 9,
                # confirmed directly from each vehicle's own .mscv extend-rule
                # tiers) isn't the crew-size concept `_personnel_to_vehicles`
                # converts via (both vehicles have NO crew data at all), so
                # routing it through that generic path would silently treat
                # the raw headcount as a literal vehicle count instead.
                care_reqs, remaining_personnel = self._split_care_helper_entries(groups.personnel)
                reqs.extend(care_reqs)
                # Polizisten gets the same treatment, same reason (found via
                # a systematic .mscv sweep, 2026-10-01): every eligible type
                # has personnel=0/planning_personnel=0, so the generic path's
                # defensive fallback would request one literal vehicle per
                # missing officer instead of ceil(N/2) FuStW.
                police_reqs, remaining_personnel = self._split_polizisten_entries(remaining_personnel)
                reqs.extend(police_reqs)
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
                if remaining_personnel:
                    headcount, vehicle = self._missing.resolve_personnel(remaining_personnel)
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

    def _split_capacity_entries(self, raw_other: str) -> tuple[list[Requirement], str]:
        """Pull Sonderlöschmittel/Wasser/Pumpenleistung liter/rate NUMBERS out
        of a raw ``other``-group string, converting each via ceil-division
        vehicle math (see :meth:`_capacity_to_requirement`). Returns
        ``(capacity_requirements, leftover_text)`` — *leftover_text* is
        whatever chunks didn't match a capacity pattern, still needing the
        normal category-lookup resolution."""
        text = (raw_other or "").strip()
        if not text:
            return [], ""
        reqs: list[Requirement] = []
        leftover: list[str] = []
        for chunk in _OTHER_CHUNK_SPLIT_RE.split(text):
            chunk = chunk.strip()
            if not chunk:
                continue
            for pattern, kind in _CAPACITY_PATTERNS:
                m = pattern.match(chunk)
                if not m:
                    continue
                req = self._capacity_to_requirement(kind, int(m.group(1)))
                if req is not None:
                    reqs.append(req)
                break
            else:
                leftover.append(chunk)
        return reqs, ", ".join(leftover)

    def _capacity_to_requirement(self, kind: str, needed_units: int) -> Requirement | None:
        """*kind* liters (``water``/``foam``) or L/min (``pump``) needed ->
        a vehicle-count Requirement, ceil-dividing by the LARGEST per-vehicle
        capacity among eligible types (same "fewest vehicles, might not have
        that exact type" optimism as :meth:`_personnel_to_vehicles` — a
        same-cycle guess that's corrected next cycle via the mission's own
        revealed need if it doesn't pan out, per the vehicle-class-reservation
        feature's established precedent). Sasha, 2026-09-22: the SW/GW-L2-
        Wasser "25% more water, stacks additively" mechanic is deliberately
        NOT modeled here — it needs real per-pool math the current count-
        based coverage algorithm (coverage.py) can't express safely; a known
        simplification, not an oversight."""
        if needed_units <= 0:
            return None
        eligible = _SONDERLOESCHMITTEL_TYPE_IDS if kind == "foam" else _WATER_PUMP_TYPE_IDS
        type_ids = frozenset(tid for tid in eligible if (self._capacity.get(tid, {}).get(kind) or 0) > 0)
        capacities = [self._capacity.get(tid, {}).get(kind, 0) for tid in type_ids]
        capacities = [c for c in capacities if c > 0]
        if not capacities:
            return None
        vehicles_needed = max(1, ceil(needed_units / max(capacities)))
        source = {"foam": "sonderloeschmittel", "water": "wasser", "pump": "pumpenleistung"}[kind]
        return Requirement(
            source=source,
            type_ids=type_ids,
            count=vehicles_needed,
            matchless_type_ids=type_ids & self._matchless,
        )

    def _split_care_helper_entries(self, raw_personnel: str) -> tuple[list[Requirement], str]:
        """Pull Betreuungshelfer/Verpflegungshelfer headcount entries out of
        a raw ``personnel``-group string (see :data:`_CARE_HELPER_PATTERN`),
        converting via ceil-division against their confirmed per-vehicle
        capacity. Returns ``(requirements, leftover_text)`` like
        :meth:`_split_capacity_entries`."""
        text = (raw_personnel or "").strip()
        if not text:
            return [], ""
        reqs: list[Requirement] = []
        leftover: list[str] = []
        for chunk in _OTHER_CHUNK_SPLIT_RE.split(text):
            chunk = chunk.strip()
            if not chunk:
                continue
            m = _CARE_HELPER_PATTERN.match(chunk)
            if not m:
                leftover.append(chunk)
                continue
            needed = int(m.group(1))
            vehicles_needed = max(1, ceil(needed / _CARE_HELPER_CAPACITY))
            reqs.append(Requirement(
                source="betreuungshelfer",
                type_ids=_CARE_HELPER_TYPE_IDS,
                count=vehicles_needed,
                matchless_type_ids=_CARE_HELPER_TYPE_IDS & self._matchless,
            ))
        return reqs, ", ".join(leftover)

    def _split_polizisten_entries(self, raw_personnel: str) -> tuple[list[Requirement], str]:
        """Pull "Polizisten" headcount entries out of a raw ``personnel``-
        group string (see :data:`_POLIZISTEN_PATTERN`), converting via
        ceil-division against the confirmed per-FuStW capacity (2). Returns
        ``(requirements, leftover_text)`` like :meth:`_split_care_helper_entries`."""
        text = (raw_personnel or "").strip()
        if not text:
            return [], ""
        reqs: list[Requirement] = []
        leftover: list[str] = []
        for chunk in _OTHER_CHUNK_SPLIT_RE.split(text):
            chunk = chunk.strip()
            if not chunk:
                continue
            m = _POLIZISTEN_PATTERN.match(chunk)
            if not m:
                leftover.append(chunk)
                continue
            needed = int(m.group(1))
            vehicles_needed = max(1, ceil(needed / _POLIZISTEN_CAPACITY))
            reqs.append(Requirement(
                source="polizisten",
                type_ids=_POLIZISTEN_TYPE_IDS,
                count=vehicles_needed,
                matchless_type_ids=_POLIZISTEN_TYPE_IDS & self._matchless,
            ))
        return reqs, ", ".join(leftover)

    def _split_polizeipferd_entries(self, raw_text: str) -> tuple[list[Requirement], str]:
        """Pull "Polizeipferde" entries out of a raw ``vehicles``- or
        ``other``-group string (see :data:`_POLIZEIPFERD_PATTERN`),
        converting via ceil-division against the confirmed per-vehicle horse
        capacity instead of treating the count as a literal vehicle count.
        Returns ``(requirements, leftover_text)`` like
        :meth:`_split_care_helper_entries`."""
        text = (raw_text or "").strip()
        if not text:
            return [], ""
        reqs: list[Requirement] = []
        leftover: list[str] = []
        for chunk in _OTHER_CHUNK_SPLIT_RE.split(text):
            chunk = chunk.strip()
            if not chunk:
                continue
            m = _POLIZEIPFERD_PATTERN.match(chunk)
            if not m:
                leftover.append(chunk)
                continue
            needed = int(m.group(1))
            vehicles_needed = max(1, ceil(needed / _POLIZEIPFERD_CAPACITY))
            reqs.append(Requirement(
                source="polizeipferd",
                type_ids=_POLIZEIPFERD_TYPE_IDS,
                count=vehicles_needed,
                matchless_type_ids=_POLIZEIPFERD_TYPE_IDS & self._matchless,
            ))
        return reqs, ", ".join(leftover)

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
