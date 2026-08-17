"""Read-only API shape probe — resolves the provisional field names in one run.

Logs in, GETs each read endpoint once, and prints its JSON *structure* (top-level
type/keys, and the keys + a few sample values of the first element). Sends
nothing, changes nothing. Paste the output back to lock the game-model parsers.

Usage (throwaway account):

    pip install -e .
    export BLAULICHT_EMAIL=...  BLAULICHT_PASSWORD=...
    python scripts/probe.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.game.session import GameSession  # noqa: E402

# (label, path). Read-only; one GET each.
ENDPOINTS = [
    ("missions_json", "/map/missions_json"),
    ("v2/vehicles", "/api/v2/vehicles"),
    ("v1/vehicles", "/api/vehicles"),
    ("buildings", "/api/buildings"),
    ("vehicle_states", "/api/vehicle_states"),
    ("userinfo", "/api/userinfo"),
]

# Fields we care about per endpoint — reported present/absent explicitly.
INTEREST = {
    "missions_json": ["id", "lat", "lng", "long", "latitude", "longitude",
                      "mission_type_id", "mtid", "caption"],
    "v2/vehicles": ["id", "vehicle_type", "vehicle_type_id", "vehicle_type_caption",
                    "fms_real", "fms_show", "fms", "building_id", "latitude", "longitude",
                    "target_type", "caption"],
    "v1/vehicles": ["id", "vehicle_type", "fms_real", "building_id", "caption"],
    "buildings": ["id", "building_type", "latitude", "longitude", "level",
                  "extensions", "personal_count", "personal_free", "caption"],
}


def _describe(label: str, payload: object) -> None:
    print(f"\n===== {label} =====")
    if isinstance(payload, list):
        print(f"top-level: LIST (len={len(payload)})")
        first = payload[0] if payload else None
    elif isinstance(payload, dict):
        print("top-level: OBJECT, keys =", list(payload.keys())[:20])
        # find the first list-valued key (the likely container)
        first = None
        for k, v in payload.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                print(f"  -> list container under key '{k}' (len={len(v)})")
                first = v[0]
                break
        if first is None:
            vals = [v for v in payload.values() if isinstance(v, dict)]
            first = vals[0] if vals else None
    else:
        print("top-level:", type(payload).__name__, repr(payload)[:200])
        return

    if not isinstance(first, dict):
        print("  (no sample element found)")
        return

    print("  element keys:", sorted(first.keys()))
    wanted = INTEREST.get(label)
    if wanted:
        present = {k: first.get(k) for k in wanted if k in first}
        missing = [k for k in wanted if k not in first]
        print("  interesting present:", json.dumps(present, default=str)[:400])
        print("  interesting MISSING:", missing)
    print("  sample element:", json.dumps(first, default=str)[:600])


async def main() -> int:
    email = os.environ.get("BLAULICHT_EMAIL")
    password = os.environ.get("BLAULICHT_PASSWORD")
    if not email or not password:
        print("Set BLAULICHT_EMAIL and BLAULICHT_PASSWORD.")
        return 2

    settings = get_settings()
    async with GameSession(settings.base_url("de"), user_agent=settings.user_agent) as session:
        result = await session.login(email, password)
        print("login:", result.status.value, result.detail)
        if not result.ok:
            return 1
        for label, path in ENDPOINTS:
            try:
                payload = await session.get_json(path)
                _describe(label, payload)
            except Exception as exc:  # noqa: BLE001
                print(f"\n===== {label} =====\n  ERROR: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
