"""Typed client over the game endpoints, built on a :class:`GameSession`.

The game's **official APIs are read-only (GET); there is no write API**
(see ``data/reference/api-endpoints.md``). So reads use the official endpoints,
but the one write the bot needs — *dispatch* — is a form POST to the same
``/missions/{id}/alarm`` route a browser submits, carrying a per-mission token
fetched fresh from the mission page. That is the only shape confirmed against a
live capture. One vehicle is dispatched per POST; multi-vehicle single-POST was
never verified and is intentionally not attempted.

    read (official)   fetch_vehicles      GET  /api/v2/vehicles
                      fetch_vehicle_states GET /api/vehicle_states
                      fetch_buildings     GET  /api/buildings
                      fetch_alliance_buildings GET /api/alliance_buildings
                      fetch_userinfo      GET  /api/userinfo   (credits)
                      fetch_einsaetze     GET  /einsaetze.json (public)
    read (undocumented but required)
                      fetch_missions      GET  /map/missions_json
    act (form POST / GET, not an API)
                      dispatch, recall, transport_patient, transport_prisoner
"""

from __future__ import annotations

import logging
import re
from types import MappingProxyType
from typing import NamedTuple

from .models import Building, Mission, Vehicle
from .session import GameSession

# The "Anfahren" buttons on a vehicle's page — one per hospital/cell that still
# has capacity (the game pre-filters by beds, range, price, own/alliance per the
# vehicle's in-game transport settings). We just pick from what it offers.
# NB: the game renders the href with SINGLE quotes (href='...'), and may append a
# query string — accept either quote and trailing params up to the closing quote.
_APPROACH = re.compile(
    r"""href=["'](/vehicles/\d+/(?:patient|gefangener)/\d+[^"']*)["']"""
)

# Live-verified 2026-09-11: an alarm POST that includes a vehicle the game
# can't actually use (missing crew, or its towing vehicle isn't at the
# station) still succeeds at the HTTP level — the response page just carries
# a Bootstrap alert-danger div per rejected vehicle, e.g.:
#   <div id="alert_danger_..." class="alert fade in alert-danger  ">
#     <button ...>×</button><a href="/vehicles/125521931">🟢 Anh TeSi</a>:
#     Das Zugfahrzeug befindet sich nicht auf der Wache.</div>
# Matched generically on the alert-danger class + a /vehicles/{id} link,
# NOT on the specific reason text (which varies by failure — missing crew
# vs. missing tow vehicle — and we don't have every exact wording), so any
# reason the game reports this way is caught the same way.
#
# Every gap uses a bounded "any char that doesn't start a div tag" class
# (`(?:(?!</?div\b).)*?`), NOT a bare `.*?` — code review (2026-09-11) found
# that a bare lazy dot-match under DOTALL happily crosses `</div>`/`<div>`
# boundaries: an alert-danger div with NO vehicle link at all (a generic
# error banner, plausible on a real page) would let the match run on and
# latch onto the FIRST unrelated `<a href="/vehicles/…">…</a>: …</div>` found
# ANYWHERE later on the page — e.g. a perfectly healthy vehicle's own status
# row — and misreport it as a confirmed rejection, striking a healthy
# vehicle out immediately. Bounding every gap to the current div closes that.
_DISPATCH_FAILURE = re.compile(
    r'class="[^"]*\balert-danger\b[^"]*"[^>]*>'
    r'(?:(?!</?div\b).)*?'
    r'<a href="/vehicles/(\d+)"[^>]*>'
    r'(?:(?!</?div\b).)*?</a>'
    r'\s*:\s*'
    r'((?:(?!</?div\b).)*?)'
    r'</div>',
    re.DOTALL,
)


def _parse_dispatch_failures(html: str) -> dict[int, str]:
    """Vehicles the game explicitly rejected in an alarm response, keyed by
    id, with the reason text — see ``_DISPATCH_FAILURE``. Empty if the game
    accepted every vehicle (the common case)."""
    return {int(vid): reason.strip() for vid, reason in _DISPATCH_FAILURE.findall(html)}


_NO_REJECTIONS: "MappingProxyType[int, str]" = MappingProxyType({})


class DispatchResult(NamedTuple):
    """Outcome of a dispatch attempt.

    ``ok`` is the HTTP-transport-level result (unchanged meaning from the old
    plain-bool return) — a POST that goes through is ``ok=True`` even if the
    game rejected some vehicles within it. ``rejected`` maps vehicle id (among
    the ones attempted) to the game's own reason text, for every vehicle it
    explicitly named as unusable — the caller should strike these
    immediately (see ``StrikeTracker.record_confirmed_failure``) rather than
    count them as dispatched.
    """

    ok: bool
    rejected: "MappingProxyType[int, str]" = _NO_REJECTIONS


logger = logging.getLogger(__name__)

# Static query params observed on the real dispatch/recall requests. Not tokens
# — they select the "dispatch centre" (sd=d) / action context (sk=ac) views.
_CTX = "sd=d&sk=ac"

