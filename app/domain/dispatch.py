"""The dispatch planner — decide which vehicles to send to which missions.

Pure, deterministic, and network-free: given the current missions, the fleet,
and the account's buildings, it produces a :class:`DispatchPlan`. The runner
(:mod:`app.core.runner`) does the I/O around it. Keeping the decision here means
the whole policy is unit-tested without a live account.

Per cycle it:

1. keeps only missions within ``radius_km`` of some building, nearest first;
2. resolves each mission's requirements (structured catalogue + class map);
3. applies the **delta** — subtracts vehicles already en route, so a mission
   that's already covered is left alone and a partial one is only topped up;
4. runs the **multi-role coverage** matcher over the still-available pool, so one
   HLF fills an LF + RW at once (see :mod:`app.domain.coverage`);
5. consumes the chosen vehicles from the pool before planning the next mission,
   so two missions never claim the same vehicle.

Vehicles/missions/buildings are duck-typed (the game models qualify): a mission
needs ``id``, ``caption``, ``mission_type_id``, ``position``; a vehicle needs
``id``, ``type_id``, ``is_available``, ``position``; a building needs ``position``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum

from .coverage import select_coverage
from .geo import haversine_km, nearest_km
from .missingtext import parse_missing_text
from .resolver import MissionRequirementResolver, _casualties


class DispatchOrder(str, Enum):
    """The order in which in-range missions are worked each cycle."""

    NEAREST = "nearest"            # closest to your buildings first (default)
    FARTHEST = "farthest"
    CREDITS_HIGH = "credits_high"  # most cash first
    CREDITS_LOW = "credits_low"    # least cash first
    OLDEST = "oldest"              # longest-open first (by created_at)
    NEWEST = "newest"
    SMART = "smart"                # squeeze the most total credits out of the
                                    # available fleet: rank by credits/vehicle
                                    # needed so scarce vehicles go to the
                                    # missions that stretch them furthest,
                                    # then dispatch every mission the fleet
                                    # can still fully crew (Sasha, 2026-09-11)


class MissionSource(str, Enum):
    """Which missions to work: your own, alliance-shared, or both."""

    BOTH = "both"
    OWN = "own"
    ALLIANCE = "alliance"


class ModeScope(str, Enum):
    """Which missions a special mode (parasite / top-up / patient-only) applies
    to, independently of the normal-dispatch source. Lets you e.g. run normal
    dispatch on own missions only while a mode runs on alliance missions."""

    OFF = "off"
    OWN = "own"
    ALLIANCE = "alliance"
    BOTH = "both"


class ParasiteAggressiveness(str, Enum):
    """How eagerly parasite mode grabs a mission (Sasha, 2026-08-31)."""

    ACTIVE = "active"  # default/current: only missions someone's ALREADY on (vehicle_state>=1)
    ALL = "all"        # any scope-covered mission, regardless of whether anyone's on it yet
    NEEDED = "needed"  # only missions with REVEALED missing_text — never a pure "claim anyway"
    # spare send; if there's nothing genuinely still missing, parasite skips it.


def _scope_covers(scope, is_alliance: bool) -> bool:
    """True if a :class:`ModeScope` value covers a mission of this source."""
    scope = getattr(scope, "value", scope)
    if scope == ModeScope.BOTH.value:
        return True
    if scope == ModeScope.OWN.value:
        return not is_alliance
    if scope == ModeScope.ALLIANCE.value:
        return is_alliance
    return False  # "off" / unknown


def _source_covers(source: "MissionSource", is_alliance: bool) -> bool:
    """True if the normal-dispatch mission_source covers this mission."""
    if source is MissionSource.OWN:
        return not is_alliance
    if source is MissionSource.ALLIANCE:
        return is_alliance
    return True  # BOTH


@dataclass(frozen=True)
class DispatchPolicy:
    """Per-account dispatch settings (a slice of the account config).

    Cash bounds are separate for own vs alliance missions (0 = no bound).
    Alliance missions additionally have an age window (seconds since shared, or
    since the mission was created if the share time is unavailable).
    """

    radius_km: float = 40.0
    own_credit_min: int = 0
    own_credit_max: int = 0
    alliance_credit_min: int = 0
    alliance_credit_max: int = 0
    alliance_min_age_seconds: int = 0  # skip alliance missions shared too recently
    alliance_max_age_seconds: int = 0  # skip alliance missions shared too long ago
    max_vehicles_per_mission: int | None = None
    order: DispatchOrder = DispatchOrder.NEAREST
    batch_size: int | None = None
    # Only work missions in these game categories (fire, police, …). Empty = all.
    allowed_categories: frozenset[str] = frozenset()
    # Which missions to work, and an optional separate radius for alliance ones.
    mission_source: MissionSource = MissionSource.BOTH  # normal full dispatch
    alliance_radius_km: float | None = None
    # Special modes, each SCOPED independently (off / own / alliance / both) so you
    # can e.g. keep normal dispatch own-only while a mode runs on alliance missions.
    # When several cover the same mission, precedence is patient > top-up > normal.
    #
    # Parasite: leech missions others are already on — contribute a useful vehicle
    # if a role is missing, else a spare just to claim a participation share
    # (priority pass; bypasses cash/category filters + batch/order).
    parasite_scope: ModeScope = ModeScope.OFF
    # How eagerly parasite grabs a mission — see ParasiteAggressiveness.
    parasite_mode: ParasiteAggressiveness = ParasiteAggressiveness.ACTIVE
    # Top-up (symbiont): only reinforce ALREADY-begun missions (vehicle_state>=1),
    # sending the useful vehicles we have (partial ok). No fresh missions.
    top_up_scope: ModeScope = ModeScope.OFF
    # Patient-only: dispatch ONLY the units that clear patients (RTW/NEF/RTH + an
    # LNA >5 patients, OrgL >10); the runner recalls idle units once patients gone.
    patient_scope: ModeScope = ModeScope.OFF
    # Hilferuf ("call for help"): when we can't crew one of our OWN missions,
    # share it to the alliance so members can help. Runner-level add-on.
    hilferuf: bool = False
    # Partial dispatch: send whatever useful vehicles we have to a mission even if
    # we can't fully crew it (instead of waiting for a full set). Reports the gap.
    partial_starts: bool = False


@dataclass(frozen=True)
class MissionPlan:
    """What to send to one mission this cycle."""

    mission_id: int
    caption: str
    vehicle_ids: tuple[int, ...]
    satisfied: bool
    shortfall: dict[str, int]


@dataclass
class DispatchPlan:
    planned: list[MissionPlan] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)  # (mission_id, reason)
    # class → how many missions this cycle we couldn't (fully) crew because of it
    # (short supply or struck out). Feeds the dashboard's "struggling to field" list.
    shortfalls: dict[str, int] = field(default_factory=dict)

    @property
    def total_dispatches(self) -> int:
        return sum(len(m.vehicle_ids) for m in self.planned)


def assigned_from_vehicles(vehicles) -> dict[int, list[int]]:
    """Map mission id → vehicle type IDs already en route to it (the delta).

    A vehicle carries ``target_type == 'mission'`` and ``target_id`` (the mission
    id) while assigned; those reduce that mission's outstanding requirements so
    the loop tops missions up instead of re-dispatching to them.
    """
    assigned: dict[int, list[int]] = {}
    for vehicle in vehicles:
        if getattr(vehicle, "target_type", None) != "mission":
            continue
        mission_id = getattr(vehicle, "target_id", None)
        type_id = getattr(vehicle, "type_id", None)
        if mission_id is not None and type_id is not None:
            assigned.setdefault(int(mission_id), []).append(int(type_id))
    return assigned


def plan_dispatches(
    missions,
    vehicles,
    buildings,
    resolver: MissionRequirementResolver,
    policy: DispatchPolicy,
    assigned: dict[int, list[int]] | None = None,
    text_engine=None,
    now: float | None = None,
    strikes=None,
) -> DispatchPlan:
    """Plan this cycle's dispatches.

    *assigned* maps mission id → type IDs of vehicles already en route (the
    delta). *text_engine* (a RequirementEngine) parses a mission's live
    ``missing_text`` when the structured catalogue has no requirements. *now*
    (epoch seconds) drives the alliance age-since-shared window.
    """
    plan = DispatchPlan()
    origins = [b.position for b in buildings if getattr(b, "position", None)]
    # Available fleet, minus any specific vehicles struck out for repeatedly
    # FAILING to dispatch (looked free — FMS 1/2 — but the send failed, e.g. no
    # crew). We simply stop trusting those trucks; a class we merely don't have
    # is NOT struck — it's just absent from the pool and skipped per-mission.
    def _usable(v) -> bool:
        if not getattr(v, "is_available", False):
            return False
        if strikes is not None and now is not None and strikes.is_struck(str(v.id), now):
            return False
        return True

    pool = [v for v in vehicles if _usable(v)]
    # Average firetruck crew size, for SMART order's rough min_fire_personnel
    # estimate below (real per-type data, not a guessed constant). Uses
    # planning_personnel (HLF halved) for consistency with the actual
    # dispatch-time crew math below, though at today's fleet composition the
    # shift is small enough to vanish under the estimate's own int() rounding
    # -- fine, since this only needs to be a reasonable relative ranking, not
    # an exact count (see the SMART order comment further down).
    _fire_types = [
        vt for tid, vt in resolver.library.types.items()
        if tid in resolver.library.class_map.by_class.get("firetrucks", ())
    ]
    avg_fire_crew = (sum(vt.planning_personnel for vt in _fire_types) / len(_fire_types)) if _fire_types else 6.0
    # Vehicles carry no coordinates — a vehicle's position is its building's,
    # used to rank same-role candidates nearest-first.
    building_pos = {
        b.id: b.position
        for b in buildings
        if getattr(b, "position", None) and getattr(b, "id", None) is not None
    }

    # PRIORITY PASS — spread/top-up (missions we're on) and, in parasite mode,
    # leech targets. Runs first, bypassing filters/order/batch.
    handled, priority_batch, reserved_type_ids = _priority_pass(
        plan, missions, pool, resolver, policy, assigned or {}, building_pos, text_engine
    )

    # Gather in-range, category-allowed missions with their distance, the mission
    # type's average credits (for ordering) and the governing mode.
    candidates: list[tuple[float, int, object, str]] = []  # (distance, credits, mission, mode)
    for mission in missions:
        if mission.id in handled:
            continue  # already actioned in the priority pass
        is_alliance = getattr(mission, "is_alliance", False)
        # Resolve which treatment this mission gets from the per-mode scopes.
        # Precedence when several cover it: patient > top-up > normal.
        if _scope_covers(policy.patient_scope, is_alliance):
            governing = "patient"
        elif _scope_covers(policy.top_up_scope, is_alliance):
            governing = "topup"
        elif _source_covers(policy.mission_source, is_alliance):
            governing = "normal"
        else:
            plan.skipped.append((mission.id, "out of scope"))
            continue
        # Top-up only reinforces missions someone has already begun.
        if governing == "topup" and getattr(mission, "vehicle_state", 0) < 1:
            plan.skipped.append((mission.id, "not begun (top-up)"))
            continue
        pos = getattr(mission, "position", None)
        if pos is None:
            plan.skipped.append((mission.id, "no position"))
            continue
        # Alliance missions may use their own radius.
        radius = policy.radius_km
        if is_alliance and policy.alliance_radius_km is not None:
            radius = policy.alliance_radius_km
        distance = nearest_km(pos, origins)
        if distance is None or distance > radius:
            plan.skipped.append((mission.id, "out of range"))
            continue
        mtype = (
            resolver.catalog.get(mission.mission_type_id)
            if mission.mission_type_id is not None
            else None
        )
        if policy.allowed_categories:
            cats = set(mtype.categories) if mtype else set()
            if not (cats & policy.allowed_categories):
                plan.skipped.append((mission.id, "category filtered"))
                continue
        # Prefer the mission's own credits; fall back to the type average.
        credits = getattr(mission, "average_credits", 0) or (mtype.average_credits if mtype else 0)
        # Cash bounds and (for alliance) the age window are per-source.
        if is_alliance:
            cmin, cmax = policy.alliance_credit_min, policy.alliance_credit_max
            age = _age_seconds(mission, now)
            if age is not None:
                if policy.alliance_min_age_seconds and age < policy.alliance_min_age_seconds:
                    plan.skipped.append((mission.id, "shared too recently"))
                    continue
                if policy.alliance_max_age_seconds and age > policy.alliance_max_age_seconds:
                    plan.skipped.append((mission.id, "shared too long ago"))
                    continue
        else:
            cmin, cmax = policy.own_credit_min, policy.own_credit_max
        if cmin and credits < cmin:
            plan.skipped.append((mission.id, f"below credit floor ({credits})"))
            continue
        if cmax and credits > cmax:
            plan.skipped.append((mission.id, f"above credit ceiling ({credits})"))
            continue
        # Reservation for any mission we could act on that's already open and
        # short a vehicle category -- see _reserve_vehicle_group's docstring.
        _reserve_vehicle_group(reserved_type_ids, mission, mtype, resolver, assigned,
                                patient_governed=(governing == "patient"))
        # SMART order ranks by credits PER VEHICLE NEEDED (not raw credits) —
        # a cheap, well-known knapsack heuristic: one expensive mission that
        # eats a rare vehicle type can deny several cheaper missions worth
        # more combined, so rank by "value density" instead of pure value.
        # This ranking only decides PRIORITY, not the goal: the loop below
        # (plan_dispatches' candidate-consuming pass) walks every candidate in
        # this order and dispatches everything the shrinking pool can still
        # fully crew, so the actual outcome is "the most total credits this
        # fleet can earn right now" — density-first just means scarce
        # vehicles go to whichever mission stretches them furthest first.
        # Resolved with the SAME requirement derivation + delta the actual
        # dispatch step uses, so the estimate matches what will really be sent.
        efficiency = 0.0
        if policy.order is DispatchOrder.SMART:
            base = _derive_requirements(mission, resolver, text_engine,
                                         patient_only=(governing == "patient"))
            outstanding = _apply_delta(base, assigned, mission.id)
            vehicles_needed = sum(r.count for r in outstanding)
            # A mission may ALSO need a minimum firefighter headcount beyond
            # its class counts (_plan_one tops this up with extra firetrucks
            # via _fire_personnel_topup) — fold in a rough estimate using the
            # real average firetruck crew size, so a "1 firetruck" mission
            # that actually needs 3 for its personnel floor doesn't rank as
            # artificially efficient.
            min_fire = int(getattr(mtype, "min_fire_personnel", 0) or 0) if mtype is not None else 0
            if min_fire and governing != "patient":
                already = _fire_crew(resolver, (assigned or {}).get(mission.id, []))
                fire_needed = max(0, min_fire - already)
                vehicles_needed += -(-fire_needed // max(1, int(avg_fire_crew)))  # ceil division
            efficiency = credits / max(1, vehicles_needed)
        candidates.append((distance, credits, mission, governing, efficiency))

    candidates = _order(candidates, policy.order)

    # Batch limits missions ACTUALLY DISPATCHED per cycle — not the number
    # considered. Missions skipped (no vehicles, already covered, …) don't use a
    # slot; only once `batch_size` missions have been actioned is the rest
    # deferred as "beyond batch size".
    actioned = priority_batch  # priority useful contributions already count
    for _distance, _credits, mission, governing, _efficiency in candidates:
        if policy.batch_size is not None and actioned >= policy.batch_size:
            plan.skipped.append((mission.id, "beyond batch size"))
            continue
        allow_partial = (governing == "topup") or (governing == "normal" and policy.partial_starts)
        # A genuinely FRESH send (nobody there yet) can't use a reserved class —
        # that class is needed to close out a mission that's already open
        # elsewhere. Anything with vehicle_state >= 1 (help/reinforcement) is
        # exempt: it's exactly who the reservation exists for. topup missions
        # are always vehicle_state >= 1 already (enforced above), so they're
        # naturally exempt without a separate check.
        is_fresh_send = governing == "normal" and getattr(mission, "vehicle_state", 0) == 0
        call_pool = (
            [v for v in pool if v.type_id not in reserved_type_ids]
            if is_fresh_send and reserved_type_ids else pool
        )
        outcome = _plan_one(mission, call_pool, resolver, policy, assigned, building_pos,
                            text_engine, allow_partial=allow_partial,
                            patient_only=(governing == "patient"), shortfalls=plan.shortfalls)
        if isinstance(outcome, str):
            plan.skipped.append((mission.id, outcome))
            continue
        mission_plan, chosen = outcome
        plan.planned.append(mission_plan)
        actioned += 1
        chosen_set = set(chosen)
        pool = [v for v in pool if v.id not in chosen_set]

    return plan


def plan_cleanup(missions, vehicles, buildings, resolver, text_engine=None):
    """Find OUR vehicles that are surplus to a mission's needs — for the manual
    'cleanup' button.

    For every mission we have vehicles on (en route or on scene), keep the minimal
    subset of *our* vehicles that meets its requirement (vehicle classes + fire
    personnel), and mark the rest for recall. A vehicle transporting a patient
    (FMS 5) is always kept. Missions whose requirement we can't resolve at all are
    left untouched (we won't pull vehicles off something we can't judge).

    Returns a list of ``(vehicle_id, mission_id)`` to recall.
    """
    building_pos = {
        b.id: b.position
        for b in buildings
        if getattr(b, "position", None) and getattr(b, "id", None) is not None
    }
    by_mid = {m.id: m for m in missions}
    ours: dict[int, list] = {}
    for v in vehicles:
        if getattr(v, "target_type", None) != "mission":
            continue
        mid = getattr(v, "target_id", None)
        if mid is not None:
            ours.setdefault(int(mid), []).append(v)

    recalls: list[tuple[int, int]] = []
    for mid, assigned_vehicles in ours.items():
        mission = by_mid.get(mid)
        if mission is None:
            continue  # not in the feed (likely done) — leave it to the game
        reqs = _derive_requirements(mission, resolver, text_engine)
        mtype = resolver.catalog.get(getattr(mission, "mission_type_id", None))
        min_fire = int(getattr(mtype, "min_fire_personnel", 0) or 0) if mtype is not None else 0
        if not reqs and min_fire <= 0:
            continue  # requirement unknown -> don't touch this mission
        pos = getattr(mission, "position", None)
        needed: set[int] = set()
        if reqs:
            cov = select_coverage(reqs, assigned_vehicles, distance_of=_distance_fn(pos, building_pos))
            needed |= set(cov.vehicle_ids)
        if min_fire > 0:
            extra, _short = _fire_personnel_topup(
                list(needed), assigned_vehicles, resolver, min_fire, pos, building_pos
            )
            needed |= set(extra)
        for v in assigned_vehicles:
            if v.id in needed:
                continue
            if getattr(v, "fms", None) == 5:
                continue  # carrying a patient — let it deliver
            recalls.append((v.id, mid))
    return recalls


def _age_seconds(mission, now: float | None) -> float | None:
    """Seconds since a mission was shared (or created), or ``None`` if unknown."""
    if now is None:
        return None
    ref = getattr(mission, "alliance_shared_at", None) or getattr(mission, "created_at", None)
    epoch = _to_epoch(ref)
    if epoch is None:
        return None
    return max(0.0, now - epoch)


def _to_epoch(ts) -> float | None:
    """Best-effort parse of a timestamp (epoch number or ISO string) to epoch."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    s = str(ts).strip()
    if not s:
        return None
    try:
        return float(s)  # numeric string (unix epoch)
    except ValueError:
        pass
    try:
        from datetime import datetime

        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, OSError):
        return None


