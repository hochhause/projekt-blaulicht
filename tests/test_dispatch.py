import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.domain.assets import AssetLibrary
from app.domain.catalog import MissionCatalog
from app.domain.dispatch import (
    DispatchOrder,
    DispatchPolicy,
    MissionSource,
    assigned_from_vehicles,
    plan_dispatches,
)
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


def mission(mid, mtype, pos, is_alliance=False, average_credits=0, created_at=None, vehicle_state=0):
    return SimpleNamespace(
        id=mid, caption=f"M{mid}", mission_type_id=mtype, position=pos,
        is_alliance=is_alliance, average_credits=average_credits, created_at=created_at,
        vehicle_state=vehicle_state, missing_text="",
    )


def vehicle(vid, type_id, pos=BUILDING, available=True):
    return SimpleNamespace(id=vid, type_id=type_id, is_available=available, position=pos)


def building(pos=BUILDING):
    return SimpleNamespace(position=pos)


class DispatchPlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        library = AssetLibrary.load(VEHICLES_DIR)
        cls.lib_for_engine = library
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

    def test_own_credit_floor_skips_cheap_mission(self):
        plan = plan_dispatches(
            [mission(1, 1, NEAR, average_credits=170)], [vehicle(100, LF)], [building()],
            self.resolver, DispatchPolicy(radius_km=40, own_credit_min=300),
        )
        self.assertEqual(plan.planned, [])
        self.assertTrue(plan.skipped[0][1].startswith("below credit floor"))

    def test_own_credit_ceiling_skips_expensive_mission(self):
        plan = plan_dispatches(
            [mission(1, 1, NEAR, average_credits=5000)], [vehicle(100, LF)], [building()],
            self.resolver, DispatchPolicy(radius_km=40, own_credit_max=1000),
        )
        self.assertEqual(plan.planned, [])
        self.assertTrue(plan.skipped[0][1].startswith("above credit ceiling"))

    def test_credit_bounds_are_per_source(self):
        # alliance min 1000 blocks a 500-credit alliance mission, but the same
        # 500 own mission passes (own has no min).
        alliance_m = mission(1, 1, NEAR, is_alliance=True, average_credits=500)
        own_m = mission(2, 1, NEAR2, is_alliance=False, average_credits=500)
        policy = DispatchPolicy(radius_km=40, alliance_credit_min=1000)
        plan = plan_dispatches([alliance_m, own_m], [vehicle(100, LF), vehicle(101, LF)],
                               [building()], self.resolver, policy)
        ids = [mp.mission_id for mp in plan.planned]
        self.assertIn(2, ids)       # own passed
        self.assertNotIn(1, ids)    # alliance blocked by its own min

    def test_alliance_age_window(self):
        now = 1_000_000.0
        # shared 30s ago; min age 60s -> too recent.
        recent = mission(1, 1, NEAR, is_alliance=True)
        recent.alliance_shared_at = now - 30
        policy = DispatchPolicy(radius_km=40, alliance_min_age_seconds=60)
        plan = plan_dispatches([recent], [vehicle(100, LF)], [building()],
                               self.resolver, policy, now=now)
        self.assertEqual(plan.skipped, [(1, "shared too recently")])

    def test_missing_text_fallback_when_catalog_empty(self):
        # Unknown mission type (999 not in fixture catalog) but missing_text
        # names an RTW -> the regex engine resolves it and we dispatch.
        from app.domain.requirements import RequirementEngine
        engine = RequirementEngine(self.lib_for_engine)
        m = mission(1, 999, NEAR)
        m.missing_text = "noch benötigt: 1 RTW"
        plan = plan_dispatches([m], [SimpleNamespace(id=100, type_id=28, is_available=True,
                                                     position=BUILDING, building_id=1)],
                               [building()], self.resolver, DispatchPolicy(radius_km=40),
                               text_engine=engine)
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].vehicle_ids, (100,))  # RTW (type 28)

    def test_pool_consumed_across_missions(self):
        # Two firetruck missions, only one LF -> nearest gets it, other short.
        missions = [mission(1, 1, NEAR), mission(2, 1, NEAR2)]  # m2 is nearer
        plan = self.plan(missions, [vehicle(100, LF)])
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].mission_id, 2)  # nearest first
        self.assertEqual(plan.skipped, [(1, "not enough vehicles: short firetrucks x1")])

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

    def test_assigned_from_vehicles(self):
        vehicles = [
            SimpleNamespace(id=1, type_id=LF, target_type="mission", target_id=555),
            SimpleNamespace(id=2, type_id=FUSTW, target_type="mission", target_id=555),
            SimpleNamespace(id=3, type_id=LF, target_type=None, target_id=None),      # idle
            SimpleNamespace(id=4, type_id=LF, target_type="building", target_id=999),  # returning
        ]
        self.assertEqual(assigned_from_vehicles(vehicles), {555: [LF, FUSTW]})

    def test_delta_from_en_route_vehicle_prevents_redispatch(self):
        # Mission 1 (firetrucks:1) already has an LF en route (target_type=mission)
        # plus a spare idle LF -> the spare is NOT sent.
        enroute = SimpleNamespace(id=1, type_id=LF, is_available=False,
                                  position=BUILDING, building_id=1,
                                  target_type="mission", target_id=1)
        spare = vehicle(2, LF)
        assigned = assigned_from_vehicles([enroute, spare])
        plan = self.plan([mission(1, 1, NEAR)], [enroute, spare], assigned=assigned)
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

    # -- parasite mode --------------------------------------------------------
    def test_parasite_leeches_shared_mission_with_spare(self):
        # A shared mission others are on (vehicle_state=1), type our fleet can't
        # usefully fill (needs police, we only have an LF) -> send a spare LF to
        # claim participation. Filters/cash bypassed.
        para = mission(1, 32, FAR, is_alliance=True, vehicle_state=1)  # far, out of normal range
        # type 32 doesn't exist as a mission type in fixture -> resolve empty ->
        # but parasite still leeches. Use a real fixture type needing police.
        para = mission(1, 3, FAR, is_alliance=True, average_credits=99999, vehicle_state=1)
        plan = plan_dispatches(
            [para], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_mode=True, own_credit_max=1000),
        )
        # Useful (LF covers firetrucks part) OR leech — either way it's actioned,
        # despite being far and over the cash cap.
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].mission_id, 1)

    def test_parasite_off_leaves_shared_mission_to_filters(self):
        para = mission(1, 3, FAR, is_alliance=True, vehicle_state=1)
        plan = plan_dispatches(
            [para], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_mode=False),
        )
        self.assertEqual(plan.planned, [])  # out of range, not leeched

    def test_parasite_ignores_shared_mission_nobody_on(self):
        # vehicle_state=0 -> nobody contributed yet -> not a leech target.
        para = mission(1, 3, NEAR, is_alliance=True, vehicle_state=0)
        plan = plan_dispatches(
            [para], [vehicle(100, LF), vehicle(101, FUSTW)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_mode=True, mission_source=MissionSource.OWN),
        )
        # mission_source OWN filters it out; parasite didn't grab it (vehicle_state 0)
        self.assertEqual(plan.planned, [])

    def test_spread_mission_topped_up_first(self):
        # We're already on mission 1 (assigned an LF) but it still needs police;
        # priority pass tops it up before the batch even with batch_size 1.
        m1 = mission(1, 3, NEAR)  # firetrucks + police; LF en route, needs FuStW
        plan = plan_dispatches(
            [m1], [vehicle(200, FUSTW)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, batch_size=1), assigned={1: [LF]},
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].vehicle_ids, (200,))  # FuStW tops it up

    def test_shortfall_strikes_and_then_skips(self):
        from app.domain.strikes import StrikeTracker

        strikes = StrikeTracker()
        # A firetruck mission with NO firetruck available -> shortfall each time.
        def one_cycle():
            return plan_dispatches(
                [mission(1, 1, NEAR)], [vehicle(100, FUSTW)], [building()], self.resolver,
                DispatchPolicy(radius_km=40), strikes=strikes, now=1000.0,
            )

        # First 3 cycles: "not enough vehicles" (each records a strike).
        for _ in range(3):
            plan = one_cycle()
            self.assertTrue(plan.skipped[0][1].startswith("not enough vehicles"))
        # 4th: firetrucks is struck out -> skipped before even trying.
        plan = one_cycle()
        self.assertTrue(plan.skipped[0][1].startswith("needs struck-out"))

    def test_partial_coverage_is_not_dispatched(self):
        # Mission needs firetrucks + police; only a firetruck is available ->
        # not fully crewable -> skipped, nothing sent.
        plan = plan_dispatches(
            [mission(1, 3, NEAR)], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40),
        )
        self.assertEqual(plan.planned, [])
        self.assertTrue(plan.skipped[0][1].startswith("not enough vehicles"))

    def test_skipped_missions_do_not_consume_batch_slot(self):
        # batch_size=1. Mission A (processed first, nearest) is already covered
        # -> skipped, no slot used. Mission B then still gets dispatched.
        a = mission(1, 1, NEAR2)  # nearest -> first
        b = mission(2, 1, NEAR)
        plan = plan_dispatches(
            [a, b], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, batch_size=1), assigned={1: [LF]},
        )
        self.assertEqual([mp.mission_id for mp in plan.planned], [2])
        self.assertIn((1, "already covered"), plan.skipped)

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

    def test_alliance_mission_worked_by_default(self):
        plan = self.plan([mission(1, 1, NEAR, is_alliance=True)], [vehicle(100, LF)])
        self.assertEqual(len(plan.planned), 1)

    def test_own_only_filter_skips_alliance(self):
        plan = plan_dispatches(
            [mission(1, 1, NEAR, is_alliance=True)], [vehicle(100, LF)], [building()],
            self.resolver, DispatchPolicy(radius_km=40, mission_source=MissionSource.OWN),
        )
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "own-only filter")])

    def test_alliance_only_filter_skips_own(self):
        plan = plan_dispatches(
            [mission(1, 1, NEAR, is_alliance=False)], [vehicle(100, LF)], [building()],
            self.resolver, DispatchPolicy(radius_km=40, mission_source=MissionSource.ALLIANCE),
        )
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "alliance-only filter")])

    def test_order_oldest_first(self):
        missions = [
            mission(1, 1, NEAR, created_at="2026-08-17T10:00:00"),
            mission(2, 1, NEAR2, created_at="2026-08-17T08:00:00"),  # older
        ]
        vehicles = [vehicle(100, LF), vehicle(101, LF)]
        plan = plan_dispatches(
            missions, vehicles, [building()], self.resolver,
            DispatchPolicy(radius_km=40, order=DispatchOrder.OLDEST),
        )
        self.assertEqual(plan.planned[0].mission_id, 2)  # oldest first

    def test_order_newest_first(self):
        missions = [
            mission(1, 1, NEAR, created_at="2026-08-17T08:00:00"),
            mission(2, 1, NEAR2, created_at="2026-08-17T10:00:00"),  # newer
        ]
        vehicles = [vehicle(100, LF), vehicle(101, LF)]
        plan = plan_dispatches(
            missions, vehicles, [building()], self.resolver,
            DispatchPolicy(radius_km=40, order=DispatchOrder.NEWEST),
        )
        self.assertEqual(plan.planned[0].mission_id, 2)  # newest first

    def test_alliance_radius_applied(self):
        # Mission ~1.2 km away, own radius 40 (in range) but alliance radius 0.5
        # (out of range) -> alliance mission skipped for distance.
        plan = plan_dispatches(
            [mission(1, 1, NEAR, is_alliance=True)], [vehicle(100, LF)], [building()],
            self.resolver, DispatchPolicy(radius_km=40, alliance_radius_km=0.5),
        )
        self.assertEqual(plan.skipped, [(1, "out of range")])

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
