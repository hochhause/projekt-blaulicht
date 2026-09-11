import unittest

from app.domain.strikes import StrikeTracker


class StrikeTrackerTests(unittest.TestCase):
    def test_three_strikes_to_struck(self):
        t = StrikeTracker()
        now = 1000.0
        self.assertFalse(t.is_struck("firetrucks", now))
        t.record(["firetrucks"], now)
        t.record(["firetrucks"], now)
        self.assertFalse(t.is_struck("firetrucks", now))  # only 2
        t.record(["firetrucks"], now)
        self.assertTrue(t.is_struck("firetrucks", now))    # 3 -> struck

    def test_strikes_expire_after_window(self):
        t = StrikeTracker(window_seconds=100)
        for _ in range(3):
            t.record(["rw"], 1000.0)
        self.assertTrue(t.is_struck("rw", 1050.0))          # within window
        self.assertFalse(t.is_struck("rw", 1000.0 + 200))   # window lapsed -> reset

    def test_independent_classes(self):
        t = StrikeTracker()
        for _ in range(3):
            t.record(["ambulances"], 0.0)
        self.assertTrue(t.is_struck("ambulances", 0.0))
        self.assertFalse(t.is_struck("police_cars", 0.0))

    def test_struck_classes_set(self):
        t = StrikeTracker()
        for _ in range(3):
            t.record(["nef"], 0.0)
        self.assertEqual(t.struck_classes(0.0), {"nef"})

    def test_on_struck_fires_exactly_once_for_three_separate_calls(self):
        calls = []
        t = StrikeTracker(on_struck=calls.append)
        t.record(["hlf"], 0.0)
        t.record(["hlf"], 0.0)
        self.assertEqual(calls, [])
        t.record(["hlf"], 0.0)  # crosses the threshold here
        self.assertEqual(calls, ["hlf"])
        t.record(["hlf"], 0.0)  # already struck -- no repeat callback
        self.assertEqual(calls, ["hlf"])

    def test_record_confirmed_failure_strikes_immediately_and_fires_on_struck_once(self):
        # Confirmed game rejection (Sasha, 2026-09-11): bypasses the normal
        # 3-in-24h count in ONE call, and on_struck must still fire exactly
        # once, not once per strike bundled into that call.
        calls = []
        t = StrikeTracker(on_struck=calls.append)
        self.assertFalse(t.is_struck("125521931", 0.0))
        t.record_confirmed_failure("125521931", 0.0)
        self.assertTrue(t.is_struck("125521931", 0.0))
        self.assertEqual(calls, ["125521931"])


if __name__ == "__main__":
    unittest.main()
