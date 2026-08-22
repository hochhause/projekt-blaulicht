"""Vehicle pagination: fetch the whole fleet, not just page 1.

Gated on httpx (game layer). Uses a fake session so no network is touched.
"""

import asyncio
import unittest

try:
    import httpx  # noqa: F401
    HAVE_HTTPX = True
except ImportError:
    HAVE_HTTPX = False


class FakeSession:
    """Serves paginated /api/v2/vehicles?page=N from canned pages."""

    def __init__(self, pages):
        self._pages = pages  # list of lists of vehicle dicts

    async def get_json(self, path):
        # path like "/api/v2/vehicles?page=3"
        page = int(path.rsplit("page=", 1)[1])
        rows = self._pages[page - 1] if 1 <= page <= len(self._pages) else []
        return {"result": rows, "paging": {"page": page}}


@unittest.skipUnless(HAVE_HTTPX, "httpx not installed")
class VehiclePagingTests(unittest.TestCase):
    def _client(self, pages):
        from app.game.client import GameClient
        return GameClient(FakeSession(pages))

    def test_loads_all_pages(self):
        pages = [
            [{"id": 1, "vehicle_type": 0, "fms_real": 2}],
            [{"id": 2, "vehicle_type": 0, "fms_real": 2}],
            [{"id": 3, "vehicle_type": 0, "fms_real": 2}],
        ]
        vehicles = asyncio.run(self._client(pages).fetch_vehicles())
        self.assertEqual(sorted(v.id for v in vehicles), [1, 2, 3])

    def test_cap_stops_early(self):
        pages = [[{"id": i, "vehicle_type": 0, "fms_real": 2}] for i in range(1, 11)]
        vehicles = asyncio.run(self._client(pages).fetch_vehicles(cap=3))
        self.assertEqual(len(vehicles), 3)

    def test_no_pagination_does_not_loop(self):
        # Endpoint ignores ?page= and always returns the same page -> dedup stops it.
        same = [{"id": 1, "vehicle_type": 0, "fms_real": 2}]

        class RepeatSession:
            async def get_json(self, path):
                return {"result": same}

        from app.game.client import GameClient
        vehicles = asyncio.run(GameClient(RepeatSession()).fetch_vehicles())
        self.assertEqual([v.id for v in vehicles], [1])


@unittest.skipUnless(HAVE_HTTPX, "httpx not installed")
class TransportTargetsTests(unittest.TestCase):
    def test_parses_single_quoted_approach_links(self):
        # The game renders these with SINGLE quotes (real snippet from the user).
        page = (
            "<a class='btn btn-success' href='/vehicles/82999211/patient/20001217' "
            "id='btn_approach_20001217'>Anfahren</a>"
            "<a class='btn' href='/vehicles/82999211/gefangener/555?foo=1'>x</a>"
        )

        class PageSession:
            async def get_text(self, path):
                return page

        from app.game.client import GameClient
        targets = asyncio.run(GameClient(PageSession()).transport_targets(82999211))
        self.assertEqual(
            targets,
            ["/vehicles/82999211/patient/20001217", "/vehicles/82999211/gefangener/555?foo=1"],
        )

    def test_no_targets_when_page_has_none(self):
        class EmptySession:
            async def get_text(self, path):
                return "<div>no beds free</div>"

        from app.game.client import GameClient
        self.assertEqual(asyncio.run(GameClient(EmptySession()).transport_targets(1)), [])


if __name__ == "__main__":
    unittest.main()
