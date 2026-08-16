import unittest

from app.domain import geo

BERLIN = (52.5200, 13.4050)
MUNICH = (48.1372, 11.5756)
HAMBURG = (53.5511, 9.9937)


class GeoTests(unittest.TestCase):
    def test_haversine_berlin_munich(self):
        # Reference great-circle distance ~504 km.
        d = geo.haversine_km(BERLIN, MUNICH)
        self.assertAlmostEqual(d, 504, delta=6)

    def test_distance_is_symmetric(self):
        self.assertAlmostEqual(
            geo.haversine_km(BERLIN, HAMBURG),
            geo.haversine_km(HAMBURG, BERLIN),
            places=9,
        )

    def test_zero_distance(self):
        self.assertAlmostEqual(geo.haversine_km(BERLIN, BERLIN), 0.0, places=6)

    def test_nearest_of_several(self):
        d = geo.nearest_km(BERLIN, [MUNICH, HAMBURG])
        self.assertAlmostEqual(d, geo.haversine_km(BERLIN, HAMBURG), places=9)

    def test_nearest_empty_is_none(self):
        self.assertIsNone(geo.nearest_km(BERLIN, []))

    def test_within_radius(self):
        self.assertTrue(geo.within_radius(BERLIN, [MUNICH], radius_km=600))
        self.assertFalse(geo.within_radius(BERLIN, [MUNICH], radius_km=400))


if __name__ == "__main__":
    unittest.main()
