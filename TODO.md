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
      All three groups (`vehicles`+`personnel`+`other`) resolve to vehicle demand in
      `MissionRequirementResolver.resolve_mission` (`other` added 2026-08-31 — see the "other" item).
      Also live-verified 2026-08-31: a FOURTH `missing_text` shape exists,
      `{"long_text": "..."}` (a status ping, e.g. prisoner-ready-for-transport — not a vehicle-need
      reveal), correctly treated as unrevealed (catalogue fallback applies) — see docs/live-data-model.md.
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
- [x] Handle the `other` group — CAPACITY requirements, not vehicle counts. RESOLVED 2026-08-31,
      confirmed by Sasha: turns out `other`'s real entries ("Feuerlöschpumpe (z. B. LF)",
      "Schmutzwasserpumpe") are plain CATEGORY text — the exact same shape as `vehicles` — already
      correctly mapped in `data/reference/lssm-missing-requirements.json`. "Feuerlöschpumpe" maps to
      almost the whole Löschfahrzeug set (any LF/HLF satisfies it — a dedicated SW pump is too rare
      to route separately); "Schmutzwasserpumpe" maps to only the two dedicated SW types (the rare,
      genuinely specialized flooding case). No capacity math needed — `resolve_mission` now resolves
      `other` the same way as `vehicles`/`personnel` (`app/domain/resolver.py`). The numeric
      `water_damage_pump_value`/`live_current_*` fields appear to be a separate, dispatch-irrelevant
      signal (probably feeding the mission-detail progress bars) — see docs/live-data-model.md.
      The tanker/foam capacity data gaps no longer block dispatch (category-text path doesn't need
      per-vehicle liter/foam numbers).

### When-to-open-detail rule (from live verification, `docs/live-data-model.md`)
- `icon` suffix = status: `_rot` (missing) / `_gelb` (on the way) / `_gruen` (done, leaves feed).
- Skip yellow (things en route, being handled).
- Open a mission's detail only when red **and** (has `missing_text` OR `participating` false) — that
  means someone's on scene but more is needed, or another player/timed mission is involved.

---

## P2 — modes & UI cleanup
See `docs/MODES-TODO.md` (full inventory + compatibility matrix).

- [x] Resolve conflicts, esp. **patient + parasite** (parasite throws a non-medical spare at a
      patient-only mission). DECIDED 2026-08-31 (Sasha): "simple solution, if both are active,
      patients are removed AND one vehicle is sent to stay for claiming the cash" — parasite on a
      patient-governed mission we're not on never computes a "useful" patient-only pick, always just
      claims participation with a plain spare (`app/domain/dispatch.py::_priority_pass`,
      `skip_patient_contribution`). Top-up (missions we're actually assigned to) keeps its existing
      patient-aware behavior unchanged. See `tests/test_dispatch.py::test_parasite_on_patient_governed_mission_only_claims_participation`.
      Also added (Sasha, same message) a new **parasite aggressiveness** setting — active (default) /
      all / needed — see `docs/MODES-TODO.md` §1 for the new inventory entry.
      Also fixed a related real bug Sasha found: patient-only's recall (`_recall_cleared_patient_missions`
      in `app/core/runner.py`) previously recalled ANY vehicle on a cleared mission, not just the
      classes patient-only itself sends (RTW/NEF/rescue_helicopter/LNA/OrgL) — a manually-sent fire
      truck or police car on the same mission would get wrongly recalled. Now filtered by vehicle type.
- [x] Confirm parasite + top-up don't double-send (delta should prevent it). CONFIRMED structurally
      (parasite is claimed in the priority pass before the main loop's top-up branch ever runs) — see
      `docs/MODES-TODO.md` §6 and `tests/test_dispatch.py::test_parasite_and_topup_scopes_dont_double_dispatch`.
- [x] Document top-up ⊂ partial + begun-only. See `docs/MODES-TODO.md` §6 (2026-08-31 entry) —
      no code change needed, kept both settings (they answer different questions).
- [ ] UI regroup: **Scope & filters | one Primary-mode selector (normal/top-up/patient) | Add-ons
      (parasite, hilferuf, partial) | Transport**. Inline warnings + auto-disable incompatible combos.
      transport-only visually disables the dispatch sections. Mirror on the group page.
      UNBLOCKED 2026-08-31 (the patient+parasite decision above resolved what was blocking it — parasite
      no longer needs a UI-level block against patient_scope, it's handled functionally instead) but
      NOT YET BUILT — this full redesign is a separate, larger UI task not attempted this session
      (only the new parasite-aggressiveness dropdown was added to the existing account.html layout).

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
- [x] **Group-mode logic audit (Sasha, 2026-08-31).** Traced `app/domain/group.py` +
      `app/core/group_coordinator.py` end to end. CONFIRMED CORRECT: no over-send (global delta),
      visibility gating, participation guarantee (incl. per-account token cap), radius_basis any/all,
      begun-missions-first priority, partial-start ownership gating, and full reuse of the
      single-account requirement resolver (group mode automatically got every P1 fix this session —
      missing_text priority, the "other" group, the long_text bug — for free). ONE real gap found and
      fixed: group mode had NO strike-tracking or per-vehicle retry-on-failure (unlike solo dispatch) —
      a chronically-uncrewed vehicle in the combined pool would fail every cycle forever, sinking every
      good vehicle bundled with it in the same failed batch. Fixed: `AccountRunner.strikes` exposed
      publicly; `plan_group_dispatches` gained `strikes_by_account` (excludes a vehicle struck out on
      its OWNER's tracker from the combined pool); `GroupCoordinator`'s dispatch loop now retries a
      failed batch one vehicle at a time and strikes the one that actually won't go, mirroring
      `DispatchRunner.run_cycle` exactly. Code review caught and fixed a real bug in the fix itself: the
      retry loop was calling `on_stat` once per vehicle instead of once per mission-send, inflating the
      dashboard's "missions" stat on any multi-vehicle partial retry.

---

## P4 — smaller features / ideas not yet explored
- [x] Timed missions: already dispatched like normal (same feed, carry `tv`). DECIDED 2026-08-31
      (Sasha): no expiry-aware priority — treat identically to normal missions, dispatch as soon as
      seen (explicitly NOT wait-until-near-deadline). Current behavior (no special-casing in
      `plan_dispatches`) is already correct. Live-confirmed `tv`/`sw`/`sw_start_in` semantics and a
      real correctness point (missing_text reveals from creation for timed missions, already handled
      correctly by `resolve_mission`'s existing not-gated-on-vehicle_state design) — see
      docs/live-data-model.md's "Timed missions" section.
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
