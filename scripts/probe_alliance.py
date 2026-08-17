"""Discovery probe — find the alliance-missions feed, and audit einsaetze ids.

Read-only. Logs in, then:
  1. audits /einsaetze.json id distribution (why 3691 entries -> ~1008 types);
  2. tries candidate alliance/shared-mission endpoints and reports which respond;
  3. dumps /api/allianceinfo structure.

Usage:
    export BLAULICHT_EMAIL=...  BLAULICHT_PASSWORD=...
    python scripts/probe_alliance.py
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

# Candidate endpoints for the alliance / shared missions feed.
CANDIDATES = [
    "/api/missions",
    "/map/missions_json",
    "/map/alliance_missions_json",
    "/api/alliance_missions",
    "/alliance_missions",
    "/api/allianceinfo",
]


def _shape(payload: object) -> str:
    if isinstance(payload, list):
        first = payload[0] if payload else None
        keys = sorted(first.keys()) if isinstance(first, dict) else "—"
        return f"LIST len={len(payload)} elem_keys={keys}"
    if isinstance(payload, dict):
        parts = []
        for k, v in payload.items():
            if isinstance(v, list):
                fk = sorted(v[0].keys()) if v and isinstance(v[0], dict) else "—"
                parts.append(f"{k}=LIST(len={len(v)},elem_keys={fk})")
            else:
                parts.append(f"{k}={type(v).__name__}")
        return "OBJECT { " + ", ".join(parts[:15]) + " }"
    return f"{type(payload).__name__}: {repr(payload)[:120]}"


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

        # 1. einsaetze id audit
        print("\n===== einsaetze id audit =====")
        try:
            data = await session.get_json("/einsaetze.json")
            entries = data if isinstance(data, list) else []
            total = len(entries)
            int_ids, bad_ids = [], []
            for e in entries:
                raw = e.get("id")
                try:
                    int_ids.append(int(raw))
                except (TypeError, ValueError):
                    bad_ids.append(raw)
            dupes = [i for i, c in Counter(int_ids).items() if c > 1]
            print(f"entries={total}  int-parseable={len(int_ids)}  "
                  f"unique-int-ids={len(set(int_ids))}  non-int-ids={len(bad_ids)}  "
                  f"duplicate-int-ids={len(dupes)}")
            if bad_ids:
                print("  sample non-int ids:", bad_ids[:8])
            if dupes:
                print("  sample duplicate ids:", dupes[:8])
        except Exception as exc:  # noqa: BLE001
            print("  ERROR:", exc)

        # 2. alliance-mission endpoint discovery
        print("\n===== alliance mission endpoint discovery =====")
        for path in CANDIDATES:
            try:
                payload = await session.get_json(path)
                print(f"[200] {path}\n      {_shape(payload)}")
            except Exception as exc:  # noqa: BLE001
                print(f"[err] {path}  -> {type(exc).__name__}: {str(exc)[:90]}")

        # 3. allianceinfo detail
        print("\n===== allianceinfo (full keys) =====")
        try:
            info = await session.get_json("/api/allianceinfo")
            if isinstance(info, dict):
                print("keys:", list(info.keys()))
                print("sample:", json.dumps(info, default=str)[:700])
        except Exception as exc:  # noqa: BLE001
            print("  ERROR:", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
