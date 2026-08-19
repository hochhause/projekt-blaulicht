import unittest

from app.core.logbuffer import LogStore


class LogStoreTests(unittest.TestCase):
    def test_append_and_recent_newest_first(self):
        log = LogStore()
        log.append(1, "first")
        log.append(1, "second")
        msgs = [e.message for e in log.recent(1)]
        self.assertEqual(msgs, ["second", "first"])

    def test_isolated_per_account(self):
        log = LogStore()
        log.append(1, "a")
        log.append(2, "b")
        self.assertEqual([e.message for e in log.recent(1)], ["a"])
        self.assertEqual([e.message for e in log.recent(2)], ["b"])

    def test_capacity_bounded(self):
        log = LogStore(capacity=3)
        for i in range(5):
            log.append(1, str(i))
        msgs = [e.message for e in log.recent(1)]
        self.assertEqual(msgs, ["4", "3", "2"])  # oldest dropped, newest first

    def test_entries_have_timestamp(self):
        log = LogStore()
        log.append(1, "x")
        self.assertRegex(log.recent(1)[0].ts, r"^\d\d:\d\d:\d\d$")

    def test_unknown_account_empty(self):
        self.assertEqual(LogStore().recent(99), [])

    def test_recent_all_merges_and_orders_globally(self):
        log = LogStore()
        log.append(1, "a1")
        log.append(2, "b1")
        log.append(1, "a2")
        merged = log.recent_all()
        # newest first, across accounts, with account ids
        self.assertEqual([(aid, e.message) for aid, e in merged],
                         [(1, "a2"), (2, "b1"), (1, "a1")])


if __name__ == "__main__":
    unittest.main()
