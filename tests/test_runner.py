import asyncio
import json
import time
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

from app.core.runner import DispatchRunner, RunnerTiming
from app.domain.alarms import AlarmTracker
from app.domain.assets import AssetLibrary
from app.domain.catalog import MissionCatalog
from app.domain.dispatch import DispatchPlan, DispatchPolicy, HilferufAggressiveness, MissionPlan, ModeScope
from app.domain.resolver import MissionRequirementResolver

BASE = Path(__file__).resolve().parent
VEHICLES_DIR = BASE.parent / "data" / "de" / "vehicles"
FIXTURE = BASE / "fixtures" / "einsaetze_sample.json"

BUILDING = (52.5200, 13.4050)
NEAR = (52.5300, 13.4100)


def _result(ok: bool, rejected: dict[int, str] | None = None,
            mission_not_found: bool = False) -> SimpleNamespace:
    """A DispatchOutcome-shaped fake result (ok + rejected + mission_not_found),
    without pulling in app.game.client's httpx-dependent import chain."""
    return SimpleNamespace(ok=ok, rejected=rejected or {}, mission_not_found=mission_not_found)


class FakeClient:
    """Records dispatch calls; serves canned reads. No network."""

    def __init__(self, missions, vehicles, buildings):
        self._missions, self._vehicles, self._buildings = missions, vehicles, buildings
        self.dispatched: list[tuple[int, int]] = []
        self.transports: list[tuple[str, int, int]] = []
        self.shared: list[int] = []
        self.recalled: list[int] = []
        # vehicle_id -> reason: simulate the game explicitly rejecting a
        # specific vehicle within an otherwise-ok batch/single dispatch.
        self.rejections: dict[int, str] = {}
        # mission ids whose alarm route should simulate a 404 (confirmed live
        # 2026-10-01: resolved/removed between poll and dispatch).
        self.gone_missions: set[int] = set()
        # mission_id -> [(vehicle_id, type_id), ...] -- the on-scene roster
        # GameClient.fetch_on_scene_vehicles would parse off the mission
        # detail page (everyone's vehicles, not just ours). Empty/unset by
        # default, matching "nothing fetched this cycle".
        self.on_scene_rosters: dict[int, list[tuple[int, int]]] = {}
        self.on_scene_fetches: list[int] = []

    async def fetch_missions(self):
        return self._missions

    async def fetch_vehicles(self, cap: int = 0):
        return self._vehicles

    async def fetch_buildings(self):
        return self._buildings

    async def dispatch(self, mission_id, vehicle_id):
        if mission_id in self.gone_missions:
            return _result(False, mission_not_found=True)
        self.dispatched.append((mission_id, vehicle_id))
        reason = self.rejections.get(vehicle_id)
        return _result(True, {vehicle_id: reason} if reason else None)

    async def dispatch_many(self, mission_id, vehicle_ids):
        if mission_id in self.gone_missions:
            return _result(False, mission_not_found=True)
        rejected = {}
        for vid in vehicle_ids:
            self.dispatched.append((mission_id, vid))
            reason = self.rejections.get(vid)
            if reason:
                rejected[vid] = reason
        return _result(True, rejected)

    async def transport_targets(self, vehicle_id):
        # one hospital offered per vehicle
        return [f"/vehicles/{vehicle_id}/patient/900{vehicle_id}"]

    async def send_transport(self, action_url):
        self.transports.append(action_url)
        return True

    async def share_mission(self, mission_id):
        self.shared.append(mission_id)
        return True

    async def recall(self, vehicle_id):
        self.recalled.append(vehicle_id)
        return True

    async def fetch_on_scene_vehicles(self, mission_id):
        self.on_scene_fetches.append(mission_id)
        return self.on_scene_rosters.get(mission_id, [])


def m(mid, mtype, pos=NEAR, status="unknown", vehicle_state=0):
    return SimpleNamespace(id=mid, caption=f"M{mid}", mission_type_id=mtype, position=pos,
                            status=status, vehicle_state=vehicle_state)


def v(vid, type_id):
    return SimpleNamespace(id=vid, type_id=type_id, is_available=True, position=BUILDING)


