import json
import unittest
from pathlib import Path

from app.domain.catalog import MissionCatalog

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "einsaetze_sample.json"


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.catalog = MissionCatalog.from_payload(payload)

    def test_all_entries_loaded(self):
        self.assertEqual(len(self.catalog), 4)

    def test_string_id_becomes_int(self):
        mtype = self.catalog.get(1)
        self.assertIsNotNone(mtype)
        self.assertIsInstance(mtype.id, int)
        self.assertEqual(mtype.name, "Brennender Abfallcontainer")
        self.assertEqual(mtype.average_credits, 170)

    def test_structured_requirements(self):
        self.assertEqual(self.catalog.requirements(1), {"firetrucks": 1})
        self.assertEqual(
            self.catalog.requirements(2),
            {"firetrucks": 2, "platform_trucks": 1, "battalion_chief_vehicles": 1},
        )

    def test_categories_and_expansions(self):
        m1 = self.catalog.get(1)
        self.assertEqual(m1.categories, ("fire",))
        self.assertEqual(m1.expansion_ids, (2, 3, 26))

    def test_unknown_mission_type(self):
        self.assertIsNone(self.catalog.get(4242))
        self.assertEqual(self.catalog.requirements(4242), {})

    def test_min_fire_personnel_parsed(self):
        # additional.average_min_fire_personnel -> MissionType.min_fire_personnel.
        cat = MissionCatalog.from_payload([
            {"id": "159", "name": "Äste auf Fahrbahn", "requirements": {},
             "additional": {"average_min_fire_personnel": 3}},
        ])
        self.assertEqual(cat.get(159).min_fire_personnel, 3)
        self.assertEqual(self.catalog.get(1).min_fire_personnel, 0)  # absent -> 0

    def test_care_includes_staff_members_parsed(self):
        # additional.care_includes_staff_members -> MissionType.care_includes_staff_members
        # (Sasha, 2026-10-24, confirmed live on mission 505/abcde).
        cat = MissionCatalog.from_payload([
            {"id": "505", "name": "Hochwasserschadenslage", "requirements": {},
             "additional": {"care_includes_staff_members": True,
                            "care_affected_people_min": 100, "care_affected_people_max": 500}},
        ])
        self.assertTrue(cat.get(505).care_includes_staff_members)
        self.assertFalse(self.catalog.get(1).care_includes_staff_members)  # absent -> False

    def test_accepts_id_keyed_object(self):
        obj = {"1": {"id": "1", "name": "X", "average_credits": 10, "requirements": {"firetrucks": 1}}}
        cat = MissionCatalog.from_payload(obj)
        self.assertEqual(len(cat), 1)
        self.assertEqual(cat.requirements(1), {"firetrucks": 1})


if __name__ == "__main__":
    unittest.main()
