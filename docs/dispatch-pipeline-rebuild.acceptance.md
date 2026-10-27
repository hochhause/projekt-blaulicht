# Dispatch pipeline rebuild — acceptance criteria

Proof-level override (recorded per the staged-implementation default): this is a backend decision
engine with no UI of its own to drive end-to-end. Highest available proof level is **functional**
— calling the real `plan_dispatches`/`resolver`/`select_coverage` pipeline against hand-built
fixture missions/vehicles/catalogue data shaped exactly like the real game feed (per
`docs/live-data-model.md`), asserting on the real `DispatchPlan`/`Resolution` output. Unit tests
support individual helpers (`_apply_delta`, `missing_text_ever_revealed`, the new recall core) but
never substitute for a functional case where one exists. No e2e/integration/UI layer applies.

Every criterion below becomes an `xfail`/skip stub (named with its id) before its milestone's
first implementation step starts, per the staged-implementation rule — the step is done when the
stub passes for real.

## Milestone 1 — the delta fix (D0, D5) + missing-text memory

- **AC-M1-01** — Plan ref D5. Given a mission whose `missing_text` reveals `{"vehicles": "1 ELW 1",
  ...}` (one ELW 1 still missing per the game's own S4-netted count), one of our own vehicles of
  type ELW 1 currently `fms=4` (on scene, `target_id` = this mission), and a second, unrelated
  vehicle of type ELW 1 currently `fms=3` (en route, `target_id` = this mission). When
  `plan_dispatches` runs. Then no additional ELW 1 is dispatched to this mission (the S3 vehicle
  alone accounts for the one still-missing unit; the S4 vehicle is not subtracted a second time)
  — mission appears in neither `plan.planned` nor needing an ELW 1 in `plan.skipped`'s shortfall.
- **AC-M1-02** — Plan ref D5, the original live bug this closes. Given the same revealed
  `missing_text` ("1 ELW 1" missing), one S4 ELW 1 genuinely on scene, and **no** S3 vehicle at all
  (the earlier "stale assigned" symptom — nothing really en route despite old bookkeeping). When
  `plan_dispatches` runs on a mission whose live `status` is still `"red"`. Then exactly one ELW 1
  is dispatched (Rule 1's existing red-bypass combined with the corrected delta — the fix removes
  the need for Rule 1 to mask this specific case, but Rule 1 still protects the genuinely-stale
  case; both paths must independently arrive at "send one").
- **AC-M1-03** — Plan ref D5, regression guard. Given a mission that has **never** revealed
  `missing_text` (catalogue fallback applies, e.g. mission type requiring 2 LF), one S3 LF and one
  S4 LF already assigned. When `plan_dispatches` runs. Then the outstanding requirement is reduced
  by both (2 − 1 S3 − 1 S4 = 0) — unchanged from today's behaviour; catalogue-path subtraction is
  NOT scoped to en-route-only.
- **AC-M1-04** — Plan ref "missing-text ever revealed, now gone" design. Given a mission whose
  `missing_text` showed real categories on a previous cycle (simulate via the resolver's
  in-process memory), and on the current cycle `missing_text` is absent/empty entirely (not merely
  revealed-with-empty-groups — the raw field itself is gone). When `vehicle_requirements` is
  called. Then it returns no outstanding vehicle-class requirement (treated as fully covered) —
  it does NOT fall back to re-deriving from the catalogue.
- **AC-M1-05** — Plan ref same, regression guard. Given a mission that has **never** revealed
  `missing_text` on any prior cycle, and currently shows nothing. When `vehicle_requirements` is
  called. Then it falls back to the catalogue estimate, as today (the two "nothing revealed right
  now" states are not conflated).
- **AC-M1-06** — Plan ref same. Given a mission that was revealed, then had nothing revealed for a
  cycle (AC-M1-04's state), then reveals `missing_text` again with a new category. When
  `vehicle_requirements` is called on that third cycle. Then it resolves the newly-revealed
  category as a fresh requirement (the "ever revealed" memory does not suppress a later, genuinely
  new need).
- **AC-M1-07** — Plan ref D5, casualty-layer non-regression. Given a mission with `patients_count=2`
  (revealed), one S3 ambulance and one S4 ambulance already assigned. When `plan_dispatches` runs.
  Then the ambulance requirement is reduced by both (2 − 1 − 1 = 0) — the casualty-count layer is
  untouched by the `en_route_only` scope change (explicit non-regression test, not just an
  omission).

## Milestone 2 — mode/rule changes (D1, D2, D7)

- **AC-M2-01** — Plan ref D1. Given a mission confidently alliance-owned, type id in
  `_VGE_MISSION_TYPE_IDS`, with revealed `missing_text` needing e.g. 2× ELW 1. When
  `plan_dispatches` runs with sufficient pool. Then both ELW 1s are dispatched (full normal
  treatment, no one-vehicle cap, no command-vehicle exclusion).
- **AC-M2-02** — Plan ref D1. Same mission type, `missing_text` NOT revealed, `parasite_scope`
  covering it OFF. When `plan_dispatches` runs. Then nothing is sent to this mission this cycle.
- **AC-M2-03** — Plan ref D1. Same as AC-M2-02 but `parasite_scope` covering it ON. Then exactly
  one spare vehicle is sent (unchanged from today's token-send behaviour), marked as a free/
  unsatisfied participation claim.
- **AC-M2-04** — Plan ref D2. Given a mission type NOT in the catalogue at all (simulating an
  unrecognized mission type), `missing_text` revealed with a real category, `vehicle_state` still
  `0`, `top_up_scope` covering it. When `plan_dispatches` runs with a matching vehicle on hand.
  Then it is dispatched via the top-up path (the live `missing_text` signal, not the catalogue or
  `vehicle_state`, gates eligibility).
- **AC-M2-05** — Plan ref D2, regression guard. Given a mission with `vehicle_state=1` (someone
  has been on scene) but `missing_text` currently not revealed, `top_up_scope` covering it. When
  `plan_dispatches` runs. Then it is NOT treated as top-up-eligible (old signal alone no longer
  qualifies).
- **AC-M2-06..09** — Plan ref D7, the four gaps already scoped in the dispatch-pipeline-map
  artifact, each as its own functional case against a mission reached via `_priority_pass`:
  (06) a red, parasite-covered mission with a stale/wrong `assigned` entry still gets the full
  requirement (Rule 1 now reaches this path); (07) an on-it, top-up-covered mission below its
  `min_fire_personnel` floor gets topped up with extra firetrucks; (08) an on-it, top-up-covered
  mass-casualty mission (`patient_load >= 20`) prefers a SEG-ELW over a plain ELW when both are
  available; (09) a parasite-sent MZB gets its GW-Taucher/GW-Wasserrettung escort when available.

## Milestone 3 — automatic recall (D4, D6)

- **AC-M3-01** — Plan ref D4. Given a mission transitioning to `status == "green"` this cycle for
  the first time (not in `fired_once`), with our own 2 S4 LFs on scene against a requirement of 1
  LF. When the reconciliation pass runs. Then exactly 1 of our LFs is recalled (the surplus), the
  other is kept, and the mission id is added to `fired_once`.
- **AC-M3-02** — Plan ref D4, cadence. Same mission, still green, checked again 5 minutes later
  (no new vehicles, nothing changed). When the reconciliation pass runs. Then no recall happens
  (within the 15-minute window since last check).
- **AC-M3-03** — Plan ref D4, cadence. Same mission, checked again at 16 minutes. When the
  reconciliation pass runs. Then the comparison is re-evaluated (recall if still surplus).
- **AC-M3-04** — Plan ref D4, the "stop S3 on green" rule. Given a green mission with one of our
  own LFs still `fms=3` (en route) toward it, already fully satisfied by S4 vehicles on scene.
  When the reconciliation pass runs. Then the en-route LF is recalled.
- **AC-M3-05** — Plan ref D4, THE highest-consequence case, run as an explicit mutation check per
  CLAUDE.md's money-path rule. Given a mission with our own 1 S4 LF (exactly matching its
  requirement of 1) plus another player's 2 S4 LFs also on scene (alliance-shared mission, over-
  satisfied by the combined total). When the reconciliation pass runs. Then our own LF is **not**
  recalled (we are not over our own share; other players' vehicles are counted toward the total
  but never trigger recalling OUR vehicle below what we'd otherwise keep). Mutation check: remove
  the "ours only" filter from the implementation and confirm this exact test fails.
- **AC-M3-06** — Plan ref D6. Given the same over-dispatch scenario as a prior `plan_cleanup`
  functional test (reused fixture). When the manual `cleanup()` action runs. Then it produces the
  same recall decision as the new reconciliation core (both paths converge on one implementation;
  `cleanup()` ignores the cadence and checks every mission unconditionally).

## Milestone 4 — SMART validation, cleanup, full regression

- **AC-M4-01** — Plan ref D3. Given a fixed pool and a set of candidate missions with varying
  credits and vehicle-class costs, constructed so that a naive credits-high ordering would leave
  more total credits un-closed this cycle than a better combination. When `plan_dispatches` runs
  with `order=SMART`. Then record which missions actually close (go fully satisfied) and their
  total credits; compare against the theoretical best combination for this fixture. If the gap is
  within an accepted margin (documented at implementation time — "good enough" per Sasha), the
  existing heuristic is confirmed unchanged; if a concrete, non-trivial gap is found, it is
  reported back to Sasha before any formula change is made (per the plan's non-goal).
- **AC-M4-02** — Plan ref D8. Given a mission skipped with reason `"already covered"` while its
  live `status` is `"red"` (constructed directly against `_mission_breakdown`, bypassing the now-
  structurally-impossible path through the real planner). When `_mission_breakdown` runs. Then the
  relabel to `"awaiting arrival"` no longer exists in the code (test removed, not just passing
  vacuously) — confirmed by the absence of the branch, not by a test that can never execute it.
- **AC-M4-03** — Full existing suite (494 tests as of this plan) plus every AC-M1 through AC-M3
  stub passes. `python -m pytest tests/ -x -q`, real output recorded, exit code 0.
- **AC-M4-04** — By-hand check (no UI, so this substitutes for a recorded walkthrough): run one
  full cycle against the real account in a read-only/dry-run capacity (or the account paused with
  a single manual cycle trigger, per the project's established "pause → verify → resume" pattern
  used earlier this session for Rule 1), compare the shortfall log and mission-breakdown panel
  before/after for at least one mission previously known to show the false "already covered"/
  "satisfied" symptom (missions 4447662938 / 4447716684 if still open, else the nearest live
  equivalent), and confirm it now reports correctly.

## Milestone evidence

Filled in during Phase 5 (Verify), per milestone, in `docs/logs/dispatch-pipeline-rebuild/M<n>.md`
— not in this file.
