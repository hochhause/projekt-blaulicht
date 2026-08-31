# Live mission data model — verified against the running game

Verified via `scripts/inspect_live_playwright.py` (headed login, read-only) across two runs on a
real alliance (149 then 156 open missions). This is the ground truth the dispatcher should read;
our `einsaetze.json` catalogue is only an estimate/fallback.

Related: `docs/lssm-requirement-mapping.md` (requirement→type-id table) and
`data/reference/lssm-missing-requirements.json` / `data/reference/vehicle-water-pump.json`.

---

## `/map/missions_json` — per-mission fields

Observed keys: `id, mtid, caption, latitude, longitude, address, icon, missing_text,
patients_prisoners, participating, patients_count, possible_patients_count, prisoners_count,
possible_prisoners_count, average_credits, created_at, alliance_id, alliance_shared_at, filter_id,
additive_overlays, vehicle_state, tv, sw, sw_start_in, ct, kt, pt, handoff, finish_url,
water_damage_pump_value, live_current_water_damage_pump_value, pumping_mission_value,
pumping_date_start, pumping_date_end, user_id`.

### Status — `icon`
`"<type>_rot" | "_gelb" | "_gruen"` = **red / yellow / green**:
- **red** = missing something / untouched, **yellow** = something on the way, **green** = fully
  crewed (runs to completion). Green missions stay in the feed only briefly, then drop out.
- Counts from one run: `rot 142, gelb 11, gruen 3`.

### `participating` — CONFIRMED
`True` when **this account currently has vehicles on the mission**; the key is **absent / null**
otherwise. It's dynamic — **retract all your vehicles and it flips back to false.** So:
- `missing_text` populated **without** `participating` ⇒ *another player* is on it (or a timed
  mission) → a join opportunity.
- red + no `missing_text` + not participating ⇒ fresh / untouched.

Cross-tab observed `(color, participating, has_missing_text) → count`:
`(gruen,T,F):3  (gelb,F,T):6  (gelb,T,T):5  (rot,F,F):77  (rot,F,T):39  (rot,T,T):26`.

### `missing_text` — the still-missing list (NOT en-route-adjusted)
- **Absent** when the mission is untouched; present as a **JSON blob** once ≥1 unit is on scene:
  `{"vehicles": "...", "personnel": "...", "other": "..."}`.
- It is **"Am Einsatzort fehlend"** — what's missing **at the scene**. It does **NOT** subtract
  vehicles already **on the way**. The true send list is
  **Noch benötigt = fehlend − Auf Anfahrt** (see "en route" below).
- Entries are requirement **CATEGORY names with counts**, not vehicle captions — real examples:
  `"4 Löschfahrzeuge (LF)"`, `"2 Rüstwagen oder HLF"`, `"1 ELW 1"`,
  `"3 Schlauchwagen (GW-L2 Wasser oder SW)"`, `"1 Polizeihubschrauber"`,
  `"3 Funkstreifenwagen oder Polizeimotorräder"`, `"1 Radlader (BRmG R)"`.
