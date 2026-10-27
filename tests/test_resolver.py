import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.domain.assets import AssetLibrary
from app.domain.catalog import MissionCatalog
from app.domain.coverage import select_coverage
from app.domain.resolver import MissionRequirementResolver

BASE = Path(__file__).resolve().parent
VEHICLES_DIR = BASE.parent / "data" / "de" / "vehicles"
FIXTURE = BASE / "fixtures" / "einsaetze_sample.json"


class ResolverTests(unittest.TestCase):
    def setUp(self):
        # A fresh resolver per test (not setUpClass/cls): MissionRequirementResolver
        # now carries small per-mission-id sticky state (effective_count_revealed)
        # that must not leak between tests reusing the same literal mission ids
        # for unrelated scenarios.
        library = AssetLibrary.load(VEHICLES_DIR)
        catalog = MissionCatalog.from_payload(json.loads(FIXTURE.read_text(encoding="utf-8")))
        self.resolver = MissionRequirementResolver(catalog, library)

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
        # SEG-ELW (59, "usable as a plain ELW") joins ELW1/ELW2/ELW2-Drohne
        # as a battalion-chief substitute -- confirmed live 2026-09-11.
        self.assertEqual(set(by_class["battalion_chief_vehicles"].type_ids), {3, 34, 59, 78})
        self.assertEqual(res.total_vehicles, 2 + 1 + 1)

    def test_police_class(self):
        res = self.resolver.resolve(3)
        police = next(r for r in res.requirements if r.source == "police_cars")
        self.assertEqual(set(police.type_ids), {32, 95})

    def test_prisoner_count_above_base_police_requirement_sends_the_larger_count(self):
        # Mission type 3's base need is 1 police_cars (X); more possible
        # prisoners than that (Y=3 here) must result in 3 FuStW sent, not 1
        # -- relies on the coverage matcher's max-not-sum multi-role
        # semantics (coverage.py) rather than any special-case code: the base
        # requirement and the casualty-layer prisoner requirement are two
        # separate Requirement rows sharing the same type_ids, so a vehicle
        # satisfies both simultaneously until the smaller is exhausted, then
        # keeps going for the larger. Live-verified 2026-09-11.
        m = SimpleNamespace(mission_type_id=3, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=3, missing_text="")
        res = self.resolver.resolve_mission(m)
        police_reqs = [r for r in res.requirements if r.source == "police_cars"]
        self.assertEqual(len(police_reqs), 2)  # base (1) + casualty layer (3), separate rows
        pool = [SimpleNamespace(id=i, type_id=32) for i in range(1, 4)]
        cov = select_coverage(police_reqs, pool)  # isolate from the mission's other requirements
        self.assertTrue(cov.satisfied)
        self.assertEqual(len(cov.vehicle_ids), 3)  # max(1, 3), not 1+3

    def test_unknown_class_is_reported_not_dropped(self):
        res = self.resolver.resolve(99)
        self.assertEqual(res.requirements, [])
        self.assertIn("totally_unknown_class", res.unmatched)

    def test_wildfire_catalog_classes_resolve_to_real_vehicle_types(self):
        # 2026-10-12 wildfire update, confirmed live against /einsaetze.json:
        # a wildfire mission variant carries "brush_truck" (scales 1-3 with
        # severity) and "wildfire_equipment" (always 1) as their OWN
        # requirement keys, alongside the ordinary "firetrucks" count --
        # three separate rows, not one. A dedicated one-off catalog/resolver
        # (not the shared fixture) isolates this from einsaetze_sample.json's
        # other tests.
        from app.domain.catalog import MissionCatalog, MissionType

        mtype = MissionType(id=900, name="Waldbrand (Wald- und Vegetation)",
                             average_credits=5000,
                             requirements={"brush_truck": 2, "wildfire_equipment": 1, "firetrucks": 1})
        catalog = MissionCatalog({900: mtype})
        resolver = MissionRequirementResolver(catalog, self.resolver.library)
        res = resolver.resolve(900)
        self.assertEqual(res.unmatched, [])
        by_class = {r.source: r for r in res.requirements}
        self.assertEqual(set(by_class["brush_truck"].type_ids), {187, 188, 189})
        self.assertEqual(by_class["brush_truck"].count, 2)
        self.assertEqual(set(by_class["wildfire_equipment"].type_ids), {190, 191, 192})
        self.assertEqual(by_class["wildfire_equipment"].count, 1)
        self.assertEqual(by_class["firetrucks"].count, 1)

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

    def test_resolve_mission_lna_orgl_use_max_possible_not_revealed(self):
        # Begun mission (revealed count authoritative for ambulances) with only
        # 2 patients confirmed so far, but the mission COULD have up to 12 --
        # LNA/OrgL must be sized to the worst case (12), not the 2 currently
        # on scene, so incident command shows up proactively rather than only
        # once enough patients have individually been confirmed (Sasha, 2026-09-16).
        m = SimpleNamespace(mission_type_id=1, patients_count=2, possible_patients_count=12,
                            prisoners_count=0, possible_prisoners_count=0, vehicle_state=1)
        res = self.resolver.resolve_mission(m)
        by = {r.source: r.count for r in res.requirements}
        self.assertEqual(by.get("ambulances"), 2)   # ambulances still track revealed
        self.assertEqual(by.get("kdow_lna"), 1)     # but LNA/OrgL track the max
        self.assertEqual(by.get("kdow_orgl"), 1)

    def test_tragehilfe_single_reveal_adds_one_firetruck(self):
        # Confirmed live 2026-09-21: "Wir benötigen: Tragehilfe (z.B. durch
        # ein LF)" -- an ambulance crew needing a fire crew's help carrying a
        # patient out of a building. Any LF/HLF satisfies it (Sasha: "just
        # deploy any lf, hlf, or such vehicle"), mapped to `firetrucks`.
        from app.domain.catalog import MissionCatalog, MissionType
        from app.domain.resolver import MissionRequirementResolver

        mt = MissionType(id=920, name="Med", average_credits=100, requirements={}, chances={})
        resolver = MissionRequirementResolver(MissionCatalog({920: mt}), self.resolver._library)
        m = SimpleNamespace(mission_type_id=920, patients_count=1, possible_patients_count=1,
                            prisoners_count=0, possible_prisoners_count=0,
                            patient_missing=("Wir benötigen: Tragehilfe (z.B. durch ein LF)",))
        by = {r.source: r.count for r in resolver.resolve_mission(m).requirements}
        self.assertEqual(by.get("firetrucks"), 1)

    def test_tragehilfe_explicit_count_suffix(self):
        from app.domain.catalog import MissionCatalog, MissionType
        from app.domain.resolver import MissionRequirementResolver

        mt = MissionType(id=921, name="Med", average_credits=100, requirements={}, chances={})
        resolver = MissionRequirementResolver(MissionCatalog({921: mt}), self.resolver._library)
        m = SimpleNamespace(mission_type_id=921, patients_count=2, possible_patients_count=2,
                            prisoners_count=0, possible_prisoners_count=0,
                            patient_missing=("Wir benötigen: Tragehilfe (z.B. durch ein LF) x2",))
        by = {r.source: r.count for r in resolver.resolve_mission(m).requirements}
        self.assertEqual(by.get("firetrucks"), 2)

    def test_tragehilfe_repeated_entries_sum(self):
        # If the game instead reveals one entry per affected patient (the
        # same shape as two patients each independently asking for a NEF),
        # count each occurrence rather than relying on an "xN" suffix.
        from app.domain.catalog import MissionCatalog, MissionType
        from app.domain.resolver import MissionRequirementResolver

        mt = MissionType(id=922, name="Med", average_credits=100, requirements={}, chances={})
        resolver = MissionRequirementResolver(MissionCatalog({922: mt}), self.resolver._library)
        m = SimpleNamespace(mission_type_id=922, patients_count=2, possible_patients_count=2,
                            prisoners_count=0, possible_prisoners_count=0,
                            patient_missing=("Wir benötigen: Tragehilfe (z.B. durch ein LF)",
                                              "Wir benötigen: Tragehilfe (z.B. durch ein LF)"))
        by = {r.source: r.count for r in resolver.resolve_mission(m).requirements}
        self.assertEqual(by.get("firetrucks"), 2)

    def test_tragehilfe_layers_as_separate_row_on_base_firetruck_requirement(self):
        # Fixture type 1 already needs 1 firetruck as a base requirement;
        # Tragehilfe adds a SEPARATE row rather than replacing it -- same
        # max-not-sum multi-role precedent as the police_cars/prisoners case
        # (test_prisoner_count_above_base_police_requirement_sends_the_larger_count).
        m = SimpleNamespace(mission_type_id=1, patients_count=1, possible_patients_count=1,
                            prisoners_count=0, possible_prisoners_count=0,
                            patient_missing=("Wir benötigen: Tragehilfe (z.B. durch ein LF) x2",))
        res = self.resolver.resolve_mission(m)
        firetruck_reqs = [r for r in res.requirements if r.source == "firetrucks"]
        self.assertEqual(len(firetruck_reqs), 2)  # base (1) + casualty layer (2)
        pool = [SimpleNamespace(id=i, type_id=0) for i in range(1, 3)]
        cov = select_coverage(firetruck_reqs, pool)
        self.assertTrue(cov.satisfied)
        self.assertEqual(len(cov.vehicle_ids), 2)  # max(1, 2), not 1+2

    def test_lna_from_explicit_patient_reveal_even_below_threshold(self):
        # Confirmed live 2026-09-21 (mission #519 "Hüpfburgunfall"): a patient
        # can explicitly ask for an LNA via "Wir benötigen: LNA", the same
        # per-patient mechanism as NEF/RTH -- honored even with only 1
        # patient, well below the 5-patient threshold, and does NOT add an
        # OrgL (that's still threshold-only, never explicitly revealed).
        m = SimpleNamespace(mission_type_id=1, patients_count=1, possible_patients_count=1,
                            prisoners_count=0, possible_prisoners_count=0, vehicle_state=0,
                            patient_missing=("Wir benötigen: LNA",))
        by = {r.source: r.count for r in self.resolver.resolve_mission(m).requirements}
        self.assertEqual(by.get("kdow_lna"), 1)
        self.assertNotIn("kdow_orgl", by)

    def test_resolve_mission_no_lna_orgl_when_patients_confirmed_cleared(self):
        # Begun mission with patients AUTHORITATIVELY confirmed gone (0 revealed,
        # vehicle_state=1) -- the possible-max ceiling is stale for a resolved
        # scene, so no incident command should be re-escalated onto it, even
        # though the type's ceiling is well above both thresholds.
        m = SimpleNamespace(mission_type_id=1, patients_count=0, possible_patients_count=20,
                            prisoners_count=0, possible_prisoners_count=0, vehicle_state=1)
        res = self.resolver.resolve_mission(m)
        by = {r.source: r.count for r in res.requirements}
        self.assertIsNone(by.get("ambulances"))
        self.assertNotIn("kdow_lna", by)
        self.assertNotIn("kdow_orgl", by)

    def test_resolve_mission_no_lna_orgl_when_max_possible_low(self):
        # Max possible is small -> no incident command, even though it's the
        # unrevealed/pre-arrival branch that would otherwise send for the max.
        m = SimpleNamespace(mission_type_id=1, patients_count=0, possible_patients_count=3,
                            prisoners_count=0, possible_prisoners_count=0, vehicle_state=0)
        res = self.resolver.resolve_mission(m)
        by = {r.source: r.count for r in res.requirements}
        self.assertNotIn("kdow_lna", by)
        self.assertNotIn("kdow_orgl", by)


    def test_effective_count_revealed_uses_injected_set_not_private_state(self):
        # AccountRunner hands in its OWN long-lived set (survives a resolver
        # being torn down and rebuilt on reconnect, see account_runner.py) --
        # confirm a resolver built with an injected set both writes to it and
        # reads another resolver's prior writes through it.
        shared: set[int] = set()
        r1 = MissionRequirementResolver(self.resolver.catalog, self.resolver.library,
                                         ever_revealed=shared)
        self.assertTrue(r1.effective_count_revealed(SimpleNamespace(id=42, vehicle_state=1)))
        self.assertIn(42, shared)
        # A brand-new resolver instance (as reconnect would build) sharing the
        # SAME injected set still remembers mission 42, even with vehicle_state
        # back to 0 and no prior calls of its own.
        r2 = MissionRequirementResolver(self.resolver.catalog, self.resolver.library,
                                         ever_revealed=shared)
        self.assertTrue(r2.effective_count_revealed(SimpleNamespace(id=42, vehicle_state=0)))
        # An unrelated mission id is unaffected.
        self.assertFalse(r2.effective_count_revealed(SimpleNamespace(id=43, vehicle_state=0)))

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

    def test_patient_only_lna_orgl_use_max_possible_not_on_site_count(self):
        # Only 2 patients on scene right now, but the mission could reach 12 --
        # incident command should size to the worst case, matching resolve_mission.
        m = SimpleNamespace(mission_type_id=1, patients_count=2, possible_patients_count=12,
                            prisoners_count=0, possible_prisoners_count=0, vehicle_state=1)
        by = {r.source: r.count for r in self.resolver.resolve_patient_only(m).requirements}
        self.assertEqual(by.get("ambulances"), 2)   # ambulances still track on-site count
        self.assertEqual(by.get("kdow_lna"), 1)
        self.assertEqual(by.get("kdow_orgl"), 1)

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

    def test_rth_comes_from_patient_need_as_rescue_helicopter_not_nef(self):
        # Live-verified 2026-09-11: a patient can specifically ask for "RTH"
        # (the helicopter), not just "NEF" -- must map to the dedicated
        # rescue_helicopter class (type 31 only) so a plain ground Notarzt
        # vehicle (also NEF-class, types 29/74) never gets sent in its place.
        from app.domain.catalog import MissionCatalog, MissionType
        from app.domain.resolver import MissionRequirementResolver

        mt = MissionType(id=903, name="Med", average_credits=100, requirements={}, chances={})
        resolver = MissionRequirementResolver(MissionCatalog({903: mt}), self.resolver._library)
        m = SimpleNamespace(mission_type_id=903, patients_count=1, possible_patients_count=1,
                             prisoners_count=0, possible_prisoners_count=0,
                             patient_missing=("Wir benötigen: RTH",))
        res = resolver.resolve_mission(m)
        by = {r.source: r for r in res.requirements}
        self.assertIn("rescue_helicopter", by)
        self.assertEqual(by["rescue_helicopter"].count, 1)
        self.assertEqual(set(by["rescue_helicopter"].type_ids), {31})
        self.assertNotIn("nef", by)

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

    # -- NEF generosity (Sasha, 2026-09-16) -------------------------------------
    def test_nef_pairs_every_patient_below_five_with_no_reveal_at_all(self):
        # Below 5 patients, EVERY ambulance is paired with a NEF, even with no
        # "Wir benötigen: NEF" reveal and a 0% modeled chance.
        from app.domain.catalog import MissionCatalog, MissionType
        from app.domain.resolver import MissionRequirementResolver

        mt = MissionType(id=910, name="Med", average_credits=100, requirements={}, chances={})
        resolver = MissionRequirementResolver(MissionCatalog({910: mt}), self.resolver._library)
        m = SimpleNamespace(mission_type_id=910, patients_count=4, possible_patients_count=4,
                             prisoners_count=0, possible_prisoners_count=0, patient_missing=())
        by = {r.source: r.count for r in resolver.resolve_mission(m).requirements}
        self.assertEqual(by.get("ambulances"), 4)
        self.assertEqual(by.get("nef"), 4)

    def test_nef_throttled_at_five_or_more_using_the_type_chance(self):
        # Sasha's own worked example: 10 patients, 40% modeled NEF chance ->
        # 4 expected + half of the remaining 6 (3) = 7.
        from app.domain.catalog import MissionCatalog, MissionType
        from app.domain.resolver import MissionRequirementResolver

        mt = MissionType(id=911, name="MCI", average_credits=100, requirements={}, chances={"nef": 40})
        resolver = MissionRequirementResolver(MissionCatalog({911: mt}), self.resolver._library)
        m = SimpleNamespace(mission_type_id=911, patients_count=10, possible_patients_count=10,
                             prisoners_count=0, possible_prisoners_count=0, patient_missing=())
        by = {r.source: r.count for r in resolver.resolve_mission(m).requirements}
        self.assertEqual(by.get("nef"), 7)

    def test_nef_half_of_all_patients_when_type_has_no_modeled_chance(self):
        # 0% modeled chance -> "expected" is 0, so half of ALL patients still
        # get a NEF anyway once the mission reaches mass-casualty scale.
        from app.domain.catalog import MissionCatalog, MissionType
        from app.domain.resolver import MissionRequirementResolver

        mt = MissionType(id=912, name="MCI", average_credits=100, requirements={}, chances={})
        resolver = MissionRequirementResolver(MissionCatalog({912: mt}), self.resolver._library)
        m = SimpleNamespace(mission_type_id=912, patients_count=6, possible_patients_count=6,
                             prisoners_count=0, possible_prisoners_count=0, patient_missing=())
        by = {r.source: r.count for r in resolver.resolve_mission(m).requirements}
        self.assertEqual(by.get("nef"), 3)

    def test_nef_pool_excludes_patients_already_covered_by_rth(self):
        # A patient already getting the helicopter's own Notarzt doesn't also
        # need a ground NEF -- only the OTHER patients count toward the
        # below-5 pairing.
        from app.domain.catalog import MissionCatalog, MissionType
        from app.domain.resolver import MissionRequirementResolver

        mt = MissionType(id=913, name="Med", average_credits=100, requirements={}, chances={})
        resolver = MissionRequirementResolver(MissionCatalog({913: mt}), self.resolver._library)
        m = SimpleNamespace(mission_type_id=913, patients_count=3, possible_patients_count=3,
                             prisoners_count=0, possible_prisoners_count=0,
                             patient_missing=("Wir benötigen: RTH",))
        by = {r.source: r.count for r in resolver.resolve_mission(m).requirements}
        self.assertEqual(by.get("rescue_helicopter"), 1)
        self.assertEqual(by.get("nef"), 2)   # 3 patients - 1 RTH-covered = 2

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

    def test_resolve_mission_vehicle_name_under_personnel_not_headcount_converted(self):
        # Live-verified 2026-09-11 (mission #4397704222 "Person in Wasser"):
        # the game puts "2 GW-Wasserrettung" -- a VEHICLE category (GW-W,
        # type 64), not a crew role -- under the `personnel` group. Must
        # resolve as 2 vehicles directly, NOT run through the headcount/crew
        # conversion (which would silently drop it to 0, since it isn't in
        # the staff table at all -- the original bug).
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":null,"personnel":"2 GW-Wasserrettung","other":""}')
        res = self.resolver.resolve_mission(m)
        by = {r.source: r.count for r in res.requirements}
        self.assertEqual(by.get("GW-Wasserrettung"), 2)
        self.assertEqual(res.unmatched, [])

    def test_resolve_mission_ausbildung_qualification_texts_resolve_to_their_guaranteed_vehicle(self):
        # Live-reported (Sasha, 2026-09-14): "1 Person mit
        # GW-Gefahrgut-Ausbildung" under `personnel` -- a qualification
        # phrasing, not the plain vehicle name "GW-Gefahrgut" already in the
        # table -- was previously unmapped and silently dropped, same class
        # of gap as the GW-Wasserrettung fix above. User: "for this and any
        # -ausbildung requirements send a vehicle that has exactly that
        # education guaranteed" -- researched every OTHER "-Ausbildung"
        # extend-rule across data/de/vehicles/*.mscv (the authoritative
        # per-vehicle crew-qualification source the regex engine already
        # trusts) and added the same STAFF-table mapping for each, so the
        # existing headcount->vehicle ceil-division machinery
        # (_personnel_to_vehicles, via VehicleType.planning_personnel's new
        # _AUSBILDUNG_CREW_PER_VEHICLE override) reproduces the SAME tiering
        # each .mscv's own extend rule specifies -- NOT the vehicle table's
        # literal-count route, which would ask for e.g. 7 vehicles when the
        # game only ever needs 2: GW-Taucher (63, 69; 2 crew/vehicle),
        # GW-Wasserrettung (69 only -- NOT the plain GW-W vehicle, type 64;
        # 2 crew/vehicle), Dekon-P (53, 54; 6 crew/vehicle), and ELW-2
        # (34 only -- NOT the broader ELW1/ELW2/drone class [34,78,129],
        # since only type 34's own .mscv carries this qualification; 9
        # crew/vehicle, i.e. always just 1).
        cases = [
            ("1 Person mit GW-Gefahrgut-Ausbildung", "Person mit GW-Gefahrgut-Ausbildung", 1),
            ("3 Personen mit GW-Taucher Ausbildung", "Personen mit GW-Taucher Ausbildung", 2),  # ceil(3/2)
            ("5 Personen mit GW-Wasserrettung Ausbildung", "Personen mit GW-Wasserrettung Ausbildung", 3),  # ceil(5/2)
            ("7 Personen mit Dekon-P-Ausbildung", "Personen mit Dekon-P-Ausbildung", 2),  # ceil(7/6)
            ("1 Person mit ELW-2 Ausbildung", "Person mit ELW-2 Ausbildung", 1),
        ]
        for personnel_text, source, expected_count in cases:
            with self.subTest(personnel_text=personnel_text):
                m = SimpleNamespace(
                    mission_type_id=2, patients_count=0, possible_patients_count=0,
                    prisoners_count=0, possible_prisoners_count=0,
                    missing_text=f'{{"vehicles":null,"personnel":"{personnel_text}","other":""}}',
                )
                res = self.resolver.resolve_mission(m)
                by = {r.source: r.count for r in res.requirements}
                self.assertEqual(by.get(source), expected_count)
                self.assertEqual(res.unmatched, [])

    def test_elw2_ausbildung_maps_only_to_the_type_whose_mscv_confirms_it(self):
        # NOT the broader ELW1/ELW2/drone class -- only type 34's .mscv
        # carries the crew-qualification extend rule; 78/129 don't.
        headcount, vehicle = self.resolver._missing.resolve_personnel(
            "1 Person mit ELW-2 Ausbildung"
        )
        self.assertEqual(vehicle.requirements, [])
        self.assertEqual(len(headcount.requirements), 1)
        self.assertEqual(headcount.requirements[0].type_ids, frozenset({34}))

    def test_gw_wasserrettung_ausbildung_maps_only_to_69_not_the_plain_gw_w_vehicle(self):
        # Type 64 is the plain GW-Wasserrettung VEHICLE; the crew-
        # qualification extend rule lives only in type 69's (GW-Taucher/
        # Tauchkraftwagen) .mscv, a different vehicle entirely.
        headcount, vehicle = self.resolver._missing.resolve_personnel(
            "1 Person mit GW-Wasserrettung Ausbildung"
        )
        self.assertEqual(vehicle.requirements, [])
        self.assertEqual(len(headcount.requirements), 1)
        self.assertEqual(headcount.requirements[0].type_ids, frozenset({69}))

    def test_resolve_personnel_two_different_staff_categories_route_separately(self):
        # Code review (2026-09-14, LOW): the existing mixed-chunk precedent
        # only covers one staff hit + one vehicle-table hit in the same
        # string -- confirm TWO DIFFERENT staff-table hits (a genuine crew
        # role plus a new -Ausbildung qualification) don't get collapsed or
        # cross-contaminated, each keeping its own type_ids/count.
        headcount, vehicle = self.resolver._missing.resolve_personnel(
            "8 Feuerwehrmann, 3 Personen mit GW-Taucher Ausbildung"
        )
        self.assertEqual(vehicle.requirements, [])
        self.assertEqual(len(headcount.requirements), 2)
        by_source = {r.source: r for r in headcount.requirements}
        self.assertEqual(by_source["Feuerwehrmann"].count, 8)
        self.assertEqual(by_source["Personen mit GW-Taucher Ausbildung"].count, 3)
        self.assertEqual(by_source["Personen mit GW-Taucher Ausbildung"].type_ids, frozenset({63, 69}))

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

    def test_missing_text_revealed_then_gone_is_fully_covered_not_catalogue(self):
        # AC-M1-04 (Sasha, 2026-10-27): once missing_text has revealed a real
        # category, a LATER cycle where it shows nothing at all (the raw
        # field itself absent, not merely empty-but-present) means "we have
        # everything" -- must NOT re-derive the catalogue's firetrucks:2
        # estimate for fixture type 2.
        m = SimpleNamespace(id=1, mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"1 ELW 1","personnel":"","other":""}')
        first = self.resolver.resolve_mission(m)
        self.assertTrue(first.requirements)  # sanity: it did reveal something

        m.missing_text = ""  # gone entirely on a later cycle
        second = self.resolver.resolve_mission(m)
        self.assertEqual(second.requirements, [])
        self.assertEqual(second.unmatched, [])

    def test_missing_text_never_revealed_still_uses_catalogue(self):
        # AC-M1-05, regression guard: a mission that has NEVER revealed
        # anything still falls back to the catalogue estimate -- the two
        # "nothing revealed right now" states are not conflated.
        m = SimpleNamespace(id=2, mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0, missing_text="")
        res = self.resolver.resolve_mission(m)
        self.assertTrue(res.requirements)  # the catalogue's firetrucks/platform/chief estimate

    def test_missing_text_revealed_again_after_going_quiet_is_a_fresh_need(self):
        # AC-M1-06: revealed, then gone (covered), then revealed again with a
        # genuinely new category -- the "ever revealed" memory must not
        # suppress this later, real need.
        m = SimpleNamespace(id=3, mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"1 ELW 1","personnel":"","other":""}')
        self.resolver.resolve_mission(m)
        m.missing_text = ""
        covered = self.resolver.resolve_mission(m)
        self.assertEqual(covered.requirements, [])

        m.missing_text = '{"vehicles":"1 Rüstwagen oder HLF","personnel":"","other":""}'
        third = self.resolver.resolve_mission(m)
        self.assertTrue(third.requirements)
        self.assertEqual(third.requirements[0].delta_scope, "en_route_only")

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
        # Below 5 patients every ambulance is paired with a NEF (Sasha,
        # 2026-09-16) -- the explicit "Wir benötigen: NEF" reveal (1) is
        # superseded by the generosity heuristic (3), not the other way round.
        self.assertEqual(by.get("nef"), 3)

    # -- Capacity math: Sonderlöschmittel / Wasser / Pumpenleistung (Sasha, 2026-09-22) --
    def test_sonderloeschmittel_single_slf_covers_it(self):
        # Confirmed live 2026-09-21, mission #519 "Hüpfburgunfall": "Uns
        # fehlt: 1950 Sonderlöschmittel" -- SLF (type 167) carries 5000L,
        # the only vehicle worth tracking for this per Sasha.
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"","personnel":"","other":"1950 Sonderlöschmittel"}')
        res = self.resolver.resolve_mission(m)
        by = {r.source: r for r in res.requirements}
        self.assertIn("sonderloeschmittel", by)
        self.assertEqual(by["sonderloeschmittel"].count, 1)
        self.assertEqual(set(by["sonderloeschmittel"].type_ids), {167})

    def test_sonderloeschmittel_needs_multiple_slf(self):
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"","personnel":"","other":"12000 Sonderlöschmittel"}')
        by = {r.source: r for r in self.resolver.resolve_mission(m).requirements}
        self.assertEqual(by["sonderloeschmittel"].count, 3)  # ceil(12000 / 5000)

    def test_wasser_capacity_converts_to_vehicle_count(self):
        # Scoped to Sasha's own table (2026-09-22): max water among those is
        # 10000L (GTLF, type 121) -- NOT the full reference file's 20000L
        # outlier from a type Sasha never listed.
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"","personnel":"","other":"3200 l. Wasser"}')
        by = {r.source: r for r in self.resolver.resolve_mission(m).requirements}
        self.assertIn("wasser", by)
        self.assertEqual(by["wasser"].count, 1)  # ceil(3200 / 10000)
        self.assertIn(121, by["wasser"].type_ids)
        # The exotic dedicated tankers Sasha didn't list must never appear.
        self.assertNotIn(166, by["wasser"].type_ids)
        self.assertNotIn(120, by["wasser"].type_ids)

    def test_pumpenleistung_capacity_converts_to_vehicle_count(self):
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"","personnel":"","other":"3500 l/min Pumpenleistung"}')
        by = {r.source: r for r in self.resolver.resolve_mission(m).requirements}
        self.assertIn("pumpenleistung", by)
        self.assertEqual(by["pumpenleistung"].count, 2)  # ceil(3500 / 2000)
        self.assertNotIn(121, by["pumpenleistung"].type_ids)  # GTLF has no pump

    def test_capacity_entry_coexists_with_plain_category_entry_in_other_group(self):
        # A capacity-phrased entry and a plain category entry can appear
        # together in the same `other` group -- both must resolve correctly.
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"","personnel":"",'
                                         '"other":"1950 Sonderlöschmittel, 1 Feuerlöschpumpe (z. B. LF)"}')
        res = self.resolver.resolve_mission(m)
        by = {r.source: r.count for r in res.requirements}
        self.assertEqual(by.get("sonderloeschmittel"), 1)
        self.assertEqual(by.get("Feuerlöschpumpe (z. B. LF)"), 1)

    def test_resolve_mission_unmatched_missing_text_category_reported(self):
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"1 Marsrover","personnel":"","other":""}')
        res = self.resolver.resolve_mission(m)
        self.assertEqual(res.requirements, [])
        self.assertIn("1 Marsrover", res.unmatched)

    # -- live bugs found 2026-10-01 (mission #4420055266 "Berittene Streife",
    # #4420164223 "Brand in Mehrfamilienhaus") ---------------------------------
    def test_police_horse_catalog_class_resolves(self):
        # Confirmed live: catalog_reqs={'police_horse': 2} for an untouched
        # mission -> was "no requirements found" because `police_horse` had
        # no Vehicle.mscv class mapping at all (unlike the text-based
        # "Polizeipferd" mapping, which is a separate missing_text table).
        from app.domain.catalog import MissionCatalog, MissionType
        from app.domain.resolver import MissionRequirementResolver

        mt = MissionType(id=837, name="Berittene Streife", average_credits=100,
                          requirements={"police_horse": 2}, chances={})
        resolver = MissionRequirementResolver(MissionCatalog({837: mt}), self.resolver._library)
        m = SimpleNamespace(mission_type_id=837, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0, missing_text="")
        res = resolver.resolve_mission(m)
        by = {r.source: r for r in res.requirements}
        self.assertIn("police_horse", by)
        self.assertEqual(by["police_horse"].count, 2)
        self.assertEqual(set(by["police_horse"].type_ids), {134, 135, 136})
        self.assertEqual(res.unmatched, [])

    def test_betreuungshelfer_nx_format_converts_via_confirmed_capacity(self):
        # Confirmed live: missing_text personnel group = "5x Betreuungshelfer"
        # -- the "Nx" format (no space before the x) previously failed to
        # parse as a count+category pair at all (fell through as one
        # unmatched blob "5x Betreuungshelfer"). ceil(5/3) = 2 (the smallest
        # crew across the eligible SEG pool, confirmed from each vehicle's
        # own .mscv extend-rule tiers / the live vehicle API's staff data).
        # Fixture mission type 1 has no care_includes_staff_members flag, so
        # only the always-eligible SEG pool applies -- not the Feuerwehr/THW
        # responder-only one.
        m = SimpleNamespace(mission_type_id=1, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":null,"personnel":"5x Betreuungshelfer","other":""}')
        res = self.resolver.resolve_mission(m)
        by = {r.source: r for r in res.requirements}
        self.assertIn("betreuungshelfer", by)
        self.assertEqual(by["betreuungshelfer"].count, 2)
        self.assertEqual(set(by["betreuungshelfer"].type_ids), {130, 131, 133})
        self.assertEqual(res.unmatched, [])

    def test_betreuungshelfer_widens_to_feuerwehr_and_thw_when_staff_must_be_cared_for(self):
        # Sasha, 2026-10-24, confirmed live against mission 505/abcde ("490
        # Betroffene und 732 Einsatzkräfte müssen versorgt werden"): a mission
        # whose own responders also need care additionally accepts the
        # Feuerwehr (GW-Verpflegung/GW-Küche/MTW-Verpflegung) and THW
        # (LKW 7 Lbw FGr Log-V/MTW-FGr Log-V) care-service vehicles -- useless
        # on a Betroffene-only mission (they can't help civilians), so they're
        # excluded unless the mission's catalogue says staff need care too.
        from app.domain.catalog import MissionCatalog, MissionType

        mtype = MissionType(id=900, name="Hochwasserschadenslage", average_credits=22975,
                             requirements={}, care_includes_staff_members=True)
        resolver = MissionRequirementResolver(MissionCatalog({900: mtype}), self.resolver.library)
        m = SimpleNamespace(mission_type_id=900, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":null,"personnel":"10x Betreuungshelfer","other":""}')
        res = resolver.resolve_mission(m)
        by = {r.source: r for r in res.requirements}
        self.assertEqual(set(by["betreuungshelfer"].type_ids), {130, 131, 133, 138, 139, 140, 176, 177})

    def test_verpflegungshelfer_nx_format_also_converts(self):
        m = SimpleNamespace(mission_type_id=1, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"","personnel":"9x Verpflegungshelfer","other":""}')
        by = {r.source: r.count for r in self.resolver.resolve_mission(m).requirements}
        self.assertEqual(by.get("betreuungshelfer"), 3)  # ceil(9/3)

    def test_nx_format_coexists_with_plain_personnel_entry(self):
        m = SimpleNamespace(mission_type_id=1, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"","personnel":"5x Betreuungshelfer, 3 Feuerwehrleute","other":""}')
        res = self.resolver.resolve_mission(m)
        by = {r.source: r.count for r in res.requirements}
        self.assertEqual(by.get("betreuungshelfer"), 2)
        self.assertIn("Feuerwehrleute", by)
        self.assertEqual(res.unmatched, [])

    # -- found via a systematic .mscv sweep, 2026-10-01 -------------------------
    def test_polizisten_headcount_converts_via_confirmed_fustw_capacity(self):
        # Every type in the "Polizisten" staff-table entry has
        # personnel=0/planning_personnel=0 in AssetLibrary -- without this
        # dedicated conversion, _personnel_to_vehicles' defensive fallback
        # would request 5 literal vehicles instead of ceil(5/2)=3 FuStW
        # (confirmed via type 32's own .mscv extend-rule tiers: 1-2->1,
        # 3-4->2, 5-6->3, ...).
        m = SimpleNamespace(mission_type_id=1, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"","personnel":"5 Polizisten","other":""}')
        by = {r.source: r.count for r in self.resolver.resolve_mission(m).requirements}
        self.assertEqual(by.get("polizisten"), 3)

    def test_polizisten_nx_format_also_converts(self):
        m = SimpleNamespace(mission_type_id=1, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"","personnel":"10x Polizisten","other":""}')
        by = {r.source: r.count for r in self.resolver.resolve_mission(m).requirements}
        self.assertEqual(by.get("polizisten"), 5)  # ceil(10/2)

    def test_polizeipferde_converts_via_factors_not_as_a_literal_vehicle_count(self):
        # The LSSM table's "factors" field ({134: 2, 135: 4, 136: 2}) has
        # always been silently ignored by _build_table, so "N Polizeipferde"
        # was treated like any other vehiclesByRequirement entry -- a literal
        # vehicle count. Each vehicle's own .mscv extend-rule tiers
        # independently confirm the same divisors, so 7 horses should need
        # ceil(7/2)=4 vehicles, not 7.
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"7 Polizeipferde","personnel":"","other":""}')
        by = {r.source: r for r in self.resolver.resolve_mission(m).requirements}
        self.assertIn("polizeipferd", by)
        self.assertEqual(by["polizeipferd"].count, 4)
        self.assertEqual(set(by["polizeipferd"].type_ids), {134, 135, 136})

    def test_polizeipferde_also_converts_if_it_arrives_in_the_other_group(self):
        # Not live-confirmed which raw group this text actually arrives in,
        # so it's intercepted in both `vehicles` and `other`.
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"","personnel":"","other":"3 Polizeipferd"}')
        by = {r.source: r.count for r in self.resolver.resolve_mission(m).requirements}
        self.assertEqual(by.get("polizeipferd"), 2)  # ceil(3/2)

    def test_schaum_is_an_alternate_sonderloeschmittel_phrasing(self):
        # Confirmed directly in SLF's (type 167) own .mscv extend-rule text
        # as an alternate spelling of the same foam need "N Sonderlöschmittel"
        # already covers for the same type -- not live-confirmed in a real
        # mission yet, but authoritative straight from the vehicle's own
        # definition.
        m = SimpleNamespace(mission_type_id=2, patients_count=0, possible_patients_count=0,
                            prisoners_count=0, possible_prisoners_count=0,
                            missing_text='{"vehicles":"","personnel":"","other":"1950 l. Schaum"}')
        by = {r.source: r for r in self.resolver.resolve_mission(m).requirements}
        self.assertIn("sonderloeschmittel", by)
        self.assertEqual(by["sonderloeschmittel"].count, 1)
        self.assertEqual(set(by["sonderloeschmittel"].type_ids), {167})


if __name__ == "__main__":
    unittest.main()
