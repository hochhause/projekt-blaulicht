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
from collections import Counter
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
    """Which missions to work — an independent tag a mission can carry.

    Every mission is exactly one of OWN/ALLIANCE (by ``is_alliance``); EVENT
    is a SEPARATE, ADDITIVE tag (Sasha, 2026-09-12) — the game marks a
    cooperative event mission "[Event]" in the caption regardless of who
    bought/set it up, so an event mission can ALSO be own or alliance-shared.
    ``DispatchPolicy.mission_source`` is therefore a SET of these (a
    multi-select, not a single choice) — a mission is worked if it matches
    ANY selected tag: its own/alliance tag, OR (if it's an event) EVENT."""

    OWN = "own"
    ALLIANCE = "alliance"
    EVENT = "event"


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


class HilferufAggressiveness(str, Enum):
    """How much Hilferuf shares to the alliance (Sasha, 2026-09-11)."""

    OFF = "off"
    UNCREWABLE = "uncrewable"  # only OWN missions we genuinely couldn't crew this cycle
    ALL = "all"                # every OWN, not-yet-shared mission, regardless of crew status


# Official Verbandsgroßeinsatz (VGE) mission-type ids (Sasha, 2026-10-07, from
# their alliance's own list of which mission types the Leitungsteam picks
# from when starting one). Originally always treated as parasite-only (one
# spare vehicle, never a real dispatch) because their alliance's rules (§4/
# §6) reserve command vehicles (ELW 1/2, AB-Einsatzleitung) on these for the
# Leitungsteam only, for the first 12h after release, and there was no way to
# tell whether THIS account is Leitungsteam or inside that window.
#
# D1 (Sasha, 2026-10-27): "adopt the new VGE logic" -- revised to graduate
# once missing_text is revealed (see _priority_pass): a revealed VGE mission
# is now treated like any other mission, full normal dispatch, no cap and no
# command-vehicle exclusion. Only an UNREVEALED VGE mission still gets the
# single-spare participation claim, and only when parasite_scope actually
# covers it. This deliberately drops the Leitungsteam reservation for a
# revealed mission -- recorded here, not silently dropped, so it's visible
# and revisable (see docs/dispatch-pipeline-rebuild.plan.md's D1).
#
# "Custom" (player-started) Großschadenslagen have no fixed type id at all
# ("because they are... custom") and so aren't caught by this list.
_VGE_MISSION_TYPE_IDS = frozenset({41, 42, 43, 44, 45, 159, 216, 250})


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


def _infer_own_user_id(missions) -> int | None:
    """Derive this account's own user id from the mission feed itself
    (Sasha, 2026-09-14), rather than guessing an unverified API endpoint's
    key name — a code-review finding on an earlier draft of this fix noted
    that ``/api/userinfo``'s shape was never independently captured, so a
    wrong key guess there could silently misclassify every genuinely-own
    mission, not just the narrow shared-own case this fix targets.

    Self-verifying instead: an UNSHARED mission (``is_alliance`` False, i.e.
    ``alliance_shared_at is None``) can only ever be one of OUR OWN — the
    same invariant the pre-fix ``is_alliance`` proxy already relied on
    correctly for the unshared case — so its ``user_id``, if present, IS
    this account's id. Returns ``None`` if no unshared mission with a known
    ``user_id`` is currently visible (self-healing: the caller re-tries next
    cycle once one appears — a brand-new own mission always starts unshared,
    so this resolves quickly in practice)."""
    for m in missions:
        if not getattr(m, "is_alliance", False):
            uid = getattr(m, "user_id", None)
            if uid is not None:
                return uid
    return None


def is_own_mission(mission, own_user_id: int | None) -> bool:
    """True if this account genuinely OWNS the mission, independent of
    whether it's since been shared to the alliance (Sasha, 2026-09-14).

    ``Mission.is_alliance`` (``alliance_shared_at is not None``) means "has
    this been shared," NOT "who owns it" — sharing your OWN mission (via
    Hilferuf, or manually) flips it True from the very next poll even
    though ownership never changed, which silently dropped it out of an
    own-only ``mission_source``/mode scope (live-reported bug: mission_source
    OWN + hilferuf on -> a mission Hilferuf itself just shared stopped being
    worked).

    Two independent signals, checked in order:
    1. The caption tag "[Verband]" (Sasha, 2026-09-14: "shared, not mine
       tend to have [Verband] in the name") — a genuinely foreign, merely
       alliance-visible mission carries this tag; OUR OWN missions never do,
       shared or not. A strong, immediate signal that needs no own_user_id
       at all, so it correctly classifies a foreign mission from cycle one
       -- same bracketed-tag convention already used for ``Mission.is_event``.
    2. The mission's ``user_id`` against the account's own id (see
       :func:`_infer_own_user_id`), when both are known — the deciding
       signal for OUR OWN missions once shared (which never carry
       "[Verband]", so signal 1 can't help distinguish shared-but-mine from
       shared-and-foreign the way it can for a foreign mission).

    Falls back to the original ``not is_alliance`` proxy if neither signal
    resolves — never worse than the pre-fix behavior, only more accurate as
    each signal becomes available.
    """
    if "[Verband]" in (getattr(mission, "caption", "") or ""):
        return False
    mission_user_id = getattr(mission, "user_id", None)
    if mission_user_id is not None and own_user_id is not None:
        return mission_user_id == own_user_id
    return not getattr(mission, "is_alliance", False)


