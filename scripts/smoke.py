"""Phase 1 smoke test — log in once and read the world.

Exercises the game client end-to-end against the live German server:
logs in, reads the four GET endpoints, prints counts and a sample, and loads
the requirement engine. It changes nothing (no dispatch).

Usage::

    pip install -e .            # installs httpx
    # put credentials in the environment (see env.example):
    export BLAULICHT_EMAIL=...  BLAULICHT_PASSWORD=...
    python scripts/smoke.py

Run it from the project root. Requires a real account.

WARNING: automating the game violates its terms of service; even this
read-only login originates from automation. Use a throwaway account you are
willing to lose. See the safety section of the plan.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Allow running as `python scripts/smoke.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.domain import AssetLibrary, MissionCatalog, MissionRequirementResolver  # noqa: E402
from app.game import GameClient, GameSession  # noqa: E402


async def main() -> int:
    email = os.environ.get("BLAULICHT_EMAIL")
    password = os.environ.get("BLAULICHT_PASSWORD")
    if not email or not password:
        print("Set BLAULICHT_EMAIL and BLAULICHT_PASSWORD in the environment.")
        return 2

    settings = get_settings()
    base = settings.base_url("de")
    print(f"→ server: {base}")

    async with GameSession(base, user_agent=settings.user_agent) as session:
        result = await session.login(email, password)
        print(f"→ login: {result.status.value} {result.detail}".rstrip())
        if not result.ok:
            return 1

        client = GameClient(session)
        missions = await client.fetch_missions()
        vehicles = await client.fetch_vehicles()
        buildings = await client.fetch_buildings()
        einsaetze = await client.fetch_einsaetze()

        available = [v for v in vehicles if v.is_available]
        print(f"→ missions:  {len(missions)}")
        print(f"→ vehicles:  {len(vehicles)}  ({len(available)} available)")
        print(f"→ buildings: {len(buildings)}")
        print(f"→ mission types (einsaetze): {len(einsaetze)}")

        # End-to-end requirement resolution via the PRIMARY structured path:
        # einsaetze.json (class -> count) joined with Vehicle.mscv (class -> IDs).
        library = AssetLibrary.load(settings.vehicles_dir)
        catalog = MissionCatalog.from_payload(einsaetze)
        resolver = MissionRequirementResolver(catalog, library)
        print(f"→ catalogue: {len(catalog)} types; assets: {len(library.types)} vehicle types")

        for m in missions[:3]:
            res = resolver.resolve(m.mission_type_id) if m.mission_type_id is not None else None
            if res and res.requirements:
                need = ", ".join(f"{r.source}×{r.count}" for r in res.requirements)
                print(f"→ mission #{m.id} {m.caption!r} (type {m.mission_type_id}) needs: {need}")
            else:
                print(f"→ mission #{m.id} {m.caption!r} (type {m.mission_type_id}) — no base requirements resolved")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
