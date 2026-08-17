import unittest

from app.domain.estate import BuildingState

# Real /api/buildings element (trimmed), from a live capture.
REAL_BUILDING = {
    "id": 15989545,
    "personal_count": 56,
    "personal_count_target": 100,
    "level": 9,
    "building_type": 0,
    "caption": "Feuer 01",
    "latitude": 47.5487,
    "longitude": 7.5907,
    "extensions": [
        {"caption": "Abrollbehälter-Stellplatz", "available": True, "enabled": True, "type_id": 2},
        {"caption": "Abrollbehälter-Stellplatz", "available": True, "enabled": True, "type_id": 1},
        {"caption": "Großwache", "available": True, "enabled": True, "type_id": 9},
        {"caption": "not built", "available": False, "enabled": False, "type_id": 5},
    ],
}


class EstateFromApiTests(unittest.TestCase):
    def test_parses_real_building(self):
        state = BuildingState.from_api(REAL_BUILDING, vehicle_type_ids=[0, 0, 4])
        self.assertEqual(state.id, 15989545)
        self.assertEqual(state.building_type, 0)
        self.assertEqual(state.level, 9)
        self.assertEqual(state.caption, "Feuer 01")

    def test_extensions_use_type_id(self):
        state = BuildingState.from_api(REAL_BUILDING)
        # enabled/available extensions counted (by type_id); the unbuilt one too
        # only if available — here type_id 5 is neither, so excluded.
        self.assertIn(1, state.extensions)
        self.assertIn(9, state.extensions)
        self.assertNotIn(5, state.extensions)

    def test_personnel_from_personal_count(self):
        state = BuildingState.from_api(REAL_BUILDING)
        self.assertEqual(state.personnel_total, 56)
        self.assertEqual(state.personnel_available, 56)  # no personal_free field

    def test_vehicle_counts(self):
        state = BuildingState.from_api(REAL_BUILDING, vehicle_type_ids=[0, 0, 4])
        self.assertEqual(state.vehicle_type_counts, {0: 2, 4: 1})


if __name__ == "__main__":
    unittest.main()
