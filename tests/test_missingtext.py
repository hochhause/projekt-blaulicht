import unittest

from app.domain.missingtext import (
    MissingTextGroups,
    MissingTextResolver,
    default_resolver,
    parse_missing_text,
)

# A small, self-contained table mirroring the real
# data/reference/lssm-missing-requirements.json shape, so these tests don't
# depend on (or need to enumerate) the full 88-entry LSSM table.
SAMPLE_DATA = {
    "vehiclesByRequirement": [
        {"texts": ["Löschfahrzeug (LF)", "Löschfahrzeuge (LF)"],
         "vehicles": [0, 1, 30],
         "conditionalVehicles": {"allow_rw_instead_of_lf": [4, 39, 47]}},
        {"texts": ["ELW 1"], "vehicles": [3]},
        {"texts": ["RTW"], "vehicles": [28, 74, 97]},
        {"texts": ["GW-Öl"], "vehicles": [10, 49], "equipment": ["oil"]},
        {"texts": ["Schlauchwagen (GW-L2 Wasser, SW 1000, SW 2000 oder Ähnliches)"],
         "vehicles": [11, 13]},
        {"texts": ["GW-Wasserrettung", "GW-Wasserrettungen"], "vehicles": [64]},
    ],
    "staff": [
        {"texts": ["Feuerwehrmann", "Feuerwehrleute"], "vehicles": [0, 1, 30]},
    ],
}


class ParseMissingTextTests(unittest.TestCase):
    def test_empty_is_no_demand(self):
        groups = parse_missing_text("")
        self.assertEqual(groups, MissingTextGroups())
        self.assertFalse(groups.has_vehicle_demand)
        self.assertFalse(groups.revealed)

    def test_none_is_no_demand(self):
        self.assertFalse(parse_missing_text(None).has_vehicle_demand)
        self.assertFalse(parse_missing_text(None).revealed)

    def test_plain_untouched_string_is_no_demand(self):
        # Untouched missions carry no missing_text at all in practice, but a
        # non-JSON value should degrade gracefully rather than crash.
        groups = parse_missing_text("some plain string")
        self.assertFalse(groups.has_vehicle_demand)
        self.assertFalse(groups.revealed)

    def test_parses_all_three_groups(self):
        raw = '{"vehicles":"1 ELW 1","personnel":"1 Feuerwehrmann","other":"1 Feuerlöschpumpe"}'
        groups = parse_missing_text(raw)
        self.assertEqual(groups.vehicles, "1 ELW 1")
        self.assertEqual(groups.personnel, "1 Feuerwehrmann")
        self.assertEqual(groups.other, "1 Feuerlöschpumpe")
        self.assertTrue(groups.has_vehicle_demand)
        self.assertTrue(groups.revealed)

    def test_malformed_json_is_no_demand(self):
        groups = parse_missing_text("{not json")
        self.assertFalse(groups.has_vehicle_demand)
        self.assertFalse(groups.revealed)

    def test_nbsp_between_count_and_category_is_normalized(self):
        # Live-verified 2026-09-11 (mission #4397737525 "Straße unter
        # Wasser"): the game renders these strings with U+00A0 (non-breaking
        # space) wherever a plain space would go -- between the count and the
        # category, and inside abbreviations like "z. B.". Un-normalized,
        # "Löschfahrzeug\xa0(LF)" doesn't match the reference table's
        # plain-space "Löschfahrzeug (LF)" key, so a real, visible need
        # silently resolved to nothing.
        raw = ('{"vehicles":"1\xa0Löschfahrzeug\xa0(LF)","personnel":"",'
               '"other":"1\xa0Feuerlöschpumpe\xa0(z.\xa0B.\xa0LF)"}')
        groups = parse_missing_text(raw)
        self.assertEqual(groups.vehicles, "1 Löschfahrzeug (LF)")
        self.assertEqual(groups.other, "1 Feuerlöschpumpe (z. B. LF)")
        self.assertTrue(groups.has_vehicle_demand)

    def test_other_unicode_space_variants_are_also_normalized(self):
        # Not yet observed live, but the game is already inconsistent about
        # which invisible space codepoint it uses -- guard against the same
        # failure mode recurring for U+2009 (thin space) / U+202F (narrow
        # no-break space) etc. by normalizing the same \s class the count and
        # split regexes already tolerate, not just U+00A0.
        raw = '{"vehicles":"1 ELW 1","personnel":"","other":""}'
        groups = parse_missing_text(raw)
        self.assertEqual(groups.vehicles, "1 ELW 1")

    def test_missing_keys_default_empty(self):
        groups = parse_missing_text('{"vehicles":"1 RTW"}')
        self.assertEqual(groups.vehicles, "1 RTW")
        self.assertEqual(groups.personnel, "")
        self.assertEqual(groups.other, "")
        self.assertTrue(groups.revealed)

    def test_revealed_but_all_groups_empty_is_still_revealed(self):
        # The authoritative "nothing more needed" case — distinct from an
        # untouched mission (missing_text absent entirely).
        groups = parse_missing_text('{"vehicles":"","personnel":"","other":""}')
        self.assertFalse(groups.has_vehicle_demand)
        self.assertTrue(groups.revealed)

    def test_long_text_only_blob_is_not_revealed(self):
        # Live-verified 2026-08-31: some UNTOUCHED (still red, vehicle_state=0)
        # prisoner-related missions carry {"long_text": "..."} with NONE of
        # vehicles/personnel/other present at all -- a flavor-text hint, not
        # a vehicle-need reveal. Must NOT be treated as "nothing more needed"
        # (that would wrongly skip the catalogue estimate for a mission that
        # clearly still needs vehicles).
        groups = parse_missing_text('{"long_text":"Gefangene sollen abtransportiert werden."}')
        self.assertFalse(groups.revealed)
        self.assertFalse(groups.has_vehicle_demand)


class MissingTextResolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resolver = MissingTextResolver(SAMPLE_DATA)

    def test_resolves_counted_category(self):
        req = self.resolver.resolve_line("4 Löschfahrzeuge (LF)")
        self.assertEqual(req.source, "Löschfahrzeuge (LF)")
        self.assertEqual(req.count, 4)
        self.assertEqual(set(req.type_ids), {0, 1, 30})

    def test_resolves_x_prefixed_count(self):
        # Confirmed live 2026-10-01 ("5x Betreuungshelfer") and directly in
        # several vehicles' own .mscv extend-rule text (e.g. 134/135/136.mscv:
        # "\d+x Reiterstaffel") -- the "Nx Category" format, no space before
        # the x, must resolve identically to the plain "N Category" form.
        req = self.resolver.resolve_line("4x Löschfahrzeuge (LF)")
        self.assertEqual(req.count, 4)
        self.assertEqual(set(req.type_ids), {0, 1, 30})

    def test_bare_category_defaults_to_count_one(self):
        # A category whose own text happens to start with a digit-like token
        # ("ELW 1") must not be mistaken for a count prefix.
        req = self.resolver.resolve_line("ELW 1")
        self.assertEqual(req.count, 1)
        self.assertEqual(set(req.type_ids), {3})

    def test_count_prefixed_category_with_trailing_number(self):
        req = self.resolver.resolve_line("1 ELW 1")
        self.assertEqual(req.count, 1)
        self.assertEqual(set(req.type_ids), {3})

    def test_conditional_vehicles_never_added(self):
        # allow_rw_instead_of_lf types (4, 39, 47) are a game-setting-gated
        # extra — off by default, never folded into the base type set.
        req = self.resolver.resolve_line("1 Löschfahrzeug (LF)")
        self.assertEqual(set(req.type_ids), {0, 1, 30})
        self.assertNotIn(4, req.type_ids)

    def test_equipment_gated_category_not_filtered(self):
        # Conservative fallback: no per-vehicle equipment state tracked yet,
        # so every carrier of the base type counts (a failed dispatch strikes).
        req = self.resolver.resolve_line("1 GW-Öl")
        self.assertEqual(set(req.type_ids), {10, 49})

    def test_unknown_category_is_unmatched_not_dropped(self):
        self.assertIsNone(self.resolver.resolve_line("1 Marsrover"))

    def test_staff_category_not_matched_by_resolve_line(self):
        # resolve_line/resolve only look at the VEHICLE table now (staff role
        # text never appears in "vehicles"/"other" groups in practice) --
        # staff categories are resolved via resolve_personnel instead (below).
        self.assertIsNone(self.resolver.resolve_line("8 Feuerwehrmann"))

    def test_staff_category_resolves_via_resolve_personnel_as_headcount(self):
        headcount, vehicle = self.resolver.resolve_personnel("8 Feuerwehrmann")
        self.assertEqual(vehicle.requirements, [])
        req = headcount.requirements[0]
        self.assertEqual(req.count, 8)
        self.assertEqual(set(req.type_ids), {0, 1, 30})

    def test_vehicle_category_under_personnel_resolves_as_direct_vehicle_count(self):
        # Live-verified 2026-09-11 (mission #4397704222 "Person in Wasser"):
        # the game puts "2 GW-Wasserrettung" -- a VEHICLE category, GW-W type
        # 64 -- under the `personnel` group by mistake. resolve_personnel must
        # recognize it via the vehicle table and NOT run it through headcount
        # conversion (it's already a vehicle count, not a headcount).
        headcount, vehicle = self.resolver.resolve_personnel("2 GW-Wasserrettung")
        self.assertEqual(headcount.requirements, [])
        req = vehicle.requirements[0]
        self.assertEqual(req.count, 2)

    def test_resolve_personnel_unmatched_category(self):
        headcount, vehicle = self.resolver.resolve_personnel("3 Marsrover")
        self.assertEqual(headcount.requirements, [])
        self.assertEqual(vehicle.requirements, [])
        self.assertEqual(headcount.unmatched, ["Marsrover"])

    def test_resolve_personnel_mixed_staff_and_vehicle_chunks_route_separately(self):
        # A mission could plausibly need both remaining crew AND a
        # mislabeled-vehicle category in the same `personnel` string --
        # confirm each chunk routes to its own bucket independently, not
        # just when it's the only chunk present.
        headcount, vehicle = self.resolver.resolve_personnel("8 Feuerwehrmann, 2 GW-Wasserrettung")
        self.assertEqual(len(headcount.requirements), 1)
        self.assertEqual(headcount.requirements[0].count, 8)
        self.assertEqual(len(vehicle.requirements), 1)
        self.assertEqual(vehicle.requirements[0].count, 2)
        self.assertEqual(headcount.unmatched, [])
        self.assertEqual(vehicle.unmatched, [])

    def test_resolve_splits_multiple_categories(self):
        res = self.resolver.resolve("4 Löschfahrzeuge (LF), 1 ELW 1")
        by = {r.source: r.count for r in res.requirements}
        self.assertEqual(by["Löschfahrzeuge (LF)"], 4)
        self.assertEqual(by["ELW 1"], 1)
        self.assertEqual(res.unmatched, [])

    def test_resolve_does_not_split_commas_inside_a_category(self):
        # This category's OWN display text contains commas — splitting on
        # every comma would shatter it into unmatched garbage.
        res = self.resolver.resolve(
            "1 Schlauchwagen (GW-L2 Wasser, SW 1000, SW 2000 oder Ähnliches)"
        )
        self.assertEqual(len(res.requirements), 1)
        self.assertEqual(res.requirements[0].count, 1)
        self.assertEqual(set(res.requirements[0].type_ids), {11, 13})
        self.assertEqual(res.unmatched, [])

    def test_resolve_reports_unmatched_alongside_matched(self):
        res = self.resolver.resolve("1 ELW 1, 2 Marsrover")
        self.assertEqual(res.requirements[0].source, "ELW 1")
        self.assertEqual(res.unmatched, ["2 Marsrover"])

    def test_resolve_empty_string_is_empty_resolution(self):
        res = self.resolver.resolve("")
        self.assertEqual(res.requirements, [])
        self.assertEqual(res.unmatched, [])

    def test_load_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            MissingTextResolver.load("does/not/exist.json")


