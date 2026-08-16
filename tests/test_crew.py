import unittest
from pathlib import Path

from app.domain.assets import AssetLibrary
from app.domain.crew import building_crew_requirement, crew_gap, required_crew

VEHICLES_DIR = Path(__file__).resolve().parent.parent / "data" / "de" / "vehicles"


class CrewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = AssetLibrary.load(VEHICLES_DIR)

    def test_required_crew_single_vehicle(self):
        lf = self.lib.types[0]  # 9 crew
        self.assertEqual(required_crew(lf), 9)

    def test_required_crew_with_trailer_adds_trailer_crew(self):
        lf = self.lib.types[0]
        trailer = next(t for t in self.lib.types.values() if t.is_trailer)
        self.assertEqual(required_crew(lf, trailer), lf.personnel + trailer.personnel)

    def test_building_crew_requirement_sums_and_skips_trailers(self):
        # 2× LF (9 each) = 18; a stationed trailer contributes 0 as a carrier.
        trailer_id = next(t.id for t in self.lib.types.values() if t.is_trailer)
        counts = {0: 2, trailer_id: 1}
        self.assertEqual(building_crew_requirement(self.lib, counts), 18)

    def test_crew_gap(self):
        gap = crew_gap(self.lib, building_id=5, vehicle_type_counts={0: 2}, personnel_available=12)
        self.assertEqual(gap.required, 18)
        self.assertEqual(gap.missing, 6)

    def test_no_gap_when_fully_staffed(self):
        gap = crew_gap(self.lib, building_id=5, vehicle_type_counts={0: 1}, personnel_available=9)
        self.assertEqual(gap.missing, 0)


if __name__ == "__main__":
    unittest.main()
