"""Find WHERE a running mission exposes its currently-missing vehicles.

The true requirement (e.g. a NEF) is only revealed once units are on scene. This
dumps, for one or more running mission ids:

  * its full entry from /map/missions_json (every key/value), and
  * the /missions/{id} page HTML around requirement-ish keywords, and
  * /missions/{id}.json if that exists,

so we can see which field / markup carries the revealed "still needed" list and
teach the planner to read it (instead of guessing from einsaetze `chances`).

Read-only. PowerShell (throwaway account):

    $env:BLAULICHT_EMAIL="..."; $env:BLAULICHT_PASSWORD="..."
    python scripts/probe_mission_detail.py 4377701869 [more ids...]

Pick ids of missions that currently show a missing NEF / DGL. Paste the output.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.game.session import GameSession  # noqa: E402

_KEYWORDS = ("benötigt", "benoetigt", "fehlt", "fehlend", "missing", "Notarzt",
             "NEF", "NAW", "RTW", "FuStW", "DGL", "erforderlich", "requirement")
_ROW = re.compile(r".{0,80}(?:%s).{0,80}" % "|".join(_KEYWORDS), re.IGNORECASE)


async def main() -> int:
    email = os.environ.get("BLAULICHT_EMAIL")
    password = os.environ.get("BLAULICHT_PASSWORD")
    if not email or not password:
        print("Set BLAULICHT_EMAIL and BLAULICHT_PASSWORD.")
        return 2
    ids = [int(a) for a in sys.argv[1:]]

    settings = get_settings()
    async with GameSession(settings.base_url("de"), user_agent=settings.user_agent) as session:
        result = await session.login(email, password)
        print("login:", result.status.value, result.detail)
        if not result.ok:
            return 1

        # locate each id in the missions feed
        feed = await session.get_json("/map/missions_json")
        entries = feed.get("missions", feed) if isinstance(feed, dict) else feed
        by_id = {}
        for e in (entries or []):
            try:
                by_id[int(e["id"])] = e
            except (KeyError, TypeError, ValueError):
                pass

        for mid in ids:
            print(f"\n===== mission {mid} =====")
            entry = by_id.get(mid)
            if entry is not None:
                print("  missions_json keys:", sorted(entry.keys()))
                print("  missions_json FULL:", json.dumps(entry, ensure_ascii=False, default=str))
            else:
                print("  (not in /map/missions_json right now)")

            # the mission page HTML (what the bot already loads for the token)
            try:
                page = await session.get_text(f"/missions/{mid}")
                print(f"  page length: {len(page)}")
                hits = _ROW.findall(page)
                print(f"  requirement-ish snippets ({len(hits)}):")
                for h in hits[:25]:
                    print("    …", " ".join(h.split()))
            except Exception as exc:  # noqa: BLE001
                print("  page ERROR:", exc)

            # some entities have a sibling .json
            try:
                dj = await session.get_json(f"/missions/{mid}.json")
                print("  /missions/{id}.json keys:", sorted(dj.keys()) if isinstance(dj, dict) else type(dj).__name__)
            except Exception as exc:  # noqa: BLE001
                print("  /missions/{id}.json: none (", exc, ")")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