def _confidently_alliance(mission, own_user_id: int | None) -> bool:
    """True only with POSITIVE evidence this mission is genuinely foreign —
    not just the absence of evidence it's ours. Reuses ``is_own_mission``'s
    own two signals (the "[Verband]" tag; a known, differing user_id) but,
    unlike it, returns False rather than falling back to the raw
    ``is_alliance``/``alliance_shared_at`` flag when neither resolves.

    That fallback is the right call for `is_own_mission` itself (never worse
    than pre-fix behavior) — but one caller (the VGE override, see
    ``_VGE_MISSION_TYPE_IDS``) treats "alliance" as a hard trigger with a
    real cost if it misfires on an own mission (under-dispatching your own
    big incident to a single spare vehicle). A narrow, self-healing window
    exists where it could: your own mission gets Hilferuf-shared before
    ``own_user_id`` has ever been inferred (needs some OTHER currently-
    unshared own mission in the feed first) — at that exact moment it has
    no "[Verband]" tag (never does, it's yours) and no resolvable user_id
    match, so `is_own_mission` would fall back to the raw (now-True)
    `is_alliance` flag and misclassify it. This helper keeps that case
    "not confidently alliance" instead, so the VGE override simply doesn't
    fire until real evidence arrives — safe either way, since briefly
    under-covering a genuinely foreign VGE for a cycle or two costs nothing
    comparable to briefly under-crewing your own mission would.
    """
    if "[Verband]" in (getattr(mission, "caption", "") or ""):
        return True
    mission_user_id = getattr(mission, "user_id", None)
    if mission_user_id is not None and own_user_id is not None:
        return mission_user_id != own_user_id
    return False


def _source_covers(source: "frozenset[MissionSource]", is_alliance: bool, is_event: bool) -> bool:
    """True if the normal-dispatch mission_source selection covers this
    mission — matches if its own/alliance tag is selected, OR (independently)
    if it's an event and EVENT is selected. An empty selection deliberately
    covers nothing (a valid "run no normal dispatch, only special modes"
    setting), not a silent fallback to "both"."""
    tag = MissionSource.ALLIANCE if is_alliance else MissionSource.OWN
    if tag in source:
        return True
    return is_event and MissionSource.EVENT in source


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
    # Which missions to work (a SET — see MissionSource's docstring), and an
    # optional separate radius for alliance ones.
    mission_source: frozenset[MissionSource] = field(
        default_factory=lambda: frozenset({MissionSource.OWN, MissionSource.ALLIANCE})
    )
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
    # PRIORITIZED ahead of the normal order/batch — but, like a fresh "normal"
    # send without partial_starts, only sent once the pool can fully satisfy
    # the outstanding requirement this cycle (Sasha, 2026-10-05: "TopUp ...
    # ONLY prioritizes a mission if we're certain we can feed it"). No fresh
    # missions. Also covers a mission we're already personally on (in
    # `assigned`) when this scope applies to it — but when it DOESN'T (this
    # scope OFF, or the mission's own/alliance source isn't covered), being
    # already on a mission grants it NO special treatment: it falls through
    # to the ordinary order/filter/batch/partial_starts handling like any
    # other mission (Sasha, 2026-10-05, after seeing an unsatisfiable
    # already-on mission trickle-fed every cycle ahead of cheaper ones
    # despite this scope being off: "...which would be correct if i had top
    # up active which i dont").
    top_up_scope: ModeScope = ModeScope.OFF
    # Patient-only: dispatch ONLY the units that clear patients (RTW/NEF/RTH + an
    # LNA >5 patients, OrgL >10); the runner recalls idle units once patients gone.
    # Unlike top_up_scope, a mission we're already on under THIS scope always
    # sends whatever's useful regardless of full satisfaction — a partial
    # medical response is still clearly worth sending, not something to
    # withhold pending a complete set.
    patient_scope: ModeScope = ModeScope.OFF
    # Hilferuf ("call for help"): share OWN missions to the alliance so
    # members can help. UNCREWABLE = only ones we couldn't crew this cycle;
    # ALL = every own mission. Runner-level add-on.
    hilferuf_mode: HilferufAggressiveness = HilferufAggressiveness.OFF
    # Never share a mission worth less than this many credits, REGARDLESS of
    # hilferuf_mode -- including ALL ("gebe alles frei"), which otherwise has
    # no floor at all (Sasha, 2026-10-07, quoting their alliance's own rule:
    # "Kleineinsätze sind nicht freizugeben" -- small jobs, needing only 1-2
    # vehicles, must never be released). 0 (default) = no floor. Checked
    # against the mission's own average_credits, falling back to its
    # mission type's average -- same source/fallback own_credit_min uses.
    hilferuf_min_credits: int = 0
    # Partial dispatch for FRESH ("normal") sends only: send whatever useful
    # vehicles we have to a mission we haven't touched yet, even if we can't
    # fully crew it, instead of waiting for a full set (Sasha, 2026-10-05:
    # "partial send is just for new missions where we know we can send some
    # but not all"). Does NOT affect top-up or patient-care (see those
    # scopes) — but DOES apply to a mission we're already on once it falls
    # through to "normal" governing (top_up_scope/patient_scope don't cover
    # it): same rule as a fresh mission, since by then that's exactly what
    # it's being treated as.
    partial_starts: bool = False
    # Never let PARASITE mode spend a vehicle type we own this few or fewer of,
    # fleet-wide (Sasha, 2026-10-12: "leave parasite to the not-scarce
    # vehicles" — simpler than teaching the priority pass to rank scarce
    # contributions across competing missions). A parasite leech contribution
    # is a "nice to have," not a real requirement -- it has no business
    # spending one of our 2-3 GKW/MzGW/Anh TeSi on a VGE-scale mission we can
    # never fully crew anyway, when that same unit might finish one of OUR OWN
    # actually-completable missions via the ordinary reservation/unstick-first
    # path instead. Based on TOTAL OWNED count (not momentary pool
    # availability), so it doesn't flicker as vehicles come and go from other
    # missions. Default 8 (Sasha, 2026-10-12: most vehicle types need well
    # more than 2 of a kind to fill a mission outright, so a low threshold
    # would barely rule anything out -- "5-10 sounds more reasonable"). 0 =
    # off. Does NOT affect top-up/patient/normal dispatch -- those already
    # can't waste a scarce vehicle on an unsatisfiable mission (top-up/normal-
    # without-partial_starts require full satisfaction first; see their own
    # docstrings).
    parasite_scarce_threshold: int = 8


