"""Dump a vehicle's type + which requirement classes we map it to.

Diagnoses "needed but not sent": a mission asks for class X, but the free vehicle
is a variant whose type id isn't in class X's set (e.g. a FuStW-DGL /
Dienstgruppenleiter is type 103 = police_service_group_leader, NOT police_cars).

Read-only. Usage (throwaway account):

    pip install -e .
    export BLAULICHT_EMAIL=...  BLAULICHT_PASSWORD=...
    python scripts/probe_vehicle.py 81048893 [more ids...]

Paste the output back so the class map / derivation can be corrected safely.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.domain.assets import AssetLibrary  # noqa: E402
from app.game.client import GameClient  # noqa: E402
from app.game.session import GameSession  # noqa: E402


async def main() -> int:
    email = os.environ.get("BLAULICHT_EMAIL")
    password = os.environ.get("BLAULICHT_PASSWORD")
    if not email or not password:
        print("Set BLAULICHT_EMAIL and BLAULICHT_PASSWORD.")
        return 2
    ids = {int(a) for a in sys.argv[1:]}

    settings = get_settings()
    library = AssetLibrary.load(settings.vehicles_dir)
    # type_id -> classes that contain it
    classes_of: dict[int, list[str]] = {}
    for cls, tids in library.class_map.by_class.items():
        for t in tids:
            classes_of.setdefault(t, []).append(cls)

    async with GameSession(settings.base_url("de"), user_agent=settings.user_agent) as session:
        result = await session.login(email, password)
        print("login:", result.status.value, result.detail)
        if not result.ok:
            return 1
        client = GameClient(session)
        vehicles = await client.fetch_vehicles()
        by_id = {v.id: v for v in vehicles}
        for vid in ids:
            v = by_id.get(vid)
            print(f"\n===== vehicle {vid} =====")
            if v is None:
                print("  not found in fleet")
                continue
            tid = v.type_id
            vt = library.types.get(tid)
            print(f"  caption   : {v.caption!r}")
            print(f"  type_id   : {tid}")
            print(f"  fms       : {v.fms}  (available={v.is_available})")
            print(f"  crew(base): {vt.personnel if vt else '?'}")
            print(f"  in classes: {classes_of.get(tid, []) or '(NONE — not mapped to any class!)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
