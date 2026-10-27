"""Dispatch outcome parsing: confirmed game rejections (Sasha, 2026-09-11).

An alarm POST can succeed at the HTTP level while the game silently refuses
specific vehicles within the batch (missing crew, or its towing vehicle
isn't at the station) -- surfaced as a Bootstrap alert-danger div per
rejected vehicle in the response page. Gated on httpx (game layer). Uses a
fake session so no network is touched.
"""

import asyncio
import unittest
from types import SimpleNamespace

from app.game.client import _parse_dispatch_failures, _parse_on_scene_vehicles

try:
    import httpx  # noqa: F401
    HAVE_HTTPX = True
except ImportError:
    HAVE_HTTPX = False

# Verbatim from a live capture (Sasha, 2026-09-11).
_LIVE_ALERT = (
    '<div id="alert_danger_1789156130488" class="alert fade in alert-danger  ">'
    '<button type="button" class="close" data-dismiss="alert">×</button>'
    '<a href="/vehicles/125521931">\U0001F7E2 Anh TeSi</a>: '
    'Das Zugfahrzeug befindet sich nicht auf der Wache.</div>'
)


class ParseDispatchFailuresTests(unittest.TestCase):
    def test_parses_the_live_captured_alert(self):
        failures = _parse_dispatch_failures(_LIVE_ALERT)
        self.assertEqual(set(failures), {125521931})
        self.assertIn("Zugfahrzeug", failures[125521931])

    def test_empty_html_has_no_failures(self):
        self.assertEqual(_parse_dispatch_failures("<html>ok</html>"), {})

    def test_multiple_rejected_vehicles_in_one_response(self):
        html = _LIVE_ALERT + (
            '<div class="alert alert-danger"><a href="/vehicles/42">LF 20</a>: '
            "Es fehlt die Besatzung.</div>"
        )
        failures = _parse_dispatch_failures(html)
        self.assertEqual(set(failures), {125521931, 42})

    def test_a_plain_alert_success_div_is_not_matched(self):
        # Only alert-danger carries a rejection; a success/info banner (if
        # the game ever renders one this way) must not be misread as one.
        html = '<div class="alert alert-success"><a href="/vehicles/5">LF</a>: alarmiert.</div>'
        self.assertEqual(_parse_dispatch_failures(html), {})

    def test_alert_danger_with_no_vehicle_link_does_not_leak_into_a_later_unrelated_link(self):
        # Code review (2026-09-11): a generic alert-danger banner with NO
        # vehicle link at all (plausible -- a session/rate-limit/general
        # error) must NOT let the match run on across the closing </div> and
        # latch onto some unrelated, healthy vehicle's own status row later
        # on the page. Confirmed live before the fix: a bare lazy `.*?`
        # under DOTALL did exactly this and would have struck out vehicle
        # 999 (perfectly healthy, "unterwegs zur Einsatzstelle") as if the
        # game had rejected it.
        html = (
            '<div id="alert_danger_xyz" class="alert fade in alert-danger  ">'
            "<button>x</button>"
            "Es ist ein allgemeiner Fehler aufgetreten, keine Fahrzeuge betroffen."
            "</div>"
            '<div class="panel"><div class="vehicle-row">'
            '<a href="/vehicles/999">LF 20 KatS</a>: unterwegs zur Einsatzstelle'
            "</div></div>"
        )
        self.assertEqual(_parse_dispatch_failures(html), {})