def _order(candidates, order: DispatchOrder):
    """Sort (distance, credits, mission) tuples by the chosen strategy."""
    if order is DispatchOrder.SMART:
        # Credits per vehicle needed, descending (index 4 — see plan_dispatches).
        # group.py's plan_group_dispatches builds plain 3-tuples and rejects
        # SMART before ever calling here (see GroupConfig.group_policy), so
        # the exact-5 check below is a belt-and-braces no-op degrade to raw
        # credits, never expected to actually trigger.
        # ponytail: tuple-shape sniffing, not a typed Candidate — upgrade to a
        # NamedTuple if another field is ever added to either tuple shape.
        return sorted(candidates, key=lambda t: (-(t[4] if len(t) == 5 else t[1]), t[0], t[2].id))
    if order is DispatchOrder.CREDITS_HIGH:
        return sorted(candidates, key=lambda t: (-t[1], t[0], t[2].id))
    if order is DispatchOrder.CREDITS_LOW:
        return sorted(candidates, key=lambda t: (t[1], t[0], t[2].id))
    if order is DispatchOrder.FARTHEST:
        return sorted(candidates, key=lambda t: (-t[0], t[2].id))
    if order in (DispatchOrder.OLDEST, DispatchOrder.NEWEST):
        # Sort by created_at (ISO strings sort chronologically). Missing
        # timestamps sort last for oldest / first for newest via the sentinel.
        reverse = order is DispatchOrder.NEWEST
        return sorted(
            candidates,
            key=lambda t: (getattr(t[2], "created_at", None) or "9999", t[2].id),
            reverse=reverse,
        )
    return sorted(candidates, key=lambda t: (t[0], t[2].id))  # NEAREST


