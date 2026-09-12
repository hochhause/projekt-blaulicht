import unittest

from app.core.account_config import DEFAULT_CONFIG, AccountConfig, GroupConfig


class AccountConfigTests(unittest.TestCase):
    def test_defaults_map_to_policies(self):
        cfg = DEFAULT_CONFIG
        dp = cfg.dispatch_policy()
        self.assertEqual(dp.radius_km, 40.0)
        tp = cfg.transport_policy()
        self.assertTrue(tp.prefer_own)

    def test_roundtrip_dict(self):
        cfg = AccountConfig(radius_km=25, own_credit_min=100, max_vehicles_per_mission=6)
        again = AccountConfig.from_dict(cfg.to_dict())
        self.assertEqual(again, cfg)

    def test_from_dict_ignores_unknown_and_keeps_defaults(self):
        cfg = AccountConfig.from_dict({"radius_km": 12, "bogus": 1})
        self.assertEqual(cfg.radius_km, 12)
        self.assertEqual(cfg.poll_seconds, 45.0)  # default preserved

    def test_from_none(self):
        self.assertEqual(AccountConfig.from_dict(None), AccountConfig())

    def test_legacy_mode_booleans_migrate_to_scopes(self):
        # Old stored configs used booleans; they must map to the equivalent scope
        # so existing accounts keep behaving (parasite was alliance-only).
        cfg = AccountConfig.from_dict(
            {"parasite_mode": True, "top_up_mode": True, "patient_only": True}
        )
        self.assertEqual(cfg.parasite_scope, "alliance")
        self.assertEqual(cfg.top_up_scope, "both")
        self.assertEqual(cfg.patient_scope, "both")
        dp = cfg.dispatch_policy()
        self.assertEqual(dp.parasite_scope.value, "alliance")
        self.assertEqual(dp.patient_scope.value, "both")

    def test_default_mission_source_is_own_and_alliance(self):
        from app.domain.dispatch import MissionSource
        dp = DEFAULT_CONFIG.dispatch_policy()
        self.assertEqual(dp.mission_source, frozenset({MissionSource.OWN, MissionSource.ALLIANCE}))

    def test_legacy_mission_source_string_migrates_to_a_tuple(self):
        # Old stored configs saved mission_source as a single exclusive
        # string; must migrate to the equivalent multi-select tuple so
        # existing accounts keep behaving exactly the same (Sasha, 2026-09-12
        # added the "event" tag as a third, independent, additive option).
        from app.domain.dispatch import MissionSource
        self.assertEqual(AccountConfig.from_dict({"mission_source": "own"}).mission_source, ("own",))
        self.assertEqual(AccountConfig.from_dict({"mission_source": "alliance"}).mission_source, ("alliance",))
        both = AccountConfig.from_dict({"mission_source": "both"})
        self.assertEqual(set(both.mission_source), {"own", "alliance"})
        self.assertEqual(
            both.dispatch_policy().mission_source,
            frozenset({MissionSource.OWN, MissionSource.ALLIANCE}),
        )

    def test_mission_source_multiselect_roundtrips_and_maps_to_policy(self):
        from app.domain.dispatch import MissionSource
        cfg = AccountConfig(mission_source=("own", "event"))
        again = AccountConfig.from_dict(cfg.to_dict())
        self.assertEqual(again.mission_source, ("own", "event"))
        self.assertEqual(
            again.dispatch_policy().mission_source, frozenset({MissionSource.OWN, MissionSource.EVENT})
        )

    def test_mission_source_empty_tuple_maps_to_empty_frozenset(self):
        # A deliberately empty selection means "normal dispatch works
        # nothing" -- not a silent fallback to a default.
        cfg = AccountConfig(mission_source=())
        self.assertEqual(cfg.dispatch_policy().mission_source, frozenset())

    def test_mission_source_unknown_entries_are_dropped_not_defaulted(self):
        from app.domain.dispatch import MissionSource
        cfg = AccountConfig(mission_source=("own", "bogus"))
        self.assertEqual(cfg.dispatch_policy().mission_source, frozenset({MissionSource.OWN}))

    def test_smart_order_maps_through(self):
        from app.domain.dispatch import DispatchOrder
        self.assertEqual(AccountConfig(order="smart").dispatch_policy().order, DispatchOrder.SMART)

    def test_mode_scopes_roundtrip(self):
        cfg = AccountConfig(parasite_scope="alliance", patient_scope="own")
        again = AccountConfig.from_dict(cfg.to_dict())
        self.assertEqual(again.parasite_scope, "alliance")
        self.assertEqual(again.patient_scope, "own")

    def test_legacy_parasite_mode_boolean_does_not_collide_with_aggressiveness(self):
        # The legacy `parasite_mode` key was a BOOLEAN; the new
        # `parasite_aggressiveness` field is a distinctly-named string enum to
        # avoid colliding with it. A stored `parasite_mode: true` must still
        # only drive the scope migration, leaving aggressiveness at its
        # default (active).
        cfg = AccountConfig.from_dict({"parasite_mode": True})
        self.assertEqual(cfg.parasite_scope, "alliance")
        self.assertEqual(cfg.parasite_aggressiveness, "active")
        self.assertEqual(cfg.dispatch_policy().parasite_mode.value, "active")

    def test_parasite_aggressiveness_roundtrip_and_maps_to_policy(self):
        cfg = AccountConfig(parasite_aggressiveness="needed")
        again = AccountConfig.from_dict(cfg.to_dict())
        self.assertEqual(again.parasite_aggressiveness, "needed")
        self.assertEqual(again.dispatch_policy().parasite_mode.value, "needed")

    def test_invalid_parasite_aggressiveness_falls_back_to_active(self):
        cfg = AccountConfig(parasite_aggressiveness="bogus")
        self.assertEqual(cfg.dispatch_policy().parasite_mode.value, "active")

    def test_legacy_hilferuf_boolean_migrates_to_uncrewable_mode(self):
        # A stored `hilferuf: true` (the old on/off toggle) must map to the
        # new mode's UNCREWABLE value -- preserving the original behavior for
        # existing accounts.
        cfg = AccountConfig.from_dict({"hilferuf": True})
        self.assertEqual(cfg.hilferuf_mode, "uncrewable")
        self.assertEqual(cfg.dispatch_policy().hilferuf_mode.value, "uncrewable")

    def test_hilferuf_mode_roundtrip_and_maps_to_policy(self):
        cfg = AccountConfig(hilferuf_mode="all")
        again = AccountConfig.from_dict(cfg.to_dict())
        self.assertEqual(again.hilferuf_mode, "all")
        self.assertEqual(again.dispatch_policy().hilferuf_mode.value, "all")

    def test_invalid_hilferuf_mode_falls_back_to_off(self):
        cfg = AccountConfig(hilferuf_mode="bogus")
        self.assertEqual(cfg.dispatch_policy().hilferuf_mode.value, "off")

    def test_hilferuf_mode_explicit_key_takes_precedence_over_legacy_boolean(self):
        # An explicit hilferuf_mode in the stored dict wins even if the
        # legacy boolean is also present (matches the parasite/top_up/patient
        # migration precedent: legacy only fills in when the new key is absent).
        cfg = AccountConfig.from_dict({"hilferuf": True, "hilferuf_mode": "off"})
        self.assertEqual(cfg.hilferuf_mode, "off")

    def test_policies_reflect_config(self):
        cfg = AccountConfig(
            radius_km=15, own_credit_min=200, alliance_credit_max=9000,
            alliance_min_age_minutes=2, max_vehicles_per_mission=3,
            transport_max_distance_km=50, transport_max_tax=10,
            transport_prefer_own=False, transport_allow_alliance=False,
        )
        dp = cfg.dispatch_policy()
        self.assertEqual((dp.radius_km, dp.own_credit_min, dp.max_vehicles_per_mission), (15, 200, 3))
        self.assertEqual(dp.alliance_credit_max, 9000)
        self.assertEqual(dp.alliance_min_age_seconds, 120)  # 2 min -> 120 s
        tp = cfg.transport_policy()
        self.assertEqual((tp.max_distance_km, tp.max_tax), (50, 10))
        self.assertFalse(tp.prefer_own)
        self.assertFalse(tp.allow_alliance)


