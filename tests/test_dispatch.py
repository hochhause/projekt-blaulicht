import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.domain.assets import AssetLibrary
from app.domain.catalog import MissionCatalog
from app.domain.dispatch import DispatchOrder, DispatchPolicy, plan_dispatches
from app.domain.resolver import MissionRequirementResolver

BASE = Path(__file__).resolve().parent
VEHICLES_DIR = BASE.parent / "data" / "de" / "vehicles"
FIXTURE = BASE / "fixtures" / "einsaetze_sample.json"

# Fixture mission types (see einsaetze_sample.json):
#   1 -> firetrucks:1               (avg 170)
#   3 -> firetrucks:1, police_cars:1 (avg 320)
LF = 0     # firetrucks
HLF = 30   # firetrucks + heavy_rescue
FUSTW = 32  # police_cars

# A building and nearby/far missions (small coordinate deltas ~ a few km).
BUILDING = (52.5200, 13.4050)
NEAR = (52.5300, 13.4100)   # ~1.2 km
NEAR2 = (52.5250, 13.4000)  # ~0.6 km
FAR = (53.5500, 9.9900)     # Hamburg, ~250 km


def mission(mid, mtype, pos):
    return SimpleNamespace(id=mid, caption=f"M{mid}", mission_type_id=mtype, position=pos)


def vehicle(vid, type_id, pos=BUILDING, available=True):
    return SimpleNamespace(id=vid, type_id=type_id, is_available=available, position=pos)


def building(pos=BUILDING):
    return SimpleNamespace(position=pos)


class DispatchPlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        library = AssetLibrary.load(VEHICLES_DIR)
        catalog = MissionCatalog.from_payload(json.loads(FIXTURE.read_text(encoding="utf-8")))
        cls.resolver = MissionRequirementResolver(catalog, library)

    def plan(self, missions, vehicles, policy=None, assigned=None):
        return plan_dispatches(
            missions, vehicles, [building()], self.resolver,
            policy or DispatchPolicy(radius_km=40), assigned=assigned,
        )

    def test_simple_dispatch(self):
        plan = self.plan([mission(1, 1, NEAR)], [vehicle(100, LF)])
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].vehicle_ids, (100,))
        self.assertTrue(plan.planned[0].satisfied)

    def test_out_of_range_skipped(self):
        plan = self.plan([mission(1, 1, FAR)], [vehicle(100, LF)])
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "out of range")])

    def test_credit_floor_skips_cheap_mission(self):
        # type 1 avg 170 < floor 300 -> skipped.
        plan = plan_dispatches(
            [mission(1, 1, NEAR)], [vehicle(100, LF)], [building()],
            self.resolver, DispatchPolicy(radius_km=40, credit_floor=300),
        )
        self.assertEqual(plan.planned, [])
        self.assertTrue(plan.skipped[0][1].startswith("below credit floor"))

    def test_pool_consumed_across_missions(self):
        # Two firetruck missions, only one LF -> nearest gets it, other short.
        missions = [mission(1, 1, NEAR), mission(2, 1, NEAR2)]  # m2 is nearer
        plan = self.plan(missions, [vehicle(100, LF)])
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].mission_id, 2)  # nearest first
        self.assertEqual(plan.skipped, [(1, "no available vehicles for requirements")])

    def test_multi_role_hlf_used_across_requirements(self):
        # type 3 needs firetrucks:1 + police_cars:1. HLF covers firetrucks; a
        # FuStW covers police. Two vehicles, HLF is the firetruck.
        plan = self.plan([mission(1, 3, NEAR)], [vehicle(100, HLF), vehicle(101, FUSTW)])
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(set(plan.planned[0].vehicle_ids), {100, 101})
        self.assertTrue(plan.planned[0].satisfied)

    def test_delta_already_covered_is_skipped(self):
        # A firetruck already en route to mission 1 (type 1) -> nothing to do.
        plan = self.plan([mission(1, 1, NEAR)], [vehicle(100, LF)], assigned={1: [LF]})
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "already covered")])

    def test_max_vehicles_cap_skips_big_mission(self):
        plan = plan_dispatches(
            [mission(1, 3, NEAR)], [vehicle(100, LF), vehicle(101, FUSTW)], [building()],
            self.resolver, DispatchPolicy(radius_km=40, max_vehicles_per_mission=1),
        )
        self.assertEqual(plan.planned, [])
        self.assertTrue(plan.skipped[0][1].startswith("needs 2 > cap 1"))

    def test_unavailable_vehicles_ignored(self):
        plan = self.plan([mission(1, 1, NEAR)], [vehicle(100, LF, available=False)])
        self.assertEqual(plan.planned, [])

    def test_order_credits_high_first(self):
        # type 1 avg 170, type 3 avg 320. Give each its own LF so both plan.
        missions = [mission(1, 1, NEAR), mission(2, 3, NEAR2)]
        vehicles = [vehicle(100, LF), vehicle(101, LF), vehicle(102, FUSTW)]
        plan = plan_dispatches(
            missions, vehicles, [building()], self.resolver,
            DispatchPolicy(radius_km=40, order=DispatchOrder.CREDITS_HIGH),
        )
        # The 320-credit mission (id 2) is planned before the 170 one.
        self.assertEqual(plan.planned[0].mission_id, 2)

    def test_batch_size_limits_missions(self):
        missions = [mission(i, 1, NEAR) for i in range(1, 6)]  # 5 firetruck missions
        vehicles = [vehicle(100 + i, LF) for i in range(5)]
        plan = plan_dispatches(
            missions, vehicles, [building()], self.resolver,
            DispatchPolicy(radius_km=40, batch_size=2),
        )
        self.assertEqual(len(plan.planned), 2)
        self.assertIn((3, "beyond batch size"), [(mid, r) for mid, r in plan.skipped])

    def test_category_filter(self):
        # type 1 is category "fire"; only allow "police" -> filtered out.
        plan = plan_dispatches(
            [mission(1, 1, NEAR)], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, allowed_categories=frozenset({"police"})),
        )
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "category filtered")])

    def test_category_filter_allows_matching(self):
        plan = plan_dispatches(
            [mission(1, 1, NEAR)], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, allowed_categories=frozenset({"fire"})),
        )
        self.assertEqual(len(plan.planned), 1)

    def test_vehicle_position_derived_from_building(self):
        # Vehicles carry no coordinates (real API); distance uses their building.
        near_b = SimpleNamespace(id=1, position=NEAR)
        far_b = SimpleNamespace(id=2, position=(52.90, 13.90))
        v_near = SimpleNamespace(id=100, type_id=LF, is_available=True, position=None, building_id=1)
        v_far = SimpleNamespace(id=101, type_id=LF, is_available=True, position=None, building_id=2)
        plan = plan_dispatches(
            [mission(1, 1, NEAR)], [v_far, v_near], [near_b, far_b], self.resolver,
            DispatchPolicy(radius_km=200),
        )
        # The LF at the nearer building is chosen despite list order.
        self.assertEqual(plan.planned[0].vehicle_ids, (100,))


if __name__ == "__main__":
    unittest.main()
