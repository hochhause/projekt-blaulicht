# Projekt Blaulicht — master TODO / build plan

Single reference for picking this up on another machine. Detail docs live in `docs/`.
Ordered by priority. Nothing here is built unless noted.

---

## P0 — blockers (things that are wrong or unverified and gate other work)

### 0.1 `share_mission` endpoint — FIXED (real route captured), confirm it shares
- Now `GET /missions/{id}/alliance?ifs=fi&ift=kt_al_ae&sd=a&sk=cr` (the real
  `mission_alliance_share_btn` href). Still worth a glance that a shared mission actually becomes
  alliance-visible on first live run. (dispatch_many/HTTP-200 success detection: DROPPED — not worth
  the extra requests, per decision.)

### 0.2 Bulk multi-vehicle POST not confirmed live  ← "the third one"
- `vehicle_ids[]` batch POST is the browser's own form but never verified on the account. Watch the
  first real multi-vehicle dispatch actually roll (not just log success).

### 0.3 DGL (FuStW-DGL) confirmation
- Likely fixed by the requirement-resolver rework (P1.1). Still run `probe_vehicle.py 81048893`
  (PowerShell `$env:...`) to confirm the vehicle is type 103 and mapped.

---

## P1 — requirement resolver rework (the big correctness win)
See `docs/lssm-requirement-mapping.md` + `docs/live-data-model.md`.

The live feed is the source of truth; our einsaetze-derivation guesses. Switch to reading it.

- [ ] Build a resolver from `data/reference/lssm-missing-requirements.json`
      (requirement CATEGORY text → type-id set, + `equipment`, + `conditionalVehicles`, + `staff`).
- [ ] Parse `missing_text` (JSON blob: `vehicles` + `personnel` + `other`) against it. This is the
      **authoritative remaining need** (category names like "Löschfahrzeug (LF)", "Polizeihubschrauber",
      compound "LF oder RW"). REPLACES the brittle regex engine (which mis-parsed counts + LF).
- [ ] En-route subtraction: `Noch benötigt = fehlend − Auf Anfahrt`. Our delta subtracts OUR
      en-route; for SHARED missions, other players' en-route is only on the DETAIL page — open it
      only for red / not-participating / has-missing_text missions (the "when to open" rule below).
- [ ] When `missing_text` is empty (untouched red missions), fall back to the einsaetze estimate.
- [ ] `equipment` (heavy_rescue/oil = GW/AB modules): only count a carrier if it has the module —
      needs `/api/equipments` or vehicle detail. Conservative fallback: assume ok, let a failed
      dispatch strike it.
- [ ] `conditionalVehicles` (allow_rw_instead_of_lf, …): off unless the game setting is detected.
- [ ] Fix / retire the old regex `RequirementEngine` (bugs: ignores quantity prefix "2 FuStW"→1;
      no LF captions) once missing_text-based resolution is primary.
- [ ] Handle the `other` group — CAPACITY requirements, not vehicle counts. missing_text carries
      e.g. `"other":"1 Feuerlöschpumpe (z. B. LF)"`; the mission page has progress bars
      (`mission_{water|foam|pump}_holder` with driving/missing/selected values) and the feed has
      `water_damage_pump_value` / `live_current_water_damage_pump_value` / `pumping_mission_value`.
      Satisfied by vehicles that supply the capacity (pumps/water tankers; an LF often counts).
      LSSM reads the bars; we currently ignore it. Needs vehicle capacity data (`.mscv additional.base`)
      + bar/field reading. **If the exact mechanic is unclear, ask the user.**

### When-to-open-detail rule (from live verification, `docs/live-data-model.md`)
- `icon` suffix = status: `_rot` (missing) / `_gelb` (on the way) / `_gruen` (done, leaves feed).
- Skip yellow (things en route, being handled).
- Open a mission's detail only when red **and** (has `missing_text` OR `participating` false) — that
  means someone's on scene but more is needed, or another player/timed mission is involved.

---

## P2 — modes & UI cleanup
See `docs/MODES-TODO.md` (full inventory + compatibility matrix).

- [ ] Resolve conflicts, esp. **patient + parasite** (parasite throws a non-medical spare at a
      patient-only mission). Decide: parasite skips patient-governed missions, or block in UI.
- [ ] Confirm parasite + top-up don't double-send (delta should prevent it).
- [ ] Document top-up ⊂ partial + begun-only.
- [ ] UI regroup: **Scope & filters | one Primary-mode selector (normal/top-up/patient) | Add-ons
      (parasite, hilferuf, partial) | Transport**. Inline warnings + auto-disable incompatible combos.
      transport-only visually disables the dispatch sections. Mirror on the group page.

---

## P3 — group mode follow-ups
- [ ] **Share-before-send**: share all members' own missions FIRST, then **RE-FETCH** missions, then
      plan/dispatch — so everything's visible in the same cycle (user's fix; supersedes the
      visibility-ramp). Keep the visibility guard as a safety net.
- [ ] Combined group stats on the dashboard while running (credits/dispatched).
- [ ] Decide: per-member modes inside a group vs the single group-wide policy (current = group-wide).

---

## P4 — smaller features / ideas not yet explored
- [ ] Timed missions: already dispatched like normal (same feed, carry `tv`). Optional **expiry-aware
      priority** (sort by remaining time before the timer runs out) — confirm the timer field first.
- [ ] Surface live `icon` status on the Mission model + dashboard.
- [ ] `additive_overlays` (mtid variants "6/a"): variants ADD requirements to the base — CONFIRMED.
      The live `missing_text` already reflects the full base+overlay requirement, so the
      missing_text-based resolver (P1) handles overlays automatically. Only the catalogue FALLBACK
      path (base mtid only, when missing_text is empty) under-derives overlay missions — minor.
- [x] `participating` field — RESOLVED: it's `True` when THIS account has vehicles on the mission,
      absent/None otherwise. So `missing_text` present + not participating = another player is on it
      (join opportunity); the key isn't even in the JSON when we're not involved.
- Estate / building automation — REMOVED (user has their own script; stubs deleted:
  crew/estate/layout/reconcile/training + executor + writes).
- [ ] Prisoner transport: handled via the transport phase (gefangener hrefs) — sanity-check it works
      the same as patient transport.
- [ ] FMS-5 immediacy: DECIDED to keep as-is (poll each cycle). Event-driven would need the game's
      client-side `radioMessage` JS (browser only) — not worth a headless browser.

---

## Reference docs
- `docs/live-data-model.md` — verified live mission data (icon/missing_text/participating/patients).
- `docs/lssm-requirement-mapping.md` — LSSM's requirement→type-id model + our plan.
- `docs/MODES-TODO.md` — modes inventory + compatibility matrix + UI proposal.
- `data/reference/lssm-missing-requirements.json` — the requirement→type-id mapping (from LSSM).
- `scripts/probe_*.py`, `scripts/inspect_live_playwright.py` — read-only live inspection tools.
