import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.core.runner import DispatchRunner, RunnerTiming
from app.domain.assets import AssetLibrary
from app.domain.catalog import MissionCatalog
from app.domain.dispatch import DispatchPolicy
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

    async def fetch_missions(self):
        return self._missions

    async def fetch_vehicles(self, cap: int = 0):
        return self._vehicles

    async def fetch_buildings(self):
        return self._buildings

    async def dispatch(self, mission_id, vehicle_id):
        self.dispatched.append((mission_id, vehicle_id))
        return True

    async def transport_targets(self, vehicle_id):
        # one hospital offered per vehicle
        return [f"/vehicles/{vehicle_id}/patient/900{vehicle_id}"]

    async def send_transport(self, action_url):
        self.transports.append(action_url)
        return True


def m(mid, mtype, pos=NEAR):
    return SimpleNamespace(id=mid, caption=f"M{mid}", mission_type_id=mtype, position=pos)


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

        async def fail(mission_id, vehicle_id):
            return False

        client.dispatch = fail
        report = asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.dispatched, 0)

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

    def test_transport_phase_sends_fms5_vehicles(self):
        # A vehicle in FMS 5 (has a patient) gets its first offered target hit.
        v5 = SimpleNamespace(id=77, type_id=28, is_available=False, position=BUILDING, fms=5)
        client = FakeClient([], [v5], [b()])
        asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(client.transports, ["/vehicles/77/patient/90077"])

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
