# Live mission data model — verified against the running game (2026-08)

Verified via `scripts/inspect_live_playwright.py` (headed login, read-only) on a real
alliance with 149 open missions.

## What `/map/missions_json` gives us (per mission)

- **`icon`** — `"<type>_rot" | "_gelb" | "_gruen"` = **live status**: red (missing) / yellow
  (something on the way) / green (all there). Green missions leave the feed (completed), so
  in practice we see `rot` and `gelb`. This is our red/yellow/green with no extra request.
- **`missing_text`** — a JSON blob `{"vehicles": "...", "personnel": "...", "other": "..."}` =
  the **authoritative remaining requirement, net of ALL participants** (own + alliance + others).
  Matches the mission-page "Fehlende Fahrzeuge / Fehlendes Personal" exactly. Populated once ≥1
  unit is on scene/en route; empty (or absent) when the mission is untouched.
  - `vehicles`: e.g. `"1 Polizeihubschrauber"`, `"2 FuStW"`.
  - `personnel`: e.g. `"1 Einsatzleiter 2"` (missing personnel — command/firefighters).
- **`patients_prisoners.patients[].missing_text`** — per-patient needs, e.g. `"Wir benötigen: NEF"`
  (the NEF is revealed once the patient is on scene). Already used by the resolver.
- **`participating`** — bool per mission. Combined with the above (user's rule, matches the data):
  - `missing_text` populated **without** participating ⇒ someone else is on it (or it's a timed
    mission) — a candidate to join.
  - red + no missing_text + not participating ⇒ fresh/untouched.
- **`tv`**, `sw`, `sw_start_in` — timer / speech-request fields (timed missions carry `tv`).
- **`vehicle_state`** — 0 nobody / ≥1 someone responding.

Cross-tab observed (color, participating, has_missing_text) → count:
`(gelb,F,T):8  (gelb,T,T):7  (rot,F,F):70  (rot,F,T):37  (rot,T,F):1  (rot,T,T):26`.
Every yellow mission had `missing_text` populated.

## Opening a mission detail (`/missions/{id}`)

Adds the itemized **en route** and **at-scene** vehicle lists (incl. other players'), but the
**requirement itself is already in `missing_text`** — so we do NOT need to open missions to know
what's still needed. Opening is only worth it for the itemised breakdown, which we rarely need.

## BLOCKER: requirement-engine parsing gaps

`RequirementEngine.resolve([caption])` on the real captions:

| caption | result |
|---|---|
| Polizeihubschrauber, Einsatzleiter 2, HLF 20, DLK 23, RTW, FuStW (DGL), ELW 1, FuStW | ✅ correct type_ids |
| **LF 20 / LF 10** | ❌ `unmatched` (no firetruck match) |
| **"2 FuStW"** | ⚠️ count = **1** (the quantity prefix is ignored) |

So we cannot yet treat `missing_text` as the source of truth — it would under-send LF missions and
ignore multi-vehicle counts.

## Recommended path (TODO)

- [ ] Fix `RequirementEngine`: (a) parse the leading quantity `N` into `count`; (b) add the missing
      firetruck captions (LF 20/10/8, TLF, …) so LF resolves.
- [ ] Then: when `missing_text` is populated, use it as the **authoritative outstanding requirement**
      (REPLACE catalogue−delta, don't merge — the merge double-counts because the engine keys by
      caption, not class). Parse `vehicles` + `personnel`. Fall back to the catalogue estimate only
      when `missing_text` is empty (untouched missions). This fixes DGL, multi-count, missing
      personnel and all "revealed later" cases at once, with zero detail fetches.
- [ ] Surface `icon` status (red/yellow) on the model for prioritisation / UI, and wire the
      "only act on red/yellow-with-missing_text, skip fresh-red / green" selection.
- [ ] Timed missions: they're in the same feed (carry `tv`); optional expiry-aware ordering.
