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
class CombinedTotalsTests(unittest.TestCase):
    """TODO.md P3.2: combined group stats — sums the same per-account `stat`
    rows the normal dashboard already shows (group dispatches are recorded
    per-account), no new persistence."""

    def setUp(self):
        from app.store import Store

        self.store = Store(":memory:")

    def test_sums_across_members(self):
        from app.web.app import _combined_totals

        a1 = self.store.add_account("a", "a@e.com")
        a2 = self.store.add_account("b", "b@e.com")
        self.store.add_stat(a1, dispatched=3, missions=2, credits=100)
        self.store.add_stat(a2, dispatched=5, missions=1, credits=50)
        self.assertEqual(
            _combined_totals(self.store, {a1, a2}),
            {"dispatched": 8, "missions": 3, "credits": 150},
        )

    def test_empty_members_is_empty_dict(self):
        from app.web.app import _combined_totals

        self.assertEqual(_combined_totals(self.store, set()), {})


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

        from app.core.logbuffer import LogStore

        self.store = Store(":memory:")
        self.box = SecretBox("test-master-key")
        self.auth = AdminAuth(self.store)
        self.auth.bootstrap("adminpw")
        self.log_store = LogStore()
        # Factory is never invoked (no account is started in these tests).
        orch = Orchestrator(lambda aid: None)
        self.client = TestClient(
            create_app(self.store, orch, self.box, get_settings(), self.auth, self.log_store)
        )

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
                "own_credit_min": 100, "alliance_credit_max": 8000,
                "max_vehicles_per_mission": "", "order": "oldest",
                "mission_source": "own",
                "parasite_scope": "alliance", "parasite_aggressiveness": "needed",
                "transport_enabled": "1", "transport_max_distance_km": 50,
                "transport_max_tax": "", "transport_prefer_own": "1",
                "transport_allow_alliance": "",
            },
        )
        cfg = json.loads(self.store.get_config(aid))
        self.assertEqual(cfg["radius_km"], 25)
        self.assertEqual(cfg["own_credit_min"], 100)
        self.assertEqual(cfg["alliance_credit_max"], 8000)
        self.assertEqual(cfg["mission_source"], ["own"])
        self.assertEqual(cfg["parasite_aggressiveness"], "needed")

    def test_mission_source_checkboxes_roundtrip_as_a_set(self):
        # mission_source is now a multi-select (Sasha, 2026-09-12: own /
        # alliance / event are independent tags, not one exclusive choice) --
        # posting several checked boxes must save all of them.
        import json

        self._login()
        self.client.post("/accounts", data={"label": "n", "email": "x@e.com", "password": "p"})
        aid = self.store.list_accounts()[0].id
        self.client.post(
            f"/accounts/{aid}",
            data={
                "label": "n", "email": "x@e.com", "password": "",
                "mission_source": ["own", "event"],
            },
        )
        cfg = json.loads(self.store.get_config(aid))
        self.assertEqual(sorted(cfg["mission_source"]), ["event", "own"])

    def test_mission_source_no_boxes_checked_saves_empty(self):
        import json

        self._login()
        self.client.post("/accounts", data={"label": "n", "email": "x@e.com", "password": "p"})
        aid = self.store.list_accounts()[0].id
        self.client.post(
            f"/accounts/{aid}",
            data={"label": "n", "email": "x@e.com", "password": ""},  # no mission_source key at all
        )
        cfg = json.loads(self.store.get_config(aid))
        self.assertEqual(cfg["mission_source"], [])

    def test_account_config_page_renders_with_mission_source_checkboxes(self):
        # account.html isn't rendered by any of the POST-only tests above --
        # this is the only regression guard against a template error in the
        # checkbox markup (Sasha, 2026-09-12's own/alliance/event multiselect).
        self._login()
        self.client.post("/accounts", data={"label": "n", "email": "x@e.com", "password": "p"})
        aid = self.store.list_accounts()[0].id
        r = self.client.get(f"/accounts/{aid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn('value="event"', r.text)
        self.assertNotIn("No source selected", r.text)  # default is own+alliance, not empty

    def test_account_config_page_warns_when_no_source_is_selected(self):
        self._login()
        self.client.post("/accounts", data={"label": "n", "email": "x@e.com", "password": "p"})
        aid = self.store.list_accounts()[0].id
        self.client.post(
            f"/accounts/{aid}",
            data={"label": "n", "email": "x@e.com", "password": ""},  # no mission_source checked
        )
        r = self.client.get(f"/accounts/{aid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("No source selected", r.text)

    def test_account_details_page_shows_mission_source_as_a_joined_list(self):
        self._login()
        self.client.post("/accounts", data={"label": "n", "email": "x@e.com", "password": "p"})
        aid = self.store.list_accounts()[0].id
        self.client.post(f"/accounts/{aid}", data={
            "label": "n", "email": "x@e.com", "password": "", "mission_source": ["own", "event"],
        })
        r = self.client.get(f"/accounts/{aid}/details")
        self.assertEqual(r.status_code, 200)
        self.assertIn("own, event", r.text)
        self.assertNotIn("('own', 'event')", r.text)

    def test_settings_page_and_password_change(self):
        self._login()
        self.assertEqual(self.client.get("/settings").status_code, 200)
        # change password
        r = self.client.post(
            "/settings/password",
            data={"current": "adminpw", "new_password": "newpw"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.auth.check("newpw"))
        self.assertFalse(self.auth.check("adminpw"))

    def test_password_change_rejects_wrong_current(self):
        self._login()
        r = self.client.post(
            "/settings/password",
            data={"current": "wrong", "new_password": "x"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertTrue(self.auth.check("adminpw"))  # unchanged

    def test_details_page_shows_log_and_totals(self):
        self._login()
        self.client.post("/accounts", data={"label": "n", "email": "u@e.com", "password": "p"})
        aid = self.store.list_accounts()[0].id
        self.store.add_stat(aid, dispatched=4, missions=2)
        self.log_store.append(aid, "sent to #123 'Brand': 2 vehicle(s)")
        r = self.client.get(f"/accounts/{aid}/details")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Brand", r.text)
        self.assertIn("4", r.text)  # dispatched total

    def test_details_page_shows_mission_breakdown_grouped_by_category(self):
        self._login()
        self.client.post("/accounts", data={"label": "n", "email": "u@e.com", "password": "p"})
        aid = self.store.list_accounts()[0].id
        self.store.set_mission_breakdown(aid, [
            {"mission_id": 1, "caption": "Brennender Container", "category": "own",
             "live_status": "red", "bot_status": "fully staffed", "explanation": "1 vehicle(s) sent"},
            {"mission_id": 2, "caption": "[Verband] Verkehrsunfall", "category": "verband",
             "live_status": "yellow", "bot_status": "not enough vehicles",
             "explanation": "sent 1, still short: nef x1"},
        ])
        r = self.client.get(f"/accounts/{aid}/details")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Mission breakdown", r.text)
        self.assertIn("Brennender Container", r.text)
        self.assertIn("[Verband] Verkehrsunfall", r.text)
        self.assertIn("fully staffed", r.text)
        self.assertIn("not enough vehicles", r.text)
        self.assertIn("No event missions this cycle.", r.text)  # empty group still shown

    def test_dashboard_shows_live_mission_status_breakdown(self):
        self._login()
        self.client.post("/accounts", data={"label": "n", "email": "u@e.com", "password": "p"})
        aid = self.store.list_accounts()[0].id
        self.store.set_mission_status(aid, {"red": 12, "yellow": 3, "green": 1})
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("12 red", r.text)
        self.assertIn("3 yellow", r.text)
        self.assertIn("1 green", r.text)

    def test_dashboard_omits_status_breakdown_when_unset(self):
        self._login()
        self.client.post("/accounts", data={"label": "n", "email": "u@e.com", "password": "p"})
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(" red</span>", r.text)

    def test_classify_log_colors(self):
        from app.web.app import classify_log
        self.assertEqual(classify_log("FAILED LF (#1) → #2"), "log-err")
        self.assertEqual(classify_log("STRUCK OUT: no firetrucks available"), "log-err")
        self.assertEqual(classify_log("  short #5 'x': not enough vehicles"), "log-warn")
        self.assertEqual(classify_log("dispatched RTW (#9) → #3"), "log-ok")
        self.assertEqual(classify_log("polled 10 mission(s)"), "log-info")

    def test_classify_log_recognizes_the_new_dispatch_verbs(self):
        # Sasha, 2026-09-11: "sent to help"/"sent reinforcements" fell
        # through uncoloured before -- only "dispatched" was recognized.
        from app.web.app import classify_log
        self.assertEqual(classify_log("sent to help 2 → #4 'Brand': LF"), "log-ok")
        self.assertEqual(classify_log("sent reinforcements 1 → #4 'Brand': LF"), "log-ok")

    def test_classify_log_strips_the_group_mode_account_prefix(self):
        # Group mode prefixes every line with "[#<id>] " -- this made EVERY
        # group log line fall through uncoloured, since all the rules below
        # are startswith-based and never saw past the prefix.
        from app.web.app import classify_log
        self.assertEqual(classify_log("[#12] dispatched 2 → #4 'Brand': LF, HLF"), "log-ok")
        self.assertEqual(classify_log("[#12] sent to help 1 → #4 'Brand': LF"), "log-ok")
        self.assertEqual(classify_log("[#12] FAILED LF (#9) → #4 'Brand' (no crew?)"), "log-err")
        self.assertEqual(classify_log("[#12] struck out LF (#9): game rejected it (reason)"), "log-err")

    def test_compound_log_on_dashboard(self):
        self._login()
        self.client.post("/accounts", data={"label": "nord", "email": "u", "password": "p"})
        aid = self.store.list_accounts()[0].id
        self.log_store.append(aid, "dispatched RTW (#9) → #3 'Unfall'")
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Live log", r.text)
        self.assertIn("nord", r.text)      # account prefix
        self.assertIn("dispatched RTW", r.text)

    def test_tag_account_id_extracts_the_group_prefix(self):
        # Sasha, 2026-09-12: "add options for viewing compound log or
        # per-account log" -- needs the real account a group line concerns,
        # since every group line lives under one shared GROUP_KEY bucket.
        from app.web.app import _tag_account_id
        self.assertEqual(_tag_account_id("[#12] dispatched 2 → #4 'Brand': LF"), 12)
        self.assertIsNone(_tag_account_id("group: 3 account(s), 5 mission(s), 2 idle vehicle(s)"))
        self.assertIsNone(_tag_account_id("dispatched RTW (#9) → #3 'Unfall'"))  # solo line, no tag

    def test_dashboard_log_rows_carry_a_per_account_filter_id(self):
        self._login()
        self.client.post("/accounts", data={"label": "nord", "email": "u", "password": "p"})
        aid = self.store.list_accounts()[0].id
        self.log_store.append(aid, "dispatched RTW (#9) → #3 'Unfall'")  # solo line
        self.log_store.append(0, f"[#{aid}] dispatched 1 → #7 'Brand': LF")  # group line, tagged
        self.log_store.append(0, "group: 1 account(s), 2 mission(s), 0 idle vehicle(s)")  # group summary, untagged
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f'data-acct="{aid}"', r.text)   # both the solo line and the tagged group line
        self.assertIn('data-acct=""', r.text)          # the untagged group summary line has no owner
        self.assertIn(f'value="{aid}"', r.text)         # the filter dropdown lists this account

    def test_account_details_merges_in_group_lines_tagged_to_it(self):
        # An account's own details page previously showed NOTHING it did
        # while running inside a group (every group line lives under the
        # shared GROUP_KEY bucket, never under the account's own id).
        self._login()
        self.client.post("/accounts", data={"label": "n", "email": "u@e.com", "password": "p"})
        aid = self.store.list_accounts()[0].id
        self.log_store.append(0, f"[#{aid}] dispatched 1 → #7 'Brand': LF")
        self.log_store.append(0, "group: 1 account(s), 2 mission(s), 0 idle vehicle(s)")
        r = self.client.get(f"/accounts/{aid}/details")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Brand", r.text)                       # the tagged line made it in, tag stripped
        self.assertNotIn(f"[#{aid}]", r.text)
        self.assertNotIn("group: 1 account(s)", r.text)       # the untagged summary line stays out

    def test_account_log_helper_merges_and_sorts_by_seq(self):
        from app.web.app import _account_log
        self.log_store.append(1, "polled 3 mission(s)")           # seq 1, account 1's own
        self.log_store.append(0, "[#1] dispatched 1 → #7: LF")    # seq 2, tagged to account 1
        self.log_store.append(2, "polled 5 mission(s)")           # seq 3, a DIFFERENT account
        rows = _account_log(self.log_store, 1)
        self.assertEqual([r["message"] for r in rows], ["dispatched 1 → #7: LF", "polled 3 mission(s)"])

    def test_username_accepted_as_login_id(self):
        # The account "email" field is just the login id; a username is fine.
        self._login()
        self.client.post("/accounts", data={"label": "n", "email": "just_a_username", "password": "p"})
        self.assertEqual(self.store.list_accounts()[0].email, "just_a_username")


if __name__ == "__main__":
    unittest.main()
