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

    def test_policy_reassigned_before_first_cycle_reaches_the_dispatch_runner(self):
        # The dashboard's shift-click "run once" action
        # (orchestrator.make_run_once) builds a runner via the normal
        # factory, then overrides `._policy` to an uncapped copy BEFORE the
        # first run_cycle() -- relying on the policy being read lazily at
        # _connect() time, not baked in at construction (Sasha, 2026-10-28).
        # Confirms that assumption holds, independent of make_run_once's own
        # httpx/Store/SecretBox wiring (untested, like make_cleanup).
        from app.domain.dispatch import DispatchPolicy

        FakeSession, FakeClient = _make_fakes()
        with mock.patch("app.core.account_runner.GameSession", FakeSession), \
             mock.patch("app.core.account_runner.GameClient", FakeClient):
            runner = self._runner()
            override = DispatchPolicy(batch_size=999)
            runner._policy = override
            asyncio.run(runner.run_cycle())
            self.assertIs(runner._dispatch._policy, override)

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

    def test_ever_revealed_survives_a_reconnect(self):
        # The sticky ambulance/NEF memory (MissionRequirementResolver.
        # effective_count_revealed) must not be silently wiped by a reconnect --
        # _connect() reruns on ANY exception (not just a confirmed logout), and
        # used to always build a resolver with a fresh, empty set.
        FakeSession, FakeClient = _make_fakes()
        with mock.patch("app.core.account_runner.GameSession", FakeSession), \
             mock.patch("app.core.account_runner.GameClient", FakeClient):
            runner = self._runner()
            asyncio.run(runner.connect())
            from types import SimpleNamespace
            runner._resolver.effective_count_revealed(SimpleNamespace(id=1, vehicle_state=1))
            self.assertIn(1, runner._resolver._ever_revealed)
            first_resolver = runner._resolver
            asyncio.run(runner._connect())  # e.g. a transient failure's reconnect
            self.assertIsNot(runner._resolver, first_resolver)  # genuinely rebuilt
            self.assertIn(1, runner._resolver._ever_revealed)   # memory survived

    def test_adopt_ever_revealed_repoints_an_already_built_resolver(self):
        # GroupCoordinator calls this to pool sticky ambulance/NEF memory
        # across members. If this account already connected (built its own
        # resolver) before being handed to the group, swapping only
        # runner._ever_revealed wouldn't be enough -- the resolver captured
        # the OLD set by reference at construction time and would keep
        # writing into it, invisible to the rest of the group, until its next
        # reconnect. adopt_ever_revealed must repoint the live resolver too.
        from types import SimpleNamespace

        FakeSession, FakeClient = _make_fakes()
        with mock.patch("app.core.account_runner.GameSession", FakeSession), \
             mock.patch("app.core.account_runner.GameClient", FakeClient):
            runner = self._runner()
            asyncio.run(runner.connect())
            own_set_before = runner._resolver._ever_revealed
            shared: set[int] = {7}
            runner.adopt_ever_revealed(shared)
            self.assertIs(runner._ever_revealed, shared)
            self.assertIs(runner._resolver._ever_revealed, shared)  # not the old, orphaned set
            self.assertIsNot(runner._resolver._ever_revealed, own_set_before)
            self.assertIn(7, shared)
            runner._resolver.effective_count_revealed(SimpleNamespace(id=8, vehicle_state=1))
            self.assertIn(8, shared)  # a later reveal lands in the shared set

    def test_adopt_ever_revealed_folds_in_this_accounts_own_entries(self):
        # A reveal this account already witnessed, before adopting a group's
        # pooled set, must not be silently dropped.
        from types import SimpleNamespace

        FakeSession, FakeClient = _make_fakes()
        with mock.patch("app.core.account_runner.GameSession", FakeSession), \
             mock.patch("app.core.account_runner.GameClient", FakeClient):
            runner = self._runner()
            asyncio.run(runner.connect())
            runner._resolver.effective_count_revealed(SimpleNamespace(id=3, vehicle_state=1))
            shared: set[int] = set()
            runner.adopt_ever_revealed(shared)
            self.assertIn(3, shared)

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