def b():
    return SimpleNamespace(position=BUILDING)


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        library = AssetLibrary.load(VEHICLES_DIR)
        catalog = MissionCatalog.from_payload(json.loads(FIXTURE.read_text(encoding="utf-8")))
        cls.resolver = MissionRequirementResolver(catalog, library)

    def _runner(self, client, on_log=None, strikes=None, alarm_tracker=None, recall_cadence=None):
        # No-op sleep so the test is instant.
        async def no_sleep(_):
            return None

        return DispatchRunner(
            client, self.resolver, DispatchPolicy(radius_km=40),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
            on_log=on_log, strikes=strikes, alarm_tracker=alarm_tracker,
            recall_cadence=recall_cadence,
        )

    def test_cycle_dispatches_planned_vehicles(self):
        client = FakeClient([m(1, 1)], [v(100, 0)], [b()])  # type 1 -> firetrucks:1, LF=0
        report = asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(client.dispatched, [(1, 100)])
        self.assertEqual(report.dispatched, 1)
        self.assertEqual(report.failed, 0)

    def test_cycle_abandons_dispatch_when_mission_no_longer_available(self):
        # Confirmed live 2026-10-01, mission #4420218988 "Reizgas in
        # geschlossenem Raum": the alarm POST 404'd -- the mission was
        # resolved/removed between being polled and dispatched. Every
        # vehicle in the batch would 404 identically; must NOT be retried
        # individually (that produced 18 misleading "no crew?" log lines and
        # 18 wasted round trips for a single real event), and must NOT
        # strike any of the (perfectly fine) vehicles involved.
        from app.domain.strikes import StrikeTracker

        strikes = StrikeTracker()
        # type 2's requirement: firetrucks:2, platform_trucks:1, battalion_chief_vehicles:1
        vehicles = [v(100, 0), v(101, 0), v(102, 2), v(103, 3)]
        client = FakeClient([m(1, 2)], vehicles, [b()])
        client.gone_missions.add(1)
        log_lines: list[str] = []
        report = asyncio.run(self._runner(client, on_log=log_lines.append, strikes=strikes).run_cycle())
        self.assertEqual(client.dispatched, [])  # no individual retries attempted
        self.assertEqual(report.dispatched, 0)
        self.assertTrue(any("no longer available" in line for line in log_lines))
        self.assertFalse(any("no crew?" in line for line in log_lines))
        for vid in (100, 101, 102, 103):
            self.assertFalse(strikes.is_struck(str(vid), time.time()))

    def test_cycle_abandons_remaining_individual_retries_once_mission_found_gone(self):
        # The batch attempt can fail for an ordinary ambiguous reason (no
        # mission_not_found signal yet) and only reveal the mission is gone
        # partway through the individual-retry loop -- the rest must still
        # be abandoned rather than each independently discovering the same 404.
        vehicles = [v(100, 0), v(101, 0), v(102, 2), v(103, 3)]
        client = FakeClient([m(1, 2)], vehicles, [b()])

        async def dispatch_many(mission_id, vehicle_ids):
            return _result(False)  # ambiguous batch failure, no mission_not_found yet

        first_call = {"done": False}
        real_dispatch = client.dispatch

        async def dispatch(mission_id, vehicle_id):
            if first_call["done"]:
                return _result(False, mission_not_found=True)
            first_call["done"] = True
            return await real_dispatch(mission_id, vehicle_id)

        client.dispatch_many = dispatch_many
        client.dispatch = dispatch
        log_lines: list[str] = []
        report = asyncio.run(self._runner(client, on_log=log_lines.append).run_cycle())
        # Only the first vehicle was genuinely attempted; the rest were
        # abandoned once the mission was found gone.
        self.assertEqual(len(client.dispatched), 1)
        self.assertTrue(any("abandoning remaining vehicles" in line for line in log_lines))

    def test_cycle_records_mission_interaction_timestamp_on_dispatch(self):
        # Sasha, 2026-09-22: "how long since we last interacted with that
        # mission" -- recorded whenever we actually send >=1 vehicle.
        client = FakeClient([m(1, 1)], [v(100, 0)], [b()])
        before = time.time()
        report = asyncio.run(self._runner(client).run_cycle())
        self.assertIn(1, report.mission_interactions)
        self.assertGreaterEqual(report.mission_interactions[1], before)

    def test_cycle_records_no_interaction_when_nothing_sent(self):
        # Out of scope -> nothing dispatched -> no interaction recorded.
        client = FakeClient([m(1, 1)], [v(100, 0)], [b()])
        runner = DispatchRunner(
            client, self.resolver, DispatchPolicy(radius_km=40, mission_source=frozenset()),
            timing=RunnerTiming(between_dispatches=0, sleep=lambda _: asyncio.sleep(0)),
        )
        report = asyncio.run(runner.run_cycle())
        self.assertEqual(report.mission_interactions, {})

    # -- alarm tracking (Sasha, 2026-10-27: "how often we need to alarm to
    # each mission" -- only a real send/reinforcement counts, never a
    # parasite send or S5 processing) -----------------------------------
    def test_alarm_recorded_on_a_real_send_and_finalized_once_mission_closes(self):
        client = FakeClient([m(1, 1)], [v(100, 0)], [b()])
        tracker = AlarmTracker()
        runner = self._runner(client, alarm_tracker=tracker)
        report = asyncio.run(runner.run_cycle())
        self.assertEqual(client.dispatched, [(1, 100)])  # sanity: a real send happened
        self.assertEqual(report.alarm_counts_closed, {})  # mission still live -- not finalized yet

        client._missions = []  # mission #1 dropped out of the feed -- it's done
        report2 = asyncio.run(runner.run_cycle())
        self.assertEqual(report2.alarm_counts_closed, {1: 1})  # closed after exactly 1 alarm

    def test_alarm_not_recorded_when_the_send_is_tagged_as_not_counting(self):
        # A parasite/VGE-spare send (MissionPlan.counts_as_alarm=False) still
        # dispatches a real vehicle, but must never feed the alarm tally.
        client = FakeClient([m(1, 1)], [v(100, 0)], [b()])
        tracker = AlarmTracker()
        runner = self._runner(client, alarm_tracker=tracker)
        fake_plan = DispatchPlan(planned=[
            MissionPlan(1, "M1", (100,), True, {}, counts_as_alarm=False),
        ])
        with unittest.mock.patch("app.core.runner.plan_dispatches", return_value=fake_plan):
            asyncio.run(runner.run_cycle())
        self.assertEqual(client.dispatched, [(1, 100)])  # the vehicle really went out
        self.assertEqual(tracker.finalize(set()), {})     # but it never counted as an alarm

    def test_alarm_not_recorded_when_nothing_actually_went_out(self):
        # mission_not_found (404): "sent" stays 0 even though a MissionPlan
        # with counts_as_alarm=True was queued -- must not be recorded.
        client = FakeClient([m(1, 1)], [v(100, 0)], [b()])
        client.gone_missions.add(1)
        tracker = AlarmTracker()
        report = asyncio.run(self._runner(client, alarm_tracker=tracker).run_cycle())
        self.assertEqual(report.dispatched, 0)
        self.assertEqual(tracker.finalize(set()), {})

    def test_runner_works_without_an_alarm_tracker(self):
        # alarm_tracker is optional -- omitting it must not break a cycle.
        client = FakeClient([m(1, 1)], [v(100, 0)], [b()])
        report = asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(report.alarm_counts_closed, {})

    def test_cycle_reports_mission_status_breakdown(self):
        # TODO.md P4: surface live icon status. A snapshot of THIS cycle's
        # missions by Mission.status, for the dashboard.
        missions = [
            m(1, 1, status="red"), m(2, 1, status="red"),
            m(3, 1, status="yellow"), m(4, 1, status="green"),
        ]
        client = FakeClient(missions, [v(100, 0)], [b()])
        report = asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(report.mission_status_counts, {"red": 2, "yellow": 1, "green": 1})

    # -- mission breakdown (Sasha, 2026-09-22) ----------------------------
    def test_cycle_reports_mission_breakdown_fully_staffed(self):
        # "fully staffed" + red here is a deliberate, confirmed-still-correct
        # one-cycle-lag artifact (re-checked 2026-10-26, see the
        # "awaiting arrival" tests below for the case that ISN'T): the live
        # status was polled before we dispatched THIS cycle, so a mission
        # with zero vehicles before now always still shows red in this row.
        missions = [m(1, 1, status="red")]
        client = FakeClient(missions, [v(100, 0)], [b()])  # type 1 needs 1 firetruck; LF supplied
        report = asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(len(report.mission_breakdown), 1)
        row = report.mission_breakdown[0]
        self.assertEqual(row["mission_id"], 1)
        self.assertEqual(row["category"], "own")
        self.assertEqual(row["live_status"], "red")
        self.assertEqual(row["bot_status"], "fully staffed")

    def test_cycle_reports_mission_breakdown_not_enough_vehicles(self):
        # Type 2 needs firetrucks:2 + platform_trucks:1 + battalion_chief_vehicles:1;
        # a single LF comes nowhere near that, and partial_starts defaults off.
        missions = [m(2, 2, status="red")]
        client = FakeClient(missions, [v(100, 0)], [b()])
        report = asyncio.run(self._runner(client).run_cycle())
        row = report.mission_breakdown[0]
        self.assertEqual(row["bot_status"], "not enough vehicles")
        self.assertIn("short", row["explanation"])

    def test_cycle_reports_mission_breakdown_already_covered_distinct_from_fully_staffed(self):
        # Sasha, 2026-09-22: "already covered" (nothing dispatched THIS
        # cycle -- our own tracking says a vehicle from a PREVIOUS cycle
        # already satisfies it) must be its own bucket, not folded into
        # "fully staffed" (which means we just now dispatched this cycle).
        # On a non-red live status this combination is unremarkable, so it
        # keeps the plain "already covered" label.
        already_assigned = SimpleNamespace(id=200, type_id=0, is_available=False,
                                            position=BUILDING, target_type="mission", target_id=1)
        missions = [m(1, 1, status="yellow")]  # type 1 needs 1 firetruck; already met
        client = FakeClient(missions, [already_assigned], [b()])
        report = asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(client.dispatched, [])  # nothing sent this cycle
        row = report.mission_breakdown[0]
        self.assertEqual(row["bot_status"], "already covered")
        self.assertEqual(row["explanation"], "already covered")

    def test_red_mission_ignores_stale_assigned_tracking_and_re_requests_in_full(self):
        # Rule 1 hotfix (Sasha, 2026-10-26): a mission still RED has had
        # NOTHING arrive, no matter what our own "assigned" bookkeeping
        # claims -- live-observed missions stuck red and marked "already
        # covered" for a long time because a vehicle we believed was en
        # route delta-subtracted the requirement to zero without ever
        # showing up. A red mission is now always evaluated against the
        # FULL current requirement, ignoring assigned/delta entirely, so a
        # stale "already assigned" entry can no longer mask a genuine
        # shortfall -- this one becomes "not enough vehicles" (there's
        # nothing actually usable in the pool), not silently "already
        # covered".
        already_assigned = SimpleNamespace(id=200, type_id=0, is_available=False,
                                            position=BUILDING, target_type="mission", target_id=1)
        missions = [m(1, 1, status="red")]  # type 1 needs 1 firetruck
        client = FakeClient(missions, [already_assigned], [b()])
        report = asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(client.dispatched, [])  # nothing usable to send
        row = report.mission_breakdown[0]
        self.assertEqual(row["bot_status"], "not enough vehicles")

    def test_red_mission_still_dispatches_a_genuinely_free_vehicle_despite_stale_assigned_entry(self):
        # Same stale-assigned-tracking setup, but this time a real, free
        # vehicle also exists -- confirms Rule 1 doesn't just report the
        # shortfall correctly, it actually dispatches to close it.
        already_assigned = SimpleNamespace(id=200, type_id=0, is_available=False,
                                            position=BUILDING, target_type="mission", target_id=1)
        missions = [m(1, 1, status="red")]  # type 1 needs 1 firetruck
        client = FakeClient(missions, [already_assigned, v(201, 0)], [b()])
        report = asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(client.dispatched, [(1, 201)])
        row = report.mission_breakdown[0]
        self.assertEqual(row["bot_status"], "fully staffed")

    def test_yellow_mission_still_trusts_assigned_delta_as_already_covered(self):
        # Rule 1 is scoped to RED only (Sasha, 2026-10-26) -- once a mission
        # has shown SOME real response (yellow/green), normal delta
        # adjustment against our own assigned tracking still applies.
        already_assigned = SimpleNamespace(id=200, type_id=0, is_available=False,
                                            position=BUILDING, target_type="mission", target_id=1)
        missions = [m(1, 1, status="yellow")]  # type 1 needs 1 firetruck; already met
        client = FakeClient(missions, [already_assigned], [b()])
        report = asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(client.dispatched, [])
        row = report.mission_breakdown[0]
        self.assertEqual(row["bot_status"], "already covered")

    def test_cycle_reports_mission_breakdown_out_of_scope(self):
        missions = [m(1, 1, status="red")]
        client = FakeClient(missions, [v(100, 0)], [b()])
        runner = DispatchRunner(
            client, self.resolver, DispatchPolicy(radius_km=40, mission_source=frozenset()),
            timing=RunnerTiming(between_dispatches=0, sleep=lambda _: asyncio.sleep(0)),
        )
        report = asyncio.run(runner.run_cycle())
        row = report.mission_breakdown[0]
        self.assertEqual(row["bot_status"], "out of scope")
        self.assertEqual(row["explanation"], "out of scope")

    def test_cycle_reports_mission_breakdown_category_verband_and_event(self):
        # Category uses the SAME ownership classification plan_dispatches
        # itself used, not the raw is_alliance flag -- event takes priority.
        verband = SimpleNamespace(id=10, caption="[Verband] X", mission_type_id=1, position=NEAR,
                                   status="yellow", vehicle_state=0, is_alliance=True)
        event = SimpleNamespace(id=11, caption="[Event] Y", mission_type_id=1, position=NEAR,
                                 status="green", vehicle_state=0, is_alliance=False, is_event=True)
        client = FakeClient([verband, event], [v(100, 0), v(101, 0)], [b()])
        report = asyncio.run(self._runner(client).run_cycle())
        by_id = {r["mission_id"]: r for r in report.mission_breakdown}
        self.assertEqual(by_id[10]["category"], "verband")
        self.assertEqual(by_id[11]["category"], "event")

    def test_cycle_with_nothing_to_do(self):
        client = FakeClient([], [v(100, 0)], [b()])
        report = asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(client.dispatched, [])
        self.assertEqual(report.dispatched, 0)

    # -- dispatch-verb log wording (Sasha, 2026-09-11) --------------------

    def test_log_says_dispatched_for_the_first_vehicles_on_a_mission(self):
        # vehicle_state=0 -- untouched, nobody there yet.
        client = FakeClient([m(1, 1, vehicle_state=0)], [v(100, 0)], [b()])
        lines: list[str] = []
        asyncio.run(self._runner(client, on_log=lines.append).run_cycle())
        joined = "\n".join(lines)
        self.assertIn("dispatched 1 → #1", joined)
        self.assertNotIn("sent to help", joined)
        self.assertNotIn("sent reinforcements", joined)

    def test_log_says_sent_to_help_when_an_ally_is_already_on_it(self):
        # vehicle_state>=1 (someone's there) but none of OUR vehicles are
        # already assigned to it -- must be an alliance member.
        client = FakeClient([m(1, 1, vehicle_state=1)], [v(100, 0)], [b()])
        lines: list[str] = []
        asyncio.run(self._runner(client, on_log=lines.append).run_cycle())
        joined = "\n".join(lines)
        self.assertIn("sent to help 1 → #1", joined)

    def test_log_says_sent_reinforcements_when_we_already_have_units_there(self):
        # Mission type 2 needs firetrucks:2, platform_trucks:1,
        # battalion_chief_vehicles:1 -- one of OUR firetrucks is already
        # assigned (busy, target_type=mission), so the top-up dispatch for
        # the rest should read "reinforcements", not "sent to help".
        already_ours = SimpleNamespace(
            id=99, type_id=0, is_available=False, position=BUILDING,
            target_type="mission", target_id=1,
        )
        new_ft = v(100, 0)
        platform = v(101, 2)
        elw = v(102, 3)
        client = FakeClient([m(1, 2, vehicle_state=1)], [already_ours, new_ft, platform, elw], [b()])
        lines: list[str] = []
        asyncio.run(self._runner(client, on_log=lines.append).run_cycle())
        joined = "\n".join(lines)
        self.assertIn("sent reinforcements", joined)
        self.assertNotIn("sent to help", joined)

    def test_log_lines_report_missing_vehicles(self):
        from app.core.runner import CycleReport
        from app.domain.dispatch import DispatchPlan, MissionPlan

        plan = DispatchPlan(
            planned=[MissionPlan(1, "Brand", (100,), False, {"heavy_rescue_vehicles": 1})],
            skipped=[],
        )
        lines = CycleReport(plan=plan, dispatched=1).log_lines()
        self.assertTrue(any("MISSING" in ln and "heavy_rescue_vehicles" in ln for ln in lines))

    def test_log_lines_idle_cycle(self):
        from app.core.runner import CycleReport
        from app.domain.dispatch import DispatchPlan

        lines = CycleReport(plan=DispatchPlan(planned=[], skipped=[(1, "out of range")])).log_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("idle", lines[0])

    def test_realtime_log_reports_poll_and_skip_reasons(self):
        # A mission far out of range -> log shows it was polled and why skipped.
        far = m(1, 1, pos=(53.55, 9.99))  # Hamburg, out of 40km radius
        client = FakeClient([far], [v(100, 0)], [b()])
        lines: list[str] = []

        async def no_sleep(_):
            return None

        runner = DispatchRunner(
            client, self.resolver, DispatchPolicy(radius_km=40),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
            on_log=lines.append,
        )
        asyncio.run(runner.run_cycle())
        joined = "\n".join(lines)
        self.assertIn("polled 1 mission", joined)
        self.assertIn("out of range", joined)

    def test_failed_dispatch_counted(self):
        client = FakeClient([m(1, 1)], [v(100, 0)], [b()])

        async def fail(*args):
            return _result(False)

        client.dispatch = fail
        client.dispatch_many = fail  # batch fails -> falls back to single, also fails
        report = asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.dispatched, 0)

    def test_bulk_dispatch_one_post_for_multi_vehicle_mission(self):
        # type 3 needs firetrucks + police; both go in ONE dispatch_many call.
        calls = {"many": 0, "single": 0}
        client = FakeClient([m(1, 3)], [v(100, 0), v(101, 32)], [b()])  # LF + FuStW
        orig_many = client.dispatch_many

        async def counting_many(mission_id, ids):
            calls["many"] += 1
            return await orig_many(mission_id, ids)

        async def counting_single(mission_id, vid):
            calls["single"] += 1
            return _result(True)

        client.dispatch_many = counting_many
        client.dispatch = counting_single
        report = asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(report.dispatched, 2)
        self.assertEqual(calls["many"], 1)     # one POST for both vehicles
        self.assertEqual(calls["single"], 0)   # no per-vehicle fallback needed
        self.assertEqual(set(client.dispatched), {(1, 100), (1, 101)})

    def test_dispatch_failure_strikes_vehicle_and_excludes_it(self):
        # A vehicle that LOOKS free but fails to send (e.g. no crew) is struck
        # (keyed by id, threshold 1 here), tallied on the dashboard, and dropped
        # from the pool next cycle so we stop retrying it.
        import time as _time

        from app.domain.strikes import StrikeTracker

        client = FakeClient([m(1, 1)], [v(100, 0)], [b()])

        async def fail(mission_id, vehicle_id):
            return _result(False)

        client.dispatch = fail
        client.dispatch_many = fail  # batch fails -> per-vehicle fallback isolates + strikes
        strikes = StrikeTracker(threshold=1)
        lines: list[str] = []

        async def no_sleep(_):
            return None

        runner = DispatchRunner(
            client, self.resolver, DispatchPolicy(radius_km=40),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
            strikes=strikes, on_log=lines.append,
        )
        report = asyncio.run(runner.run_cycle())
        self.assertEqual(report.failed, 1)
        self.assertIn("100", strikes.struck_classes(_time.time()))          # struck by id
        self.assertEqual(report.plan.shortfalls.get("vehicle #100"), 1)     # on the left panel
        self.assertTrue(any("struck out" in ln for ln in lines))

        # Next cycle: the struck vehicle is excluded -> not attempted again.
        report2 = asyncio.run(runner.run_cycle())
        self.assertEqual(report2.failed, 0)
        self.assertEqual(report2.dispatched, 0)

    # -- confirmed game rejection (Sasha, 2026-09-11) ---------------------

    def test_confirmed_rejection_within_an_ok_batch_strikes_immediately(self):
        # The alarm POST succeeds overall (ok=True), but the game explicitly
        # rejected one vehicle within it (e.g. "towing vehicle not at the
        # station") -- that vehicle must be struck out on ONE occurrence, not
        # counted as dispatched, and not require the normal 3-strike wait.
        from app.domain.strikes import StrikeTracker

        client = FakeClient([m(1, 3)], [v(100, 0), v(101, 32)], [b()])  # LF + FuStW
        client.rejections = {101: "Das Zugfahrzeug befindet sich nicht auf der Wache."}
        strikes = StrikeTracker()  # default threshold=3
        lines: list[str] = []
        report = asyncio.run(self._runner(client, on_log=lines.append, strikes=strikes).run_cycle())

        self.assertEqual(report.dispatched, 1)   # only the LF counted
        self.assertEqual(report.failed, 1)       # the rejected FuStW counted as failed
        self.assertTrue(strikes.is_struck("101", time.time()))  # struck immediately, threshold bypassed
        self.assertTrue(any("game rejected it" in ln for ln in lines))
        self.assertTrue(any("Zugfahrzeug" in ln for ln in lines))  # reason surfaced in the log

    def test_confirmed_rejection_in_the_per_vehicle_retry_path_also_strikes_immediately(self):
        # Same confirmed-rejection handling applies when the batch itself
        # failed and we fall back to dispatching one vehicle at a time.
        from app.domain.strikes import StrikeTracker

        client = FakeClient([m(1, 1)], [v(100, 0)], [b()])
        client.rejections = {100: "Die Besatzung ist nicht vollständig."}

        async def fail_batch(*args):
            return _result(False)

        client.dispatch_many = fail_batch  # forces the per-vehicle retry path
        strikes = StrikeTracker()
        report = asyncio.run(self._runner(client, strikes=strikes).run_cycle())

        self.assertEqual(report.dispatched, 0)
        self.assertEqual(report.failed, 1)
        self.assertTrue(strikes.is_struck("100", time.time()))

    def test_patient_only_recalls_idle_units_when_cleared(self):
        # A mission whose patients are all gone (patients_count 0, vehicle_state 1):
        # an idle NEF on scene is recalled; an RTW with a patient aboard (FMS 5) is
        # left to deliver.
        async def no_sleep(_):
            return None

        mission = SimpleNamespace(id=5, caption="RD", mission_type_id=1, position=NEAR,
                                  patients_count=0, vehicle_state=1)
        nef = SimpleNamespace(id=10, type_id=29, is_available=False, position=BUILDING,
                              fms=4, target_type="mission", target_id=5)
        rtw = SimpleNamespace(id=11, type_id=28, is_available=False, position=BUILDING,
                              fms=5, target_type="mission", target_id=5)
        client = FakeClient([mission], [nef, rtw], [b()])
        runner = DispatchRunner(
            client, self.resolver, DispatchPolicy(radius_km=40, patient_scope=ModeScope.BOTH),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
        )
        asyncio.run(runner.run_cycle())
        self.assertEqual(client.recalled, [10])  # NEF home; transporting RTW kept

    def test_patient_only_recalls_en_route_once_confirmed_clear(self):
        # An on-scene NEF confirms 0 patients; an en-route (FMS 3) spare RTW at the
        # same mission is then recalled too (no wasted trip).
        async def no_sleep(_):
            return None

        mission = SimpleNamespace(id=5, caption="RD", mission_type_id=1, position=NEAR,
                                  patients_count=0, vehicle_state=1)
        nef = SimpleNamespace(id=10, type_id=29, is_available=False, position=BUILDING,
                              fms=4, target_type="mission", target_id=5)
        spare = SimpleNamespace(id=12, type_id=28, is_available=False, position=BUILDING,
                                fms=3, target_type="mission", target_id=5)
        client = FakeClient([mission], [nef, spare], [b()])
        runner = DispatchRunner(
            client, self.resolver, DispatchPolicy(radius_km=40, patient_scope=ModeScope.BOTH),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
        )
        asyncio.run(runner.run_cycle())
        self.assertEqual(sorted(client.recalled), [10, 12])

    def test_patient_only_keeps_units_until_someone_on_scene(self):
        # No unit on scene yet (only en route) -> count may be hidden -> DON'T
        # recall; units stick around until confirmed.
        async def no_sleep(_):
            return None

        mission = SimpleNamespace(id=5, caption="RD", mission_type_id=1, position=NEAR,
                                  patients_count=0, vehicle_state=1)
        enroute = SimpleNamespace(id=12, type_id=28, is_available=False, position=BUILDING,
                                  fms=3, target_type="mission", target_id=5)
        client = FakeClient([mission], [enroute], [b()])
        runner = DispatchRunner(
            client, self.resolver, DispatchPolicy(radius_km=40, patient_scope=ModeScope.BOTH),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
        )
        asyncio.run(runner.run_cycle())
        self.assertEqual(client.recalled, [])

    def test_patient_only_recall_never_touches_non_governed_vehicle_types(self):
        # Confirmed bug, fixed 2026-08-31 (Sasha): patient-only's recall must
        # ONLY ever touch vehicle types it itself would send (ambulances/NEF/
        # rescue_helicopter/incident-command). A fire truck someone manually
        # sent to the SAME now-cleared mission (unrelated reason) must be left
        # alone -- patient-only never governs it, so it never recalls it.
        async def no_sleep(_):
            return None

        mission = SimpleNamespace(id=5, caption="RD", mission_type_id=1, position=NEAR,
                                  patients_count=0, vehicle_state=1)
        nef = SimpleNamespace(id=10, type_id=29, is_available=False, position=BUILDING,
                              fms=4, target_type="mission", target_id=5)
        manual_lf = SimpleNamespace(id=13, type_id=0, is_available=False, position=BUILDING,
                                    fms=4, target_type="mission", target_id=5)
        client = FakeClient([mission], [nef, manual_lf], [b()])
        runner = DispatchRunner(
            client, self.resolver, DispatchPolicy(radius_km=40, patient_scope=ModeScope.BOTH),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
        )
        asyncio.run(runner.run_cycle())
        self.assertEqual(client.recalled, [10])  # NEF recalled; LF untouched

    def test_patient_recall_respects_true_ownership_for_a_shared_own_mission(self):
        # Sasha, 2026-09-14, code review: patient_scope=OWN must recall from
        # a mission Hilferuf itself shared just as readily as an unshared
        # one -- using the SAME ownership-aware classification
        # plan_dispatches uses, not the raw "has been shared" proxy (which
        # would otherwise leave a cleared unit stranded forever once the
        # mission it's on gets shared).
        async def no_sleep(_):
            return None

        # An unshared own mission establishes own_user_id=42 via inference.
        seed = SimpleNamespace(id=1, caption="seed", mission_type_id=1, position=NEAR,
                                is_alliance=False, user_id=42, vehicle_state=0,
                                patients_count=0)
        shared_but_mine = SimpleNamespace(id=5, caption="RD", mission_type_id=1, position=NEAR,
                                           is_alliance=True, user_id=42,
                                           patients_count=0, vehicle_state=1)
        nef = SimpleNamespace(id=10, type_id=29, is_available=False, position=BUILDING,
                              fms=4, target_type="mission", target_id=5)
        client = FakeClient([seed, shared_but_mine], [nef], [b()])
        runner = DispatchRunner(
            client, self.resolver, DispatchPolicy(radius_km=40, patient_scope=ModeScope.OWN),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
        )
        asyncio.run(runner.run_cycle())
        self.assertEqual(client.recalled, [10])

    def test_patient_recall_still_excludes_a_genuinely_alliance_mission(self):
        async def no_sleep(_):
            return None

        seed = SimpleNamespace(id=1, caption="seed", mission_type_id=1, position=NEAR,
                                is_alliance=False, user_id=42, vehicle_state=0,
                                patients_count=0)
        someone_elses = SimpleNamespace(id=5, caption="RD", mission_type_id=1, position=NEAR,
                                         is_alliance=True, user_id=999,
                                         patients_count=0, vehicle_state=1)
        nef = SimpleNamespace(id=10, type_id=29, is_available=False, position=BUILDING,
                              fms=4, target_type="mission", target_id=5)
        client = FakeClient([seed, someone_elses], [nef], [b()])
        runner = DispatchRunner(
            client, self.resolver, DispatchPolicy(radius_km=40, patient_scope=ModeScope.OWN),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
        )
        asyncio.run(runner.run_cycle())
        self.assertEqual(client.recalled, [])

    def test_cleanup_recalls_surplus_via_runner(self):
        # Two LFs assigned to a mission that needs one -> cleanup recalls the extra.
        async def no_sleep(_):
            return None

        m1 = m(1, 1)  # firetrucks:1
        v100 = SimpleNamespace(id=100, type_id=0, is_available=False, position=BUILDING,
                               building_id=1, fms=4, target_type="mission", target_id=1)
        v101 = SimpleNamespace(id=101, type_id=0, is_available=False, position=BUILDING,
                               building_id=1, fms=4, target_type="mission", target_id=1)
        client = FakeClient([m1], [v100, v101], [b()])
        # The on-scene roster cleanup() fetches confirms both LFs are really
        # there (everyone's roster, which here is only ever us) -- only 1 is
        # needed, so the other is surplus.
        client.on_scene_rosters[1] = [(100, 0), (101, 0)]
        runner = DispatchRunner(
            client, self.resolver, DispatchPolicy(radius_km=40),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
        )
        recalled = asyncio.run(runner.cleanup())
        self.assertEqual(recalled, 1)
        self.assertEqual(len(client.recalled), 1)

    def test_no_recall_without_patient_only(self):
        mission = SimpleNamespace(id=5, caption="RD", mission_type_id=1, position=NEAR,
                                  patients_count=0, vehicle_state=1)
        nef = SimpleNamespace(id=10, type_id=29, is_available=False, position=BUILDING,
                              fms=4, target_type="mission", target_id=5)
        client = FakeClient([mission], [nef], [b()])
        asyncio.run(self._runner(client).run_cycle())  # patient_only off
        self.assertEqual(client.recalled, [])

    # -- automatic recall (Sasha, 2026-10-27: D4) ---------------------------
    def test_automatic_recall_is_off_without_a_recall_cadence(self):
        # recall_cadence is optional -- omitting it must not fetch rosters
        # or recall anything, even for an obviously-surplus green mission.
        m1 = m(1, 1, status="green")
        v100 = SimpleNamespace(id=100, type_id=0, is_available=False, position=BUILDING,
                               building_id=1, fms=4, target_type="mission", target_id=1)
        client = FakeClient([m1], [v100], [b()])
        client.on_scene_rosters[1] = [(100, 0)]
        asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(client.recalled, [])
        self.assertEqual(client.on_scene_fetches, [])

    def test_automatic_recall_stops_en_route_on_first_sight_of_green(self):
        from app.domain.dispatch import RecallCadence

        m1 = m(1, 1, status="green")
        en_route = SimpleNamespace(id=100, type_id=0, is_available=False, position=BUILDING,
                                   building_id=1, fms=3, target_type="mission", target_id=1)
        client = FakeClient([m1], [en_route], [b()])
        cadence = RecallCadence()
        asyncio.run(self._runner(client, recall_cadence=cadence).run_cycle())
        self.assertEqual(client.recalled, [100])
        self.assertIn(1, cadence.fired_once)
        self.assertIn(1, cadence.last_checked)

    def test_automatic_recall_pulls_s4_surplus_once_everyone_is_counted(self):
        from app.domain.dispatch import RecallCadence

        m1 = m(1, 1, status="green")  # firetrucks:1
        v100 = SimpleNamespace(id=100, type_id=0, is_available=False, position=BUILDING,
                               building_id=1, fms=4, target_type="mission", target_id=1)
        v101 = SimpleNamespace(id=101, type_id=0, is_available=False, position=BUILDING,
                               building_id=1, fms=4, target_type="mission", target_id=1)
        client = FakeClient([m1], [v100, v101], [b()])
        client.on_scene_rosters[1] = [(100, 0), (101, 0)]  # both ours, confirmed on the page
        cadence = RecallCadence()
        asyncio.run(self._runner(client, recall_cadence=cadence).run_cycle())
        self.assertEqual(len(client.recalled), 1)
        self.assertIn(client.recalled[0], {100, 101})
        self.assertEqual(client.on_scene_fetches, [1])

    def test_automatic_recall_does_not_refire_within_15_minutes(self):
        from app.domain.dispatch import RecallCadence

        m1 = m(1, 1, status="green")
        v100 = SimpleNamespace(id=100, type_id=0, is_available=False, position=BUILDING,
                               building_id=1, fms=4, target_type="mission", target_id=1)
        v101 = SimpleNamespace(id=101, type_id=0, is_available=False, position=BUILDING,
                               building_id=1, fms=4, target_type="mission", target_id=1)
        client = FakeClient([m1], [v100, v101], [b()])
        client.on_scene_rosters[1] = [(100, 0), (101, 0)]
        cadence = RecallCadence()
        runner = self._runner(client, recall_cadence=cadence)
        asyncio.run(runner.run_cycle())
        self.assertEqual(len(client.recalled), 1)
        # A second cycle moments later: fired_once already covers it, and
        # last_checked is nowhere near 15 minutes old -- no re-fetch, no
        # second recall attempt.
        asyncio.run(runner.run_cycle())
        self.assertEqual(len(client.recalled), 1)
        self.assertEqual(client.on_scene_fetches, [1])

    def test_automatic_recall_leaves_a_mission_with_no_fetched_roster_untouched(self):
        # The roster fetch itself can legitimately come back empty (page
        # error, no table found) -- GameClient.fetch_on_scene_vehicles
        # already returns [] for that; the runner must not then treat it as
        # "confirmed nothing there" and recall anyway.
        from app.domain.dispatch import RecallCadence

        m1 = m(1, 1, status="green")
        v100 = SimpleNamespace(id=100, type_id=0, is_available=False, position=BUILDING,
                               building_id=1, fms=4, target_type="mission", target_id=1)
        client = FakeClient([m1], [v100], [b()])
        # client.on_scene_rosters left empty -> fetch_on_scene_vehicles returns []
        cadence = RecallCadence()
        asyncio.run(self._runner(client, recall_cadence=cadence).run_cycle())
        self.assertEqual(client.recalled, [])
        # Still marked checked/fired -- the cadence itself isn't stuck retrying
        # every cycle just because the fetch came back empty once.
        self.assertIn(1, cadence.fired_once)

    def test_hilferuf_shares_own_uncrewable_mission(self):
        # Own mission needs a firetruck; we only have police -> can't crew ->
        # with Hilferuf on, share it to the alliance. Idempotent per session.
        async def no_sleep(_):
            return None

        client = FakeClient([m(1, 1)], [v(100, 32)], [b()])  # need fire, only police
        runner = DispatchRunner(
            client, self.resolver,
            DispatchPolicy(radius_km=40, hilferuf_mode=HilferufAggressiveness.UNCREWABLE),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
        )
        asyncio.run(runner.run_cycle())
        self.assertEqual(client.shared, [1])
        asyncio.run(runner.run_cycle())
        self.assertEqual(client.shared, [1])  # not re-shared

    def test_hilferuf_off_does_not_share(self):
        client = FakeClient([m(1, 1)], [v(100, 32)], [b()])
        asyncio.run(self._runner(client).run_cycle())  # hilferuf off by default
        self.assertEqual(client.shared, [])

    def test_hilferuf_ignores_alliance_missions(self):
        async def no_sleep(_):
            return None

        # An alliance (already-shared) mission we can't crew is NOT ours to share.
        am = SimpleNamespace(id=2, caption="A", mission_type_id=1, position=NEAR,
                             is_alliance=True)
        client = FakeClient([am], [v(100, 32)], [b()])
        runner = DispatchRunner(
            client, self.resolver,
            DispatchPolicy(radius_km=40, hilferuf_mode=HilferufAggressiveness.UNCREWABLE),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
        )
        asyncio.run(runner.run_cycle())
        self.assertEqual(client.shared, [])

    def test_hilferuf_all_shares_own_mission_even_when_fully_crewed(self):
        # ALL mode (Sasha, 2026-09-11): share every own mission, not just
        # ones we can't crew -- this one dispatches fine (LF available) but
        # still gets shared so allies can pile on for extra credit.
        async def no_sleep(_):
            return None

        client = FakeClient([m(1, 1)], [v(100, 0)], [b()])  # LF -- fully crewable
        runner = DispatchRunner(
            client, self.resolver,
            DispatchPolicy(radius_km=40, hilferuf_mode=HilferufAggressiveness.ALL),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
        )
        asyncio.run(runner.run_cycle())
        self.assertEqual(client.shared, [1])
        self.assertEqual(client.dispatched, [(1, 100)])  # still dispatched normally too

    def test_hilferuf_all_ignores_alliance_missions(self):
        async def no_sleep(_):
            return None

        am = SimpleNamespace(id=2, caption="A", mission_type_id=1, position=NEAR,
                             is_alliance=True)
        client = FakeClient([am], [v(100, 0)], [b()])
        runner = DispatchRunner(
            client, self.resolver,
            DispatchPolicy(radius_km=40, hilferuf_mode=HilferufAggressiveness.ALL),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
        )
        asyncio.run(runner.run_cycle())
        self.assertEqual(client.shared, [])

    def test_hilferuf_min_credits_blocks_small_missions_even_under_all(self):
        # Sasha, 2026-10-07, quoting their alliance's own rule: "Kleineinsätze
        # sind nicht freizugeben" -- a floor that holds even under ALL mode,
        # which otherwise shares every own mission with no credit check at all.
        # Fixture type 1 is a 170-credit mission; floor it out at 200.
        async def no_sleep(_):
            return None

        client = FakeClient([m(1, 1)], [v(100, 0)], [b()])  # LF -- fully crewable
        runner = DispatchRunner(
            client, self.resolver,
            DispatchPolicy(radius_km=40, hilferuf_mode=HilferufAggressiveness.ALL,
                            hilferuf_min_credits=200),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
        )
        asyncio.run(runner.run_cycle())
        self.assertEqual(client.shared, [])
        self.assertEqual(client.dispatched, [(1, 100)])  # still dispatched normally

    def test_hilferuf_min_credits_lets_big_missions_through(self):
        # Same floor, but a mission worth enough still gets shared.
        async def no_sleep(_):
            return None

        client = FakeClient([m(1, 1)], [v(100, 0)], [b()])
        runner = DispatchRunner(
            client, self.resolver,
            DispatchPolicy(radius_km=40, hilferuf_mode=HilferufAggressiveness.ALL,
                            hilferuf_min_credits=100),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
        )
        asyncio.run(runner.run_cycle())
        self.assertEqual(client.shared, [1])

    def test_hilferuf_min_credits_also_applies_to_uncrewable_mode(self):
        # The floor is universal -- not just an ALL-mode carve-out.
        async def no_sleep(_):
            return None

        client = FakeClient([m(1, 1)], [v(100, 32)], [b()])  # need fire, only police
        runner = DispatchRunner(
            client, self.resolver,
            DispatchPolicy(radius_km=40, hilferuf_mode=HilferufAggressiveness.UNCREWABLE,
                            hilferuf_min_credits=200),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
        )
        asyncio.run(runner.run_cycle())
        self.assertEqual(client.shared, [])

    def test_hilferuf_min_credits_zero_means_no_floor(self):
        # Default (0) must not change any existing behavior.
        async def no_sleep(_):
            return None

        client = FakeClient([m(1, 1)], [v(100, 0)], [b()])
        runner = DispatchRunner(
            client, self.resolver,
            DispatchPolicy(radius_km=40, hilferuf_mode=HilferufAggressiveness.ALL,
                            hilferuf_min_credits=0),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
        )
        asyncio.run(runner.run_cycle())
        self.assertEqual(client.shared, [1])

    def test_hilferuf_min_credits_prefers_the_missions_own_value_over_the_type_average(self):
        # Code-review gap, 2026-10-07: every other hilferuf_min_credits test
        # uses m(), which never sets average_credits, so they only exercise
        # the mtype.average_credits fallback. Fixture type 1 averages 170
        # credits; a per-instance override to 500 must win over that, both
        # when it clears the floor and when the type's own average wouldn't.
        async def no_sleep(_):
            return None

        own_credits_mission = SimpleNamespace(id=1, caption="M1", mission_type_id=1,
                                              position=NEAR, status="unknown",
                                              vehicle_state=0, average_credits=500)
        client = FakeClient([own_credits_mission], [v(100, 0)], [b()])
        runner = DispatchRunner(
            client, self.resolver,
            # Floor (300) sits ABOVE the type's own 170-credit average but
            # BELOW this specific mission's 500 -- only passes if the
            # mission's own average_credits is what's actually checked.
            DispatchPolicy(radius_km=40, hilferuf_mode=HilferufAggressiveness.ALL,
                            hilferuf_min_credits=300),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
        )
        asyncio.run(runner.run_cycle())
        self.assertEqual(client.shared, [1])

    def test_transport_phase_sends_fms5_vehicles(self):
        # A vehicle in FMS 5 (has a patient) gets its first offered target hit.
        v5 = SimpleNamespace(id=77, type_id=28, is_available=False, position=BUILDING, fms=5)
        client = FakeClient([], [v5], [b()])
        asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(client.transports, ["/vehicles/77/patient/90077"])

    def test_transport_phase_sends_prisoner_fms5_vehicles_the_same_way(self):
        # TODO.md P4: sanity-check prisoner transport works the same as patient
        # transport. _handle_transports doesn't branch on href content at all —
        # a `gefangener` (prisoner) target is followed identically to a
        # `patient` one (app/game/client.py's _APPROACH regex already parses
        # both symmetrically, per tests/test_client_paging.py).
        class PrisonerClient(FakeClient):
            async def transport_targets(self, vehicle_id):
                return [f"/vehicles/{vehicle_id}/gefangener/500{vehicle_id}"]

        v5 = SimpleNamespace(id=88, type_id=32, is_available=False, position=BUILDING, fms=5)
        client = PrisonerClient([], [v5], [b()])
        asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(client.transports, ["/vehicles/88/gefangener/50088"])

    def test_no_transport_when_no_fms5(self):
        client = FakeClient([], [v(100, 0)], [b()])  # v() has no fms attr
        asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(client.transports, [])

    def test_buildings_cache_avoids_refetch(self):
        calls = {"n": 0}
        client = FakeClient([m(1, 1)], [v(100, 0)], [b()])
        orig = client.fetch_buildings

        async def counting():
            calls["n"] += 1
            return await orig()

        client.fetch_buildings = counting
        runner = DispatchRunner(
            client, self.resolver, DispatchPolicy(radius_km=40),
            buildings_cache=[b()],
            timing=RunnerTiming(between_dispatches=0, sleep=lambda _: asyncio.sleep(0)),
        )
        asyncio.run(runner.run_cycle())
        asyncio.run(runner.run_cycle())
        self.assertEqual(calls["n"], 0)  # cache used, never fetched

    # -- shortfall log ordering (Sasha, 2026-10-26) ------------------------

    def test_shortfall_log_lists_closest_to_complete_first(self):
        # Fixture types 2 (firetrucks:2, platform_trucks:1,
        # battalion_chief_vehicles:1 -> magnitude 4), 3 (firetrucks:1,
        # police_cars:1 -> magnitude 2), 1 (firetrucks:1 -> magnitude 1), all
        # with zero vehicles to crew them. Listed 1, 2, 3 (worst-to-best) so
        # the log's own order can't coincidentally match -- the fixed
        # first-N-in-plan-order cap used to just show whichever sorted first
        # by mission id, not whichever was most actionable.
        client = FakeClient([m(1, 2), m(2, 3), m(3, 1)], [], [b()])
        lines: list[str] = []
        asyncio.run(self._runner(client, on_log=lines.append).run_cycle())
        short_lines = [ln for ln in lines if ln.strip().startswith("short #")]
        ids_in_order = [int(ln.split("#", 1)[1].split()[0]) for ln in short_lines]
        self.assertEqual(ids_in_order, [3, 2, 1])  # magnitude 1, 2, 4


if __name__ == "__main__":
    unittest.main()
