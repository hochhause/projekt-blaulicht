import unittest

from app.core.account_config import DEFAULT_CONFIG, AccountConfig


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

    def test_mode_scopes_roundtrip(self):
        cfg = AccountConfig(parasite_scope="alliance", patient_scope="own")
        again = AccountConfig.from_dict(cfg.to_dict())
        self.assertEqual(again.parasite_scope, "alliance")
        self.assertEqual(again.patient_scope, "own")

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


if __name__ == "__main__":
    unittest.main()
