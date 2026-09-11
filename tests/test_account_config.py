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

    def test_smart_order_rejected_falls_back_to_nearest(self):
        # SMART needs per-candidate requirement resolution plan_group_dispatches
        # doesn't do -- reject explicitly rather than silently degrade inside
        # the shared _order() sort (no silent failures on ordering logic).
        from app.domain.dispatch import DispatchOrder
        self.assertEqual(GroupConfig(order="smart").group_policy().order, DispatchOrder.NEAREST)

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
