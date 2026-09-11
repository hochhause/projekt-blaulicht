import unittest
from pathlib import Path

from app.domain.assets import AssetLibrary

VEHICLES_DIR = Path(__file__).resolve().parent.parent / "data" / "de" / "vehicles"


class AssetLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = AssetLibrary.load(VEHICLES_DIR)

    def test_all_type_files_loaded(self):
        # 186 files on disk minus Vehicle.mscv = 185 vehicle types.
        self.assertEqual(len(self.lib.types), 185)

    def test_class_map_known_groups(self):
        cm = self.lib.class_map
        self.assertIn("firetrucks", cm.by_class)
        self.assertIn(0, cm.by_class["firetrucks"])
        self.assertEqual(cm.by_class["ambulances"], (28, 74))
        # reverse index
        self.assertIn("ambulances", cm.class_of[28])

    def test_lf_capacities_parsed(self):
        lf = self.lib.types[0]
        self.assertEqual(lf.capacities.get("water"), 2000.0)
        self.assertEqual(lf.capacities.get("personnel"), 9.0)

    def test_lf_dotnet_group_translated_to_capacity_rule(self):
        lf = self.lib.types[0]
        water_rules = [r for r in lf.extend if r.dimension == "water"]
        self.assertTrue(water_rules, "expected a water capacity-scaling rule on LF")
        self.assertEqual(water_rules[0].group_name, "firetrucksNeededWater")

    def test_fustw_enumerated_rules(self):
        fustw = self.lib.types[32]
        # Enumerated police rules have no capacity dimension.
        self.assertTrue(all(r.dimension is None for r in fustw.extend))
        # "3 or 4 police -> 2 cars" is present as value 2.
        self.assertIn(2, {r.value for r in fustw.extend})

    def test_matchless_flag_loaded(self):
        # Exactly two files carry is_matchless=true (schema exploration).
        matchless = [t for t in self.lib.types.values() if t.is_matchless]
        self.assertEqual(len(matchless), 2)

    def test_personnel_capacity(self):
        # LF (type 0) carries 9 crew per the salvaged data.
        self.assertEqual(self.lib.types[0].personnel, 9)

    def test_planning_personnel_halves_only_hlf(self):
        # Confirmed live 2026-09-11: "usually max is fine but for HLF
        # because they carry a lot go for only half" -- HLF 20 (30) and
        # HLF 10 (90) plan at half their nominal 9-crew cap; every other
        # firetruck type (e.g. plain LF, type 0) keeps its full nominal
        # capacity for planning purposes.
        self.assertEqual(self.lib.types[30].personnel, 9)
        self.assertEqual(self.lib.types[30].planning_personnel, 4)
        self.assertEqual(self.lib.types[90].personnel, 9)
        self.assertEqual(self.lib.types[90].planning_personnel, 4)
        self.assertEqual(self.lib.types[0].planning_personnel, self.lib.types[0].personnel)

    def test_trailer_types_flagged(self):
        trailers = {t.id for t in self.lib.types.values() if t.is_trailer}
        # 13 trailer (Anhänger) types confirmed in the DE data.
        self.assertEqual(len(trailers), 13)
        self.assertIn(43, trailers)  # THW GKW trailer family


if __name__ == "__main__":
    unittest.main()
