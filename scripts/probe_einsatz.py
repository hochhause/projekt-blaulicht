"""Dump the FULL raw einsaetze.json entry for given mission-type ids.

Read-only. Use it to find where a personnel requirement ("3 Feuerwehrleute
mind. benötigt") lives in the catalogue — our parser currently only reads
`requirements` (vehicle classes) + `chances`, so a personnel/min-crew field
elsewhere is dropped and the mission looks like it has no requirements.

Usage (throwaway account):

    pip install -e .
    export BLAULICHT_EMAIL=...  BLAULICHT_PASSWORD=...
    python scripts/probe_einsatz.py 12 159 232      # any mtids; defaults below

Paste the output back so the personnel requirement can be wired correctly.
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

# mtids seen in the "unresolved" log (empty requirements) — good candidates.
DEFAULT_MTIDS = [12, 159]

# Keys worth calling out if present anywhere in the entry (personnel-ish).
_PERSONNEL_HINTS = (
    "personnel", "personal", "firefighter", "feuerwehr", "min_personnel",
    "crew", "wasserrettung", "staff",
)


def _hunt_personnel(entry: dict, path: str = "") -> list[str]:
    """Recursively surface any key/value that looks personnel-related."""
    hits: list[str] = []
    for k, v in entry.items():
        here = f"{path}.{k}" if path else k
        if any(h in k.lower() for h in _PERSONNEL_HINTS):
            hits.append(f"{here} = {json.dumps(v, ensure_ascii=False, default=str)[:200]}")
        if isinstance(v, dict):
            hits.extend(_hunt_personnel(v, here))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    hits.extend(_hunt_personnel(item, f"{here}[{i}]"))
    return hits


async def main() -> int:
    email = os.environ.get("BLAULICHT_EMAIL")
    password = os.environ.get("BLAULICHT_PASSWORD")
    if not email or not password:
        print("Set BLAULICHT_EMAIL and BLAULICHT_PASSWORD.")
        return 2

    mtids = [int(a) for a in sys.argv[1:]] or DEFAULT_MTIDS

    settings = get_settings()
    async with GameSession(settings.base_url("de"), user_agent=settings.user_agent) as session:
        result = await session.login(email, password)
        print("login:", result.status.value, result.detail)
        if not result.ok:
            return 1
        payload = await session.get_json("/einsaetze.json")
        entries = payload if isinstance(payload, list) else list(payload.values())

        # index by int id (ignoring overlay-variant string ids like "12/a")
        by_id: dict[int, dict] = {}
        for e in entries:
            try:
                by_id[int(e["id"])] = e
            except (KeyError, TypeError, ValueError):
                continue

        # also collect overlay variants per base id, to see if the requirement
        # is carried on a variant ("12/a") rather than the base entry.
        variants: dict[int, list[dict]] = {}
        for e in entries:
            rawid = str(e.get("id", ""))
            if "/" in rawid:
                base = rawid.split("/", 1)[0]
                if base.isdigit():
                    variants.setdefault(int(base), []).append(e)

        for mtid in mtids:
            print(f"\n===== mtid {mtid} =====")
            entry = by_id.get(mtid)
            if entry is None:
                print("  (not found as an int-id base entry)")
            else:
                print("  keys:", sorted(entry.keys()))
                print("  FULL:", json.dumps(entry, ensure_ascii=False, default=str))
                hits = _hunt_personnel(entry)
                print("  personnel-ish:", hits or "(none found in base entry)")
            vs = variants.get(mtid, [])
            if vs:
                print(f"  {len(vs)} overlay variant(s); first:",
                      json.dumps(vs[0], ensure_ascii=False, default=str)[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
