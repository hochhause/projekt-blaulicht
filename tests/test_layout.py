import unittest

from app.domain.layout import BuildingLayout, EstateLayout


class LayoutTests(unittest.TestCase):
    def test_building_layout_from_dict(self):
        bl = BuildingLayout.from_dict({"level": 20, "extensions": [0, 8], "vehicles": {"0": 4, "2": 1}})
        self.assertEqual(bl.target_level, 20)
        self.assertEqual(bl.extensions, frozenset({0, 8}))
        self.assertEqual(bl.vehicles, {0: 4, 2: 1})

    def test_defaults(self):
        bl = BuildingLayout.from_dict({})
        self.assertIsNone(bl.target_level)
        self.assertEqual(bl.extensions, frozenset())
        self.assertEqual(bl.vehicles, {})

    def test_estate_layout_type_default_and_override(self):
        estate = EstateLayout.from_dict(
            {
                "by_type": {"0": {"level": 20, "vehicles": {"0": 4}}},
                "by_building": {"12345": {"level": 25}},
            }
        )
        # A generic fire station (type 0) gets the type default.
        generic = estate.for_building(999, building_type=0)
        self.assertEqual(generic.target_level, 20)
        # The specific building 12345 gets its override (most-specific wins).
        specific = estate.for_building(12345, building_type=0)
        self.assertEqual(specific.target_level, 25)

    def test_unmanaged_building_returns_none(self):
        estate = EstateLayout.from_dict({"by_type": {"0": {"level": 20}}})
        self.assertIsNone(estate.for_building(1, building_type=7))
        self.assertIsNone(estate.for_building(1, building_type=None))


if __name__ == "__main__":
    unittest.main()
