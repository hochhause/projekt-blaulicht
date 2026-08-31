# Projekt Blaulicht — master TODO / build plan

Single reference for picking this up on another machine. Detail docs live in `docs/`.
Ordered by priority. Nothing here is built unless noted.

---

## P0 — blockers (things that are wrong or unverified and gate other work)

### 0.1 `share_mission` endpoint — CONFIRMED working live. ✅
- `GET /missions/{id}/alliance?ifs=fi&ift=kt_al_ae&sd=a&sk=cr`. Live-confirmed 2026-08-31 (throwaway
  account HyperCoderio, real alliance "Leitstelle Sommerblüte"): shared mission #4387674698 — before
  `is_alliance=False, alliance_shared_at=None`; after `is_alliance=True, alliance_shared_at=<ts>`.
  (dispatch_many/HTTP-200 success detection: DROPPED — not worth the extra requests, per decision.)

### 0.2 Bulk multi-vehicle POST — CONFIRMED working live. ✅

### 0.3 DGL (FuStW-DGL) confirmation — CONFIRMED. ✅
- Live-confirmed 2026-08-31: vehicle 81048893 on the throwaway account is captioned
  `"FuStW (DGL) #I - Polizei 01"`, `vehicle_type=103` — matches our local mapping
  (`police_service_group_leader` class, `data/de/vehicles/103.mscv`) exactly.

---

## P1 — requirement resolver rework (the big correctness win)
See `docs/lssm-requirement-mapping.md` + `docs/live-data-model.md`.

The live feed is the source of truth; our einsaetze-derivation guesses. Switch to reading it.

- [x] Build a resolver from `data/reference/lssm-missing-requirements.json`
      (requirement CATEGORY text → type-id set, + `equipment`, + `conditionalVehicles`, + `staff`).
      `app/domain/missingtext.py` (`MissingTextResolver`).
- [x] Parse `missing_text` (JSON blob: `vehicles` + `personnel` + `other`) against it. This is the
      **authoritative remaining need** (category names like "Löschfahrzeug (LF)", "Polizeihubschrauber",
      compound "LF oder RW"). REPLACES the brittle regex engine (which mis-parsed counts + LF).
      `vehicles`+`personnel` are resolved to vehicle demand in `MissionRequirementResolver.resolve_mission`;
      `other` is parsed (`MissingTextGroups.other`) but not yet acted on — see the "other" item below.
- [ ] En-route subtraction: `Noch benötigt = fehlend − Auf Anfahrt`. Our delta subtracts OUR
      en-route (unchanged, already done); for SHARED missions, other players' en-route is only on the
      DETAIL page — open it only for red / not-participating / has-missing_text missions (the
      "when to open" rule below). BLOCKED — see WORKFLOW.md §2 (needs a live capture of the detail page).
- [x] When `missing_text` is empty (untouched red missions), fall back to the einsaetze estimate.
- [x] `equipment` (heavy_rescue/oil = GW/AB modules): only count a carrier if it has the module —
      needs `/api/equipments` or vehicle detail. Conservative fallback: assume ok, let a failed
      dispatch strike it. (No per-vehicle equipment state tracked — every carrier counts, per fallback.)
- [x] `conditionalVehicles` (allow_rw_instead_of_lf, …): off unless the game setting is detected.
      (Never added — `MissingTextResolver` only reads each entry's `vehicles` list, ignoring
      `conditionalVehicles` entirely.)
- [x] Fix / retire the old regex `RequirementEngine` (bugs: ignores quantity prefix "2 FuStW"→1;
      no LF captions) once missing_text-based resolution is primary. Retired to a tertiary,
      last-resort fallback in `app/domain/dispatch.py::_derive_requirements` (only fires when the
      resolver returns nothing at all); the class itself is untouched/still tested.
- [ ] Handle the `other` group — CAPACITY requirements, not vehicle counts. BLOCKED — see
      WORKFLOW.md §2 (fill mechanic + tanker/foam capacity data unconfirmed). Live check 2026-08-31
      found `water_damage_pump_value` is 0 on every real fire mission and only nonzero (40000) on a
      water-damage/flooding mission ("Parkdeck voll Wasser gelaufen") — see docs/live-data-model.md's
      "Live check" note. Re-scope signal: this field may be Schmutzwasserpumpe-specific, not the
      general Feuerlöschpumpe need this item originally assumed. missing_text carries
      e.g. `"other":"1 Feuerlöschpumpe (z. B. LF)"`; the mission page has progress bars
      (`mission_{water|foam|pump}_holder` with driving/missing/selected values) and the feed has
      `water_damage_pump_value` / `live_current_water_damage_pump_value` / `pumping_mission_value`.
      Satisfied by vehicles that supply the capacity (pumps/water tankers; an LF often counts).
      LSSM reads the bars; we currently ignore it.
      DATA: water + pump capacity per type IS in the assets — extracted to
      `data/reference/vehicle-water-pump.json` (22 firetruck types, e.g. LF water 2000/pump 2000).
      GAPS: (a) dedicated water tankers (GTLF type 11 etc.) have NO capacity in the salvaged .mscv —
      need another source (LSSM vehicles API / game) for those; (b) foam ("löschmittel") appears in
      14 .mscv files but not as a flat numeric — needs deeper parsing. Mechanic (how much fills the
      bar / when satisfied) still to confirm.

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
      BLOCKED on Sasha — see WORKFLOW.md §2.