def _missing_text_vehicles(mission) -> str:
    """The still-missing vehicle list the live feed reveals, as a caption string.

    ``missing_text`` is a JSON blob once units are on scene, e.g.
    ``{"vehicles":"1 ELW 1","personnel":"","other":""}`` — we take ``vehicles``
    (personnel = firefighters, handled via the fire-personnel path), delegating
    the blob parsing to :func:`app.domain.missingtext.parse_missing_text` (the
    same parser :meth:`MissionRequirementResolver.resolve_mission` uses).
    Older/plain (non-JSON) strings are passed through as-is, for the legacy
    regex engine below.
    """
    raw = (getattr(mission, "missing_text", "") or "").strip()
    if not raw:
        return ""
    if raw[0] == "{":
        return parse_missing_text(raw).vehicles
    return raw


def _derive_requirements(mission, resolver, text_engine, patient_only=False):
    """Base (pre-delta) requirements.

    Primary = ``resolver.resolve_mission`` — the LSSM ``missing_text`` category
    table once a mission has revealed it, else the structured catalogue
    estimate (patient-only picks just the medical units either way; see
    :mod:`app.domain.resolver`). *text_engine* (the legacy regex engine,
    keyed on raw vehicle CAPTIONS rather than categories) is now only a
    tertiary, last-resort fallback for a mission type the resolver returns
    NOTHING for at all — e.g. one missing from the catalogue AND whose
    missing_text categories don't match the LSSM table either — so a mission
    is still handled instead of silently skipped. (Only on an EMPTY resolver
    result, to avoid double-counting: the text engine keys requirements by raw
    caption, not class/category, so merging it with resolver output would
    over-count shared needs.)
    """
    if patient_only:
        return list(resolver.resolve_patient_only(mission).requirements)
    reqs = list(resolver.resolve_mission(mission).requirements)
    if not reqs and text_engine is not None:
        veh = _missing_text_vehicles(mission)
        if veh:
            parsed = text_engine.resolve(re.split(r"[,;\n/]+", veh))
            reqs = list(parsed.requirements)
    return reqs


