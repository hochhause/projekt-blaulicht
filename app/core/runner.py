"""The per-account dispatch runner — one cycle of poll → plan → dispatch.

This is the I/O shell around the pure planner in
:mod:`app.domain.dispatch`. It is intentionally decoupled from the concrete
game client (and therefore from ``httpx``): it talks to any object satisfying
:class:`GameClientLike`, so a cycle can be unit-tested with a fake client and no
network. The infinite loop, jitter and back-off live here; the decision logic
does not.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from ..domain.dispatch import (
    DispatchPlan,
    DispatchPolicy,
    HilferufAggressiveness,
    MissionRequirementResolver,
    assigned_from_vehicles,
    plan_cleanup,
    plan_dispatches,
)

logger = logging.getLogger(__name__)


# Strip the per-mission detail (a "(600)" credit value or a ": short …" list) so
# the cycle summary compounds like reasons instead of one line per credit amount.
_REASON_DETAIL = re.compile(r"\s*[:(].*$")


def _summary_key(reason: str) -> str:
    return _REASON_DETAIL.sub("", reason).strip()


def _vehicle_name(vehicle, vehicle_id: int) -> str:
    """Readable label for a vehicle in the log (type name, else its caption)."""
    if vehicle is None:
        return f"vehicle #{vehicle_id}"
    label = getattr(vehicle, "type_caption", "") or getattr(vehicle, "caption", "")
    return f"{label} (#{vehicle_id})" if label else f"vehicle #{vehicle_id}"


def _dispatch_verb(mission, assigned: dict, mission_id: int) -> str:
    """Log verb reflecting who's already on the mission (Sasha, 2026-09-11):
    "sent reinforcements" if WE already have vehicles there (topping up our
    own partial dispatch), "sent to help" if someone else — an alliance
    member — is already on it (``vehicle_state`` revealed but none of ours),
    else "dispatched" for the genuine first vehicles to go."""
    if assigned.get(mission_id):
        return "sent reinforcements"
    if getattr(mission, "vehicle_state", 0) >= 1:
        return "sent to help"
    return "dispatched"


class DispatchOutcome(Protocol):
    """Structural shape of a dispatch result: an ``ok`` (HTTP-transport-level
    success) flag and a ``rejected`` mapping of vehicle id -> the game's own
    reason text, for every vehicle explicitly named as unusable within an
    otherwise-``ok`` batch — e.g. missing crew or its towing vehicle isn't at
    the station (confirmed live 2026-09-11). Kept structural (not importing
    ``app.game.client.DispatchResult`` directly) so this module stays
    testable without httpx installed."""

    ok: bool
    rejected: dict[int, str]


class GameClientLike(Protocol):
    """The slice of the game client the runner needs."""

    async def fetch_missions(self) -> list: ...
    async def fetch_vehicles(self, cap: int = 0) -> list: ...
    async def fetch_buildings(self) -> list: ...
    async def dispatch(self, mission_id: int, vehicle_id: int) -> DispatchOutcome: ...
    async def dispatch_many(self, mission_id: int, vehicle_ids: list[int]) -> DispatchOutcome: ...
    async def transport_targets(self, vehicle_id: int) -> list[str]: ...
    async def send_transport(self, action_url: str) -> bool: ...
    async def share_mission(self, mission_id: int) -> bool: ...
    async def recall(self, vehicle_id: int) -> bool: ...


@dataclass
class CycleReport:
    """Outcome of one dispatch cycle."""

    plan: DispatchPlan
    dispatched: int = 0
    failed: int = 0
    # Estimated credits from the missions we fully crewed this cycle (mission
    # average_credits summed). An estimate — no extra API call — labelled as such.
    credits_est: int = 0
    # Live "red/yellow/green/unknown" breakdown of THIS cycle's open missions
    # (Mission.status, from the confirmed `icon` field) — a snapshot, not a
    # cumulative tally like shortfalls. Empty in transport-only mode (no
    # mission poll that cycle).
    mission_status_counts: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"{len(self.plan.planned)} mission(s) actioned, "
            f"{self.dispatched} vehicle(s) dispatched, "
            f"{self.failed} failed, {len(self.plan.skipped)} skipped"
        )

    @property
    def missions_actioned(self) -> int:
        return len(self.plan.planned)

    def log_lines(self) -> list[str]:
        """Human-readable activity lines for the per-account details view."""
        if not self.plan.planned and self.dispatched == 0:
            return [f"idle - {len(self.plan.skipped)} mission(s) seen, none actionable"]
        lines = [self.summary()]
        for mp in self.plan.planned:
            if mp.satisfied:
                lines.append(f"sent to #{mp.mission_id} {mp.caption!r}: {len(mp.vehicle_ids)} vehicle(s)")
            else:
                missing = ", ".join(f"{cls} x{n}" for cls, n in mp.shortfall.items())
                lines.append(
                    f"partial #{mp.mission_id} {mp.caption!r}: sent {len(mp.vehicle_ids)}, "
                    f"MISSING {missing}"
                )
        return lines


@dataclass
class RunnerTiming:
    """Timing between actions. Injectable so tests run instantly."""

    # Human-ish gap between individual sends: a random value in this range.
    between_dispatches: float = 0.9
    between_dispatches_max: float = 2.2
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)

    def dispatch_gap(self) -> float:
        lo = self.between_dispatches
        hi = max(lo, self.between_dispatches_max)
        return random.uniform(lo, hi)


class DispatchRunner:
    """Runs dispatch cycles for a single account."""

    def __init__(
        self,
        client: GameClientLike,
        resolver: MissionRequirementResolver,
        policy: DispatchPolicy,
        *,
        buildings_cache: list | None = None,
        timing: RunnerTiming | None = None,
        on_log: Callable[[str], None] | None = None,
        on_stat: Callable[[int, int], None] | None = None,
        text_engine=None,
        strikes=None,
        vehicle_cap: int = 0,
        transport_enabled: bool = True,
        transport_only: bool = False,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._policy = policy
        self._text_engine = text_engine
        self._strikes = strikes
        self._vehicle_cap = vehicle_cap
        self._transport_enabled = transport_enabled
        self._transport_only = transport_only
        # Buildings change rarely; the runner may be handed a cached list to
        # avoid re-fetching them every cycle.
        self._buildings = buildings_cache
        # Missions already shared to the alliance this session (Hilferuf), so we
        # don't re-share one every cycle before the shared flag propagates.
        self._shared: set[int] = set()
        self._timing = timing or RunnerTiming()
        # Real-time activity sink (the panel's per-account log). Called as the
        # cycle progresses, so a long or in-progress cycle still shows what it
        # is doing rather than nothing until it finishes.
        self._on_log = on_log
        # Called (dispatched, missions) after each mission so the panel counters
        # tick up during the cycle, not only at its end.
        self._on_stat = on_stat

    def _log(self, message: str) -> None:
        logger.info(message)
        if self._on_log is not None:
            try:
                self._on_log(message)
            except Exception:  # noqa: BLE001 — logging must never break the cycle
                logger.exception("on_log hook failed")

    def _record_dispatch_failure(
        self, vehicle, vehicle_id: int, now: float, report, *, reason: str | None = None
    ) -> None:
        """A vehicle that LOOKED dispatchable (FMS 1/2, in the fleet) failed to
        send. If *reason* is given, the game itself confirmed why (e.g. "missing
        crew" or "towing vehicle not at the station" — live-verified 2026-09-11,
        the alarm response carries an alert-danger div naming the vehicle) —
        strike it out IMMEDIATELY, since that's much stronger evidence than an
        ambiguous HTTP-level failure. Otherwise strike normally (3 such
        failures in 24h before the planner drops it from the pool). Either way,
        surface it on the dashboard's "struggling to field" list."""
        label = (
            getattr(vehicle, "type_caption", "")
            or getattr(vehicle, "caption", "")
            or f"vehicle #{vehicle_id}"
        )
        tally = report.plan.shortfalls
        tally[label] = tally.get(label, 0) + 1
        if self._strikes is None:
            return
        name = _vehicle_name(vehicle, vehicle_id)
        if reason is not None:
            self._strikes.record_confirmed_failure(str(vehicle_id), now)
            self._log(f"struck out {name}: game rejected it ({reason}) — skipping until it clears")
            return
        self._strikes.record([str(vehicle_id)], now)
        if self._strikes.is_struck(str(vehicle_id), now):
            self._log(
                f"struck out {name}: failed to dispatch 3× — likely no crew; "
                f"skipping this vehicle for 24h"
            )

    async def _buildings_for_cycle(self) -> list:
        if self._buildings is None:
            self._buildings = await self._client.fetch_buildings()
        return self._buildings

    async def _handle_transports(self, vehicles) -> int:
        """Unload patients/prisoners from vehicles requesting it (FMS 5).

        Runs before dispatch each cycle. Picks the game's first offered
        hospital/cell (already filtered to free capacity + the vehicle's own
        in-game distance/price/own-alliance settings).
        """
        pending = [v for v in vehicles if getattr(v, "fms", None) == 5]
        if not pending:
            return 0
        self._log(f"transport: {len(pending)} vehicle(s) awaiting a bed/cell")
        done = 0
        for v in pending:
            name = _vehicle_name(v, v.id)
            targets = await self._client.transport_targets(v.id)
            if not targets:
                self._log(f"  no free target for {name} — all full / out of range")
                continue
            if await self._client.send_transport(targets[0]):
                done += 1
                self._log(f"  transported {name}")
            await self._timing.sleep(self._timing.dispatch_gap())
        return done

    async def run_cycle(self) -> CycleReport:
        """Run one cycle: transport (Sprechwünsche) first, then poll → plan →
        dispatch (skipped in transport-only mode)."""
        vehicles = await self._client.fetch_vehicles(self._vehicle_cap)

        if self._transport_enabled:
            await self._handle_transports(vehicles)
        if self._transport_only:
            self._log("transport-only mode: dispatch skipped")
            return CycleReport(plan=DispatchPlan())

        missions = await self._client.fetch_missions()
        buildings = await self._buildings_for_cycle()

        idle = sum(1 for v in vehicles if getattr(v, "is_available", False))
        self._log(f"polled {len(missions)} mission(s), {idle} idle vehicle(s)")

        status_counts = dict(Counter(getattr(m, "status", "unknown") for m in missions))

        # Subtract vehicles already en route so missions are topped up, not
        # re-dispatched (avoids piling vehicles onto an already-covered mission).
        now = time.time()
        assigned = assigned_from_vehicles(vehicles)
        plan = plan_dispatches(
            missions, vehicles, buildings, self._resolver, self._policy,
            assigned=assigned, text_engine=self._text_engine, now=now,
            strikes=self._strikes,
        )
        report = CycleReport(plan=plan, mission_status_counts=status_counts)

        # Show why missions were (or weren't) actionable — the key diagnostic.
        reasons = Counter(_summary_key(reason) for _mid, reason in plan.skipped)
        if reasons:
            breakdown = "; ".join(f"{n}x {r}" for r, n in reasons.most_common(6))
            self._log(f"plan: {len(plan.planned)} actionable, {len(plan.skipped)} skipped ({breakdown})")
        else:
            self._log(f"plan: {len(plan.planned)} actionable")

        # Log shortfalls / strike-outs prominently — these matter more than
        # successes (they're why a mission didn't get handled).
        by_id = {m.id: m for m in missions}
        shortfalls = [
            (mid, r) for mid, r in plan.skipped
            if r.startswith("not enough vehicles") or r.startswith("needs struck-out")
        ]
        for mid, reason in shortfalls[:5]:
            mm = by_id.get(mid)
            cap = getattr(mm, "caption", "") if mm is not None else ""
            self._log(f"  short #{mid} {cap!r}: {reason}")
        if len(shortfalls) > 5:
            self._log(f"  … and {len(shortfalls) - 5} more short/struck missions")

        # Diagnose unresolved missions: show a few with their live requirement
        # text so we can see whether it's empty or just not parsing.
        unresolved = [mid for mid, r in plan.skipped if r == "no requirements found"]
        if unresolved:
            catalog = self._resolver.catalog
            for mid in unresolved[:3]:
                mm = by_id.get(mid)
                if mm is None:
                    continue
                mtid = getattr(mm, "mission_type_id", None)
                mtype = catalog.get(mtid) if mtid is not None else None
                reqs = mtype.requirements if mtype is not None else None
                chances = mtype.chances if mtype is not None else None
                text = (getattr(mm, "missing_text", "") or "").strip() or "(empty)"
                self._log(
                    f"  unresolved #{mid} {getattr(mm, 'caption', '')!r}: mtid={mtid} "
                    f"in_catalog={mtype is not None} catalog_reqs={reqs} chances={chances} "
                    f"missing_text={text[:100]!r}"
                )

        veh_by_id = {v.id: v for v in vehicles}

        for mission_plan in plan.planned:
            ids = list(mission_plan.vehicle_ids)
            if not ids:
                continue
            mid, cap = mission_plan.mission_id, mission_plan.caption
            verb = _dispatch_verb(by_id.get(mid), assigned, mid)
            sent = 0
            # One alarm POST for the whole mission (the game's multi-select form).
            result = await self._client.dispatch_many(mid, ids)
            if result.ok:
                # The POST succeeded, but the game may still have explicitly
                # rejected specific vehicles within it (missing crew / its
                # towing vehicle isn't at the station — live-verified
                # 2026-09-11) rather than erroring the whole request. Those
                # are struck immediately, not counted as dispatched.
                succeeded = [v for v in ids if v not in result.rejected]
                sent = len(succeeded)
                report.dispatched += sent
                if succeeded:
                    names = ", ".join(_vehicle_name(veh_by_id.get(v), v) for v in succeeded)
                    self._log(f"{verb} {sent} → #{mid} {cap!r}: {names}")
                for vehicle_id, reason in result.rejected.items():
                    report.failed += 1
                    self._record_dispatch_failure(
                        veh_by_id.get(vehicle_id), vehicle_id, now, report, reason=reason
                    )
                await self._timing.sleep(self._timing.dispatch_gap())
            else:
                # Batch failed — retry one-by-one so we can isolate and strike the
                # vehicle that actually won't go (e.g. a crewless truck).
                self._log(f"batch dispatch failed for #{mid} {cap!r} — retrying individually")
                for vehicle_id in ids:
                    name = _vehicle_name(veh_by_id.get(vehicle_id), vehicle_id)
                    single = await self._client.dispatch(mid, vehicle_id)
                    if single.ok and vehicle_id not in single.rejected:
                        report.dispatched += 1
                        sent += 1
                        self._log(f"{verb} {name} → #{mid} {cap!r}")
                    else:
                        report.failed += 1
                        reason = single.rejected.get(vehicle_id)
                        self._log(f"FAILED {name} → #{mid} {cap!r} ({reason or 'no crew?'})")
                        self._record_dispatch_failure(
                            veh_by_id.get(vehicle_id), vehicle_id, now, report, reason=reason
                        )
                    await self._timing.sleep(self._timing.dispatch_gap())
            if not mission_plan.satisfied:
                self._log(f"#{mid} still MISSING {mission_plan.shortfall}")
            # Estimate earned credits: a fully-crewed mission we actually sent to.
            if sent and mission_plan.satisfied:
                mm = by_id.get(mid)
                report.credits_est += int(getattr(mm, "average_credits", 0) or 0)
            # Tick the counters per mission, not only at cycle end.
            if self._on_stat is not None and sent:
                self._on_stat(sent, 1)

        hilferuf_mode = getattr(self._policy, "hilferuf_mode", HilferufAggressiveness.OFF)
        if hilferuf_mode is not HilferufAggressiveness.OFF:
            await self._hilferuf(plan, by_id, hilferuf_mode)

        if getattr(getattr(self._policy, "patient_scope", None), "value", "off") != "off":
            await self._recall_cleared_patient_missions(missions, vehicles)

        self._log(f"cycle done: {report.summary()}")
        return report

    async def cleanup(self) -> int:
        """One-shot: recall OUR vehicles that are surplus to what each mission
        needs (over-dispatch, from manual play or earlier cycles). See
        :func:`plan_cleanup`."""
        vehicles = await self._client.fetch_vehicles(self._vehicle_cap)
        missions = await self._client.fetch_missions()
        buildings = await self._buildings_for_cycle()
        recalls = plan_cleanup(missions, vehicles, buildings, self._resolver, self._text_engine)
        veh_by_id = {v.id: v for v in vehicles}
        cap_by_id = {m.id: getattr(m, "caption", "") for m in missions}
        if not recalls:
            self._log("cleanup: nothing surplus — every assigned vehicle is needed")
            return 0
        self._log(f"cleanup: recalling {len(recalls)} surplus vehicle(s)")
        done = 0
        for vehicle_id, mid in recalls:
            name = _vehicle_name(veh_by_id.get(vehicle_id), vehicle_id)
            if await self._client.recall(vehicle_id):
                done += 1
                self._log(f"recalled {name}: surplus at #{mid} {cap_by_id.get(mid, '')!r}")
            await self._timing.sleep(self._timing.dispatch_gap())
        self._log(f"cleanup done: recalled {done} vehicle(s)")
        return done

    # Vehicle classes patient-only mode itself ever sends (see
    # resolver.resolve_patient_only) — the ONLY ones its recall may touch.
    _PATIENT_GOVERNED_CLASSES = ("ambulances", "nef", "rescue_helicopter", "kdow_lna", "kdow_orgl")

    async def _recall_cleared_patient_missions(self, missions, vehicles) -> None:
        """Patient-only cleanup: recall units we no longer need at a mission whose
        patients are all handled.

        A mission counts as cleared only when one of OUR units is actually **on
        scene** (FMS 4) and sees zero patients — so units stick around until every
        patient is treated or on the way to a hospital, and we never recall on a
        still-hidden count. Then every unit of ours there whose TYPE is one
        patient-only mode itself would have sent (ambulances/NEF/rescue
        helicopter/incident-command) is recalled, except one with a patient
        aboard (FMS 5), which is left to reach the hospital. A vehicle another
        mode or a manual send put on that same mission (a fire truck, a police
        car, …) is left alone — patient-only never governs it, so it never
        recalls it (confirmed by Sasha: a real live bug, this was previously
        unfiltered by vehicle type)."""
        from ..domain.dispatch import _scope_covers

        scope = getattr(self._policy, "patient_scope", None)
        # Only recall from missions the patient mode actually governs.
        patients_by_mid = {
            m.id: int(getattr(m, "patients_count", 0) or 0)
            for m in missions
            if _scope_covers(scope, getattr(m, "is_alliance", False))
        }
        on_scene = {
            getattr(v, "target_id", None)
            for v in vehicles
            if getattr(v, "target_type", None) == "mission" and getattr(v, "fms", None) == 4
        }
        cleared = {mid for mid in on_scene if mid in patients_by_mid and patients_by_mid[mid] == 0}
        if not cleared:
            return
        by_class = self._resolver.library.class_map.by_class
        governed_type_ids = {
            tid for cls in self._PATIENT_GOVERNED_CLASSES for tid in by_class.get(cls, ())
        }
        for v in vehicles:
            if getattr(v, "target_type", None) != "mission":
                continue
            if getattr(v, "target_id", None) not in cleared:
                continue
            if getattr(v, "fms", None) == 5:
                continue  # patient aboard — must reach a hospital, don't recall
            if getattr(v, "type_id", None) not in governed_type_ids:
                continue  # not a vehicle patient-only mode itself would send
            name = _vehicle_name(v, v.id)
            if await self._client.recall(v.id):
                self._log(f"recalled {name}: patients cleared at #{v.target_id}")
            await self._timing.sleep(self._timing.dispatch_gap())

    async def _hilferuf(self, plan, by_id, mode: HilferufAggressiveness) -> None:
        """Share our OWN missions to the alliance so members can help.

        UNCREWABLE (Sasha, 2026-08-31 — the original behavior): only missions
        we genuinely couldn't crew this cycle (a "not enough vehicles" skip).
        ALL (Sasha, 2026-09-11): every own, not-yet-shared mission, regardless
        of whether we handled it ourselves — lets allies pile on for the
        extra participation credit too, not just the ones we're stuck on.
        """
        if mode is HilferufAggressiveness.UNCREWABLE:
            candidates = [
                (mid, "can't crew it")
                for mid, reason in plan.skipped
                if reason.startswith("not enough vehicles")
            ]
        else:  # ALL
            candidates = [(mid, "sharing everything") for mid in by_id]
        for mid, why in candidates:
            mission = by_id.get(mid)
            if mission is None or getattr(mission, "is_alliance", False):
                continue  # not ours, or already shared
            if mid in self._shared:
                continue
            if await self._client.share_mission(mid):
                self._shared.add(mid)
                self._log(
                    f"Hilferuf: shared #{mid} {getattr(mission, 'caption', '')!r} "
                    f"to the alliance — {why}"
                )
            await self._timing.sleep(self._timing.dispatch_gap())
