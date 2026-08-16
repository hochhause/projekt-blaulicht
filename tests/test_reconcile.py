import unittest
from pathlib import Path

from app.domain.assets import AssetLibrary
from app.domain.estate import BuildingState
from app.domain.layout import EstateLayout
from app.domain.reconcile import ActionKind, Automation, reconcile

VEHICLES_DIR = Path(__file__).resolve().parent.parent / "data" / "de" / "vehicles"


def fire_station(**kw) -> BuildingState:
    base = dict(
        id=1,
        building_type=0,
        level=10,
        extensions=frozenset(),
        vehicle_type_counts={},
        personnel_available=0,
        personnel_total=0,
        caption="Feuerwache Nord",
    )
    base.update(kw)
    return BuildingState(**base)


class ReconcileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = AssetLibrary.load(VEHICLES_DIR)
        # Every fire station: level 20, extension 0, 4× LF (type 0).
        cls.layout = EstateLayout.from_dict(
            {"by_type": {"0": {"level": 20, "extensions": [0], "vehicles": {"0": 4}}}}
        )

    def _kinds(self, state):
        result = reconcile([state], self.layout, self.lib)
        return result, {a.kind for a in result.actions}

    def test_level_up_action(self):
        result, kinds = self._kinds(fire_station(level=10, vehicle_type_counts={0: 4}, personnel_available=36))
        self.assertIn(ActionKind.LEVEL_UP, kinds)
        lvl = next(a for a in result.actions if a.kind is ActionKind.LEVEL_UP)
        self.assertEqual(lvl.quantity, 10)  # 20 - 10

    def test_missing_extension_and_vehicles(self):
        _, kinds = self._kinds(fire_station(level=20, vehicle_type_counts={0: 1}, personnel_available=9))
        self.assertIn(ActionKind.BUILD_EXTENSION, kinds)
        self.assertIn(ActionKind.BUY_VEHICLE, kinds)

    def test_buy_vehicle_quantity(self):
        result, _ = self._kinds(fire_station(level=20, extensions=frozenset({0}), vehicle_type_counts={0: 1}, personnel_available=9))
        buy = next(a for a in result.actions if a.kind is ActionKind.BUY_VEHICLE)
        self.assertEqual(buy.quantity, 3)  # 4 target - 1 present

    def test_personnel_shortfall(self):
        # 4× LF need 36 crew; only 20 available -> hire 16.
        result, kinds = self._kinds(
            fire_station(level=20, extensions=frozenset({0}), vehicle_type_counts={0: 4}, personnel_available=20)
        )
        self.assertIn(ActionKind.HIRE_PERSONNEL, kinds)
        hire = next(a for a in result.actions if a.kind is ActionKind.HIRE_PERSONNEL)
        self.assertEqual(hire.quantity, 36 - 20)

    def test_excess_vehicle_is_manual(self):
        result, _ = self._kinds(
            fire_station(level=20, extensions=frozenset({0}), vehicle_type_counts={0: 6}, personnel_available=54)
        )
        excess = next(a for a in result.actions if a.kind is ActionKind.EXCESS_VEHICLE)
        self.assertIs(excess.automation, Automation.MANUAL)

    def test_fully_satisfied_station_yields_no_actions(self):
        result, _ = self._kinds(
            fire_station(level=20, extensions=frozenset({0}), vehicle_type_counts={0: 4}, personnel_available=36)
        )
        self.assertEqual(result.actions, [])
        self.assertIn("no action required", result.report())

    def test_unmanaged_building_ignored(self):
        # building_type 7 has no layout -> nothing emitted
        result = reconcile([fire_station(building_type=7, level=1)], self.layout, self.lib)
        self.assertEqual(result.actions, [])

    def test_report_groups_and_needs_operator(self):
        result = reconcile(
            [fire_station(level=10, vehicle_type_counts={0: 6}, personnel_available=0)],
            self.layout,
            self.lib,
        )
        # Nothing is auto yet (no captured writes) -> everything needs operator.
        self.assertEqual(result.automatable, [])
        self.assertTrue(result.needs_operator)
        self.assertIn("Action required", result.report())

    def test_capability_flips_action_to_auto_ready(self):
        # Once a write is captured, passing its kind as a capability makes the
        # reconciler emit it as AUTO_READY (ready to perform).
        state = fire_station(level=10, vehicle_type_counts={0: 4}, personnel_available=36)
        result = reconcile([state], self.layout, self.lib, capabilities={ActionKind.LEVEL_UP})
        lvl = next(a for a in result.actions if a.kind is ActionKind.LEVEL_UP)
        self.assertIs(lvl.automation, Automation.AUTO_READY)
        self.assertIn(lvl, result.automatable)

    def test_manual_kind_stays_manual_even_if_capable(self):
        # Selling surplus is judgement — capture never auto-enables it.
        state = fire_station(level=20, extensions=frozenset({0}), vehicle_type_counts={0: 6}, personnel_available=54)
        result = reconcile([state], self.layout, self.lib, capabilities={ActionKind.EXCESS_VEHICLE})
        excess = next(a for a in result.actions if a.kind is ActionKind.EXCESS_VEHICLE)
        self.assertIs(excess.automation, Automation.MANUAL)


if __name__ == "__main__":
    unittest.main()