def _outstanding(mission, resolver, text_engine, assigned, patient_only=False):
    """Post-delta requirements still to send (catalogue/revealed minus en-route)."""
    return _apply_delta(
        _derive_requirements(mission, resolver, text_engine, patient_only), assigned, mission.id
    )


def _distance_fn(mission_pos, building_pos):
    def distance_of(vehicle) -> float:
        vp = getattr(vehicle, "position", None) or building_pos.get(getattr(vehicle, "building_id", None))
        return haversine_km(mission_pos, vp) if (vp and mission_pos) else float("inf")
    return distance_of


def _abundance_vehicle(pool, mission_pos, building_pos):
    """The nearest instance of whatever vehicle type we hold most of — a spare
    to throw at a leech mission just to claim participation."""
    if not pool:
        return None
    from collections import Counter

    common_type = Counter(v.type_id for v in pool).most_common(1)[0][0]
    candidates = [v for v in pool if v.type_id == common_type]
    return min(candidates, key=_distance_fn(mission_pos, building_pos))


def _priority_pass(plan, missions, pool, resolver, policy, assigned, building_pos, text_engine):
    """Handle spread/top-up and parasite missions BEFORE the normal filtered pass.

    Bypasses cash/source/category filters, ordering and batch. Returns the set of
    handled mission ids, how many count toward the batch (useful contributions
    do; pure leech sends do not), and the vehicle TYPE IDS reserved because a
    mission evaluated here already has some response (vehicle_state >= 1) and
    still mentions them as needed — see ``reserved_type_ids`` in
    :func:`plan_dispatches` for the full rationale (unconditional, not gated
    on whether this pass's own attempt happens to look sufficient).
    """
    handled: set[int] = set()
    batch_used = 0
    reserved: set[int] = set()
    assigned = assigned or {}
    for mission in missions:
        mid = mission.id
        mtype = resolver.catalog.get(getattr(mission, "mission_type_id", None))
        on_it = mid in assigned                       # a mission we're already on
        others_on = getattr(mission, "vehicle_state", 0) >= 1
        is_alliance = getattr(mission, "is_alliance", False)
        scope_ok = _scope_covers(policy.parasite_scope, is_alliance) and not on_it
        # Aggressiveness (Sasha, 2026-08-31): ACTIVE (default) requires someone
        # already on it, matching the original behavior; ALL drops that
        # requirement entirely (grab any scope-covered mission); NEEDED adds
        # the extra requirement that missing_text is actually revealed (skip
        # missions others are on but that are already fully covered).
        if policy.parasite_mode is ParasiteAggressiveness.ALL:
            is_parasite = scope_ok
        elif policy.parasite_mode is ParasiteAggressiveness.NEEDED:
            revealed = parse_missing_text(getattr(mission, "missing_text", "")).revealed
            is_parasite = scope_ok and others_on and revealed
        else:  # ACTIVE
            is_parasite = scope_ok and others_on
        if not (on_it or is_parasite):
            continue

        pos = getattr(mission, "position", None)
        patient_here = _scope_covers(policy.patient_scope, is_alliance)
        # Parasite on a patient-governed mission we're not on (leeching, not
        # topping up): never compute a "useful" patient-only contribution
        # here — go straight to the plain participation-claiming spare send
        # below. Decision (Sasha, 2026-08-31): patient_scope's own governance
        # (the top-up/`on_it` branch above, for missions we're actually
        # assigned to) is what handles real medical need; a parasite leech
        # isn't medically aware and shouldn't pretend a random "useful" pick
        # is patient care. Once this claim send lands, the mission becomes
        # `on_it` from NEXT cycle onward, so real medical coverage follows
        # with a one-cycle delay, not immediately this same cycle.
        skip_patient_contribution = is_parasite and patient_here
        chosen: list[int] = []
        if not skip_patient_contribution:
            reqs = _outstanding(mission, resolver, text_engine, assigned, patient_here)
            if reqs and pos is not None:
                cov = select_coverage(reqs, pool, distance_of=_distance_fn(pos, building_pos))
                if cov.vehicle_ids:
                    chosen = list(cov.vehicle_ids)
                    plan.planned.append(MissionPlan(mid, getattr(mission, "caption", ""),
                                                    tuple(chosen), cov.satisfied, dict(cov.shortfall)))
                    if not cov.satisfied:
                        _note_shortfall(plan.shortfalls, cov.shortfall.keys())
                    batch_used += 1  # useful contribution -> counts to batch
        if not chosen and is_parasite:
            spare = _abundance_vehicle(pool, pos, building_pos)
            if spare is not None:
                chosen = [spare.id]
                plan.planned.append(MissionPlan(mid, getattr(mission, "caption", ""),
                                                (spare.id,), False, {}))  # pure leech, free
        if chosen:
            cset = set(chosen)
            pool[:] = [v for v in pool if v.id not in cset]
            handled.add(mid)
        # Same reservation rule as plan_dispatches' main loop, regardless of
        # whether OUR OWN attempt above happens to look sufficient this cycle.
        _reserve_vehicle_group(reserved, mission, mtype, resolver, assigned,
                                patient_governed=patient_here)
    return handled, batch_used, reserved


