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

    def test_credits_tracked_in_stats(self):
        aid = self.store.add_account("x", "x@e.com")
        self.store.add_stat(aid, dispatched=3, missions=1, credits=500)
        self.store.add_stat(aid, dispatched=0, missions=0, credits=250)
        totals = self.store.totals(aid)
        self.assertEqual(totals["credits"], 750)
        self.assertEqual(totals["dispatched"], 3)

    def test_stat_series_buckets_by_hour(self):
        aid = self.store.add_account("x", "x@e.com")
        self.store.add_stat(aid, dispatched=2, missions=1, credits=100)
        self.store.add_stat(aid, dispatched=1, missions=1, credits=50)
        series = self.store.stat_series(None, hours=24)
        # same hour -> one bucket, summed
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0]["dispatched"], 3)
        self.assertEqual(series[0]["credits"], 150)

    def test_shortfalls_increment_and_rank(self):
        aid = self.store.add_account("x", "x@e.com")
        self.store.add_shortfalls(aid, {"police_cars": 2, "ambulances": 1})
        self.store.add_shortfalls(aid, {"police_cars": 3})
        self.store.add_shortfalls(aid, {"ladders": 0})  # zero is ignored
        rows = self.store.shortfalls(aid)
        self.assertEqual(rows[0], {"vehicle_class": "police_cars", "count": 5})
        classes = {r["vehicle_class"] for r in rows}
        self.assertEqual(classes, {"police_cars", "ambulances"})

    def test_clear_shortfalls_on_restart(self):
        aid = self.store.add_account("x", "x@e.com")
        self.store.add_shortfalls(aid, {"police_cars": 3})
        self.assertTrue(self.store.shortfalls(aid))
        self.store.clear_shortfalls(aid)
        self.assertEqual(self.store.shortfalls(aid), [])

    def test_shortfalls_scoped_per_account(self):
        a1 = self.store.add_account("a", "a@e.com")
        a2 = self.store.add_account("b", "b@e.com")
        self.store.add_shortfalls(a1, {"police_cars": 1})
        self.assertEqual(len(self.store.shortfalls(a1)), 1)
        self.assertEqual(self.store.shortfalls(a2), [])

    def test_mission_status_is_a_snapshot_not_cumulative(self):
        aid = self.store.add_account("x", "x@e.com")
        self.store.set_mission_status(aid, {"red": 5, "yellow": 2})
        self.assertEqual(self.store.mission_status(aid), {"red": 5, "yellow": 2})
        # A second cycle's snapshot REPLACES the first, unlike shortfalls.
        self.store.set_mission_status(aid, {"red": 1, "green": 3})
        self.assertEqual(self.store.mission_status(aid), {"red": 1, "green": 3})

    def test_mission_status_zero_counts_dropped(self):
        aid = self.store.add_account("x", "x@e.com")
        self.store.set_mission_status(aid, {"red": 2, "yellow": 0})
        self.assertEqual(self.store.mission_status(aid), {"red": 2})

    def test_mission_status_scoped_per_account(self):
        a1 = self.store.add_account("a", "a@e.com")
        a2 = self.store.add_account("b", "b@e.com")
        self.store.set_mission_status(a1, {"red": 4})
        self.assertEqual(self.store.mission_status(a1), {"red": 4})
        self.assertEqual(self.store.mission_status(a2), {})

    def test_mission_status_empty_when_unset(self):
        aid = self.store.add_account("x", "x@e.com")
        self.assertEqual(self.store.mission_status(aid), {})


if __name__ == "__main__":
    unittest.main()