# Trimmed from a LIVE mission detail page (Sasha logged the bot into a
# browser session, 2026-10-27, to confirm the roster is server-rendered, not
# AJAX-injected) -- the real <tr> tag has the id attribute on its own line
# with surrounding whitespace, not a contiguous `<tr id="...">` literal. The
# second row (82900774, an NAW) is a REAL row from a real live mission that
# day, included verbatim: FMS 5 (mid-transport, "Patient: Melanie O.") in the
# SAME "Fahrzeuge an der Einsatzstelle" table as the FMS 4 rows -- the table
# name is misleading, it is NOT exclusively on-scene vehicles.
_LIVE_ON_SCENE_TABLE = """
<table id="mission_vehicle_at_mission" class="table table-striped">
<thead><tr><th>Fahrzeug</th></tr></thead>
<tbody>
<tr
    id="vehicle_row_81048893"
>
  <td><span class="building_list_fms building_list_fms_4">4</span></td>
  <td rowspan="">
    <a href="/vehicles/81048893" vehicle_type_id="103">\U0001F7E2FuStW (DGL) #I-1</a>
  </td>
  <td class="hidden-xs"><a href="/profile/2641503">HyperCoderio</a></td>
</tr>
<tr
    id="vehicle_row_82900774"
>
  <td><span class="building_list_fms building_list_fms_5">5</span></td>
  <td rowspan="">
    <a href="/vehicles/82900774" vehicle_type_id="74">\U0001F7E2NAW #I-23</a>
    <br>Patient: Melanie O.
  </td>
  <td class="hidden-xs"><a href="/profile/2641503">HyperCoderio</a></td>
</tr>
<tr
    id="vehicle_row_125535599"
>
  <td><span class="building_list_fms building_list_fms_4">4</span></td>
  <td><a href="/vehicles/125535599" vehicle_type_id="32">\U0001F7E2GW-Messtechnik</a></td>
</tr>
</tbody>
</table>
<table id="mission_vehicle_driving">
<tr id="vehicle_row_999999">
  <td><span class="building_list_fms building_list_fms_3">3</span></td>
  <a vehicle_type_id="7">en route, not on scene</a>
</tr>
</table>
"""


class ParseOnSceneVehiclesTests(unittest.TestCase):
    def test_parses_every_fms4_row_in_the_on_scene_table(self):
        rows = _parse_on_scene_vehicles(_LIVE_ON_SCENE_TABLE)
        self.assertEqual(rows, [(81048893, 103), (125535599, 32)])

    def test_an_fms5_row_in_the_same_table_is_excluded(self):
        # Live-verified 2026-10-27: the "on-scene" table also lists a
        # vehicle actively transporting a patient away (FMS 5) -- counting
        # it toward "everyone's coverage" would be wrong (it's leaving and
        # typically never comes back), which could misjudge one of OUR
        # genuinely-still-needed vehicles as surplus.
        rows = _parse_on_scene_vehicles(_LIVE_ON_SCENE_TABLE)
        self.assertNotIn((82900774, 74), rows)

    def test_the_en_route_table_is_never_counted(self):
        # The "Fahrzeuge auf Anfahrt" table uses the exact same row id shape
        # -- a sloppy regex spanning both tables would double-count S3 as S4.
        rows = _parse_on_scene_vehicles(_LIVE_ON_SCENE_TABLE)
        self.assertNotIn((999999, 7), rows)

    def test_missing_table_returns_empty_not_an_error(self):
        # A page-shape change or an unrelated page must fail closed (treated
        # by the caller the same as "not fetched"), never raise.
        self.assertEqual(_parse_on_scene_vehicles("<html>no such table</html>"), [])

    def test_empty_on_scene_table_returns_empty(self):
        html = '<table id="mission_vehicle_at_mission"><tbody></tbody></table>'
        self.assertEqual(_parse_on_scene_vehicles(html), [])

    def test_a_row_missing_the_type_id_does_not_leak_into_the_next_row(self):
        # Bounded the same way as _DISPATCH_FAILURE's own regex: a row
        # lacking vehicle_type_id for any reason must not let the match run
        # on into the following row and misattribute ITS type.
        html = (
            '<table id="mission_vehicle_at_mission"><tbody>'
            '<tr id="vehicle_row_1"><span class="building_list_fms building_list_fms_4">4</span>'
            "no type id here</tr>"
            '<tr id="vehicle_row_2"><span class="building_list_fms building_list_fms_4">4</span>'
            '<a vehicle_type_id="30">HLF 20</a></tr>'
            "</tbody></table>"
        )
        self.assertEqual(_parse_on_scene_vehicles(html), [(2, 30)])

    def test_a_row_missing_the_fms_span_does_not_leak_into_the_next_row(self):
        html = (
            '<table id="mission_vehicle_at_mission"><tbody>'
            '<tr id="vehicle_row_1"><a vehicle_type_id="30">no fms span here</a></tr>'
            '<tr id="vehicle_row_2"><span class="building_list_fms building_list_fms_4">4</span>'
            '<a vehicle_type_id="30">HLF 20</a></tr>'
            "</tbody></table>"
        )
        self.assertEqual(_parse_on_scene_vehicles(html), [(2, 30)])