def _note_shortfall(shortfalls, classes) -> None:
    """Tally each class that left a mission short/struck this cycle."""
    if shortfalls is None:
        return
    for cls in classes:
        shortfalls[cls] = shortfalls.get(cls, 0) + 1


def _fire_crew(resolver, type_ids) -> int:
    """Total firefighter crew carried by these vehicle TYPE ids (firetruck
    class) — uses each type's PLANNING crew (halved for HLF; see
    ``VehicleType.planning_personnel``), not its nominal max, so a personnel
    floor doesn't look satisfied by crew that's rarely actually aboard."""
    lib = resolver.library
    fire = lib.class_map.by_class.get("firetrucks", ())
    fire = set(fire)
    total = 0
    for tid in type_ids:
        if tid in fire:
            vt = lib.types.get(tid)
            total += vt.planning_personnel if vt is not None else 0
    return total


def _fire_personnel_topup(chosen_ids, pool, resolver, need, mission_pos, building_pos):
    """Add nearest firetruck-class vehicles until their crew reaches *need*.

    Crew already provided by *chosen_ids* (fire vehicles picked for the class
    requirements) counts first. Returns ``(extra_ids, still_short)``.
    """
    lib = resolver.library
    fire = set(lib.class_map.by_class.get("firetrucks", ()))

    def crew(tid: int) -> int:
        vt = lib.types.get(tid)
        return vt.planning_personnel if vt is not None else 0

    by_id = {v.id: v for v in pool}
    have = sum(
        crew(by_id[i].type_id)
        for i in chosen_ids
        if i in by_id and by_id[i].type_id in fire
    )
    if have >= need:
        return [], 0
    chosen_set = set(chosen_ids)
    dist = _distance_fn(mission_pos, building_pos)
    # Nearest first; among equals prefer more crew so we send fewer vehicles.
    cands = [v for v in pool if v.id not in chosen_set and v.type_id in fire and crew(v.type_id) > 0]
    cands.sort(key=lambda v: (dist(v), -crew(v.type_id)))
    extra: list[int] = []
    for v in cands:
        if have >= need:
            break
        extra.append(v.id)
        have += crew(v.type_id)
    return extra, max(0, need - have)