class DefaultResolverTests(unittest.TestCase):
    """Sanity checks against the real committed LSSM mapping data."""

    def test_loads_real_data_file(self):
        resolver = default_resolver()
        req = resolver.resolve_line("4 Löschfahrzeuge (LF)")
        self.assertIsNotNone(req)
        self.assertEqual(req.count, 4)
        self.assertIn(0, req.type_ids)  # LF (type 0) is a real Löschfahrzeug

    def test_is_cached_singleton(self):
        self.assertIs(default_resolver(), default_resolver())

    def test_live_nbsp_pump_text_resolves_end_to_end(self):
        # Mission #4397737525 "Straße unter Wasser", verbatim missing_text.
        raw = ('{"vehicles":"1\xa0Löschfahrzeug\xa0(LF)","personnel":"",'
               '"other":"1\xa0Feuerlöschpumpe\xa0(z.\xa0B.\xa0LF)"}')
        groups = parse_missing_text(raw)
        resolver = default_resolver()
        vehicles = resolver.resolve(groups.vehicles)
        other = resolver.resolve(groups.other)
        self.assertEqual(vehicles.unmatched, [])
        self.assertEqual(other.unmatched, [])
        self.assertTrue(vehicles.requirements)
        self.assertTrue(other.requirements)

    def test_live_gw_wasserrettung_under_personnel_resolves_as_vehicle(self):
        # Mission #4397704222 "Person in Wasser", verbatim missing_text.
        raw = '{"vehicles":null,"personnel":"2 GW-Wasserrettung","other":""}'
        groups = parse_missing_text(raw)
        resolver = default_resolver()
        headcount, vehicle = resolver.resolve_personnel(groups.personnel)
        self.assertEqual(headcount.requirements, [])
        self.assertEqual(headcount.unmatched, [])
        self.assertEqual(len(vehicle.requirements), 1)
        req = vehicle.requirements[0]
        self.assertEqual(req.count, 2)
        self.assertIn(64, req.type_ids)  # GW-W is vehicle type 64

    def test_seg_elw_is_a_generic_elw1_substitute(self):
        # Confirmed live 2026-09-11: SEG-ELW (59) is "usable as a plain ELW"
        # in addition to its own dedicated "ELW 1 (SEG)" category.
        resolver = default_resolver()
        req = resolver.resolve_line("1 ELW 1")
        self.assertIn(59, req.type_ids)

    def test_elw2_drohne_is_in_the_drone_category(self):
        # Type 129 ("ELW2 Drohne") already matched "Drohneneinheit(en)" in
        # the live table -- this locks that in against regression.
        resolver = default_resolver()
        req = resolver.resolve_line("1 Drohneneinheit")
        self.assertIn(129, req.type_ids)


if __name__ == "__main__":
    unittest.main()