class GroupConfigTests(unittest.TestCase):
    def test_defaults_map_to_group_policy(self):
        gp = GroupConfig().group_policy()
        self.assertEqual(gp.radius_km, 40.0)
        self.assertTrue(gp.participation)
        self.assertEqual(gp.radius_basis.value, "any")

    def test_smart_order_maps_through(self):
        # SMART is supported in group mode too (Sasha, 2026-09-11) --
        # plan_group_dispatches computes its own per-candidate requirement
        # estimate (see _smart_efficiency in app/domain/group.py).
        from app.domain.dispatch import DispatchOrder
        self.assertEqual(GroupConfig(order="smart").group_policy().order, DispatchOrder.SMART)

    def test_roundtrip_with_members(self):
        cfg = GroupConfig(radius_km=25, credit_max=5000, participation=False,
                          radius_basis="all", max_token_per_account=1, member_ids=(3, 7))
        again = GroupConfig.from_dict(cfg.to_dict())
        self.assertEqual(again, cfg)
        gp = again.group_policy()
        self.assertEqual(gp.credit_max, 5000)
        self.assertFalse(gp.participation)
        self.assertEqual(gp.radius_basis.value, "all")
        self.assertEqual(gp.max_token_per_account, 1)

    def test_member_ids_coerced_to_int_tuple(self):
        cfg = GroupConfig.from_dict({"member_ids": ["3", "7"]})
        self.assertEqual(cfg.member_ids, (3, 7))


if __name__ == "__main__":
    unittest.main()