# ELW-family vehicles that are also valuable elsewhere (SEG-ELW: preferred
# for its own automatic patient handling on mass-casualty missions;
# ELW2-Drohne: also a drone) -- deprioritized for a plain ELW slot so they
# aren't spent on ordinary missions when a generic ELW1/ELW2 would do just
# as well. Confirmed by Sasha, 2026-09-11.
_ELW_SPECIALIST_TYPE_IDS = frozenset({59, 129})  # SEG-ELW, ELW2-Drohne
# SEG-ELW specifically becomes the BETTER choice (not just an acceptable
# substitute) once a mission's casualty load is heavy enough that its
# automatic patient-FSM handling earns its keep.
_SEG_ELW_TYPE_ID = 59
_SEG_ELW_PREFERRED_PATIENT_THRESHOLD = 20

# The self-propelled Mehrzweckboot (not its trailers -- Anh MzB/SchlB/MzAB,
# types 66/67/68/71, which move only when towed by something else already).
_MZB_TYPE_ID = 70


def _add_water_rescue_escort(chosen, pool, resolver, mission_pos, building_pos):
    """A dispatched MZB always wants a GW-Taucher + GW-Wasserrettung riding
    along too, preferably from the SAME STATION (confirmed by Sasha,
    2026-09-11). Purely opportunistic: never a hard requirement, never
    recorded as a shortfall, never blocks the MZB's own dispatch -- if
    either escort isn't idle and available, that one is just skipped.
    """
    by_id = {v.id: v for v in pool}
    mzb = next(
        (by_id[i] for i in chosen if i in by_id and by_id[i].type_id == _MZB_TYPE_ID),
        None,
    )
    if mzb is None:
        return chosen

    lib = resolver.library
    dist = _distance_fn(mission_pos, building_pos)
    chosen_set = set(chosen)
    extra = list(chosen)
    for escort_class in ("diver_units", "gw_wasser"):  # GW-Taucher, GW-Wasserrettung
        wanted = set(lib.class_map.by_class.get(escort_class, ()))
        candidates = [v for v in pool if v.id not in chosen_set and v.type_id in wanted]
        if not candidates:
            continue
        # `is not None` guard: two vehicles with an UNKNOWN station both read
        # building_id=None, which would otherwise compare equal and wrongly
        # look like a same-station match.
        same_station = [
            v for v in candidates
            if mzb.building_id is not None and v.building_id == mzb.building_id
        ]
        pick = min(same_station or candidates, key=dist)
        extra.append(pick.id)
        chosen_set.add(pick.id)
    return extra


