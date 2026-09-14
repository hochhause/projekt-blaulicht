"""AccountRunner: one persistent session, reused across cycles, reconnect on
failure. Gated on httpx (game layer). GameSession/GameClient are patched with
fakes so no network is touched."""

import asyncio
import unittest
from pathlib import Path
from unittest import mock

try:
    import httpx  # noqa: F401
    HAVE_HTTPX = True
except ImportError:
    HAVE_HTTPX = False

VEHICLES = Path(__file__).resolve().parent.parent / "data" / "de" / "vehicles"


class _C:
    sessions = 0
    logins = 0
    closes = 0


def _make_fakes(missions_raise=False, missions=None):
    from app.game.session import LoginResult, LoginStatus

    class FakeSession:
        def __init__(self, *a, **k):
            _C.sessions += 1

        async def login(self, email, password):
            _C.logins += 1
            return LoginResult(LoginStatus.OK)

        async def aclose(self):
            _C.closes += 1

    class FakeClient:
        def __init__(self, session):
            pass

        async def fetch_einsaetze(self):
            return []

        async def fetch_buildings(self):
            return []

        async def fetch_vehicles(self, cap=0):
            return []

        async def fetch_missions(self):
            if missions_raise:
                raise RuntimeError("boom")
            return missions if missions is not None else []

        async def dispatch(self, m, v):
            return True

    return FakeSession, FakeClient


@unittest.skipUnless(HAVE_HTTPX, "httpx not installed")
class AccountRunnerTests(unittest.TestCase):
    def setUp(self):
        from app.domain.assets import AssetLibrary

        _C.sessions = _C.logins = _C.closes = 0
        self.lib = AssetLibrary.load(VEHICLES)

    def _runner(self, password_provider=lambda: "p"):
        from app.core.account_runner import AccountRunner
        from app.domain.dispatch import DispatchPolicy

        return AccountRunner(
            base_url="http://x", user_agent="ua", email="e",
            password_provider=password_provider,
            library=self.lib, policy=DispatchPolicy(),
        )

    def test_connects_once_and_reuses_across_cycles(self):
        FakeSession, FakeClient = _make_fakes()
        with mock.patch("app.core.account_runner.GameSession", FakeSession), \
             mock.patch("app.core.account_runner.GameClient", FakeClient):
            runner = self._runner()
            asyncio.run(runner.run_cycle())
            asyncio.run(runner.run_cycle())
            self.assertEqual(_C.logins, 1)    # one login for two cycles
            self.assertEqual(_C.sessions, 1)

    def test_reconnects_after_failure(self):
        FakeSession, FakeClient = _make_fakes(missions_raise=True)
        with mock.patch("app.core.account_runner.GameSession", FakeSession), \
             mock.patch("app.core.account_runner.GameClient", FakeClient):
            runner = self._runner()
            with self.assertRaises(RuntimeError):
                asyncio.run(runner.run_cycle())
            self.assertEqual(_C.closes, 1)    # dropped session on error
            with self.assertRaises(RuntimeError):
                asyncio.run(runner.run_cycle())
            self.assertEqual(_C.logins, 2)    # reconnected next cycle

    def test_own_user_id_inferred_from_an_unshared_mission_and_cached(self):
        # Sasha, 2026-09-14: lets the planner tell a genuinely-own mission
        # apart from one merely alliance-shared, even after it's been
        # shared. Inferred from the feed itself (an unshared mission can
        # only be ours) rather than a separate, unverified API call.
        from types import SimpleNamespace
        own_unshared = SimpleNamespace(id=1, is_alliance=False, user_id=42, status="unknown")
        FakeSession, FakeClient = _make_fakes(missions=[own_unshared])
        with mock.patch("app.core.account_runner.GameSession", FakeSession), \
             mock.patch("app.core.account_runner.GameClient", FakeClient):
            runner = self._runner()
            asyncio.run(runner.run_cycle())
            self.assertEqual(runner._dispatch._own_user_id, 42)

    def test_own_user_id_stays_none_until_an_unshared_mission_appears(self):
        # No unshared mission with a known user_id visible yet -- must not
        # crash, and simply keeps trying next cycle (self-healing).
        FakeSession, FakeClient = _make_fakes(missions=[])
        with mock.patch("app.core.account_runner.GameSession", FakeSession), \
             mock.patch("app.core.account_runner.GameClient", FakeClient):
            runner = self._runner()
            asyncio.run(runner.run_cycle())  # must not raise
            self.assertIsNone(runner._dispatch._own_user_id)

    def test_bad_credentials_surface_as_error_not_crash(self):
        # A password_provider that raises (e.g. undecryptable secret) becomes a
        # cycle error, not a hard failure at construction.
        def boom():
            raise RuntimeError("cannot decrypt")

        FakeSession, FakeClient = _make_fakes()
        with mock.patch("app.core.account_runner.GameSession", FakeSession), \
             mock.patch("app.core.account_runner.GameClient", FakeClient):
            runner = self._runner(password_provider=boom)
            with self.assertRaises(RuntimeError):
                asyncio.run(runner.run_cycle())
            self.assertEqual(_C.logins, 0)  # never got as far as logging in


if __name__ == "__main__":
    unittest.main()
