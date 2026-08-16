"""Authenticated HTTP session against one game server, for one account.

Wraps a single :class:`httpx.AsyncClient` (one cookie jar, one keep-alive
connection pool, one User-Agent) so every request for an account shares the
same browser-like identity. Sessions are never shared between accounts.

The game is a Ruby-on-Rails (Devise) app:

* login is a form POST to ``/users/sign_in`` carrying a hidden
  ``authenticity_token`` scraped from the sign-in page;
* state-changing requests carry a CSRF token from the page's
  ``<meta name="csrf-token">`` (or a per-page form token).

Because this runs **outside a browser**, none of the ``Sec-Fetch-*`` header
restrictions that forced the userscript into hidden iframes apply here — a
plain form POST with the cookie jar is a genuine, correct request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

import httpx

# Scrape helpers. Kept as regexes to avoid an HTML-parser dependency for what
# is only ever one hidden input / one meta tag on a Rails-rendered page.
_AUTH_TOKEN = re.compile(
    r'name=["\']authenticity_token["\'][^>]*value=["\']([^"\']+)["\']'
    r"|"
    r'value=["\']([^"\']+)["\'][^>]*name=["\']authenticity_token["\']'
)
_CSRF_META = re.compile(r'<meta[^>]*name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\']')


class LoginStatus(str, Enum):
    OK = "ok"
    BAD_CREDENTIALS = "bad_credentials"
    NEEDS_VERIFICATION = "needs_verification"  # 2FA / device-verification mail / captcha
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LoginResult:
    status: LoginStatus
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is LoginStatus.OK


def _find_auth_token(html: str) -> str | None:
    m = _AUTH_TOKEN.search(html)
    if not m:
        return None
    return m.group(1) or m.group(2)


class GameSession:
    """One authenticated session against ``base_url`` for a single account."""

    def __init__(
        self,
        base_url: str,
        *,
        user_agent: str,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": user_agent,
                "Accept-Language": "de-DE,de;q=0.9",
            },
        )
        self._csrf_token: str | None = None

    # -- lifecycle ---------------------------------------------------------
    async def __aenter__(self) -> "GameSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- auth --------------------------------------------------------------
    async def login(self, email: str, password: str) -> LoginResult:
        """Perform the Devise sign-in flow and retain the session cookie."""
        try:
            page = await self._client.get("/users/sign_in")
        except httpx.HTTPError as exc:
            return LoginResult(LoginStatus.UNKNOWN, f"could not load sign-in page: {exc}")

        token = _find_auth_token(page.text)
        if not token:
            return LoginResult(
                LoginStatus.UNKNOWN, "no authenticity_token on sign-in page"
            )

        resp = await self._client.post(
            "/users/sign_in",
            data={
                "authenticity_token": token,
                "user[email]": email,
                "user[password]": password,
                "commit": "Anmelden",
            },
        )

        # Success = we are no longer on the sign-in page and a session cookie
        # was set. Devise re-renders sign_in (HTTP 200) on bad credentials.
        landed_on_sign_in = resp.url.path.rstrip("/").endswith("/users/sign_in")
        body = resp.text.lower()

        if not landed_on_sign_in:
            # Cache the CSRF token from the landing page for later POSTs.
            self._csrf_token = self._extract_csrf(resp.text)
            return LoginResult(LoginStatus.OK)

        if any(
            kw in body
            for kw in ("bestätigung", "verification", "captcha", "code", "zwei-faktor")
        ):
            return LoginResult(
                LoginStatus.NEEDS_VERIFICATION,
                "server asked for verification (2FA / device / captcha)",
            )
        return LoginResult(LoginStatus.BAD_CREDENTIALS, "sign-in was rejected")

    # -- requests ----------------------------------------------------------
    async def get_json(self, path: str) -> object:
        resp = await self._client.get(path)
        resp.raise_for_status()
        return resp.json()

    async def get_text(self, path: str) -> str:
        resp = await self._client.get(path)
        resp.raise_for_status()
        # Refresh the cached CSRF token whenever we load a full HTML page.
        token = self._extract_csrf(resp.text)
        if token:
            self._csrf_token = token
        return resp.text

    async def post_form(self, path: str, data: dict[str, object]) -> httpx.Response:
        resp = await self._client.post(path, data=data)
        resp.raise_for_status()
        return resp

    # -- helpers -----------------------------------------------------------
    @property
    def csrf_token(self) -> str | None:
        return self._csrf_token

    @staticmethod
    def _extract_csrf(html: str) -> str | None:
        m = _CSRF_META.search(html)
        return m.group(1) if m else None

    @staticmethod
    def extract_form_token(html: str) -> str | None:
        """Pull the per-page ``authenticity_token`` from a mission/vehicle page.

        Preferred over the cached meta token for dispatch, in case Rails issues
        per-page tokens.
        """
        return _find_auth_token(html)
