"""Login-response classification (app.game.session).

Gated on httpx (a runtime dep of the game package) so the pure-stdlib suite
stays importable without it. Only the pure classification helper is tested
here — GameSession itself builds its own httpx.AsyncClient with no injection
seam, so exercising the full login() flow needs a live account (see
scripts/smoke.py) or a future refactor to accept a transport.
"""

import unittest

try:
    import httpx  # noqa: F401
    HAVE_HTTPX = True
except ImportError:
    HAVE_HTTPX = False


@unittest.skipUnless(HAVE_HTTPX, "httpx not installed")
class ClassifyRejectedLoginTests(unittest.TestCase):
    def test_bad_credentials_phrase_wins_even_with_word_code_present(self):
        # Regression: live-verified 2026-08-31 — a genuine "invalid
        # credentials" page was misclassified as NEEDS_VERIFICATION because
        # the removed "code" keyword matched incidental page content
        # (e.g. a <script> or meta tag mentioning "code"). The specific
        # German phrase must win regardless of what else is on the page.
        from app.game.session import LoginStatus, _classify_rejected_login

        body = (
            "...<div class=\"alert\">ungültige anmeldedaten.</div>..."
            "<script>var languageCode = 'de';</script>..."
        )
        result = _classify_rejected_login(body)
        self.assertEqual(result.status, LoginStatus.BAD_CREDENTIALS)

    def test_real_verification_challenge_detected(self):
        from app.game.session import LoginStatus, _classify_rejected_login

        result = _classify_rejected_login("bitte gib deinen bestätigungscode ein")
        self.assertEqual(result.status, LoginStatus.NEEDS_VERIFICATION)

    def test_captcha_detected(self):
        from app.game.session import LoginStatus, _classify_rejected_login

        result = _classify_rejected_login("please solve this captcha to continue")
        self.assertEqual(result.status, LoginStatus.NEEDS_VERIFICATION)

    def test_unrecognized_rejection_falls_back_to_bad_credentials(self):
        from app.game.session import LoginStatus, _classify_rejected_login

        result = _classify_rejected_login("something went wrong, please try again")
        self.assertEqual(result.status, LoginStatus.BAD_CREDENTIALS)


if __name__ == "__main__":
    unittest.main()