# Safety bound on vehicle pagination (huge accounts still finish; a broken
# endpoint can't loop forever).
_MAX_VEHICLE_PAGES = 200


# Envelope keys the game wraps list payloads in (v2 endpoints are paginated
# under "result"). Checked before falling back to id-keyed-map handling.
_CONTAINER_KEYS = ("result", "results", "vehicles", "buildings", "missions", "data")


def _as_list(payload: object) -> list[dict]:
    """Normalise an endpoint payload to a list of dicts.

    Accepts a bare JSON array, an envelope like ``{"result": [...]}`` (v2
    endpoints), or an object keyed by id — so a server-side shape difference
    doesn't break reading.
    """
    if isinstance(payload, list):
        return [d for d in payload if isinstance(d, dict)]
    if isinstance(payload, dict):
        for key in _CONTAINER_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return [d for d in value if isinstance(d, dict)]
        return [v for v in payload.values() if isinstance(v, dict)]
    return []


class GameClient:
    """High-level game operations for one authenticated account."""

    def __init__(self, session: GameSession) -> None:
        self._s = session

    # -- reads -------------------------------------------------------------
    async def fetch_missions(self) -> list[Mission]:
        """Current open missions with coordinates.

        Uses ``/map/missions_json`` — undocumented, but the official API list
        has no open-missions endpoint, and this is capture-verified.
        """
        payload = await self._s.get_json("/map/missions_json")
        return [Mission.from_json(d) for d in _as_list(payload)]

    async def fetch_vehicles(self, cap: int = 0) -> list[Vehicle]:
        """Your **entire** fleet, paging through ``/api/v2/vehicles``.

        Large accounts return the fleet in pages; fetching only page 1 makes the
        bot think most vehicles are unavailable and rack up needless strikes. We
        page until no new vehicles appear (dedup-guarded, so it also works if the
        endpoint ignores ``?page=`` and just repeats page 1). *cap* > 0 stops
        after that many vehicles (0 = load all).
        """
        seen: set[int] = set()
        rows: list[dict] = []
        for page in range(1, _MAX_VEHICLE_PAGES + 1):
            payload = await self._s.get_json(f"/api/v2/vehicles?page={page}")
            fresh = [d for d in _as_list(payload) if d.get("id") not in seen]
            if not fresh:
                break  # empty page, or a repeat -> no real pagination
            for d in fresh:
                seen.add(d.get("id"))
                rows.append(d)
            if cap and len(rows) >= cap:
                rows = rows[:cap]
                break
        return [Vehicle.from_json(d) for d in rows]

    async def fetch_vehicle_states(self) -> dict:
        """FMS status summary (counts per status) — a cheap availability peek."""
        payload = await self._s.get_json("/api/vehicle_states")
        return payload if isinstance(payload, dict) else {}

    async def fetch_buildings(self) -> list[Building]:
        payload = await self._s.get_json("/api/buildings")
        return [Building.from_json(d) for d in _as_list(payload)]

    async def fetch_building(self, building_id: int) -> dict:
        """Single building detail (level, extensions, personnel) for estate
        reconciliation. Raw dict — parsed by ``estate.BuildingState.from_api``."""
        payload = await self._s.get_json(f"/api/buildings/{building_id}")
        return payload if isinstance(payload, dict) else {}

    async def fetch_building_vehicles(self, building_id: int) -> list[Vehicle]:
        """Vehicles stationed at a building (``/api/v2/buildings/{id}/vehicles``)."""
        payload = await self._s.get_json(f"/api/v2/buildings/{building_id}/vehicles")
        return [Vehicle.from_json(d) for d in _as_list(payload)]

    async def fetch_equipments(self) -> list[dict]:
        """Equipment / trailer modules (Rollcontainer, winches, …)."""
        payload = await self._s.get_json("/api/equipments")
        return _as_list(payload)

    async def fetch_schoolings(self) -> list[dict]:
        """Your training courses (running + available) — personnel training."""
        payload = await self._s.get_json("/api/schoolings")
        return _as_list(payload)

    async def fetch_alliance_schoolings(self) -> list[dict]:
        """Alliance-provided training courses."""
        payload = await self._s.get_json("/api/alliance_schoolings")
        return _as_list(payload)

    async def fetch_alliance_buildings(self) -> list[Building]:
        """Alliance-funded buildings — transport targets for alliance play."""
        payload = await self._s.get_json("/api/alliance_buildings")
        return [Building.from_json(d) for d in _as_list(payload)]

    async def fetch_userinfo(self) -> dict:
        """Credits / user info (``/api/userinfo``, alias ``/credits``)."""
        payload = await self._s.get_json("/api/userinfo")
        return payload if isinstance(payload, dict) else {}

    async def fetch_einsaetze(self) -> list[dict]:
        """The public mission-type catalogue as its raw array.

        Feed straight into :meth:`app.domain.catalog.MissionCatalog.from_payload`.
        Cache hard — it changes at most a few times a year.
        """
        payload = await self._s.get_json("/einsaetze.json")
        return _as_list(payload)

    # -- actions -----------------------------------------------------------
    async def dispatch_many(self, mission_id: int, vehicle_ids) -> DispatchResult:
        """Dispatch one or more vehicles to a mission in a SINGLE alarm POST —
        the same multi-select form the browser submits (repeated ``vehicle_ids[]``).

        Loads the mission page once for a fresh authenticity token, then posts the
        whole batch. One token + one request for the whole mission. ``ok=True``
        means the POST went through — some of the vehicles in it may still have
        been rejected by the game (see :attr:`DispatchResult.rejected`).
        """
        ids = [int(v) for v in vehicle_ids]
        if not ids:
            return DispatchResult(ok=False)
        page = await self._s.get_text(f"/missions/{mission_id}?{_CTX}")
        token = self._s.extract_form_token(page) or self._s.csrf_token
        if not token:
            logger.warning("dispatch %s: no authenticity token found", mission_id)
            return DispatchResult(ok=False)

        try:
            resp = await self._s.post_form(
                f"/missions/{mission_id}/alarm?{_CTX}",
                {
                    "utf8": "✓",
                    "authenticity_token": token,
                    "commit": "Dispatch",
                    "sonderrechte": "1",  # blue-lights / priority
                    "next_mission": "0",
                    "next_mission_id": "0",
                    "alliance_mission_publish": "0",
                    "sk": "ac",
                    "sd": "d",
                    # httpx encodes a list value as repeated keys -> vehicle_ids[]=..&vehicle_ids[]=..
                    "vehicle_ids[]": [str(v) for v in ids],
                },
            )
        except Exception:  # noqa: BLE001 — caller decides how to react
            logger.exception("dispatch %s <- vehicles %s failed", mission_id, ids)
            return DispatchResult(ok=False)
        attempted = set(ids)
        failures = _parse_dispatch_failures(resp.text)
        rejected = {vid: reason for vid, reason in failures.items() if vid in attempted}
        return DispatchResult(ok=True, rejected=MappingProxyType(rejected) if rejected else _NO_REJECTIONS)

    async def dispatch(self, mission_id: int, vehicle_id: int) -> DispatchResult:
        """Dispatch a single vehicle (a one-element :meth:`dispatch_many`) — used
        as the isolation fallback when a batch dispatch fails."""
        return await self.dispatch_many(mission_id, [vehicle_id])

    async def transport_targets(self, vehicle_id: int) -> list[str]:
        """Hospitals/cells this vehicle can currently unload to, as action URLs.

        Reads the vehicle's page and returns the ``/vehicles/{id}/patient|
        gefangener/{building}`` links behind its "Anfahren" buttons — already
        limited to targets with free capacity.
        """
        try:
            page = await self._s.get_text(f"/vehicles/{vehicle_id}")
        except Exception:  # noqa: BLE001
            return []
        # De-dup preserving order (page can repeat a link).
        seen, out = set(), []
        for href in _APPROACH.findall(page):
            if href not in seen:
                seen.add(href)
                out.append(href)
        return out

    async def send_transport(self, action_url: str) -> bool:
        """Perform one transport by following an ``Anfahren`` link."""
        try:
            await self._s.get_text(action_url)
        except Exception:  # noqa: BLE001
            logger.exception("transport %s failed", action_url)
            return False
        return True

    async def share_mission(self, mission_id: int) -> bool:
        """Share an OWN mission with the alliance ("Im Verband freigeben") so
        members can help. Follows the real share button's href
        ``GET /missions/{id}/alliance?ifs=fi&ift=kt_al_ae&sd=a&sk=cr``
        (id=mission_alliance_share_btn, captured from the game)."""
        try:
            await self._s.get_text(
                f"/missions/{mission_id}/alliance?ifs=fi&ift=kt_al_ae&sd=a&sk=cr"
            )
        except Exception:  # noqa: BLE001
            logger.exception("sharing mission %s to alliance failed", mission_id)
            return False
        return True

    async def recall(self, vehicle_id: int) -> bool:
        """Recall an en-route vehicle (``backalarm``)."""
        try:
            await self._s.get_text(
                f"/vehicles/{vehicle_id}/backalarm?return=mission_js&{_CTX}"
            )
        except Exception:  # noqa: BLE001
            logger.exception("recall of vehicle %s failed", vehicle_id)
            return False
        return True

    async def transport_patient(self, vehicle_id: int, building_id: int) -> bool:
        """Route a transporting vehicle's patient to a hospital building."""
        return await self._transport("patient", vehicle_id, building_id)

    async def transport_prisoner(self, vehicle_id: int, building_id: int) -> bool:
        """Route a transporting vehicle's prisoner to a cell building."""
        return await self._transport("gefangener", vehicle_id, building_id)

    async def _transport(self, kind: str, vehicle_id: int, building_id: int) -> bool:
        try:
            await self._s.get_text(f"/vehicles/{vehicle_id}/{kind}/{building_id}")
        except Exception:  # noqa: BLE001
            logger.exception("%s transport (vehicle %s -> %s) failed", kind, vehicle_id, building_id)
            return False
        return True
