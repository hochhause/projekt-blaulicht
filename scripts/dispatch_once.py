"""Controlled single-cycle live dispatch — the first real send, with rails.

Runs ONE planning cycle and, only with --send, actually dispatches — capped to
a small number of missions (default 1) so the first live send is tiny and
observable. Without --send it's a dry run (prints the plan, sends nothing).

After sending, it re-reads the fleet and checks whether each dispatched
vehicle's FMS status changed (2 = at station -> 3 = en route), which confirms
the dispatch actually took effect rather than just returning HTTP 200.

    pip install -e .
    export BLAULICHT_EMAIL=...  BLAULICHT_PASSWORD=...
    python scripts/dispatch_once.py                 # dry run, 1 mission
    python scripts/dispatch_once.py --send           # really dispatch 1 mission
    python scripts/dispatch_once.py --send --max-missions 3 --radius 60

WARNING: --send performs real in-game actions on an account. Use the expendable
one. Automating the game violates its terms.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.domain import (  # noqa: E402
    AssetLibrary,
    DispatchPolicy,
    MissionCatalog,
    MissionRequirementResolver,
    plan_dispatches,
)
from app.game import GameClient, GameSession  # noqa: E402

# FMS statuses that mean "en route / working" — i.e. no longer idle at station.
_BUSY_FMS = {3, 4, 7, 8}


async def main() -> int:
    parser = argparse.ArgumentParser(description="Controlled single-cycle live dispatch.")
    parser.add_argument("--send", action="store_true", help="actually dispatch (else dry run)")
    parser.add_argument("--max-missions", type=int, default=1, help="cap missions this run")
    parser.add_argument("--radius", type=float, default=40.0, help="dispatch radius km")
    args = parser.parse_args()

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

        client = GameClient(session)
        missions = await client.fetch_missions()
        vehicles = await client.fetch_vehicles()
        buildings = await client.fetch_buildings()
        einsaetze = await client.fetch_einsaetze()

        library = AssetLibrary.load(settings.vehicles_dir)
        resolver = MissionRequirementResolver(MissionCatalog.from_payload(einsaetze), library)
        policy = DispatchPolicy(radius_km=args.radius, batch_size=args.max_missions)
        plan = plan_dispatches(missions, vehicles, buildings, resolver, policy)

        veh_by_id = {v.id: v for v in vehicles}
        print(f"\nmissions available: {len(missions)}  "
              f"vehicles idle: {sum(1 for v in vehicles if v.is_available)}")
        print(f"plan: {len(plan.planned)} mission(s), {plan.total_dispatches} vehicle(s)")
        for mp in plan.planned:
            names = ", ".join(veh_by_id[i].caption for i in mp.vehicle_ids if i in veh_by_id)
            flag = "" if mp.satisfied else f"  (SHORT {mp.shortfall})"
            print(f"  mission #{mp.mission_id} {mp.caption!r} <- {list(mp.vehicle_ids)} [{names}]{flag}")

        if not plan.planned:
            print("\nNothing to dispatch right now (no in-range missions).")
            return 0
        if not args.send:
            print("\nDRY RUN — pass --send to actually dispatch.")
            return 0

        # --- real send ----------------------------------------------------
        pre_fms = {v.id: v.fms for v in vehicles}
        print("\n--- SENDING ---")
        for mp in plan.planned:
            for vid in mp.vehicle_ids:
                ok = await client.dispatch(mp.mission_id, vid)
                print(f"  mission {mp.mission_id} <- vehicle {vid}: POST {'ok' if ok else 'FAILED'}")
                await asyncio.sleep(random.uniform(0.8, 1.6))  # human-ish gap

        # --- verify via FMS change ---------------------------------------
        print("\n--- VERIFY (re-reading fleet) ---")
        after = {v.id: v for v in await client.fetch_vehicles()}
        confirmed = 0
        for mp in plan.planned:
            for vid in mp.vehicle_ids:
                before = pre_fms.get(vid)
                now = after[vid].fms if vid in after else None
                moved = now in _BUSY_FMS or (before != now)
                if moved:
                    confirmed += 1
                print(f"  vehicle {vid}: fms {before} -> {now} "
                      f"{'(en route - confirmed)' if moved else '(UNCHANGED - check in-game)'}")
        print(f"\n{confirmed}/{plan.total_dispatches} vehicle(s) confirmed en route.")
        print("Open the mission in-game to double-check.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
