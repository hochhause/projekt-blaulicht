import json
import unittest
from pathlib import Path
from types import SimpleNamespace

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

    # -- resolve_mission: chances + patients derivation ------------------------
    def test_resolve_mission_base_requirements(self):
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0)
        res = self.resolver.resolve_mission(m)
        by = {r.source: r.count for r in res.requirements}
        self.assertEqual(by["firetrucks"], 2)  # fixture type 2

    def test_resolve_mission_patients_add_ambulances_revealed(self):
        # A medical-ish mission: 3 patients revealed -> 3 ambulances.
        m = SimpleNamespace(mission_type_id=1, patients_count=3, possible_patients_count=5,
                            prisoners_count=0, possible_prisoners_count=0)
        res = self.resolver.resolve_mission(m)
        by = {r.source: r.count for r in res.requirements}
        self.assertEqual(by.get("ambulances"), 3)  # revealed count wins over possible

    def test_resolve_mission_patients_use_possible_when_hidden(self):
        m = SimpleNamespace(mission_type_id=1, patients_count=0, possible_patients_count=4,
                            prisoners_count=0, possible_prisoners_count=0)
        res = self.resolver.resolve_mission(m)
        by = {r.source: r.count for r in res.requirements}
        self.assertEqual(by.get("ambulances"), 4)  # send for all possible

    def test_resolve_mission_no_ambulances_when_patients_gone(self):
        # A begun mission (vehicle_state=1 -> counts revealed) with 0 patients
        # left: patients already carried off -> send NO ambulances, even though
        # the type COULD have had some. Fixes RTWs piling onto an empty scene.
        m = SimpleNamespace(mission_type_id=1, patients_count=0, possible_patients_count=4,
                            prisoners_count=0, possible_prisoners_count=0, vehicle_state=1)
        res = self.resolver.resolve_mission(m)
        by = {r.source: r.count for r in res.requirements}
        self.assertIsNone(by.get("ambulances"))

    def test_resolve_mission_revealed_patients_still_counted_when_begun(self):
        # Begun mission but patients still present -> still send for them.
        m = SimpleNamespace(mission_type_id=1, patients_count=2, possible_patients_count=4,
                            prisoners_count=0, possible_prisoners_count=0, vehicle_state=1)
        res = self.resolver.resolve_mission(m)
        by = {r.source: r.count for r in res.requirements}
        self.assertEqual(by.get("ambulances"), 2)


    # -- patient-only mode -----------------------------------------------------
    def test_patient_only_transports_all_and_drops_non_patient(self):
        # Fixture type 1 is a firetruck mission; in patient-only mode we ignore
        # the firetruck and send an RTW per patient + an LNA above 5 patients.
        m = SimpleNamespace(mission_type_id=1, patients_count=7, possible_patients_count=7,
                            prisoners_count=0, possible_prisoners_count=0, vehicle_state=0)
        res = self.resolver.resolve_patient_only(m)
        by = {r.source: r.count for r in res.requirements}
        self.assertEqual(by.get("ambulances"), 7)
        self.assertEqual(by.get("kdow_lna"), 1)      # >5 patients
        self.assertNotIn("kdow_orgl", by)            # not yet >10
        self.assertNotIn("firetrucks", by)           # non-patient class dropped

    def test_patient_only_adds_orgl_above_ten(self):
        m = SimpleNamespace(mission_type_id=1, patients_count=12, possible_patients_count=12,
                            prisoners_count=0, possible_prisoners_count=0, vehicle_state=0)
        by = {r.source: r.count for r in self.resolver.resolve_patient_only(m).requirements}
        self.assertEqual(by.get("ambulances"), 12)
        self.assertEqual(by.get("kdow_lna"), 1)
        self.assertEqual(by.get("kdow_orgl"), 1)     # >10 patients

    def test_patient_only_empty_when_no_patients_left(self):
        # On scene (vehicle_state 1) with 0 patients -> nothing to send.
        m = SimpleNamespace(mission_type_id=1, patients_count=0, possible_patients_count=5,
                            prisoners_count=0, possible_prisoners_count=0, vehicle_state=1)
        self.assertEqual(self.resolver.resolve_patient_only(m).requirements, [])


if __name__ == "__main__":
    unittest.main()
