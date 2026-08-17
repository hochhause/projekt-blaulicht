"""Game-model parsing against real API shapes (from a live probe).

Gated on httpx (a runtime dep of the game package) so the pure-stdlib suite
stays importable without it.
"""

import unittest

try:
    import httpx  # noqa: F401
    HAVE_HTTPX = True
except ImportError:
    HAVE_HTTPX = False


@unittest.skipUnless(HAVE_HTTPX, "httpx not installed")
class VehicleParsingTests(unittest.TestCase):
    def test_v2_vehicle_shape(self):
        from app.game.models import Vehicle

        # Real /api/v2/vehicles element (trimmed): no lat/lon, empty type caption.
        v = Vehicle.from_json({
            "id": 60167260, "caption": "LF 20", "building_id": 15989545,
            "vehicle_type": 0, "fms_real": 2, "fms_show": 2, "vehicle_type_caption": "",
            "target_type": None,
        })
        self.assertEqual(v.id, 60167260)
        self.assertEqual(v.type_id, 0)
        self.assertEqual(v.building_id, 15989545)
        self.assertTrue(v.is_available)          # fms_real 2
        self.assertIsNone(v.position)            # no coordinates on vehicles
        self.assertEqual(v.type_caption, "LF 20")  # falls back to caption

    def test_unavailable_when_busy(self):
        from app.game.models import Vehicle

        v = Vehicle.from_json({"id": 1, "vehicle_type": 0, "fms_real": 3})
        self.assertFalse(v.is_available)


@unittest.skipUnless(HAVE_HTTPX, "httpx not installed")
class AsListTests(unittest.TestCase):
    def test_unwraps_result_envelope(self):
        from app.game.client import _as_list

        payload = {"result": [{"id": 1}, {"id": 2}], "paging": {"page": 1}}
        self.assertEqual(_as_list(payload), [{"id": 1}, {"id": 2}])

    def test_bare_list(self):
        from app.game.client import _as_list

        self.assertEqual(_as_list([{"id": 1}]), [{"id": 1}])

    def test_missions_envelope_key(self):
        from app.game.client import _as_list

        self.assertEqual(_as_list({"missions": [{"id": 9}]}), [{"id": 9}])


if __name__ == "__main__":
    unittest.main()