def _plan_one(mission, pool, resolver, policy, assigned, building_pos=None,
              text_engine=None, allow_partial=False, patient_only=False, shortfalls=None):
    """Return a ``(MissionPlan, chosen_ids)`` pair, or a skip-reason string.

    *allow_partial* (top-up/symbiont) sends whatever useful vehicles we have to
    an already-begun mission and reports the rest, instead of holding out for a
    full crew. *patient_only* derives only patient-clearing units. Both come from
    the mission's governing mode. *shortfalls* (a dict) tallies classes that leave
    a mission short for the dashboard.

    Besides vehicle-class requirements, a mission may state a **minimum
    firefighter count** (``average_min_fire_personnel``); that's met by fire
    vehicles whose combined crew reaches it (one HLF ≈ 9), counting crew already
    en route.

    Note: a class we simply don't have is only ever a shortfall here (skipped,
    tallied) — never a strike. Strikes are for vehicles that looked dispatchable
    but FAILED to send, and are recorded by the runner (see ``struck out``).
    """
    building_pos = building_pos or {}
    assigned = assigned or {}

    base = _derive_requirements(mission, resolver, text_engine, patient_only)
    # Personnel requirement (min firefighters) — not applicable in patient-only mode.
    mtype = None if patient_only else resolver.catalog.get(getattr(mission, "mission_type_id", None))
    min_fire = int(getattr(mtype, "min_fire_personnel", 0) or 0) if mtype is not None else 0

    if not base and min_fire <= 0:
        # No known requirement at all. In patient-only mode that just means "no
        # patients here" (incl. Hitzschlag end-patients) — a quiet skip.
        return "no patients on site" if patient_only else "no requirements found"

    # Credit gating already happened during candidate collection.
    requirements = _apply_delta(base, assigned, mission.id)
    # Firefighters already en route count toward the personnel minimum.
    fire_needed = max(0, min_fire - _fire_crew(resolver, assigned.get(mission.id, []))) if min_fire else 0

    if not requirements and fire_needed <= 0:
        return "already covered"

    if requirements:
        # Same revealed-vs-possible reconciliation _casualty_counts uses for
        # ambulances (resolver.py) -- NOT a plain max(), which would never
        # decay: possible_patients_count is a static per-mission ceiling, so
        # once units are on scene and patients_count authoritatively drops
        # (most already transported), the SEG-ELW preference must drop with
        # it rather than staying "preferred" for the mission's whole life.
        count_revealed = getattr(mission, "vehicle_state", 0) >= 1
        patient_load = _casualties(
            int(getattr(mission, "patients_count", 0) or 0),
            int(getattr(mission, "possible_patients_count", 0) or 0),
            getattr(mtype, "possible_patient", 0) if mtype is not None else 0,
            count_revealed,
        )
        preferred = (
            frozenset({_SEG_ELW_TYPE_ID})
            if patient_load >= _SEG_ELW_PREFERRED_PATIENT_THRESHOLD
            else frozenset()
        )
        coverage = select_coverage(
            requirements, pool, distance_of=_distance_fn(mission.position, building_pos),
            specialist_type_ids=_ELW_SPECIALIST_TYPE_IDS, preferred_type_ids=preferred,
        )
        chosen = list(coverage.vehicle_ids)
        satisfied = coverage.satisfied
        shortfall = dict(coverage.shortfall)
    else:
        chosen, satisfied, shortfall = [], True, {}

    # Personnel top-up: add fire vehicles until their crew reaches the minimum.
    if fire_needed > 0:
        extra, still_short = _fire_personnel_topup(
            chosen, pool, resolver, fire_needed, mission.position, building_pos
        )
        chosen = chosen + extra
        if still_short > 0:
            satisfied = False
            shortfall["firefighters"] = still_short

    # Only dispatch missions we can FULLY crew — a partial send just ties up
    # vehicles on a job that won't complete. The classes we're short of are
    # tallied (dashboard) but NOT struck: we merely don't have them right now.
    if not satisfied:
        _note_shortfall(shortfalls, shortfall.keys())
        if allow_partial:
            if not chosen:
                return "nothing useful to add"
            plan = MissionPlan(
                mission_id=mission.id, caption=getattr(mission, "caption", ""),
                vehicle_ids=tuple(chosen), satisfied=False, shortfall=dict(shortfall),
            )
            return plan, chosen
        needed = ", ".join(f"{cls} x{n}" for cls, n in shortfall.items())
        return f"not enough vehicles: short {needed}"

    if not chosen:
        return "already covered"

    if (
        policy.max_vehicles_per_mission is not None
        and len(chosen) > policy.max_vehicles_per_mission
    ):
        return f"needs {len(chosen)} > cap {policy.max_vehicles_per_mission}"

    # Opportunistic add-on, after every gating check above: never causes a
    # mission to be skipped or capped, and never affects `satisfied`/shortfall.
    chosen = _add_water_rescue_escort(chosen, pool, resolver, mission.position, building_pos)

    mission_plan = MissionPlan(
        mission_id=mission.id,
        caption=getattr(mission, "caption", ""),
        vehicle_ids=tuple(chosen),
        satisfied=True,
        shortfall={},
    )
    return mission_plan, chosen


