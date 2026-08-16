import json
import unittest
from pathlib import Path

from app.domain.assets import AssetLibrary
from app.domain.catalog import MissionCatalog
from app.domain.resolver import MissionRequirementResolver

BASE = Path(__file__).resolve().parent
VEHICLES_DIR = BASE.parent / "data" / "de" / "vehicles"
FIXTURE = BASE / "fixtures" / "einsaetze_sample.json"


class ResolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        library = AssetLibrary.load(VEHICLES_DIR)
        catalog = MissionCatalog.from_payload(json.loads(FIXTURE.read_text(encoding="utf-8")))
        cls.resolver = MissionRequirementResolver(catalog, library)

    def test_single_class_requirement(self):
        res = self.resolver.resolve(1)
        self.assertEqual(len(res.requirements), 1)
        req = res.requirements[0]
        self.assertEqual(req.source, "firetrucks")
        self.assertEqual(req.count, 1)
        self.assertIn(0, req.type_ids)  # LF is a firetruck type

    def test_multi_class_requirement_resolves_real_type_ids(self):
        res = self.resolver.resolve(2)
        by_class = {r.source: r for r in res.requirements}
        self.assertEqual(by_class["firetrucks"].count, 2)
        # Real Vehicle.mscv mappings:
        self.assertEqual(set(by_class["platform_trucks"].type_ids), {2, 85})
        self.assertEqual(set(by_class["battalion_chief_vehicles"].type_ids), {3, 34, 78})
        self.assertEqual(res.total_vehicles, 2 + 1 + 1)

    def test_police_class(self):
        res = self.resolver.resolve(3)
        police = next(r for r in res.requirements if r.source == "police_cars")
        self.assertEqual(set(police.type_ids), {32, 95})

    def test_unknown_class_is_reported_not_dropped(self):
        res = self.resolver.resolve(99)
        self.assertEqual(res.requirements, [])
        self.assertIn("totally_unknown_class", res.unmatched)

    def test_knows(self):
        self.assertTrue(self.resolver.knows(1))
        self.assertFalse(self.resolver.knows(4242))

    def test_unknown_mission_type_empty_resolution(self):
        res = self.resolver.resolve(4242)
        self.assertEqual(res.requirements, [])
        self.assertEqual(res.unmatched, [])


if __name__ == "__main__":
    unittest.main()
