import unittest
from pathlib import Path
from types import SimpleNamespace

from app.domain.assets import AssetLibrary
from app.domain.coverage import select_coverage
from app.domain.requirements import Requirement

VEHICLES_DIR = Path(__file__).resolve().parent.parent / "data" / "de" / "vehicles"

# Real type IDs from Vehicle.mscv:
LF = 0     # firetrucks only
RW = 4     # heavy_rescue_vehicles only
HLF = 30   # BOTH firetrucks and heavy_rescue_vehicles


def veh(vid: int, type_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=vid, type_id=type_id)


class CoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        lib = AssetLibrary.load(VEHICLES_DIR)
        cls.firetrucks = frozenset(lib.class_map.by_class["firetrucks"])
        cls.heavy = frozenset(lib.class_map.by_class["heavy_rescue_vehicles"])

    def _lf_and_rw(self, lf=1, rw=1):
        return [
            Requirement(source="firetrucks", type_ids=self.firetrucks, count=lf),
            Requirement(source="heavy_rescue_vehicles", type_ids=self.heavy, count=rw),
        ]

    def test_one_hlf_covers_lf_and_rw(self):
        # The headline case: 1 LF + 1 RW satisfied by a single HLF.
        result = select_coverage(self._lf_and_rw(), [veh(1, HLF)])
        self.assertTrue(result.satisfied)
        self.assertEqual(result.vehicle_ids, [1])
        covered = result.assignments[0].covers
        self.assertIn("firetrucks", covered)
        self.assertIn("heavy_rescue_vehicles", covered)

    def test_prefers_the_multi_role_vehicle_over_two_singles(self):
        # HLF present alongside an LF and RW -> the single HLF is chosen.
        result = select_coverage(self._lf_and_rw(), [veh(1, LF), veh(2, RW), veh(3, HLF)])
        self.assertTrue(result.satisfied)
        self.assertEqual(result.vehicle_ids, [3])

    def test_falls_back_to_two_singles_when_no_hlf(self):
        result = select_coverage(self._lf_and_rw(), [veh(1, LF), veh(2, RW)])
        self.assertTrue(result.satisfied)
        self.assertEqual(sorted(result.vehicle_ids), [1, 2])

    def test_two_lf_one_rw_uses_hlf_plus_lf(self):
        # firetrucks:2, heavy_rescue:1 -> HLF (covers both) + one LF = 2 vehicles.
        reqs = self._lf_and_rw(lf=2, rw=1)
        result = select_coverage(reqs, [veh(1, HLF), veh(2, LF)])
        self.assertTrue(result.satisfied)
        self.assertEqual(len(result.assignments), 2)

    def test_shortfall_reported(self):
        # Only an LF for an LF+RW mission -> RW unmet.
        result = select_coverage(self._lf_and_rw(), [veh(1, LF)])
        self.assertFalse(result.satisfied)
        self.assertEqual(result.shortfall, {"heavy_rescue_vehicles": 1})

    def test_distance_breaks_ties_nearest_first(self):
        # Two interchangeable LFs; nearer one (smaller distance) is chosen.
        reqs = [Requirement(source="firetrucks", type_ids=self.firetrucks, count=1)]
        dist = {10: 9.0, 11: 2.0}
        result = select_coverage(reqs, [veh(10, LF), veh(11, LF)], distance_of=lambda v: dist[v.id])
        self.assertEqual(result.vehicle_ids, [11])

    def test_specialist_type_deprioritized_among_equally_covering_candidates(self):
        # A generic and a specialist type both satisfy the same requirement --
        # the specialist is picked last, even if it would otherwise be closer.
        req = [Requirement(source="elw", type_ids=frozenset({3, 59}), count=1)]
        dist = {1: 9.0, 2: 2.0}  # specialist (59) is CLOSER
        result = select_coverage(
            req, [veh(1, 3), veh(2, 59)],
            distance_of=lambda v: dist[v.id], specialist_type_ids=frozenset({59}),
        )
        self.assertEqual(result.vehicle_ids, [1])

    def test_specialist_still_used_when_it_is_the_only_option(self):
        # Not a hard blocker -- a specialist covers the mission when nothing
        # else does.
        req = [Requirement(source="elw", type_ids=frozenset({3, 59}), count=1)]
        result = select_coverage(req, [veh(2, 59)], specialist_type_ids=frozenset({59}))
        self.assertTrue(result.satisfied)
        self.assertEqual(result.vehicle_ids, [2])

    def test_preferred_type_overrides_specialist_penalty_and_distance(self):
        # On a mass-casualty mission the SEG-ELW is the BETTER choice, not
        # just an acceptable one -- preferred beats both the specialist
        # penalty and a closer generic alternative.
        req = [Requirement(source="elw", type_ids=frozenset({3, 59}), count=1)]
        dist = {1: 2.0, 2: 9.0}  # generic (3) is closer
        result = select_coverage(
            req, [veh(1, 3), veh(2, 59)], distance_of=lambda v: dist[v.id],
            specialist_type_ids=frozenset({59}), preferred_type_ids=frozenset({59}),
        )
        self.assertEqual(result.vehicle_ids, [2])


if __name__ == "__main__":
    unittest.main()