class FakeSession:
    """Just enough of GameSession for dispatch_many: token fetch + POST."""

    def __init__(self, post_response_text: str):
        self.csrf_token = "tok"
        self._post_response_text = post_response_text

    async def get_text(self, path):
        return '<input name="authenticity_token" value="tok">'

    @staticmethod
    def extract_form_token(html):
        return "tok"

    async def post_form(self, path, data):
        return SimpleNamespace(text=self._post_response_text)


@unittest.skipUnless(HAVE_HTTPX, "httpx not installed")
class DispatchManyRejectionTests(unittest.TestCase):
    def _client(self, post_response_text):
        from app.game.client import GameClient
        return GameClient(FakeSession(post_response_text))

    def test_ok_batch_with_no_rejections(self):
        client = self._client("<html>ok, all alarmed</html>")
        result = asyncio.run(client.dispatch_many(1, [100, 101]))
        self.assertTrue(result.ok)
        self.assertEqual(dict(result.rejected), {})

    def test_ok_batch_reports_the_rejected_vehicle_and_reason(self):
        # The batch includes vehicle 125521931 (rejected) and 999 (not
        # mentioned in the alert, so presumed successfully dispatched).
        client = self._client(_LIVE_ALERT)
        result = asyncio.run(client.dispatch_many(1, [125521931, 999]))
        self.assertTrue(result.ok)
        self.assertEqual(set(result.rejected), {125521931})
        self.assertIn("Zugfahrzeug", result.rejected[125521931])

    def test_rejection_for_a_vehicle_not_in_this_batch_is_ignored(self):
        # A stray alert mentioning an id we didn't attempt this call must not
        # leak into this result (e.g. a leftover from a previous mission).
        client = self._client(_LIVE_ALERT)
        result = asyncio.run(client.dispatch_many(1, [999]))
        self.assertTrue(result.ok)
        self.assertEqual(dict(result.rejected), {})

    def test_single_dispatch_reflects_rejection_too(self):
        client = self._client(_LIVE_ALERT)
        result = asyncio.run(client.dispatch(1, 125521931))
        self.assertTrue(result.ok)
        self.assertIn(125521931, result.rejected)


class _OnScenePageSession:
    """Just enough of GameSession for fetch_on_scene_vehicles: a plain GET."""

    def __init__(self, page_text=None, raises=False):
        self._page_text = page_text
        self._raises = raises

    async def get_text(self, path):
        if self._raises:
            raise RuntimeError("boom")
        return self._page_text


@unittest.skipUnless(HAVE_HTTPX, "httpx not installed")
class FetchOnSceneVehiclesTests(unittest.TestCase):
    def test_returns_every_on_scene_vehicle(self):
        from app.game.client import GameClient
        client = GameClient(_OnScenePageSession(_LIVE_ON_SCENE_TABLE))
        rows = asyncio.run(client.fetch_on_scene_vehicles(4448998042))
        self.assertEqual(rows, [(81048893, 103), (125535599, 32)])

    def test_a_failed_fetch_returns_empty_not_an_exception(self):
        # Sasha: "if we over-recall we have a problem" -- a network/page
        # error here must fail closed (treated as "not fetched"), never
        # propagate and never silently fabricate a roster.
        from app.game.client import GameClient
        client = GameClient(_OnScenePageSession(raises=True))
        rows = asyncio.run(client.fetch_on_scene_vehicles(1))
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
