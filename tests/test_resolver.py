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

    def test_patient_only_excludes_end_patient_missions(self):
        # Hitzschlag-style: the patient only appears at the end, so on-site count
        # is 0 even though the mission COULD have one. Patient-only ignores it
        # entirely (exact on-site count, no possible-max fallback).
        m = SimpleNamespace(mission_type_id=1, patients_count=0, possible_patients_count=1,
                            prisoners_count=0, possible_prisoners_count=0, vehicle_state=0)
        self.assertEqual(self.resolver.resolve_patient_only(m).requirements, [])

    def test_nef_comes_from_patient_need_not_chance(self):
        from app.domain.catalog import MissionCatalog, MissionType
        from app.domain.resolver import MissionRequirementResolver

        mt = MissionType(id=901, name="Med", average_credits=100, requirements={}, chances={"nef": 50})
        resolver = MissionRequirementResolver(MissionCatalog({901: mt}), self.resolver._library)

        # A nef chance alone must NOT pre-send a NEF (that drains the fleet).
        m0 = SimpleNamespace(mission_type_id=901, patients_count=0, possible_patients_count=0,
                             prisoners_count=0, possible_prisoners_count=0, patient_missing=())
        self.assertNotIn("nef", {r.source for r in resolver.resolve_mission(m0).requirements})

        # A patient on scene asking for one -> exactly one NEF.
        m1 = SimpleNamespace(mission_type_id=901, patients_count=1, possible_patients_count=1,
                             prisoners_count=0, possible_prisoners_count=0,
                             patient_missing=("Wir benötigen: NEF",))
        by = {r.source: r.count for r in resolver.resolve_mission(m1).requirements}
        self.assertEqual(by.get("nef"), 1)

    def test_two_patients_two_nefs(self):
        from app.domain.catalog import MissionCatalog, MissionType
        from app.domain.resolver import MissionRequirementResolver

        mt = MissionType(id=902, name="Med", average_credits=100, requirements={}, chances={})
        resolver = MissionRequirementResolver(MissionCatalog({902: mt}), self.resolver._library)
        m = SimpleNamespace(mission_type_id=902, patients_count=2, possible_patients_count=2,
                            prisoners_count=0, possible_prisoners_count=0,
                            patient_missing=("Wir benötigen: NEF", "Wir benötigen: NEF"))
        by = {r.source: r.count for r in resolver.resolve_mission(m).requirements}
        self.assertEqual(by.get("nef"), 2)

    def test_lna_added_in_normal_mode_above_five_patients(self):
        # Point: the LNA/OrgL rule works in EVERY mode, not just patient-only.
        m = SimpleNamespace(mission_type_id=1, patients_count=6, possible_patients_count=6,
                            prisoners_count=0, possible_prisoners_count=0, vehicle_state=0)
        by = {r.source: r.count for r in self.resolver.resolve_mission(m).requirements}
        self.assertEqual(by.get("kdow_lna"), 1)
        self.assertEqual(by.get("firetrucks"), 1)  # normal reqs still present


if __name__ == "__main__":
    unittest.main()
