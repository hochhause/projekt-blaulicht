"""Web-panel integration tests.

Skipped automatically where the ``web`` extra isn't installed, so the
pure-stdlib suite stays green. When FastAPI + cryptography are present, this
exercises the real app through Starlette's TestClient — which importantly runs
handlers in a worker thread, so it also guards the store's cross-thread safety.
"""

import os
import unittest

try:
    import cryptography  # noqa: F401
    from fastapi.testclient import TestClient  # noqa: F401

    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False


@unittest.skipUnless(HAVE_WEB, "web extra (fastapi/cryptography) not installed")
class WebPanelTests(unittest.TestCase):
    def setUp(self):
        os.environ["BOT_SECRET_KEY"] = "test-master-key"
        from fastapi.testclient import TestClient

        from app.config import get_settings
        from app.core.orchestrator import Orchestrator
        from app.core.secrets import SecretBox
        from app.store import Store
        from app.web.app import create_app
        from app.web.auth import AdminAuth

        self.store = Store(":memory:")
        self.box = SecretBox("test-master-key")
        auth = AdminAuth(self.store)
        auth.bootstrap("adminpw")
        # Factory is never invoked (no account is started in these tests).
        orch = Orchestrator(lambda aid: None)
        self.client = TestClient(create_app(self.store, orch, self.box, get_settings(), auth))

    def _login(self):
        r = self.client.post("/login", data={"password": "adminpw"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.client.cookies.update(r.cookies)

    def test_health_is_open(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})

    def test_dashboard_requires_login(self):
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login")

    def test_wrong_password_rejected(self):
        r = self.client.post("/login", data={"password": "nope"}, follow_redirects=False)
        self.assertEqual(r.status_code, 401)

    def test_login_then_dashboard(self):
        self._login()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Accounts", r.text)

    def test_create_account_seals_password(self):
        self._login()
        r = self.client.post(
            "/accounts",
            data={"label": "nord-1", "email": "x@e.com", "password": "gamepw"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        account = self.store.list_accounts()[0]
        sealed = self.store.get_secret(account.id)
        self.assertNotIn(b"gamepw", sealed)  # encrypted at rest
        self.assertEqual(self.box.open(sealed), "gamepw")  # recoverable

    def test_config_save_roundtrips(self):
        import json

        self._login()
        self.client.post(
            "/accounts", data={"label": "n", "email": "x@e.com", "password": "p"}
        )
        aid = self.store.list_accounts()[0].id
        self.client.post(
            f"/accounts/{aid}",
            data={
                "label": "n", "email": "x@e.com", "password": "",
                "radius_km": 25, "poll_seconds": 60, "jitter_seconds": 5,
                "credit_floor": 100, "max_vehicles_per_mission": "",
                "transport_enabled": "1", "transport_max_distance_km": 50,
                "transport_max_tax": "", "transport_prefer_own": "1",
                "transport_allow_alliance": "",
            },
        )
        cfg = json.loads(self.store.get_config(aid))
        self.assertEqual(cfg["radius_km"], 25)
        self.assertEqual(cfg["credit_floor"], 100)


if __name__ == "__main__":
    unittest.main()