- **`vehicles`** — vehicle categories. **`personnel`** (JSON key `personnel`, HTML label "Fehlendes
  Personal") — e.g. `"1 Einsatzleiter 2"`, `"8 GW-Wasserrettung"`. **`other`** — CAPACITY, e.g.
  `"1 Feuerlöschpumpe (z. B. LF)"` (water/foam/pump, see below).

### `patients_prisoners`
`{"patients":[{"missing_text":"Wir benötigen: NEF", ...}]}` — the **NEF is revealed per patient**
once the patient is on scene. Count patients needing a NEF for the current NEF demand.

### `additive_overlays` — variants add to the base
A mission TYPE has a base (e.g. mtid 503) plus variants (`503/a`, `503/ab`) that **add** extra
requirements. A live mission's `additive_overlays: "ac"` means it = base + overlay a + overlay c.
Our catalogue only stores the base mtid, so catalogue-only derivation under-counts overlay missions
— but the live `missing_text` already reflects the FULL (base + overlay) requirement, so the
`missing_text`-based resolver handles overlays automatically. Only the catalogue fallback misses them.

---

## Opening a mission detail (`/missions/{id}`)

The requirement itself is already in `missing_text`, so we rarely need to open a mission. What the
detail ADDS is the itemised split, per requirement:
`Am Einsatzort fehlend | Auf Anfahrt (en route) | Noch benötigt | Ausgewählt`, **including other
players' vehicles**. So the only reason to open a shared mission is to get **Auf Anfahrt** (all
players' en-route) to compute the true **Noch benötigt**. Our own en-route we already know (delta).

**When to open (avoid opening every mission):** skip yellow; open only red missions that have
`missing_text` or `participating=false` (something's on scene, more is needed / another player or a
timed mission is involved).

---

## Requirement matching (how a category → vehicles)

`missing_text` categories map to vehicle type-ids via LSSM's `vehiclesByRequirement`
(`data/reference/lssm-missing-requirements.json`): 88 vehicle requirements + 4 staff categories.
- Multi-role is baked in (an HLF is in both `Löschfahrzeug (LF)` and `Rüstwagen oder HLF`).
- Compound requirements exist ("LF oder RW"), plus `equipment` (heavy_rescue/oil = GW/AB module
  needed to count) and `conditionalVehicles` (game settings like `allow_rw_instead_of_lf`).
- `staff`: Feuerwehrmann / Polizisten / THW-Einsatzkräfte / Rettungsdienstler → crew-providing types.
Full plan in `docs/lssm-requirement-mapping.md`.

---

## Water / foam / pump (`other` capacity requirements)

The `other` group is a **capacity** need (progress bars), not a vehicle count — e.g.
`"1 Feuerlöschpumpe (z. B. LF)"`. The mission page carries `mission_{water|foam|pump}_holder`
progress bars (driving/missing/selected) and the feed has `water_damage_pump_value` /
`live_current_water_damage_pump_value` / `pumping_mission_value`.

Per-vehicle capacity IS in our assets → extracted to `data/reference/vehicle-water-pump.json`:
**22 firetruck types carry `water` + `pump`** (e.g. LF `water 2000 / pump 2000`, HLF `1600 / 2000`,
type 25 `4000 / 4000`).

**Gaps:** (a) dedicated water tankers (GTLF, type 11 …) have **no** capacity in the salvaged `.mscv`
— need another source (LSSM vehicles API / game data); (b) **foam** (`löschmittel`, in 14 files) is
not a flat numeric and wasn't extracted. The fill mechanic (how much satisfies the bar) still to
confirm.

**Live check 2026-08-31 (throwaway account, real alliance, 26 own missions):** `water_damage_pump_value`
is **NOT** a general fire-mission field — every actual fire mission observed (Reihenhaus in Flammen,
Wohnhausbrand, Bürobrand, Brennende Telefonzelle, …) reads `0`. The **only** mission with a nonzero
value was **"Parkdeck voll Wasser gelaufen"** (a flooded-parking-deck / water-damage mission, not a
fire), at `40000`. This points to `water_damage_pump_value` being specific to water-damage/flooding
cleanup missions (Schmutzwasserpumpe territory — see the LSSM category of the same name in
`data/reference/lssm-missing-requirements.json`), not the general "Feuerlöschpumpe" fire-pump need the
original `other` write-up assumed. Fire-pump need is likely conveyed entirely through vehicle CLASS
(an LF/GW-L2/SW satisfying a category), not this capacity field. Still unconfirmed: the exact
satisfy-threshold mechanic for `water_damage_pump_value` itself (all observed missions were untouched,
so no before/after delta was visible), and whether `pumping_mission_value`/`live_current_value` (both
`100` on every untouched mission observed) decrease as pump vehicles work the mission. Needs a
water-damage mission actually being worked to confirm the mechanic — the "other" TODO item stays open.

---

## Bottom line for the rework
- Read `missing_text` as the source of truth when present; catalogue as fallback when absent.
- Subtract en-route: our delta always; the detail's `Auf Anfahrt` for shared missions (gated by the
  when-to-open rule).
- Use the LSSM category→type-id table (with equipment/conditionals) to resolve captions.
- `other` (water/foam/pump) is a separate capacity dimension using the per-vehicle water/pump data.
- Overlays and NEF-per-patient come for free from `missing_text` / `patients_prisoners`.
