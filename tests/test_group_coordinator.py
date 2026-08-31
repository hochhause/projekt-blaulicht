"""GroupCoordinator.run_cycle: share-before-plan ordering (TODO.md P3.1).

Fakes stand in for AccountRunner/GameClient — no network, no httpx dependency.
An AllianceBoard simulates the alliance feed shared missions land in, so a
fake client's fetch_missions() can reflect what another account just shared.
"""

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.core.group_coordinator import GroupCoordinator
from app.domain.assets import AssetLibrary
from app.domain.catalog import MissionCatalog
from app.domain.group import GroupPolicy
from app.domain.resolver import MissionRequirementResolver

BASE = Path(__file__).resolve().parent
VEHICLES_DIR = BASE.parent / "data" / "de" / "vehicles"
FIXTURE = BASE / "fixtures" / "einsaetze_sample.json"

LF = 0  # firetrucks (fixture mission type 1 needs firetrucks:1)


def _mission(mid, average_credits=200):
    return SimpleNamespace(
        id=mid, caption=f"M{mid}", mission_type_id=1, position=(52.5200, 13.4050),
        is_alliance=False, average_credits=average_credits, vehicle_state=0,
        patients_count=0, possible_patients_count=0, prisoners_count=0,
        possible_prisoners_count=0, missing_text="",
    )


def _vehicle(vid, type_id=LF, building_id=1, fms=2):
    return SimpleNamespace(id=vid, type_id=type_id, is_available=(fms in (1, 2)),
                            position=None, building_id=building_id, fms=fms,
                            target_type=None, target_id=None)


def _building(bid=1):
    return SimpleNamespace(id=bid, position=(52.5200, 13.4050))


class AllianceBoard:
    """What's currently alliance-visible. propagate=False simulates the game
    not having propagated a just-made share yet (the visibility-guard case)."""

    def __init__(self, propagate: bool = True):
        self.visible: dict[int, object] = {}
        self.propagate = propagate

    def share(self, mission) -> None:
        if self.propagate:
            self.visible[mission.id] = mission


class FakeClient:
    def __init__(self, own_missions, board: AllianceBoard, vehicles=None):
        self.own = {m.id: m for m in own_missions}
        self.board = board
        self.vehicles = vehicles or []
        self.dispatched: list[tuple[int, list[int]]] = []
        self.shared_ids: set[int] = set()

    async def fetch_vehicles(self, cap=0):
        return self.vehicles

    async def fetch_buildings(self):
        return [_building()]

    async def fetch_missions(self):
        seen = dict(self.own)
        for mid, m in self.board.visible.items():
            seen.setdefault(mid, m)
        return list(seen.values())

    async def share_mission(self, mission_id) -> bool:
        m = self.own.get(mission_id)
        if m is not None:
            self.board.share(m)
            self.shared_ids.add(mission_id)
        return True

    async def dispatch_many(self, mission_id, vehicle_ids) -> bool:
        self.dispatched.append((mission_id, list(vehicle_ids)))
        return True


class FakeMember:
    def __init__(self, client, resolver, transport_enabled=False):
        self.client = client
        self.resolver = resolver
        self.vehicle_cap = 0
        self.transport_enabled = transport_enabled

    async def connect(self):
        pass

    async def handle_transports(self, vehicles):
        pass

    async def aclose(self):
        pass


class GroupCoordinatorShareOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        library = AssetLibrary.load(VEHICLES_DIR)
        catalog = MissionCatalog.from_payload(json.loads(FIXTURE.read_text(encoding="utf-8")))
        cls.resolver = MissionRequirementResolver(catalog, library)

    def _coordinator(self, board: AllianceBoard) -> tuple[GroupCoordinator, FakeClient, FakeClient]:
        mission = _mission(1)
        client_a = FakeClient([mission], board)               # A owns it, no free vehicle
        client_b = FakeClient([], board, vehicles=[_vehicle(100)])  # B has the LF
        member_a = FakeMember(client_a, self.resolver)
        member_b = FakeMember(client_b, self.resolver)
        coordinator = GroupCoordinator(
            {1: member_a, 2: member_b}, GroupPolicy(radius_km=40), share_enabled=True,
            dispatch_gap=0.0,
        )
        return coordinator, client_a, client_b

    def test_share_then_replan_lets_teammate_join_same_cycle(self):
        # The whole point of P3.1: A's own mission is shared and B — who never
        # owned it — can already crew it in THIS SAME run_cycle() call.
        board = AllianceBoard(propagate=True)
        coordinator, client_a, client_b = self._coordinator(board)
        plan = asyncio.run(coordinator.run_cycle())

        self.assertEqual(client_a.shared_ids, {1})  # A shared before planning
        self.assertEqual(client_b.dispatched, [(1, [100])])  # B joined THIS cycle
        self.assertEqual(plan.missions_actioned, 1)

    def test_visibility_guard_blocks_join_when_share_not_propagated(self):
        # The game hasn't propagated the share yet (occasional real-world lag)
        # -> B still can't see mission 1 this cycle, so it must NOT be assigned
        # B's vehicle even though a share attempt was made.
        board = AllianceBoard(propagate=False)
        coordinator, client_a, client_b = self._coordinator(board)
        plan = asyncio.run(coordinator.run_cycle())

        self.assertEqual(client_a.shared_ids, {1})   # share was attempted
        self.assertEqual(client_b.dispatched, [])    # but B still can't see it
        self.assertEqual(plan.missions_actioned, 0)  # A alone can't crew it either

    def test_one_member_fetch_missions_failure_does_not_abort_cycle(self):
        # A transient per-account fetch failure isolates to that account
        # (mirrors connect() isolation) instead of aborting transport/dispatch
        # for every other member.
        board = AllianceBoard(propagate=True)
        mission = _mission(1)
        client_a = FakeClient([mission], board)
        client_b = FakeClient([], board, vehicles=[_vehicle(100)])

        class FlakyClient(FakeClient):
            async def fetch_missions(self):
                raise RuntimeError("transient API error")

        client_broken = FlakyClient([], board)
        member_a = FakeMember(client_a, self.resolver)
        member_b = FakeMember(client_b, self.resolver)
        member_broken = FakeMember(client_broken, self.resolver)
        coordinator = GroupCoordinator(
            {1: member_a, 2: member_b, 3: member_broken}, GroupPolicy(radius_km=40),
            share_enabled=True, dispatch_gap=0.0,
        )
        plan = asyncio.run(coordinator.run_cycle())  # must not raise

        self.assertEqual(client_b.dispatched, [(1, [100])])  # unaffected members still work
        self.assertEqual(plan.missions_actioned, 1)

    def test_share_disabled_never_shares(self):
        board = AllianceBoard(propagate=True)
        mission = _mission(1)
        client_a = FakeClient([mission], board)
        member_a = FakeMember(client_a, self.resolver)
        coordinator = GroupCoordinator({1: member_a}, GroupPolicy(radius_km=40),
                                        share_enabled=False, dispatch_gap=0.0)
        asyncio.run(coordinator.run_cycle())
        self.assertEqual(client_a.shared_ids, set())


if __name__ == "__main__":
    unittest.main()