@dataclass(frozen=True)
class MissionPlan:
    """What to send to one mission this cycle."""

    mission_id: int
    caption: str
    vehicle_ids: tuple[int, ...]
    satisfied: bool
    shortfall: dict[str, int]
    # Whether this send should count as a real dispatch "alarm" toward
    # AlarmTracker (Sasha, 2026-10-27: "a parasite send or a S5 processing
    # doesn't count only sending and reinforcements and such"). True for
    # every ordinary initial/reinforcement/patient send; explicitly False
    # for a parasite's contribution and the VGE-unrevealed one-time
    # participation spare (see ``_priority_pass``) — both are a free token
    # claim, not a real attempt to crew the mission.
    counts_as_alarm: bool = True


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


def en_route_from_vehicles(vehicles) -> dict[int, list[int]]:
    """Map mission id → vehicle type IDs currently EN ROUTE to it only (S3,
    ``fms == 3``) — excludes vehicles already on scene (S4).

    Needed because a mission's revealed ``missing_text`` is already net of
    on-scene (S4) vehicles (see ``Requirement.delta_scope``'s docstring and
    docs/live-data-model.md) — subtracting an S4 vehicle from it a second
    time via the combined :func:`assigned_from_vehicles` dict would double-
    count it. ``_apply_delta`` picks between this and the combined dict per
    requirement, by its ``delta_scope``."""
    en_route: dict[int, list[int]] = {}
    for vehicle in vehicles:
        if getattr(vehicle, "target_type", None) != "mission":
            continue
        if getattr(vehicle, "fms", None) != 3:
            continue
        mission_id = getattr(vehicle, "target_id", None)
        type_id = getattr(vehicle, "type_id", None)
        if mission_id is not None and type_id is not None:
            en_route.setdefault(int(mission_id), []).append(int(type_id))
    return en_route


