import unittest

from app.domain.alarms import AlarmTracker


class AlarmTrackerTests(unittest.TestCase):
    def test_record_accumulates_per_mission(self):
        t = AlarmTracker()
        t.record(1)
        t.record(1)
        t.record(2)
        # Still live -> nothing finalized yet, but the counts are held.
        self.assertEqual(t.finalize({1, 2}), {})

    def test_finalize_closes_a_mission_no_longer_live(self):
        t = AlarmTracker()
        t.record(1)
        t.record(1)
        closed = t.finalize(set())  # mission 1 dropped out of the feed
        self.assertEqual(closed, {2: 1})  # one mission closed after 2 alarms

    def test_finalize_only_drops_missions_not_in_live_ids(self):
        t = AlarmTracker()
        t.record(1)
        t.record(2)
        t.record(2)
        closed = t.finalize({1})  # mission 2 is gone, mission 1 still live
        self.assertEqual(closed, {2: 1})
        # Mission 1 keeps accumulating across a later finalize.
        t.record(1)
        closed_again = t.finalize(set())
        self.assertEqual(closed_again, {2: 1})

    def test_multiple_missions_closing_the_same_cycle_aggregate_by_count(self):
        t = AlarmTracker()
        t.record(1)                 # 1 alarm
        t.record(2); t.record(2)    # 2 alarms
        t.record(3); t.record(3)    # 2 alarms, same bucket as mission 2
        closed = t.finalize(set())
        self.assertEqual(closed, {1: 1, 2: 2})

    def test_finalize_is_a_noop_when_nothing_tracked(self):
        t = AlarmTracker()
        self.assertEqual(t.finalize(set()), {})

    def test_mission_never_recorded_leaves_no_trace(self):
        # A mission we never sent a real alarm to (fully handled by someone
        # else, or only ever a parasite/VGE-spare claim) should never show up
        # in a finalize, even once it leaves the feed.
        t = AlarmTracker()
        t.record(1)
        closed = t.finalize(set())  # mission 2 was never recorded at all
        self.assertEqual(closed, {1: 1})