def _apply_delta(requirements, assigned, mission_id):
    """Reduce requirement counts by vehicles already en route to this mission.

    A multi-role vehicle already assigned (e.g. an HLF) reduces *every*
    requirement it qualifies for — matching how one counts toward both an LF and
    an RW.
    """
    if not assigned:
        return list(requirements)
    types = assigned.get(mission_id, [])
    if not types:
        return list(requirements)

    reduced = []
    for requirement in requirements:
        covered = sum(1 for t in types if t in requirement.type_ids)
        new_count = max(0, requirement.count - covered)
        if new_count > 0:
            reduced.append(replace(requirement, count=new_count))
    return reduced


def _reserve_vehicle_group(reserved, mission, mtype, resolver, assigned, patient_governed):
    """Add *mission*'s outstanding vehicle-category type IDs to *reserved*
    (in place), if it qualifies (Sasha, 2026-09-11 — see the full rationale
    in :func:`plan_dispatches`). Shared by :func:`plan_dispatches`' main loop
    and :func:`_priority_pass` so the two reservation paths can't drift apart.

    Qualifies when: NOT patient-governed (vehicle categories aren't part of
    patient-only resolution), the mission type is known, and the mission
    already has some response (``vehicle_state >= 1`` — it's revealed/
    mentioned something, not just an untouched catalogue estimate).
    Unconditional beyond that: does NOT check whether the current pool looks
    numerically sufficient, by design (a vehicle can look available/
    dispatchable yet keep silently failing to actually crew/tow, so a
    same-cycle "looks satisfiable" check is exactly the kind of signal that
    failure mode fools — trust the mission's own revealed need instead).
    """
    if patient_governed or mtype is None or getattr(mission, "vehicle_state", 0) < 1:
        return
    vreqs, _unmatched = resolver.vehicle_requirements(mission, mtype)
    for req in _apply_delta(vreqs, assigned, mission.id):
        reserved.update(req.type_ids)
