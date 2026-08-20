"""Probe active Sprechwünsche (speech requests) — run while one is showing.

Read-only. Dumps missions that have an active speech request or patients/
prisoners, and any vehicles in a speech-request FMS status, with the fields that
drive resolution (sw, missing_text, patients/prisoners). Paste the output to
wire Sprechwunsch handling (transport + Nachforderung dispatch) accurately.

    export BLAULICHT_EMAIL=...  BLAULICHT_PASSWORD=...
    python scripts/probe_sprechwunsch.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.game.session import GameSession  # noqa: E402

# Fields most relevant to a speech request / follow-up need.
MISSION_FIELDS = [
    "id", "caption", "mtid", "sw", "sw_start_in", "missing_text",
    "patients_count", "possible_patients_count", "prisoners_count",
    "possible_prisoners_count", "patients_prisoners", "alliance_shared_at",
]


async def main() -> int:
    email = os.environ.get("BLAULICHT_EMAIL")
    password = os.environ.get("BLAULICHT_PASSWORD")
    if not email or not password:
        print("Set BLAULICHT_EMAIL and BLAULICHT_PASSWORD.")
        return 2

    settings = get_settings()
    async with GameSession(settings.base_url("de"), user_agent=settings.user_agent) as session:
        result = await session.login(email, password)
        print("login:", result.status.value)
        if not result.ok:
            return 1

        data = await session.get_json("/map/missions_json")
        missions = data.get("missions", []) if isinstance(data, dict) else (data or [])
        print(f"\ntotal missions: {len(missions)}")
        print("sw value distribution:", dict(Counter(m.get("sw") for m in missions)))

        def interesting(m):
            return bool(m.get("sw")) or m.get("patients_count") or m.get("prisoners_count")

        hot = [m for m in missions if interesting(m)]
        print(f"missions with speech request / patients / prisoners: {len(hot)}")
        for m in hot[:8]:
            slim = {k: m.get(k) for k in MISSION_FIELDS if k in m}
            print("  " + json.dumps(slim, ensure_ascii=False, default=str)[:500])
        if hot:
            print("\nALL keys on first hot mission:", sorted(hot[0].keys()))
            print("\nFULL first hot mission (values):")
            print(json.dumps(hot[0], ensure_ascii=False, default=str)[:2500])
            # Decode the cryptic short keys across all hot missions (value spread).
            for k in ("sw", "vehicle_state", "handoff", "ct", "kt", "pt", "tv"):
                vals = sorted({str(m.get(k)) for m in hot if k in m})
                print(f"  {k} values seen: {vals[:12]}")

        # Vehicles currently in a speech-request-ish FMS status.
        vpayload = await session.get_json("/api/v2/vehicles")
        vehicles = vpayload.get("result", []) if isinstance(vpayload, dict) else (vpayload or [])
        sprech = [v for v in vehicles if v.get("fms_real") in (5, 6)]
        print(f"\nvehicles in FMS 5/6: {len(sprech)}")
        for v in sprech[:8]:
            print(f"  fms={v.get('fms_real')} type={v.get('vehicle_type')} "
                  f"target_type={v.get('target_type')!r} target_id={v.get('target_id')} "
                  f"caption={v.get('caption')!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
