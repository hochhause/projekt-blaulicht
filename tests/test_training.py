import unittest
from pathlib import Path
from types import SimpleNamespace

from app.domain.training import TrainingCatalog, analyze_fleet

TRAINING_JSON = Path(__file__).resolve().parent.parent / "data" / "de" / "training.json"


def vehicle(vid: int, type_caption: str) -> SimpleNamespace:
    """Minimal duck-typed stand-in for a game Vehicle."""
    return SimpleNamespace(id=vid, type_caption=type_caption)


class TrainingCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = TrainingCatalog.load(TRAINING_JSON)

    def test_known_courses(self):
        self.assertEqual(self.cat.required_course("NEF"), "Notarzt")
        self.assertEqual(self.cat.required_course("WLF"), "Wechsellader")
        self.assertEqual(self.cat.required_course("GW-Gefahrgut"), "GW-Gefahrgut")

    def test_no_training_needed(self):
        self.assertIsNone(self.cat.required_course("RTW"))
        self.assertIsNone(self.cat.required_course("LF 20"))
        self.assertFalse(self.cat.requires_training("KTW"))

    def test_matching_is_normalised(self):
        # Spacing/casing differences between wiki and live caption must match.
        self.assertEqual(self.cat.required_course("elw2"), "ELW 2")
        self.assertEqual(self.cat.required_course("E L W 2"), "ELW 2")

    def test_empty_name(self):
        self.assertIsNone(self.cat.required_course(""))


class FleetAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = TrainingCatalog.load(TRAINING_JSON)

    def test_groups_by_course(self):
        fleet = [
            vehicle(1, "RTW"),      # none
            vehicle(2, "NEF"),      # Notarzt
            vehicle(3, "NAW"),      # Notarzt
            vehicle(4, "WLF"),      # Wechsellader
            vehicle(5, "LF 20"),    # none
        ]
        report = analyze_fleet(fleet, self.cat)
        self.assertEqual(sorted(report.by_course["Notarzt"]), [2, 3])
        self.assertEqual(report.by_course["Wechsellader"], [4])
        self.assertEqual(report.total_vehicles, 3)

    def test_empty_fleet_report(self):
        report = analyze_fleet([], self.cat)
        self.assertEqual(report.total_vehicles, 0)
        self.assertIn("No special training", report.render())


if __name__ == "__main__":
    unittest.main()