- [x] Confirm parasite + top-up don't double-send (delta should prevent it). CONFIRMED structurally
      (parasite is claimed in the priority pass before the main loop's top-up branch ever runs) — see
      `docs/MODES-TODO.md` §6 and `tests/test_dispatch.py::test_parasite_and_topup_scopes_dont_double_dispatch`.
- [x] Document top-up ⊂ partial + begun-only. See `docs/MODES-TODO.md` §6 (2026-08-31 entry) —
      no code change needed, kept both settings (they answer different questions).
- [ ] UI regroup: **Scope & filters | one Primary-mode selector (normal/top-up/patient) | Add-ons
      (parasite, hilferuf, partial) | Transport**. Inline warnings + auto-disable incompatible combos.
      transport-only visually disables the dispatch sections. Mirror on the group page.
      BLOCKED on the patient+parasite decision above — see WORKFLOW.md §2 (can't build
      "auto-disable incompatible combos" without knowing which combos are incompatible).

---

## P3 — group mode follow-ups
- [x] **Share-before-send**: share all members' own missions FIRST, then **RE-FETCH** missions, then
      plan/dispatch — so everything's visible in the same cycle (user's fix; supersedes the
      visibility-ramp). Keep the visibility guard as a safety net.
      `app/core/group_coordinator.py::run_cycle` — share pass now runs before the mission fetch used
      for planning; `visible_to` (built from the post-share re-fetch) is the safety net for the game
      not having propagated a share instantly. Tested with fakes in `tests/test_group_coordinator.py`
      (both the same-cycle-join case and the propagation-lag safety-net case).
- [x] Combined group stats on the dashboard while running (credits/dispatched). `_combined_totals`
      (`app/web/app.py`) sums the same per-account `stat` rows group dispatches already write (no new
      persistence), shown in the group-running banner on the dashboard.
- [ ] Decide: per-member modes inside a group vs the single group-wide policy (current = group-wide).
      BLOCKED on Sasha — see WORKFLOW.md §2 (P3.3).

---

## P4 — smaller features / ideas not yet explored
- [ ] Timed missions: already dispatched like normal (same feed, carry `tv`). Optional **expiry-aware
      priority** (sort by remaining time before the timer runs out) — confirm the timer field first.
      BLOCKED — see WORKFLOW.md §2 (no doc confirms `tv`'s semantics; no credentials to probe it).
- [x] Surface live `icon` status on the Mission model + dashboard. `Mission.icon` + `.status`
      property (`app/game/models.py`); `DispatchRunner.run_cycle` tallies a per-cycle snapshot
      (`CycleReport.mission_status_counts`); persisted via a new `mission_status` table
      (`app/store/db.py`, overwrite-per-cycle, not cumulative like `shortfall`); shown on the
      dashboard's Missions cell as "N red · M yellow · K green". Group mode not covered (separate
      from P3.2's combined group stats).
- [x] `additive_overlays` (mtid variants "6/a"): variants ADD requirements to the base — CONFIRMED.
      The live `missing_text` already reflects the full base+overlay requirement, so the
      missing_text-based resolver (P1) handles overlays automatically. Only the catalogue FALLBACK
      path (base mtid only, when missing_text is empty) under-derives overlay missions — minor.
- [x] `participating` field — RESOLVED: it's `True` when THIS account has vehicles on the mission,
      absent/None otherwise. So `missing_text` present + not participating = another player is on it
      (join opportunity); the key isn't even in the JSON when we're not involved.
- Estate / building automation — REMOVED (user has their own script; stubs deleted:
  crew/estate/layout/reconcile/training + executor + writes).
- [x] Prisoner transport: handled via the transport phase (gefangener hrefs) — sanity-check it works
      the same as patient transport. CONFIRMED by code reading + tests, no live account needed:
      `app/core/runner.py::_handle_transports` never branches on href content — it treats whatever
      `client.transport_targets()` returns identically regardless of patient/gefangener. The regex
      (`app/game/client.py::_APPROACH`) already parsed both symmetrically
      (`tests/test_client_paging.py`); added `tests/test_runner.py::test_transport_phase_sends_prisoner_fms5_vehicles_the_same_way`
      to close the loop at the runner layer too. Note: the more elaborate `app/domain/transport.py`
      (route_transport/TransportKind/specialty+tax routing) is built and tested but NOT wired into
      the live runner — `_handle_transports` uses the simpler "take the game's first offered target"
      approach instead. That's a pre-existing gap, not something this TODO item asked to fix.
- [x] FMS-5 immediacy: DECIDED to keep as-is (poll each cycle). Event-driven would need the game's
      client-side `radioMessage` JS (browser only) — not worth a headless browser.

---

## Reference docs
- `docs/live-data-model.md` — verified live mission data (icon/missing_text/participating/patients).
- `docs/lssm-requirement-mapping.md` — LSSM's requirement→type-id model + our plan.
- `docs/MODES-TODO.md` — modes inventory + compatibility matrix + UI proposal.
- `data/reference/lssm-missing-requirements.json` — the requirement→type-id mapping (from LSSM).
- `scripts/probe_*.py`, `scripts/inspect_live_playwright.py` — read-only live inspection tools.
