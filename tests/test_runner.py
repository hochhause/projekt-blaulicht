import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.core.runner import DispatchRunner, RunnerTiming
from app.domain.assets import AssetLibrary
from app.domain.catalog import MissionCatalog
from app.domain.dispatch import DispatchPolicy, ModeScope
from app.domain.resolver import MissionRequirementResolver

BASE = Path(__file__).resolve().parent
VEHICLES_DIR = BASE.parent / "data" / "de" / "vehicles"
FIXTURE = BASE / "fixtures" / "einsaetze_sample.json"

BUILDING = (52.5200, 13.4050)
NEAR = (52.5300, 13.4100)


class FakeClient:
    """Records dispatch calls; serves canned reads. No network."""

    def __init__(self, missions, vehicles, buildings):
        self._missions, self._vehicles, self._buildings = missions, vehicles, buildings
        self.dispatched: list[tuple[int, int]] = []
        self.transports: list[tuple[str, int, int]] = []
        self.shared: list[int] = []
        self.recalled: list[int] = []

    async def fetch_missions(self):
        return self._missions

    async def fetch_vehicles(self, cap: int = 0):
        return self._vehicles

    async def fetch_buildings(self):
        return self._buildings

    async def dispatch(self, mission_id, vehicle_id):
        self.dispatched.append((mission_id, vehicle_id))
        return True

    async def dispatch_many(self, mission_id, vehicle_ids):
        for vid in vehicle_ids:
            self.dispatched.append((mission_id, vid))
        return True

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


def m(mid, mtype, pos=NEAR, status="unknown"):
    return SimpleNamespace(id=mid, caption=f"M{mid}", mission_type_id=mtype, position=pos,
                            status=status)


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

    def _runner(self, client):
        # No-op sleep so the test is instant.
        async def no_sleep(_):
            return None

        return DispatchRunner(
            client, self.resolver, DispatchPolicy(radius_km=40),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
        )

    def test_cycle_dispatches_planned_vehicles(self):
        client = FakeClient([m(1, 1)], [v(100, 0)], [b()])  # type 1 -> firetrucks:1, LF=0
        report = asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(client.dispatched, [(1, 100)])
        self.assertEqual(report.dispatched, 1)
        self.assertEqual(report.failed, 0)

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

    def test_cycle_with_nothing_to_do(self):
        client = FakeClient([], [v(100, 0)], [b()])
        report = asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(client.dispatched, [])
        self.assertEqual(report.dispatched, 0)

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
            return False

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
            return True

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
            return False

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

    def test_hilferuf_shares_own_uncrewable_mission(self):
        # Own mission needs a firetruck; we only have police -> can't crew ->
        # with Hilferuf on, share it to the alliance. Idempotent per session.
        async def no_sleep(_):
            return None

        client = FakeClient([m(1, 1)], [v(100, 32)], [b()])  # need fire, only police
        runner = DispatchRunner(
            client, self.resolver, DispatchPolicy(radius_km=40, hilferuf=True),
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
            client, self.resolver, DispatchPolicy(radius_km=40, hilferuf=True),
            timing=RunnerTiming(between_dispatches=0, sleep=no_sleep),
        )
        asyncio.run(runner.run_cycle())
        self.assertEqual(client.shared, [])

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


if __name__ == "__main__":
    unittest.main()
