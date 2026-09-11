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

from app.game.client import _parse_dispatch_failures

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


if __name__ == "__main__":
    unittest.main()
