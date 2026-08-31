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

    # -- P1: missing_text is the authoritative source once revealed ------------
    def test_resolve_mission_falls_back_to_catalogue_when_missing_text_empty(self):
        # Untouched mission (no missing_text yet) -> the einsaetze estimate.
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0, missing_text="")
        by = {r.source: r.count for r in self.resolver.resolve_mission(m).requirements}
        self.assertEqual(by["firetrucks"], 2)  # fixture type 2's catalogue requirement

    def test_resolve_mission_long_text_only_blob_falls_back_to_catalogue(self):
        # Live-verified 2026-08-31: an untouched mission's missing_text can be
        # {"long_text": "..."} -- a flavor-text hint with none of
        # vehicles/personnel/other present. Must NOT be treated as "nothing
        # more needed"; the catalogue estimate still applies.
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"long_text":"Gefangene sollen abtransportiert werden."}')
        by = {r.source: r.count for r in self.resolver.resolve_mission(m).requirements}
        self.assertEqual(by["firetrucks"], 2)  # fixture type 2's catalogue requirement

    def test_resolve_mission_prefers_missing_text_over_catalogue(self):
        # Once revealed, missing_text REPLACES the catalogue's class/chance
        # estimate for the vehicle-class demand (docs/live-data-model.md).
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"1 ELW 1","personnel":"","other":""}')
        res = self.resolver.resolve_mission(m)
        by = {r.source: r.count for r in res.requirements}
        self.assertNotIn("firetrucks", by)   # catalogue estimate dropped
        self.assertEqual(by.get("ELW 1"), 1)  # missing_text category instead

    def test_resolve_mission_timed_mission_reveals_missing_text_before_participation(self):
        # Live-verified 2026-08-31, confirmed by Sasha: TIMED missions (a
        # scheduled event like a parade/protest, "sw"/"tv" fields on the
        # feed) reveal missing_text from the very start -- even with
        # vehicle_state=0 and no participation, unlike normal missions.
        # resolve_mission must resolve it anyway (it never gates on
        # vehicle_state, only on whether missing_text itself is revealed).
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0, vehicle_state=0,
                            missing_text='{"vehicles":"4 MEK-Fahrzeuge","personnel":"","other":""}')
        res = self.resolver.resolve_mission(m)
        by = {r.source: r.count for r in res.requirements}
        self.assertNotIn("firetrucks", by)          # catalogue estimate dropped
        self.assertEqual(by.get("MEK-Fahrzeuge"), 4)  # missing_text category instead

    def test_resolve_mission_missing_text_personnel_group_converted_to_vehicles(self):
        # "personnel" is a HEADCOUNT, not a vehicle count (unlike "vehicles") —
        # 8 missing firefighters needs ceil(8/9) = 1 vehicle (max crew among
        # Feuerwehrmann's eligible types is 9, e.g. an HLF), not 8 vehicles.
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"","personnel":"8 Feuerwehrmann","other":""}')
        by = {r.source: r.count for r in self.resolver.resolve_mission(m).requirements}
        self.assertEqual(by.get("Feuerwehrmann"), 1)

    def test_resolve_mission_missing_text_large_personnel_headcount_scales_up(self):
        # 20 missing firefighters -> ceil(20/9) = 3 vehicles, not 20.
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"","personnel":"20 Feuerwehrmann","other":""}')
        by = {r.source: r.count for r in self.resolver.resolve_mission(m).requirements}
        self.assertEqual(by.get("Feuerwehrmann"), 3)

    def test_resolve_mission_revealed_and_fully_satisfied_is_not_stale_catalogue(self):
        # Revealed blob with EMPTY vehicles/personnel/other means "nothing
        # more needed" (authoritative) — must NOT fall back to the
        # catalogue's stale firetrucks:1 estimate for fixture type 2.
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"","personnel":"","other":""}')
        res = self.resolver.resolve_mission(m)
        self.assertEqual(res.requirements, [])
        self.assertEqual(res.unmatched, [])

    def test_resolve_mission_other_group_feuerloeschpumpe_resolves_to_loeschfahrzeug(self):
        # Live-verified 2026-08-31: "Feuerlöschpumpe (z. B. LF)" in the
        # `other` group is a plain category (same shape as `vehicles`), not a
        # numeric capacity need — any Löschfahrzeug already satisfies it, so
        # a mission whose only remaining need is this is fully covered by an
        # LF/HLF the base fire-vehicle requirement would send anyway.
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"","personnel":"",'
                                         '"other":"1 Feuerlöschpumpe (z. B. LF)"}')
        res = self.resolver.resolve_mission(m)
        self.assertEqual(len(res.requirements), 1)
        req = res.requirements[0]
        self.assertEqual(req.count, 1)
        self.assertIn(0, req.type_ids)   # LF (type 0)
        self.assertIn(30, req.type_ids)  # HLF (type 30)

    def test_resolve_mission_other_group_schmutzwasserpumpe_is_dedicated_only(self):
        # The rare, genuinely specialized case (per live confirmation): a
        # flooding/water-damage mission needs a DEDICATED pump vehicle, not
        # any Löschfahrzeug.
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"","personnel":"",'
                                         '"other":"1 Schmutzwasserpumpe"}')
        res = self.resolver.resolve_mission(m)
        self.assertEqual(len(res.requirements), 1)
        req = res.requirements[0]
        self.assertEqual(req.count, 1)
        self.assertNotIn(0, req.type_ids)   # plain LF does NOT count
        self.assertEqual(set(req.type_ids), {101, 102})

    def test_resolve_mission_casualty_extras_layered_on_missing_text(self):
        # NEF/ambulances/incident-command are never in missing_text's own
        # vehicles/personnel groups -> they still apply on top of it.
        m = SimpleNamespace(mission_type_id=2, patients_count=3, possible_patients_count=3,
                            prisoners_count=0, possible_prisoners_count=0, vehicle_state=1,
                            patient_missing=("Wir benötigen: NEF",),
                            missing_text='{"vehicles":"1 ELW 1","personnel":"","other":""}')
        by = {r.source: r.count for r in self.resolver.resolve_mission(m).requirements}
        self.assertEqual(by.get("ELW 1"), 1)
        self.assertEqual(by.get("ambulances"), 3)
        self.assertEqual(by.get("nef"), 1)

    def test_resolve_mission_unmatched_missing_text_category_reported(self):
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"1 Marsrover","personnel":"","other":""}')
        res = self.resolver.resolve_mission(m)
        self.assertEqual(res.requirements, [])
        self.assertIn("1 Marsrover", res.unmatched)


if __name__ == "__main__":
    unittest.main()
