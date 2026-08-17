import unittest

from app.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_account_crud(self):
        aid = self.store.add_account("nord-1", "a@example.com")
        acc = self.store.get_account(aid)
        self.assertEqual(acc.label, "nord-1")
        self.assertEqual(acc.server, "de")
        self.assertFalse(acc.enabled)

        self.store.update_account(aid, label="nord-x", email="b@example.com")
        self.store.set_enabled(aid, True)
        acc = self.store.get_account(aid)
        self.assertEqual(acc.label, "nord-x")
        self.assertEqual(acc.email, "b@example.com")
        self.assertTrue(acc.enabled)

        self.assertEqual(len(self.store.list_accounts()), 1)
        self.store.delete_account(aid)
        self.assertIsNone(self.store.get_account(aid))

    def test_secret_roundtrip_opaque_bytes(self):
        aid = self.store.add_account("x", "x@e.com")
        self.store.set_secret(aid, b"\x00sealed\xff")
        self.assertEqual(self.store.get_secret(aid), b"\x00sealed\xff")
        # upsert replaces
        self.store.set_secret(aid, b"new")
        self.assertEqual(self.store.get_secret(aid), b"new")

    def test_config_upsert(self):
        aid = self.store.add_account("x", "x@e.com")
        self.store.set_config(aid, '{"radius_km": 30}')
        self.assertEqual(self.store.get_config(aid), '{"radius_km": 30}')
        self.store.set_config(aid, '{"radius_km": 50}')
        self.assertEqual(self.store.get_config(aid), '{"radius_km": 50}')

    def test_run_state_and_stats(self):
        aid = self.store.add_account("x", "x@e.com")
        self.store.set_state(aid, "running")
        self.assertEqual(self.store.get_state(aid)["status"], "running")
        self.store.set_state(aid, "error", error="boom")
        st = self.store.get_state(aid)
        self.assertEqual(st["status"], "error")
        self.assertEqual(st["last_error"], "boom")

        self.store.add_stat(aid, dispatched=3, missions=1)
        self.store.add_stat(aid, dispatched=2, missions=2)
        totals = self.store.totals(aid)
        self.assertEqual(totals["dispatched"], 5)
        self.assertEqual(totals["missions"], 3)

    def test_cascade_delete_removes_secret_and_config(self):
        aid = self.store.add_account("x", "x@e.com")
        self.store.set_secret(aid, b"s")
        self.store.set_config(aid, "{}")
        self.store.delete_account(aid)
        self.assertIsNone(self.store.get_secret(aid))
        self.assertIsNone(self.store.get_config(aid))

    def test_settings(self):
        self.assertIsNone(self.store.get_setting("k"))
        self.store.set_setting("k", "v")
        self.assertEqual(self.store.get_setting("k"), "v")


if __name__ == "__main__":
    unittest.main()
