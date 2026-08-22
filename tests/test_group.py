import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.domain.assets import AssetLibrary
from app.domain.catalog import MissionCatalog
from app.domain.group import GroupPolicy, RadiusBasis, plan_group_dispatches


def _by_account(plan):
    """{account_id: {mission_id: vehicle_ids}} view of a GroupPlan."""
    out: dict = {}
    for gmp in plan.missions:
        for aid, ids in gmp.by_account.items():
            out.setdefault(aid, {})[gmp.mission_id] = ids
    return out
from app.domain.resolver import MissionRequirementResolver

BASE = Path(__file__).resolve().parent
VEHICLES_DIR = BASE.parent / "data" / "de" / "vehicles"
FIXTURE = BASE / "fixtures" / "einsaetze_sample.json"

BUILDING = (52.5200, 13.4050)
NEAR = (52.5300, 13.4100)
FAR = (53.5500, 9.9900)

LF = 0
FUSTW = 32


def mission(mid, mtype, pos=NEAR, is_alliance=True, average_credits=0, vehicle_state=0):
    return SimpleNamespace(id=mid, caption=f"M{mid}", mission_type_id=mtype, position=pos,
                           is_alliance=is_alliance, average_credits=average_credits,
                           vehicle_state=vehicle_state, patients_count=0,
                           possible_patients_count=0, prisoners_count=0,
                           possible_prisoners_count=0)


def vehicle(vid, type_id, building_id=1, fms=2, target_type=None, target_id=None):
    return SimpleNamespace(id=vid, type_id=type_id, is_available=(fms in (1, 2)),
                           position=None, building_id=building_id, fms=fms,
                           target_type=target_type, target_id=target_id)


def building(bid=1, pos=BUILDING):
    return SimpleNamespace(id=bid, position=pos)


class GroupPlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        library = AssetLibrary.load(VEHICLES_DIR)
        catalog = MissionCatalog.from_payload(json.loads(FIXTURE.read_text(encoding="utf-8")))
        cls.resolver = MissionRequirementResolver(catalog, library)

    def plan(self, missions, vehicles_by_account, buildings_by_account=None, policy=None):
        if buildings_by_account is None:
            buildings_by_account = {aid: [building(aid)] for aid in vehicles_by_account}
        return plan_group_dispatches(
            missions, vehicles_by_account, buildings_by_account, self.resolver,
            policy or GroupPolicy(radius_km=40),
        )

    def test_both_accounts_participate_in_one_mission(self):
        # type 1 needs one firetruck. Account A crews it; B must still tag along
        # (participation) so both earn the reward.
        plan = self.plan(
            [mission(1, 1)],
            {10: [vehicle(100, LF)], 20: [vehicle(200, LF)]},
        )
        ba = _by_account(plan)
        self.assertEqual(ba.get(10, {}).get(1), (100,))
        self.assertEqual(ba.get(20, {}).get(1), (200,))  # B's participation token

    def test_no_over_send_global_delta(self):
        # A already has a firetruck en route to mission 1 -> requirement met by the
        # global delta; A doesn't re-send, but B still participates.
        plan = self.plan(
            [mission(1, 1)],
            {
                10: [vehicle(101, LF, target_type="mission", target_id=1)],  # en route
                20: [vehicle(200, LF)],
            },
        )
        ba = _by_account(plan)
        self.assertNotIn(10, ba)                 # A not re-dispatched
        self.assertEqual(ba[20][1], (200,))      # B tags along

    def test_combined_pool_crews_mission_across_accounts(self):
        # type 3 needs firetrucks:1 + police_cars:1; A has only the LF, B only the
        # FuStW -> the group crews it together.
        plan = self.plan(
            [mission(1, 3)],
            {10: [vehicle(100, LF)], 20: [vehicle(200, FUSTW)]},
        )
        ba = _by_account(plan)
        self.assertEqual(set(ba.get(10, {}).get(1, ())), {100})
        self.assertEqual(set(ba.get(20, {}).get(1, ())), {200})

    def test_participation_off_only_solver_sends(self):
        plan = self.plan(
            [mission(1, 1)],
            {10: [vehicle(100, LF)], 20: [vehicle(200, LF)]},
            policy=GroupPolicy(radius_km=40, participation=False),
        )
        ba = _by_account(plan)
        self.assertIn(10, ba)
        self.assertNotIn(20, ba)  # no token when participation off

    def test_takes_from_account_with_most_spare(self):
        # Needs one firetruck; A holds 3 LFs, B holds 1 -> take from A (balance).
        plan = self.plan(
            [mission(1, 1)],
            {10: [vehicle(100, LF), vehicle(101, LF), vehicle(102, LF)], 20: [vehicle(200, LF)]},
            policy=GroupPolicy(radius_km=40, participation=False),
        )
        ba = _by_account(plan)
        self.assertIn(10, ba)       # solver drawn from the account with the most spare
        self.assertNotIn(20, ba)

    def test_out_of_range_all_basis(self):
        # ANY-basis: in range if any account is near. ALL-basis needs both near.
        near = {10: [building(10, BUILDING)]}
        far = {20: [building(20, FAR)]}
        buildings = {**near, **far}
        vehicles = {10: [vehicle(100, LF, building_id=10)], 20: [vehicle(200, LF, building_id=20)]}
        any_plan = plan_group_dispatches([mission(1, 1)], vehicles, buildings, self.resolver,
                                         GroupPolicy(radius_km=40, radius_basis=RadiusBasis.ANY))
        self.assertTrue(any_plan.missions)  # A is near -> worked
        all_plan = plan_group_dispatches([mission(1, 1)], vehicles, buildings, self.resolver,
                                         GroupPolicy(radius_km=40, radius_basis=RadiusBasis.ALL))
        self.assertEqual(all_plan.missions, [])  # B far -> not worked
        self.assertEqual(all_plan.skipped, [(1, "out of range")])

    def test_token_cap_limits_participation(self):
        # Two missions, cap 1 token/account -> B only tokens the first.
        plan = self.plan(
            [mission(1, 1), mission(2, 1, pos=(52.531, 13.411))],
            {10: [vehicle(100, LF), vehicle(101, LF)],
             20: [vehicle(200, LF), vehicle(201, LF)]},
            policy=GroupPolicy(radius_km=40, max_token_per_account=1),
        )
        ba = _by_account(plan)
        self.assertEqual(len(ba.get(20, {})), 1)  # capped at one token