def plan_dispatches(
    missions,
    vehicles,
    buildings,
    resolver: MissionRequirementResolver,
    policy: DispatchPolicy,
    assigned: dict[int, list[int]] | None = None,
    en_route: dict[int, list[int]] | None = None,
    text_engine=None,
    now: float | None = None,
    strikes=None,
    own_user_id: int | None = None,
) -> DispatchPlan:
    """Plan this cycle's dispatches.

    *en_route* maps mission id → type IDs of vehicles currently EN ROUTE only
    (S3, ``fms == 3``) — defaults to deriving it from *vehicles* itself
    (:func:`en_route_from_vehicles`) when not given, so a real caller (the
    runner) never needs to compute it separately; a test may still inject it
    directly. Needed alongside *assigned* (S3+S4 combined) because a
    requirement derived from a revealed ``missing_text`` is already net of
    on-scene (S4) vehicles — see ``Requirement.delta_scope``'s docstring.

    *assigned* maps mission id → type IDs of vehicles already en route (the
    delta). *text_engine* (a RequirementEngine) parses a mission's live
    ``missing_text`` when the structured catalogue has no requirements. *now*
    (epoch seconds) drives the alliance age-since-shared window. *own_user_id*
    (see :func:`is_own_mission`) tells a genuinely-own mission apart from one
    merely visible via alliance sharing, even after it's been shared.

    Unstick-first ordering (Sasha, 2026-10-07): among every candidate this
    cycle gathers (after the SAME filters as always — radius/scope/category/
    credits — this only reorders the already-eligible set, it doesn't widen
    it), any mission that already has vehicles ARRIVED gets ranked by how
    close it is to complete (``still_missing / arrived``, ascending — a
    30-vehicle mission waiting on 1 more outranks a fresh 2-vehicle one,
    since finishing it unsticks the other 29 already stuck there) and
    dispatched BEFORE any genuinely fresh mission, in that order, each only
    if the pool can fully satisfy it right now (never a partial trickle —
    ``partial_starts`` stays scoped to fresh sends only, same as today) and
    exempt from ``batch_size`` (finishing an unstick is never deferred).
    "Arrived" is derived, not fetched: ``mtype.requirements`` (what the
    mission type originally asks for) minus the current raw, non-delta
    ``vehicle_requirements()`` count (what's still missing AT THE SCENE,
    live-confirmed to only decrement once a vehicle has actually arrived,
    never for one merely en route) — so it's correct for everyone's arrived
    vehicles, not just ours, without needing to see anyone's en-route state.
    A mission with no usable catalogue total (unknown type, or governed as
    patient-only, which has no such fixed total) simply can't be ranked this
    way and falls through to the normal fresh-candidate path unchanged. Same
    for one that has never shown a responder on scene (sticky, via
    ``resolver.effective_count_revealed`` — nothing could have arrived yet)
    or whose ``vehicle_requirements()`` reports any unmatched catalogue class
    (code review, 2026-10-08: that silently drops the unmapped class from
    the "still missing" side only, so the subtraction would compare two
    different class sets and could fabricate an "arrived" count for a
    mission that hasn't actually started).
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

    # Total OWNED count per vehicle type (the whole fleet, not just the
    # currently-idle pool) -- parasite_scarce_threshold is judged against this
    # stable figure, not momentary availability (see DispatchPolicy's docstring).
    type_counts = Counter(v.type_id for v in vehicles)

    # En-route (S3) only — defaults to deriving it from `vehicles` (see this
    # function's own docstring); needed alongside the combined `assigned`
    # (S3+S4) param so `_apply_delta` can pick the right one per
    # requirement's `delta_scope` (Sasha, 2026-10-27).
    if en_route is None:
        en_route = en_route_from_vehicles(vehicles)

    # PRIORITY PASS — spread/top-up (missions we're on) and, in parasite mode,
    # leech targets. Runs first, bypassing filters/order/batch.
    handled, priority_batch, reserved_type_ids = _priority_pass(
        plan, missions, pool, resolver, policy, assigned or {}, en_route, building_pos, text_engine,
        own_user_id, type_counts=type_counts,
    )

    # Gather in-range, category-allowed missions with their distance, the mission
    # type's average credits (for ordering) and the governing mode.
    candidates: list[tuple[float, int, object, str, float]] = []  # (distance, credits, mission, mode, efficiency)
    fillable: list[tuple[float, object, str]] = []  # (ratio, mission, governing) -- see "unstick-first" above
    for mission in missions:
        if mission.id in handled:
            continue  # already actioned in the priority pass
        is_alliance = not is_own_mission(mission, own_user_id)
        # Resolve which treatment this mission gets from the per-mode scopes.
        # Precedence when several cover it: patient > top-up > normal.
        if _scope_covers(policy.patient_scope, is_alliance):
            governing = "patient"
        elif _scope_covers(policy.top_up_scope, is_alliance):
            governing = "topup"
        elif _source_covers(policy.mission_source, is_alliance, getattr(mission, "is_event", False)):
            governing = "normal"
        else:
            plan.skipped.append((mission.id, "out of scope"))
            continue
        # D2 (Sasha, 2026-10-27): top-up only reinforces missions whose
        # missing_text has revealed a real need -- the live reveal itself,
        # not effective_count_revealed's sticky vehicle_state signal (used
        # until this change). Reasoning (Sasha): this automatically covers a
        # mission TYPE our own catalogue doesn't recognise yet, since a
        # missing_text reveal doesn't depend on catalogue completeness the
        # way vehicle_state-based eligibility implicitly did.
        # missing_text_ever_revealed is itself sticky (mirrors
        # effective_count_revealed's own "once it's gone we have everything"
        # protection against a transient live flicker -- see its docstring).
        if governing == "topup" and not resolver.missing_text_ever_revealed(mission):
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
        _reserve_vehicle_group(reserved_type_ids, mission, mtype, resolver, en_route, assigned,
                                patient_governed=(governing == "patient"))
        # Unstick-first: does this mission already have vehicles ARRIVED (see
        # plan_dispatches' own docstring for the "total minus raw-still-
        # missing" derivation)? If so, it skips the normal order/batch path
        # entirely -- it's ranked and dispatched in its own pass below,
        # before any fresh candidate. Not meaningful for patient governing
        # (no fixed vehicle-class total to measure against).
        #
        # Two guards, both load-bearing (code review, 2026-10-08): (1) the
        # mission has EVER shown a responder on scene -- mirrors
        # _reserve_vehicle_group's own precondition just above, and is true
        # for every genuinely "arrived" mission anyway; without it, a
        # completely untouched mission can still trip the arithmetic below
        # into a false "arrived" count. Sticky (resolver.
        # effective_count_revealed), not the raw live vehicle_state -- that
        # field reverts to 0 the instant nobody is CURRENTLY on scene
        # (confirmed live 2026-10-01), which would otherwise make a mission
        # whose responders finished a shift and left look untouched again,
        # losing its unstick-first priority (Sasha, 2026-10-22). (2) only if
        # `vehicle_requirements` reports NO unmatched class -- it silently
        # DROPS any catalogue class it can't map to a vehicle type (a known,
        # tested, documented data-gap behavior, not a bug in it), so
        # `still_missing_raw` can legitimately enumerate a different, smaller
        # class set than `total_wanted` (which sums literally every declared
        # class, mapped or not). Either gap alone makes the subtraction
        # meaningless, not just imprecise -- skip fillable classification
        # entirely rather than risk a mission that hasn't actually started
        # jumping the queue and bypassing batch_size on a fabricated
        # "almost done" reading.
        if (governing != "patient" and mtype is not None and mtype.requirements
                and resolver.effective_count_revealed(mission)):
            total_wanted = sum(int(n) for n in mtype.requirements.values())
            if total_wanted > 0:
                raw_reqs, raw_unmatched = resolver.vehicle_requirements(mission, mtype)
                if not raw_unmatched:
                    still_missing_raw = sum(r.count for r in raw_reqs)
                    arrived = total_wanted - still_missing_raw
                    if arrived > 0:
                        ratio = still_missing_raw / arrived
                        fillable.append((ratio, mission, governing))
                        continue
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
            outstanding = _apply_delta(base, en_route, assigned, mission.id)
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

    # Unstick pass: every fillable candidate, closest-to-complete first,
    # dispatched (fully, never partial) BEFORE any fresh candidate below and
    # exempt from batch_size -- see plan_dispatches' own docstring.
    fillable.sort(key=lambda t: t[0])
    for _ratio, mission, governing in fillable:
        outcome = _plan_one(mission, pool, resolver, policy, en_route, assigned, building_pos,
                            text_engine, allow_partial=False,
                            patient_only=False, shortfalls=plan.shortfalls)
        if isinstance(outcome, str):
            plan.skipped.append((mission.id, outcome))
            continue
        mission_plan, chosen = outcome
        plan.planned.append(mission_plan)
        chosen_set = set(chosen)
        pool = [v for v in pool if v.id not in chosen_set]

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
        # partial_starts is scoped to fresh ("normal") sends only (Sasha,
        # 2026-10-05): "partial send is just for new missions where we know we
        # can send some but not all." Top-up's job is to PRIORITIZE an
        # already-begun mission ahead of the normal order/batch, not to relax
        # the full-crew requirement — "TopUp ... ONLY prioritizes a mission if
        # we're certain we can feed it." So topup governing gets no automatic
        # allow_partial; it only sends once the pool can fully satisfy it,
        # same as normal without partial_starts. A mission already on_it
        # (in `assigned`) with no patient/top-up coverage lands HERE too, as
        # plain "normal" governing — it's no longer special-cased by the
        # priority pass once neither scope applies (Sasha, 2026-10-05, after
        # an uncovered on_it mission kept trickle-feeding ahead of cheaper
        # ones: "...which would be correct if i had top up active which i
        # dont"); only a mission patient_scope or top_up_scope actually
        # covers stays in the priority pass, unaffected by this setting.
        allow_partial = governing == "normal" and policy.partial_starts
        # A genuinely FRESH send (nobody there yet) can't use a reserved class —
        # that class is needed to close out a mission that's already open
        # elsewhere. Anything ever shown active (help/reinforcement) is
        # exempt: it's exactly who the reservation exists for. Sticky check
        # (resolver.effective_count_revealed), same reason as the topup gate
        # above -- raw vehicle_state reverts to 0 once nobody's CURRENTLY on
        # scene, which would otherwise make an already-open mission whose
        # responders stepped away look fresh again. topup missions already
        # cleared this same sticky check above, so they're naturally exempt
        # without a separate check.
        is_fresh_send = governing == "normal" and not resolver.effective_count_revealed(mission)
        call_pool = (
            [v for v in pool if v.type_id not in reserved_type_ids]
            if is_fresh_send and reserved_type_ids else pool
        )
        outcome = _plan_one(mission, call_pool, resolver, policy, en_route, assigned, building_pos,
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
        # group.py's plan_group_dispatches ALSO builds 5-tuples for SMART now
        # (Sasha, 2026-09-11 — see _smart_efficiency in app/domain/group.py),
        # so the exact-5 check below is every candidate list's normal shape
        # under SMART, not a no-op degrade; a non-SMART order still passes
        # plain 3-tuples from either module, which fall back to raw credits.
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
    """Post-delta requirements still to send (catalogue/revealed minus en-route).

    Kept at its original single-``assigned`` shape deliberately: its only
    remaining caller once ``_priority_pass`` routes through ``_plan_one``
    (2026-10-27 rework) is ``app/domain/group.py``'s SMART-efficiency
    estimate, which is OUT OF SCOPE for that rework (see
    docs/dispatch-pipeline-rebuild.plan.md) and deliberately keeps today's
    un-split (S3+S4-always) behaviour, unchanged. In-scope callers use
    ``_apply_delta`` directly with the real en-route/assigned split.
    """
    return _apply_delta(
        _derive_requirements(mission, resolver, text_engine, patient_only), assigned, assigned, mission.id
    )


# Abrollbehälter/Anhänger (towed) types with a genuinely self-propelled
# alternative satisfying the SAME requirement category somewhere in our data
# (confirmed against LSSM's own vehiclesByRequirement table, 2026-10-08, not
# guessed — each one listed below co-occurs with at least one non-towed type
# id in some category entry). These get a distance PENALTY in vehicle
# selection so an equally-eligible self-propelled vehicle is preferred
# unless the towed one is meaningfully closer (Sasha, 2026-10-08): whichever
# vehicle tows one of these (a WLF or similar) is locked to it for the whole
# job, so spending that tow commitment only makes sense when there's truly
# no closer self-driving option. AB/Anh types with NO self-propelled sibling
# anywhere (e.g. AB-Tank, Anh TeSi, Anh Plattform) are deliberately excluded
# — there's nothing to prefer instead, so penalizing them would only delay a
# dispatch that always needed the tow anyway.
_TOWED_WITH_SELF_PROPELLED_ALTERNATIVE = frozenset({
    47,        # AB-Rüst            vs. the whole LF/HLF/RW family
    48,        # AB-Atemschutz      vs. GW-Atemschutz (5)
    49,        # AB-Öl              vs. GW-Öl (10)
    54,        # AB-Dekon-P         vs. Dekon-P (53)
    62,        # AB-Schlauch        vs. the GW-L2-Wasser/SW family
    66, 67, 68, 71,  # Anh MzB / Anh SchlB / Anh MzAB / AB-MZB vs. the self-propelled Boot (70)
    77,        # AB-Gefahrgut       vs. GW-Gefahrgut (27)
    # AB-Einsatzleitung vs. plain ELW 1/2 (3, 34, 128). 129 (ELW2-Drohne) is
    # also a sibling per the data, but the penalty is structurally inert
    # against it specifically: select_coverage's specialist tier (see
    # _ELW_SPECIALIST_TYPE_IDS) is compared before distance, so AB-
    # Einsatzleitung already wins over an ELW2-Drohne regardless of distance
    # -- noted here only so a future reader doesn't expect the 7km figure to
    # be what's deciding that particular matchup (code review, 2026-10-08).
    78,
    101, 102,  # Anh SwPu / Anh 7   vs. the GW-L2-Wasser/SW family
    115, 116,  # Anh Lüfter / AB-Lüfter vs. GW-Lüfter (114)
    119,       # AB-Lösch           vs. the whole LF/TLF family
    143,       # Anh Schlauch       vs. the GW-L2-Wasser/SW family
    155,       # Anh Höhenrettung (Bergrettung) vs. GW-Höhenrettung (33, 158)
    164,       # AB-Schiene         vs. HLF Schiene / Bahnrettungsfahrzeug (162, 163)
    178,       # Anh 12 Lbw (FGr Log-V) vs. the Bt-Kombi/GW-Bt family
    179,       # AB-NEA50           vs. the self-propelled NEA50 carriers
    180,       # AB-NEA200          vs. the self-propelled NEA200 carriers
    191, 192,  # Anh Waldbrand / AB-Waldbrand vs. the self-propelled GW-Waldbrand
               # (190) -- all three share the one "wildfire_equipment" class
               # (2026-10-12 wildfire update), same co-occurrence test as every
               # other entry here.
})
_TOWED_DISTANCE_PENALTY_KM = 7.0


def _distance_fn(mission_pos, building_pos):
    def distance_of(vehicle) -> float:
        vp = getattr(vehicle, "position", None) or building_pos.get(getattr(vehicle, "building_id", None))
        km = haversine_km(mission_pos, vp) if (vp and mission_pos) else float("inf")
        if km != float("inf") and getattr(vehicle, "type_id", None) in _TOWED_WITH_SELF_PROPELLED_ALTERNATIVE:
            km += _TOWED_DISTANCE_PENALTY_KM
        return km
    return distance_of


def _abundance_vehicle(pool, mission_pos, building_pos):
    """The nearest instance of whatever vehicle type we hold most of — a spare
    to throw at a leech mission just to claim participation."""
    if not pool:
        return None
    common_type = Counter(v.type_id for v in pool).most_common(1)[0][0]
    candidates = [v for v in pool if v.type_id == common_type]
    return min(candidates, key=_distance_fn(mission_pos, building_pos))


def _priority_pass(plan, missions, pool, resolver, policy, assigned, en_route, building_pos,
                    text_engine, own_user_id=None, type_counts=None):
    """Handle patient-care/top-up reinforcement, parasite missions, and
    official Verbandsgroßeinsatz missions BEFORE the normal filtered pass.

    *type_counts* (type_id -> total OWNED count, the whole fleet) backs
    ``policy.parasite_scarce_threshold`` (Sasha, 2026-10-12): a vehicle type
    we own this few or fewer of is withheld from EVERY parasite contribution
    (both the useful-coverage pick and the plain claim-a-spare fallback) —
    see that field's own docstring for why. Scoped to ``is_parasite`` only;
    a genuine top-up/patient reinforcement for a mission we're already on
    (``on_it_here``) still draws from the full pool, since it already can't
    waste a scarce vehicle on an unsatisfiable mission (full-satisfaction
    gated) the way an unconditional parasite send can.

    An alliance-shared mission whose type id is in :data:`_VGE_MISSION_TYPE_IDS`
    is always forced into a single, one-time spare-vehicle participation claim
    — never a real dispatch — regardless of `policy.parasite_scope` or any
    other mode; see that constant for the compliance rationale.

    Bypasses cash/source/category filters, ordering and batch. Returns the set of
    handled mission ids, how many count toward the batch (useful contributions
    do; pure leech sends do not), and the vehicle TYPE IDS reserved because a
    mission evaluated here already has some response (vehicle_state >= 1) and
    still mentions them as needed — see ``reserved_type_ids`` in
    :func:`plan_dispatches` for the full rationale (unconditional, not gated
    on whether this pass's own attempt happens to look sufficient).

    A mission we're already on (``on_it``) only stays in THIS pass — ahead of
    order/filters/batch — when patient_scope or top_up_scope actually covers
    it; otherwise it's just a normal mission from here on, falling through to
    the main loop's ordinary governing/order/filter/batch/partial_starts
    handling like anything else (Sasha, 2026-10-05: reinforcement jumping the
    queue, or sending a partial batch, should only happen when top_up_scope
    or partial_starts is actually what's responsible for it — not as a
    separate always-on behavior merely because we already have a vehicle
    there). Patient care (RTW/NEF response) still always sends whatever's
    useful regardless of full satisfaction — unlike a generic vehicle-class
    top-up, a partial medical response is still clearly worth sending, not
    something to withhold pending a complete set.
    """
    handled: set[int] = set()
    batch_used = 0
    reserved: set[int] = set()
    assigned = assigned or {}
    threshold = policy.parasite_scarce_threshold
    scarce_type_ids = (
        frozenset(tid for tid, cnt in (type_counts or {}).items() if cnt <= threshold)
        if threshold > 0 else frozenset()
    )
    for mission in missions:
        mid = mission.id
        on_it = mid in assigned                       # a mission we're already on
        is_alliance = not is_own_mission(mission, own_user_id)
        # Deliberately the stricter _confidently_alliance, not the plain
        # is_alliance above -- see its own docstring for why misfiring this
        # specific override on an own mission has a real cost the general
        # is_alliance proxy's fallback isn't careful enough to avoid.
        if (_confidently_alliance(mission, own_user_id)
                and getattr(mission, "mission_type_id", None) in _VGE_MISSION_TYPE_IDS
                and not parse_missing_text(getattr(mission, "missing_text", "")).revealed):
            # D1 (Sasha, 2026-10-27): "adopt the new VGE logic" -- while
            # missing_text is UNREVEALED, send a single, free, one-time
            # participation spare -- but ONLY if parasite_scope actually
            # covers this mission (previously unconditional, regardless of
            # any mode). Skips every other branch below, including on_it's
            # usual "always keep reinforcing" exemption. Once missing_text
            # IS revealed, this branch is no longer entered at all -- the
            # mission falls straight through to on_it/parasite/topup/patient
            # handling below, then the main gather loop, exactly like any
            # other mission (full normal dispatch, no one-vehicle cap). This
            # deliberately REMOVES the previous hard compliance override that
            # reserved command vehicles (ELW 1/2, AB-Einsatzleitung) for the
            # alliance's Leitungsteam -- see docs/dispatch-pipeline-rebuild.
            # plan.md's D1 for the full rationale and the explicit ruling.
            if not on_it and _scope_covers(policy.parasite_scope, True):
                pos = getattr(mission, "position", None)
                spare = _abundance_vehicle(pool, pos, building_pos) if pos is not None else None
                if spare is not None:
                    plan.planned.append(MissionPlan(mid, getattr(mission, "caption", ""),
                                                    (spare.id,), False, {},
                                                    counts_as_alarm=False))  # pure leech, free
                    pool[:] = [v for v in pool if v.id != spare.id]
            handled.add(mid)
            continue
        mtype = resolver.catalog.get(getattr(mission, "mission_type_id", None))
        # Sticky (resolver.effective_count_revealed), not the raw live
        # vehicle_state -- see plan_dispatches' topup-gate comment for why:
        # a genuinely active mission whose responders finished a shift and
        # left would otherwise read as "nobody on it" and lose parasite/
        # top-up eligibility (Sasha, 2026-10-22).
        others_on = resolver.effective_count_revealed(mission)
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
        patient_here = _scope_covers(policy.patient_scope, is_alliance)
        top_up_here = _scope_covers(policy.top_up_scope, is_alliance)
        if on_it or is_parasite:
            # Unconditional for EVERY on_it/is_parasite mission here, even one
            # that's about to fall through below with no patient/top-up
            # coverage: the main loop's OWN reservation call only runs for a
            # mission that also clears mission_source/category/credit/radius/
            # age there, so without this, an on_it mission that gets filtered
            # out at any of those (e.g. it went alliance-shared after we got
            # on it, or mission_source no longer covers its source) would
            # lose reservation entirely -- exposing exactly the fresh-send
            # poaching `_reserve_vehicle_group` exists to prevent.
            _reserve_vehicle_group(reserved, mission, mtype, resolver, en_route, assigned,
                                    patient_governed=patient_here)
        on_it_here = on_it and (patient_here or top_up_here)
        if not (on_it_here or is_parasite):
            continue

        pos = getattr(mission, "position", None)
        # Parasite on a patient-governed mission we're not on (leeching, not
        # topping up): never compute a "useful" patient-only contribution
        # here — go straight to the plain participation-claiming spare send
        # below. Decision (Sasha, 2026-08-31): patient_scope's own governance
        # (the on_it_here branch above, for missions we're actually assigned
        # to) is what handles real medical need; a parasite leech isn't
        # medically aware and shouldn't pretend a random "useful" pick is
        # patient care. Once this claim send lands, the mission becomes
        # `on_it` from NEXT cycle onward, so real medical coverage follows
        # with a one-cycle delay, not immediately this same cycle.
        skip_patient_contribution = is_parasite and patient_here
        # A parasite send never spends a scarce type (see parasite_scarce_
        # threshold's docstring) -- on_it_here reinforcement draws from the
        # full pool as usual.
        contribution_pool = (
            [v for v in pool if v.type_id not in scarce_type_ids]
            if is_parasite and scarce_type_ids else pool
        )
        chosen: list[int] = []
        if not skip_patient_contribution and pos is not None:
            # D7 (Sasha, 2026-10-27 — docs/dispatch-pipeline-rebuild.plan.md):
            # routed through _plan_one instead of this pass's own inline
            # select_coverage call, so a parasite/top-up/patient reinforcement
            # now gets every rule _plan_one carries — Rule 1's red-bypass, the
            # SEG-ELW preference, the fire-personnel top-up,
            # max_vehicles_per_mission, and the opportunistic water-rescue
            # escort — instead of a second, independently-maintained coverage
            # computation that had silently drifted from _plan_one's over
            # time (see the dispatch-pipeline-map artifact's "Asymmetries").
            #
            # allow_partial mirrors the old `allow_send` rule exactly:
            # parasite and patient-care contributions send whatever's useful
            # regardless of full satisfaction; a plain top-up reinforcement
            # (on_it_here via top_up_scope only) still requires full
            # satisfaction, same as the main loop's topup governing.
            allow_partial = is_parasite or patient_here
            outcome = _plan_one(
                mission, contribution_pool, resolver, policy, en_route, assigned, building_pos,
                text_engine, allow_partial=allow_partial, patient_only=patient_here,
                # A plain top-up miss (allow_partial=False) falls through to
                # the main loop for a second attempt below (unstick-first
                # eligible there) -- don't double-tally its shortfall when
                # that second attempt notes it too.
                shortfalls=(plan.shortfalls if allow_partial else None),
            )
            if not isinstance(outcome, str):
                mission_plan, chosen = outcome
                if is_parasite:
                    # A parasite's "useful" contribution still isn't OUR
                    # crewing attempt (see MissionPlan.counts_as_alarm) —
                    # only on_it_here's own top-up/patient reinforcement does.
                    mission_plan = replace(mission_plan, counts_as_alarm=False)
                plan.planned.append(mission_plan)
                batch_used += 1  # useful contribution -> counts to batch
        if not chosen and is_parasite:
            spare = _abundance_vehicle(contribution_pool, pos, building_pos)
            if spare is not None:
                chosen = [spare.id]
                plan.planned.append(MissionPlan(mid, getattr(mission, "caption", ""),
                                                (spare.id,), False, {},
                                                counts_as_alarm=False))  # pure leech, free
        if chosen:
            cset = set(chosen)
            pool[:] = [v for v in pool if v.id not in cset]
            handled.add(mid)
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


def _plan_one(mission, pool, resolver, policy, en_route, assigned, building_pos=None,
              text_engine=None, allow_partial=False, patient_only=False, shortfalls=None):
    """Return a ``(MissionPlan, chosen_ids)`` pair, or a skip-reason string.

    *allow_partial* (fresh "normal" sends only, with ``partial_starts`` on)
    sends whatever useful vehicles we have and reports the rest, instead of
    holding out for a full crew — top-up never sets this (it requires full
    coverage, just prioritized ahead of the normal order/batch; see
    ``plan_dispatches``). *patient_only* derives only patient-clearing units.
    Both come from the mission's governing mode. *shortfalls* (a dict) tallies
    classes that leave a mission short for the dashboard.

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
    en_route = en_route or {}

    base = _derive_requirements(mission, resolver, text_engine, patient_only)
    # Personnel requirement (min firefighters) — not applicable in patient-only mode.
    mtype = None if patient_only else resolver.catalog.get(getattr(mission, "mission_type_id", None))
    min_fire = int(getattr(mtype, "min_fire_personnel", 0) or 0) if mtype is not None else 0

    if not base and min_fire <= 0:
        # No known requirement at all. In patient-only mode that just means "no
        # patients here" (incl. Hitzschlag end-patients) — a quiet skip.
        return "no patients on site" if patient_only else "no requirements found"

    # Credit gating already happened during candidate collection.
    #
    # Rule (Sasha, 2026-10-26, hotfix): a mission still showing RED has had
    # NOTHING arrive, no matter what our own "assigned" (en-route) bookkeeping
    # claims -- live-observed missions stuck red and marked "already covered"
    # for a long time, because something we believed was already en route
    # delta-subtracted the requirement to zero without ever actually showing
    # up. Trust the mission's own currently-revealed need over bookkeeping
    # that isn't producing visible progress: skip the delta entirely while
    # red, so it's always evaluated against the FULL current requirement
    # instead of being silently skipped as "already covered". Once the
    # mission goes yellow/green (something has genuinely shown up), normal
    # delta-adjustment resumes. A known, accepted tradeoff of this hotfix:
    # a genuinely-en-route (but not yet arrived) vehicle no longer reduces
    # what gets dispatched to a still-red mission, so a slow arrival can
    # cause a harmless extra send rather than a silent stall -- deliberately
    # preferred over the stall for now; the underlying assigned-tracking
    # gets revisited properly in a follow-up pass, not as part of this fix.
    #
    # 2026-10-27: this layers on TOP of the separate S3/S4 delta-scoping fix
    # below (`_apply_delta`'s `en_route`/`assigned` split) — that fix removes
    # the specific double-counting bug that caused most of the live-observed
    # "red but already covered" cases; this rule remains as a safety net for
    # the DIFFERENT failure mode (genuinely stale/wrong `assigned` entries,
    # not a scoping bug) and needs no change itself.
    if getattr(mission, "status", None) == "red":
        requirements = list(base)
        fire_needed = min_fire
    else:
        requirements = _apply_delta(base, en_route, assigned, mission.id)
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
        # Uses the resolver's sticky memory (see
        # MissionRequirementResolver.effective_count_revealed), not the raw
        # live vehicle_state, so this doesn't re-prefer SEG-ELW the moment the
        # last vehicle leaves an already-resolved mission.
        count_revealed = resolver.effective_count_revealed(mission)
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


def _apply_delta(requirements, en_route, assigned, mission_id):
    """Reduce requirement counts by vehicles already accounted for at this
    mission — scoped PER REQUIREMENT by its ``delta_scope`` (Sasha,
    2026-10-27; see ``Requirement.delta_scope``'s own docstring and
    docs/live-data-model.md's "Noch benötigt = fehlend − Auf Anfahrt"):

    * ``"en_route_only"`` — a requirement derived from a revealed
      ``missing_text``, already net of on-scene (S4) vehicles — subtract only
      *en_route* (S3), never *assigned* (which would double-count S4).
    * ``"full"`` (default) — the catalogue fallback, or the casualty-driven
      layer (neither of which is netted against anything) — subtract
      *assigned* (S3+S4 combined, i.e. :func:`assigned_from_vehicles`'s
      output), same as before this split existed.

    A multi-role vehicle already counted (e.g. an HLF) reduces *every*
    requirement it qualifies for — matching how one counts toward both an LF
    and an RW.
    """
    if not requirements:
        return []
    en_route_types = (en_route or {}).get(mission_id, [])
    assigned_types = (assigned or {}).get(mission_id, [])

    reduced = []
    for requirement in requirements:
        types = en_route_types if requirement.delta_scope == "en_route_only" else assigned_types
        covered = sum(1 for t in types if t in requirement.type_ids)
        new_count = max(0, requirement.count - covered)
        if new_count > 0:
            reduced.append(replace(requirement, count=new_count))
    return reduced


def _reserve_vehicle_group(reserved, mission, mtype, resolver, en_route, assigned, patient_governed):
    """Add *mission*'s outstanding vehicle-category type IDs to *reserved*
    (in place), if it qualifies (Sasha, 2026-09-11 — see the full rationale
    in :func:`plan_dispatches`). Shared by :func:`plan_dispatches`' main loop
    and :func:`_priority_pass` so the two reservation paths can't drift apart.

    *en_route* (S3-only) vs *assigned* (S3+S4): passed straight to
    :func:`_apply_delta`, which picks per requirement by ``delta_scope`` —
    see that function's docstring (Sasha, 2026-10-27). ``app/domain/group.py``'s
    own call site passes the same merged dict for both (out of scope for that
    fix; see docs/dispatch-pipeline-rebuild.plan.md).

    Qualifies when: NOT patient-governed (vehicle categories aren't part of
    patient-only resolution), the mission type is known, and the mission has
    EVER shown some response (sticky, via ``resolver.effective_count_revealed``
    — not the raw live ``vehicle_state``, which reverts to 0 the instant
    nobody is CURRENTLY on scene, even on a mission that's genuinely already
    open and just had its responders finish a shift and step away; see that
    method's own docstring). Unconditional beyond that: does NOT check
    whether the current pool looks numerically sufficient, by design (a
    vehicle can look available/dispatchable yet keep silently failing to
    actually crew/tow, so a same-cycle "looks satisfiable" check is exactly
    the kind of signal that failure mode fools — trust the mission's own
    revealed need instead).
    """
    if patient_governed or mtype is None or not resolver.effective_count_revealed(mission):
        return
    vreqs, _unmatched = resolver.vehicle_requirements(mission, mtype)
    for req in _apply_delta(vreqs, en_route, assigned, mission.id):
        reserved.update(req.type_ids)
