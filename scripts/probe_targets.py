"""Dump the target_* fields of busy (en-route) vehicles — for the dispatch delta.

Read-only. Shows, for each non-idle vehicle, what it is currently assigned to
(target_type / target_id / queued_mission_id). Run this while vehicles are en
route (e.g. right after a dispatch) so we can tell a mission assignment from a
return-to-station, and subtract already-assigned vehicles next cycle.

    export BLAULICHT_EMAIL=...  BLAULICHT_PASSWORD=...
    python scripts/probe_targets.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.game.session import GameSession  # noqa: E402

IDLE_FMS = {1, 2}


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

        payload = await session.get_json("/api/v2/vehicles")
        rows = payload.get("result", []) if isinstance(payload, dict) else payload
        busy = [v for v in rows if v.get("fms_real") not in IDLE_FMS]

        print(f"\nvehicles total={len(rows)}  busy={len(busy)}")
        print("target_type values seen:",
              dict(Counter(v.get("target_type") for v in busy)))
        print("\nsample busy vehicles (fms, target_type, target_id, queued_mission_id, caption):")
        for v in busy[:15]:
            print(f"  fms={v.get('fms_real')}  target_type={v.get('target_type')!r}  "
                  f"target_id={v.get('target_id')}  queued={v.get('queued_mission_id')}  "
                  f"{v.get('caption')!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
