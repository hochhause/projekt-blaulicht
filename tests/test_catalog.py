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

    def test_accepts_id_keyed_object(self):
        obj = {"1": {"id": "1", "name": "X", "average_credits": 10, "requirements": {"firetrucks": 1}}}
        cat = MissionCatalog.from_payload(obj)
        self.assertEqual(len(cat), 1)
        self.assertEqual(cat.requirements(1), {"firetrucks": 1})


if __name__ == "__main__":
    unittest.main()
