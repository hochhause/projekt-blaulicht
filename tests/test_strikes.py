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


if __name__ == "__main__":
    unittest.main()
