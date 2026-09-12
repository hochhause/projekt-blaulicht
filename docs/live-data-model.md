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

### Caption tags — `"[Event]"` (CONFIRMED by owner, 2026-09-12)
Cooperative event missions carry `"[Event]"` in the `caption` string — an INDEPENDENT tag, not tied
to `alliance_shared_at`: an event mission can be own (nobody's shared it) or alliance-shared,
depending on who bought/set it up. Alliance detection itself does NOT rely on the caption — that's
already the `alliance_shared_at` field below; the owner separately mentioned a `"[Verband]"` caption
marker on their alliance-shared missions, consistent with that field but not what the code keys off.
`Mission.is_event` matches the substring; `MissionSource.EVENT` is a separate, additive dispatch-
source tag alongside OWN/ALLIANCE (an event mission matches if EITHER its own/alliance tag or EVENT
is selected — see `MissionSource`'s docstring in `app/domain/dispatch.py`).

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
  Personal") — e.g. `"1 Einsatzleiter 2"`, `"8 GW-Wasserrettung"`. **`other`** — live-verified
  2026-08-31: also plain CATEGORY text (`"1 Feuerlöschpumpe (z. B. LF)"`, `"1 Schmutzwasserpumpe"`),
  resolved through the same LSSM category table as `vehicles` — see "Water / foam / pump" below for
  why this ISN'T a numeric-capacity problem after all.
- **A fourth shape exists**: `{"long_text": "..."}` — live-verified 2026-08-31 on UNTOUCHED
  (`vehicle_state=0`, still red) prisoner-related missions, e.g.
  `{"long_text":"Gefangene sollen abtransportiert werden."}`. Per Sasha: this is just the police
  equivalent of FMS-5 (an already-on-scene unit is ready to transport a prisoner away) — a status
  ping, NOT a vehicle-need reveal. None of `vehicles`/`personnel`/`other` are present in this shape.
  `missingtext.parse_missing_text` treats it as **unrevealed** (requires at least one of those three
  keys to be present, even if empty) so the catalogue estimate still applies for the mission's base
  vehicle requirement.

### `patients_prisoners`
`{"patients":[{"missing_text":"Wir benötigen: NEF", ...}]}` — the **NEF is revealed per patient**
once the patient is on scene. Count patients needing a NEF for the current NEF demand.

### `additive_overlays` — variants add to the base
A mission TYPE has a base (e.g. mtid 503) plus variants (`503/a`, `503/ab`) that **add** extra
requirements. A live mission's `additive_overlays: "ac"` means it = base + overlay a + overlay c.
Our catalogue only stores the base mtid, so catalogue-only derivation under-counts overlay missions
— but the live `missing_text` already reflects the FULL (base + overlay) requirement, so the
`missing_text`-based resolver handles overlays automatically. Only the catalogue fallback misses them.

### Timed missions — `tv`, `sw`, `sw_start_in`, `sw_start_date`, `date_end`
Live-verified 2026-08-31, confirmed by Sasha. A timed mission mimics a **scheduled event** (a parade,
a planned protest) the dispatcher would know about and staff for in advance: **by the time the timer
runs out, all requested vehicles must already be on scene.**

- `tv` is a nonzero marker that a timing mechanic applies to this mission — NOT itself a countdown or
  remaining-time value. Two real timed missions both read `tv: 60` despite having completely
  different countdown targets (see below); treat it as a boolean-ish flag, not a duration.
- **Two different countdown shapes observed, both confirmed against the mission page's own
  `CountdownTimer(...)` JS call (same millisecond timestamp as the feed field):**
  - `sw: true` + `sw_start_in` (seconds) + `sw_start_date` (ms epoch) — the mission page's own DOM
    element is literally named `countdown_start_in<id>`. Per Sasha's clarification, despite the name
    this is the "must be fully staffed by" **deadline** for a scheduled event, not a "starts later"
    delay — read `sw_start_in` as "seconds remaining to get everyone there."
  - `date_end` (epoch seconds, no `sw` flag) — a plain `CountdownTimer` targeting `date_end`; a
    different mission (observed as a non-`sw` "Bus in Wasser" instance) used this shape instead.
    Whether this is the SAME "must be staffed by" deadline semantic or a different one (e.g. an
    actual expiry/failure) is not yet confirmed — only the `sw`-flagged shape has been confirmed by
    Sasha's dispatcher-domain explanation above.
- **`missing_text` is revealed from creation for timed missions** — even with `vehicle_state=0` and
  no participation, confirmed live (a fresh `sw` mission already carried
  `{"vehicles":"4 MEK-Fahrzeuge",...}`). This differs from normal missions, where `missing_text` is
  only revealed once ≥1 unit is on scene. Good news: `MissionRequirementResolver.resolve_mission`
  already handles this correctly by construction — it never gates on `vehicle_state`, only on
  whether `missing_text` itself parses as revealed (see `tests/test_resolver.py::
  test_resolve_mission_timed_mission_reveals_missing_text_before_participation`).
- **Decision (Sasha, 2026-08-31): no expiry-aware priority sorting.** Treat timed missions
  functionally identical to normal ones — dispatch as soon as they're seen, same as any other
  mission. Waiting until close to the deadline is explicitly NOT wanted. This resolves TODO.md P4's
  "optional expiry-aware priority" item as **decided against** — current behavior (no special-casing
  in `plan_dispatches`) is already correct, no code change needed.

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
(an LF/GW-L2/SW satisfying a category), not this capacity field.

**Resolved (2026-08-31), confirmed by Sasha:** the practical dispatch need here is just the category
TEXT that shows up in `missing_text.other` (`"Feuerlöschpumpe (z. B. LF)"`, `"Schmutzwasserpumpe"`),
which our `MissingTextResolver` already resolves correctly — "Feuerlöschpumpe" maps to almost the
entire Löschfahrzeug set (any LF/HLF already satisfies it: "you might as well link it to any
Löschfahrzeug... a dedicated SwPu is too rare to expend when an LF/HLF also works"), while
"Schmutzwasserpumpe" maps to ONLY the two dedicated SW types (the genuinely rare, big/specialized
flooding case). **No capacity math is needed for dispatch purposes** — `resolve_mission` now resolves
`other` the same way as `vehicles`/`personnel`. The numeric `water_damage_pump_value` /
`live_current_*` / `pumping_mission_value` fields appear to be a separate, dispatch-irrelevant signal
(probably feeding the mission-detail page's progress bars) — their exact satisfy-threshold mechanic
remains unconfirmed, but nothing currently needs it. The tanker/foam capacity data gaps above are
likewise no longer blocking (the category-text path doesn't need per-vehicle liter/foam numbers).

---

## Bottom line for the rework
- Read `missing_text` as the source of truth when present; catalogue as fallback when absent.
- Subtract en-route: our delta always; the detail's `Auf Anfahrt` for shared missions (gated by the
  when-to-open rule).
- Use the LSSM category→type-id table (with equipment/conditionals) to resolve captions.
- `other` (water/foam/pump) is a separate capacity dimension using the per-vehicle water/pump data.
- Overlays and NEF-per-patient come for free from `missing_text` / `patients_prisoners`.