class FakeGroupClient:
    def __init__(self, missions, vehicles, buildings):
        self._m, self._v, self._b = missions, vehicles, buildings
        self.dispatched: list = []
        self.shared: list = []

    async def fetch_vehicles(self, cap=0):
        return self._v

    async def fetch_missions(self):
        return self._m

    async def fetch_buildings(self):
        return self._b

    async def dispatch_many(self, mid, ids):
        self.dispatched.append((mid, tuple(ids)))
        return True

    async def share_mission(self, mid):
        self.shared.append(mid)
        return True


class FakeMember:
    def __init__(self, client, resolver, vehicle_cap=0, transport_enabled=False):
        self.client = client
        self.resolver = resolver
        self.vehicle_cap = vehicle_cap
        self.transport_enabled = transport_enabled
        self.closed = False

    async def connect(self):
        return None

    async def handle_transports(self, vehicles):
        return 0

    async def aclose(self):
        self.closed = True


class GroupCoordinatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        library = AssetLibrary.load(VEHICLES_DIR)
        catalog = MissionCatalog.from_payload(json.loads(FIXTURE.read_text(encoding="utf-8")))
        cls.resolver = MissionRequirementResolver(catalog, library)

    def _run(self, coord):
        import asyncio

        async def no_sleep(_):
            return None

        coord._sleep = no_sleep
        return asyncio.run(coord.run_cycle())

    def test_both_members_dispatch_to_shared_mission(self):
        from app.core.group_coordinator import GroupCoordinator
        from app.domain.group import GroupPolicy

        m1 = mission(1, 1, is_alliance=True)  # shared, both see it
        ca = FakeGroupClient([m1], [vehicle(100, LF, building_id=10)], [building(10)])
        cb = FakeGroupClient([m1], [vehicle(200, LF, building_id=20)], [building(20)])
        coord = GroupCoordinator(
            {10: FakeMember(ca, self.resolver), 20: FakeMember(cb, self.resolver)},
            GroupPolicy(radius_km=40),
        )
        self._run(coord)
        self.assertEqual(ca.dispatched[0][0], 1)
        self.assertEqual(cb.dispatched[0][0], 1)  # participation token

    def test_shares_own_missions(self):
        from app.core.group_coordinator import GroupCoordinator
        from app.domain.group import GroupPolicy

        m1 = mission(1, 1, is_alliance=False)  # own -> should be shared
        ca = FakeGroupClient([m1], [vehicle(100, LF, building_id=10)], [building(10)])
        coord = GroupCoordinator(
            {10: FakeMember(ca, self.resolver)},
            GroupPolicy(radius_km=40, participation=False),
        )
        self._run(coord)
        self.assertIn(1, ca.shared)

    def test_no_double_send_when_already_en_route(self):
        from app.core.group_coordinator import GroupCoordinator
        from app.domain.group import GroupPolicy

        m1 = mission(1, 1, is_alliance=True)
        # A already en route (delta covers it); B participates.
        ca = FakeGroupClient([m1], [vehicle(101, LF, building_id=10, target_type="mission", target_id=1)], [building(10)])
        cb = FakeGroupClient([m1], [vehicle(200, LF, building_id=20)], [building(20)])
        coord = GroupCoordinator(
            {10: FakeMember(ca, self.resolver), 20: FakeMember(cb, self.resolver)},
            GroupPolicy(radius_km=40),
        )
        self._run(coord)
        self.assertEqual(ca.dispatched, [])          # A not re-sent
        self.assertEqual(cb.dispatched[0][0], 1)     # B tags along


if __name__ == "__main__":
    unittest.main()
