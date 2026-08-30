"""Headed browser inspection: verify the mission status/participating/missing_text
logic against the LIVE game. YOU log in in the window; the script then reads
/map/missions_json + a few mission detail pages and writes a report.

Nothing is dispatched or changed — only reads.

    python scripts/inspect_live_playwright.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "https://www.leitstellenspiel.de"
REPORT = Path(tempfile.gettempdir()) / "lss_report.txt"


def _color(icon: str) -> str:
    icon = icon or ""
    return icon.rsplit("_", 1)[-1] if "_" in icon else icon


def _has_missing(m: dict) -> bool:
    mt = (m.get("missing_text") or "").strip()
    return bool(mt) and mt not in ("{}", '{"vehicles":"","personnel":"","other":""}')


def logged_in(ctx) -> bool:
    try:
        r = ctx.request.get(f"{BASE}/api/userinfo")
        if r.ok:
            d = r.json()
            return isinstance(d, dict) and ("credits_user_current" in d or "user_id" in d)
    except Exception:
        pass
    return False


def main() -> int:
    lines: list[str] = []

    def out(s: str = "") -> None:
        lines.append(s)
        print(s, flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(f"{BASE}/")
        print(">>> Log in in the browser window. Waiting up to 5 minutes...", flush=True)
        deadline = time.time() + 300
        while time.time() < deadline and not logged_in(ctx):
            time.sleep(3)
        if not logged_in(ctx):
            print("Not logged in within the timeout — re-run when ready.", flush=True)
            browser.close()
            return 1
        out(">>> Logged in. Reading live data (read-only)...")

        data = ctx.request.get(f"{BASE}/map/missions_json").json()
        missions = data.get("missions", data) if isinstance(data, dict) else data
        out(f"\nmissions in feed: {len(missions)}")

        colors = Counter(_color(m.get("icon", "")) for m in missions)
        out(f"icon-suffix (status?) counts: {dict(colors)}")

        # cross-tab: (color, participating, has_missing_text) -> count
        ct = Counter(
            (_color(m.get("icon", "")), bool(m.get("participating")), _has_missing(m))
            for m in missions
        )
        out("\ncross-tab (color, participating, has_missing_text): count")
        for k, v in sorted(ct.items(), key=lambda x: str(x[0])):
            out(f"  {k}: {v}")

        # one sample per color with the key fields
        samples: dict[str, dict] = {}
        for m in missions:
            c = _color(m.get("icon", ""))
            if c not in samples:
                samples[c] = m
        for c, m in samples.items():
            out(f"\n--- sample {c!r} mission {m.get('id')} {m.get('caption')!r} ---")
            out(f"  icon={m.get('icon')} participating={m.get('participating')} "
                f"vehicle_state={m.get('vehicle_state')} "
                f"patients={m.get('patients_count')}/{m.get('possible_patients_count')} "
                f"mtid={m.get('mtid')} filter_id={m.get('filter_id')} tv={m.get('tv')}")
            out(f"  missing_text={m.get('missing_text')!r}")
            pp = m.get("patients_prisoners")
            out(f"  patients_prisoners={json.dumps(pp, ensure_ascii=False)[:400] if pp else None}")
            out(f"  ALL KEYS: {sorted(m.keys())}")

        # detail pages for the samples — what does opening the mission add?
        for c, m in samples.items():
            mid = m.get("id")
            out(f"\n===== detail /missions/{mid}  (status {c}) =====")
            try:
                page.goto(f"{BASE}/missions/{mid}", wait_until="domcontentloaded")
                page.wait_for_timeout(1500)

                def rows(sel: str) -> int:
                    return len(page.query_selector_all(sel))

                def txt(sel: str):
                    el = page.query_selector(sel)
                    return " ".join(el.inner_text().split()) if el else None

                out(f"  #missing_text: {txt('#missing_text')!r}")
                # common LSS containers (best-effort; report whatever exists)
                for sel, label in [
                    ("#mission_vehicle_driving tr", "en route rows"),
                    ("#mission_vehicle_at_mission tr", "at-scene rows"),
                    ("#mission_vehicles_driving tr", "driving rows (alt)"),
                    ("#mission_vehicles_at_mission tr", "at-scene rows (alt)"),
                    (".alert-missing-vehicles", "missing-vehicles alert"),
                    ("#patient_missing_requirements", "patient missing div"),
                ]:
                    el = page.query_selector(sel.split(" ")[0]) if " " in sel else page.query_selector(sel)
                    if el is not None:
                        if " tr" in sel:
                            out(f"  {label}: {rows(sel)}")
                        else:
                            out(f"  {label}: {txt(sel)!r}")
            except Exception as exc:  # noqa: BLE001
                out(f"  detail ERROR: {exc}")

        browser.close()

    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n>>> report written to: {REPORT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
