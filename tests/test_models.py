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
class MissionParsingTests(unittest.TestCase):
    def test_real_mission_shape(self):
        from app.game.models import Mission

        # Real /map/missions_json element (trimmed): mtid, latitude, alliance fields.
        m = Mission.from_json({
            "id": 999, "caption": "Verkehrsunfall", "mtid": 3,
            "latitude": 47.5, "longitude": 7.6,
            "alliance_id": 32819, "alliance_shared_at": "2026-08-16T10:00:00Z",
            "patients_count": 2, "prisoners_count": 0,
        })
        self.assertEqual(m.id, 999)
        self.assertEqual(m.mission_type_id, 3)     # from "mtid"
        self.assertEqual(m.position, (47.5, 7.6))
        self.assertTrue(m.is_alliance)
        self.assertEqual(m.patients_count, 2)

    def test_own_mission_not_alliance(self):
        from app.game.models import Mission

        m = Mission.from_json({"id": 1, "mtid": 1, "latitude": 47, "longitude": 7})
        self.assertFalse(m.is_alliance)

    def test_overlay_mtid_tolerated(self):
        from app.game.models import Mission

        m = Mission.from_json({"id": 1, "mtid": "6/ab", "latitude": 47, "longitude": 7})
        self.assertIsNone(m.mission_type_id)  # variant id -> no int base, no crash

    def test_parses_patient_missing_and_missing_text(self):
        from app.game.models import Mission

        m = Mission.from_json({
            "id": 1, "mtid": 6, "latitude": 47, "longitude": 7,
            "missing_text": '{"vehicles":"1 ELW 1","personnel":"","other":""}',
            "patients_prisoners": {"patients": [
                {"missing_text": "Wir benötigen: NEF", "name": "Marc E."},
                {"name": "no-need"},  # patient with no missing_text -> ignored
            ]},
        })
        self.assertEqual(m.patient_missing, ("Wir benötigen: NEF",))
        self.assertIn("ELW 1", m.missing_text)

    def test_icon_status_red_yellow_green_unknown(self):
        from app.game.models import Mission

        def status_for(icon):
            m = Mission.from_json({"id": 1, "mtid": 1, "latitude": 47, "longitude": 7, "icon": icon})
            return m.status

        self.assertEqual(status_for("feuer_rot"), "red")
        self.assertEqual(status_for("rd_gelb"), "yellow")
        self.assertEqual(status_for("polizei_gruen"), "green")
        self.assertEqual(status_for(""), "unknown")
        self.assertEqual(status_for("something_else"), "unknown")


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
