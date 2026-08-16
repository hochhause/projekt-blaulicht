import unittest
from math import ceil
from pathlib import Path

from app.domain.assets import AssetLibrary
from app.domain.requirements import RequirementEngine

VEHICLES_DIR = Path(__file__).resolve().parent.parent / "data" / "de" / "vehicles"


class RequirementEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = AssetLibrary.load(VEHICLES_DIR)
        cls.engine = RequirementEngine(cls.lib)

    # -- canonical examples from the plan ---------------------------------
    def test_police_count_scales_to_two_cars(self):
        req = cls_req(self, "3 Polizisten werden benötigt")
        self.assertIn(32, req.type_ids)  # FuStW
        self.assertEqual(req.count, 2)   # "3-4 police -> 2 cars"

    def test_single_rtw(self):
        req = cls_req(self, "1 RTW")
        self.assertIn(28, req.type_ids)  # RTW
        self.assertEqual(req.count, 1)

    def test_lf_water_scaling_is_capacity_divided(self):
        # LF (type 0) carries 2000 l; a big water demand needs several LF.
        lf = self.lib.types[0]
        needed = lf.required_count("insgesamt 8000 l. Wasser benötigt")
        self.assertEqual(needed, ceil(8000 / 2000))  # 4

    # -- engine behaviour --------------------------------------------------
    def test_unmatched_line_is_reported_not_dropped(self):
        res = self.engine.resolve(["völliger Unsinn ohne Fahrzeugbezug 12345"])
        self.assertEqual(res.requirements, [])
        self.assertEqual(len(res.unmatched), 1)

    def test_blank_lines_ignored(self):
        res = self.engine.resolve(["", "   ", "\n"])
        self.assertEqual(res.requirements, [])
        self.assertEqual(res.unmatched, [])

    def test_multiple_requirements_resolved_independently(self):
        res = self.engine.resolve(["1 RTW", "3 Polizisten"])
        self.assertEqual(len(res.requirements), 2)
        sources = {r.source for r in res.requirements}
        self.assertEqual(sources, {"1 RTW", "3 Polizisten"})

    def test_total_vehicles_sums_counts(self):
        res = self.engine.resolve(["1 RTW", "3 Polizisten"])
        self.assertEqual(res.total_vehicles, 1 + 2)


def cls_req(test, line):
    """Resolve one line and fail the test if nothing matched."""
    req = test.engine.resolve_line(line)
    test.assertIsNotNone(req, f"expected a match for {line!r}")
    return req


if __name__ == "__main__":
    unittest.main()
