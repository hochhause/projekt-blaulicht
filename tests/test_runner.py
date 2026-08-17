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

    async def fetch_missions(self):
        return self._missions

    async def fetch_vehicles(self):
        return self._vehicles

    async def fetch_buildings(self):
        return self._buildings

    async def dispatch(self, mission_id, vehicle_id):
        self.dispatched.append((mission_id, vehicle_id))
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

    def test_failed_dispatch_counted(self):
        client = FakeClient([m(1, 1)], [v(100, 0)], [b()])

        async def fail(mission_id, vehicle_id):
            return False

        client.dispatch = fail
        report = asyncio.run(self._runner(client).run_cycle())
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.dispatched, 0)

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
