# Code quality notes

Running log, appended per WORKFLOW.md step 5 — one entry per module touched or
closely read for a TODO item. Never overwritten; newest entries at the bottom
of each module's history if it's revisited.

---

## app/domain/missingtext.py — reviewed 2026-08-31, TODO P1 (items 1, 2, 5, 6)
- Stability (1-5): 4 — pure stdlib (json/re/pathlib), well-tested against both
  a synthetic fixture and the real committed LSSM data file. The one
  unverified assumption is the exact live shape of `missing_text`'s
  `vehicles`/`personnel` strings for multi-category cases (comma-joined,
  per docs/live-data-model.md's examples) — not yet confirmed against a real
  multi-requirement mission (no live account this session). A code-reviewer
  pass caught a real bug here pre-commit: `has_vehicle_demand` couldn't tell
  "untouched" from "revealed and fully satisfied" (both parse to empty
  groups), which would have re-derived a stale catalogue requirement for an
  already-crewed mission. Fixed by adding an explicit `revealed` field
  (true once a JSON blob parses at all, independent of whether its groups
  are empty) — see `app/domain/resolver.py`'s notes below.
- Expandability (1-5): 5 — adding a new category is a data change to
  `lssm-missing-requirements.json`, not a code change. `equipment` /
  `conditionalVehicles` handling is centralized in one place if it ever needs
  to become smarter (e.g. once per-vehicle equipment state is tracked).
- Maintainability (1-5): 5 — small, single-purpose, thoroughly commented on
  the *why* (equipment fallback, conditionalVehicles exclusion, the
  comma-split regex's reasoning), 17 focused unit tests.
- Verdict: keep-as-is
- Rationale: does exactly one job (category text → type-ids) with no
  hidden state beyond the cached default table. The `_SPLIT_RE` comma-boundary
  heuristic is the one clever bit; it's tested against the one real category
  that would break a naive split ("Schlauchwagen (GW-L2 Wasser, SW 1000, ...)").

## app/domain/resolver.py — reviewed 2026-08-31, TODO P1 (items 1, 2, 4, 5, 6)
- Stability (1-5): 4 — `resolve_mission` now correctly prioritizes revealed
  `missing_text` over the catalogue estimate (31 tests incl. the new
  priority/casualty-layering cases), but the priority FLIP itself is a
  behavior change with no live-account confirmation yet that this is what the
  game's live feed actually looks like for a variety of mission types beyond
  the documented examples — worth a first-live-run sanity check once
  credentials exist (see README's "what's left" list). A code-reviewer pass
  pre-commit caught two real HIGH-severity bugs, both fixed: (1) an empty-but-
  revealed blob was indistinguishable from "untouched" and fell back to the
  stale catalogue estimate instead of "nothing more needed" — fixed via
  `MissingTextGroups.revealed`; (2) the `personnel` group's raw headcount
  (e.g. "12 Feuerwehrleute") was used directly as a `Requirement.count`
  (vehicle count), risking up to ~6x over-dispatch — fixed by
  `_personnel_to_vehicles`, converting via the category's largest per-vehicle
  crew size (ceil(headcount/max_crew), mirroring `assets.ExtendRule.needed`'s
  existing convention for a single type).
- Expandability (1-5): 4 — `_vehicle_requirements` / `_casualty_counts` split
  cleanly separates "what vehicle classes are demanded" from "casualty-driven
  extras," so a new casualty rule or a new vehicle-demand source slots into
  one method without touching the other. `resolve_mission` itself is a thin
  composition of the two.
- Maintainability (1-5): 4 — the docstring above `resolve_mission` explains the
  two-source priority and why casualty extras are unconditional; the split
  into private helpers (vs. the prior single 60-line method) makes each piece
  independently readable and testable.
- Verdict: keep-as-is
- Rationale: the refactor is a genuine simplification (two focused private
  methods vs. one monolithic one) as well as a behavior fix; no speculative
  abstraction was added — `missing_resolver` is injectable only because tests
  need it, production always uses the cached default.

## app/domain/dispatch.py — reviewed 2026-08-31, TODO P1 (item 7, DRY cleanup)
- Stability (1-5): 4 — unchanged dispatch/coverage/priority-pass logic; the
  only edit is `_missing_text_vehicles` now delegating JSON-blob parsing to
  the shared `missingtext.parse_missing_text` instead of duplicating a
  try/json.loads block, and the `_derive_requirements` docstring reflecting
  that the regex `text_engine` is now a tertiary last-resort (soft-retired,
  not deleted — still threaded through `runner.py`/`account_runner.py`/
  `group_coordinator.py`, which is intentionally left alone this session to
  keep the change surgical).
- Expandability (1-5): 4 — this module was already well-factored (small
  private helpers per concern); no structural change needed for the rework
  since `_derive_requirements` already called `resolver.resolve_mission`.
- Maintainability (1-5): 4 — large file (700 lines) but each function is
  short and single-purpose; the module docstring and per-function docstrings
  stay accurate after this edit.
- Verdict: keep-as-is
- Rationale: no new complexity added; removed one instance of duplicated
  JSON-blob-parsing logic (DRY) without touching the well-tested dispatch
  policy/coverage machinery.

## app/core/group_coordinator.py — reviewed 2026-08-31, TODO P3.1 (share-before-send)
- Stability (1-5): 4 — restructured `run_cycle`'s read/share/plan ordering
  (share now runs before the mission fetch used for planning, with a second
  fetch after) and removed the old end-of-cycle share call. Covered by a new
  `tests/test_group_coordinator.py` with fakes (no httpx needed — this module
  only depends on duck-typed members, not the game layer directly), including
  the propagation-lag case where the visibility guard must still hold. Not
  yet run against a live account (this session had no credentials) — the
  extra `fetch_missions()` round-trip per member per cycle is an accepted,
  intentional cost per the user's own requested fix. A code-reviewer pass
  (APPROVE, 1 MEDIUM + 1 LOW) caught that doubling the `fetch_missions()`
  calls per cycle doubled the surface for an unguarded transient failure to
  abort the WHOLE cycle (all members' transport/dispatch) instead of just
  that account's contribution — fixed via `_safe_fetch_missions` (isolates
  per-account, mirrors the existing `connect()` isolation pattern), with a
  new regression test (`test_one_member_fetch_missions_failure_does_not_abort_cycle`).
  Also fixed a stale comment that described a fetch order the code no longer had.
- Expandability (1-5): 4 — the read/share/re-read/plan/dispatch phases are
  now each a clearly delimited block in `run_cycle`; adding another
  pre-plan phase (e.g. a future auto-accept step) has an obvious slot.
- Maintainability (1-5): 4 — the module docstring's numbered list was
  updated to match the new phase order; `_share_own` itself (cap/floor
  logic) was untouched, so its existing behavior and reasoning still apply.
- Verdict: keep-as-is
- Rationale: a straightforward reordering with no new abstractions; the
  fakes-based test suite is the first direct coverage this module has had
  (previously only exercised indirectly through the pure `app/domain/group.py`
  tests), which is a net improvement independent of this specific change.

## app/core/runner.py + app/game/client.py — read 2026-08-31, TODO P4 (prisoner transport sanity-check)
- Stability (1-5): 4 (runner.py transport phase) — `_handle_transports` is
  simple and kind-agnostic by construction (no patient/prisoner branch to get
  wrong), backed by existing + one new symmetry test. Unrelated to this task
  but worth flagging: `app/domain/transport.py`'s richer routing model
  (hospital specialty, tax ceiling, prefer-own) is built/tested but never
  called from `runner.py` — `_handle_transports` relies entirely on the
  game's own pre-filtering instead. Not a bug, just an unused-in-practice
  module; a future session should decide whether to wire it in (more control,
  e.g. an alliance-tax ceiling we don't currently enforce) or delete it.
- Expandability (1-5): 3 — wiring in the richer domain/transport.py routing
  later would need `_handle_transports` to parse `transport_targets()`'s
  hrefs into `TransportRequest`s (extracting building id, specialty) instead
  of blindly taking `targets[0]`; that's a real (if modest) piece of new work,
  not a config flip.
- Maintainability (1-5): 4 — both files are small, focused, already
  well-tested; no changes made here beyond the one new test.
- Verdict: keep-as-is
- Rationale: verification-only task; no code changed. Flagging the unwired
  domain/transport.py module here since it's exactly the kind of thing that
  should get a decision (wire it in vs. remove it) rather than sit
  indefinitely — logged for a future session, not blocking this one.

## app/game/models.py, app/core/runner.py, app/store/db.py, app/core/orchestrator.py, app/web/app.py + dashboard.html — reviewed 2026-08-31, TODO P4 (surface live icon status)
- Stability (1-5): 5 — `Mission.status`'s icon-suffix matching is exactly the
  three documented suffixes (docs/live-data-model.md), no ambiguity; the
  SQLite snapshot table (`mission_status`) reuses the same lock-guarded
  delete-then-insert pattern already established elsewhere in `Store`, so no
  new concurrency surface. Code-reviewer pass: APPROVE, 0 findings at any
  severity. One noted (non-issue) behavior: transport-only accounts clear
  their mission_status snapshot every cycle (no mission poll happens in that
  mode) — correct, since there's genuinely no live data to show.
- Expandability (1-5): 4 — the snapshot pattern (distinct from `shortfall`'s
  cumulative one) is now a second precedent in `Store`; a future per-cycle
  snapshot stat can follow either depending on whether it should accumulate
  or reflect only the current cycle.
- Maintainability (1-5): 4 — small, additive changes across 6 files, each
  following an existing convention in that file (dataclass field + property
  in models.py, Counter tally in runner.py, table+methods pair in db.py,
  one more line in the on_cycle closure, one more dict key in app.py, one
  more conditional block in the template). The `on_cycle` closure itself
  remains untested (a PRE-EXISTING gap — confirmed via grep that no test
  references it at all; this change's one new line there is not a new
  regression in coverage rigor) — worth a follow-up ticket to test that
  whole closure, not blocking.
- Verdict: keep-as-is
- Rationale: mechanically simple, each piece tested at the layer where it's
  actually exercisable (model, runner, store, web route+template); the one
  genuinely untestable-without-more-scaffolding piece (`on_cycle`) already
  had zero coverage before this change touched it.

## app/web/app.py + dashboard.html — reviewed 2026-08-31, TODO P3.2 (combined group stats)
- Stability (1-5): 5 — `_combined_totals` reuses `Store.totals()` (already
  int-typed, COALESCE-safe) with no new persistence; code-reviewer traced the
  actual call sites (`group.running` can only be true when `start_group` was
  called with a non-empty member list) to confirm the `{}`-for-empty-members
  branch never coexists with the template's totals-display branch, so no
  `Undefined`-attribute risk. APPROVE, 0 findings.
- Expandability (1-5): 5 — a plain aggregation function; extending it (e.g.
  a combined shortfalls view) would follow the same one-line-per-metric shape.
- Maintainability (1-5): 5 — small, pure, directly tested.
- Verdict: keep-as-is
- Rationale: reuses existing data (no new table), small and low-risk. Noted
  gap (accepted, not fixed): no test renders the true "group running with
  totals" template branch end-to-end, since faking a live `Orchestrator`
  group requires touching its private `_group` tuple — judged not worth the
  fragility for this size of change, especially given the running-implies-
  non-empty-totals invariant is verified statically.

## app/game/session.py — reviewed 2026-08-31, live-testing bug fix (not a TODO.md item — found while probing for P0/P1/P4)
- Stability (1-5): 3 — the login-rejection classifier had a REAL live bug
  (bare `"code"` keyword false-positived a genuine "Ungültige Anmeldedaten."
  rejection as NEEDS_VERIFICATION), found via an actual failed login attempt
  with throwaway-account credentials this session. Fixed by extracting
  `_classify_rejected_login` and checking the specific bad-credentials
  phrase first. Both code-reviewer (APPROVE, 1 MEDIUM) and security-reviewer
  (no findings, net improvement) confirmed the fix is correct and that
  misclassification here is diagnostic-only (the OK/rejected split happens
  earlier, via URL+cookie state, independent of this function) — never an
  auth-bypass surface. Residual, currently-unverifiable limitation (flagged
  by both reviewers, documented in the function's docstring rather than
  guess-fixed): the same substring-match-anywhere pattern could in principle
  still misfire on an unseen real verification-challenge page; no such page
  has ever been captured to test against.
- Expandability (1-5): 3 — `GameSession` has no dependency-injection seam for
  `httpx.AsyncClient`'s transport, so `login()`'s full HTTP flow (only the
  extracted pure classification helper) remains untestable without a live
  account. Pre-existing gap (README already flags this), not introduced or
  worsened here — a future session could add a `transport` param for proper
  unit testing of the whole flow.
- Maintainability (1-5): 4 — the extraction improved this specifically: the
  classification logic is now a small, pure, directly-testable function
  instead of buried inline in an async method.
- Verdict: keep-as-is
- Rationale: a targeted, live-verified bug fix with proportionate test
  coverage; the module's larger untested-httpx-flow gap is real but
  out of scope for this fix (same conclusion both reviewers reached).

## app/domain/missingtext.py + resolver.py — reviewed 2026-08-31, TODO P1 ("other" group + revealed-semantics fix, live-verified)
- Stability (1-5): 4 — two real bugs/gaps closed via live testing against a
  real game account this session, both code-reviewed (APPROVE, 0
  CRITICAL/HIGH each time): (1) `other` group is now resolved through the
  same category table as `vehicles`/`personnel` — confirmed correct against
  real "Feuerlöschpumpe (z. B. LF)" (→ almost the whole Löschfahrzeug set)
  and "Schmutzwasserpumpe" (→ only the 2 dedicated SW types) examples, with
  domain confirmation from the project owner that this is the actual
  practical need (no capacity math required); (2) `revealed` now requires an
  actual `vehicles`/`personnel`/`other` KEY present, not just "parsed as any
  JSON object" — closes a real regression risk a 4th `missing_text` shape
  exposed (`{"long_text": "..."}` on an untouched mission, which would have
  wrongly skipped the catalogue estimate under the old definition). One
  MEDIUM noted by review, not fixed (unproven, no concrete trigger found):
  `other` is treated uniformly as vehicle-count-shaped since `MissingTextResolver`'s
  table merges `vehiclesByRequirement` (vehicle-shaped) and `staff`
  (headcount-shaped) into one flat dict — if a future real `other` entry
  ever resolves against a `staff`-table text, it'd be mis-treated as a
  vehicle count instead of a headcount. No evidence this happens; tracked
  here rather than guess-fixed.
- Expandability (1-5): 4 — the fix is a one-line addition mirroring the
  existing `vehicles` handling; the `revealed` fix is a precise, minimal
  change to one boolean expression with no new code paths.
- Maintainability (1-5): 5 — both fixes are heavily commented with the exact
  live evidence and reasoning that drove them, so a future session
  re-investigating "other" or "revealed" semantics has the full trail
  without needing to re-derive it from scratch.
- Verdict: keep-as-is
- Rationale: exactly the kind of fix live-testing is for — both bugs were
  invisible from static analysis alone (the LSSM table already had the
  right data; the bug was simply never routing `other` through it) or from
  reasoning about the documented shape alone (the 4th `missing_text` shape
  was undocumented until observed live). Docs (docs/live-data-model.md,
  TODO.md) updated in the same pass so the finding isn't siloed in code
  comments alone.

## app/domain/resolver.py — read 2026-08-31, TODO P4 (timed-mission verification, no code change)
- Stability (1-5): 5 — live-verified that `resolve_mission` already handles
  timed missions' early missing_text reveal correctly, purely as a side
  effect of never gating vehicle-demand resolution on `vehicle_state`. No
  bug found, no code changed — locked in with a regression test
  (`test_resolve_mission_timed_mission_reveals_missing_text_before_participation`)
  so a future change that DOES add a vehicle_state gate would be caught.
- Expandability (1-5): n/a — no code change.
- Maintainability (1-5): n/a — no code change.
- Verdict: keep-as-is
- Rationale: verification-only task (confirming existing correct behavior
  against real timed-mission data), no production code touched.

## app/core/runner.py, app/domain/dispatch.py, app/core/account_config.py, app/web/app.py + account.html — reviewed 2026-08-31, TODO P2 (patient+parasite resolution, real recall bug, new aggressiveness setting)
- Stability (1-5): 4 — three related, Sasha-specified changes: (1) a REAL bug
  fix (`_recall_cleared_patient_missions` wasn't filtered by vehicle type —
  confirmed via code reading that it would recall a manually-sent fire truck
  on a mission it never governed), (2) the patient+parasite conflict
  resolution (parasite on a patient-governed mission always claims
  participation only, never a fake "useful" pick), (3) a new
  `ParasiteAggressiveness` (active/all/needed) setting wired end-to-end
  (domain → account config → web form → template). Code-reviewer: APPROVE,
  1 MEDIUM (missing test for `ALL` mode's filter-bypass interaction — added:
  `test_parasite_mode_all_preempts_missions_the_main_loop_would_have_filtered`)
  + 1 LOW (docstring implied immediate patient coverage on a leeched mission;
  clarified to "from next cycle onward") — both addressed before commit.
  Confirmed `is_parasite`/`on_it` are provably disjoint (`scope_ok = ... and
  not on_it` gates every `is_parasite` branch), so the fix never touches the
  top-up path's existing patient-aware behavior.
- Expandability (1-5): 4 — `ParasiteAggressiveness` follows the exact same
  enum + `_scope`-style-helper + `try/except ValueError` pattern already
  established for `ModeScope`/`DispatchOrder`/`MissionSource`, so a future
  4th aggressiveness level (or an analogous setting for top-up) has a clear
  template. `_PATIENT_GOVERNED_CLASSES` as a class-level tuple keeps the
  governed-class list in one place, next to the method that uses it.
- Maintainability (1-5): 4 — deliberate care taken to avoid a real naming
  collision: `AccountConfig.parasite_aggressiveness` (new) is named
  DISTINCTLY from the pre-existing legacy migration key `parasite_mode`
  (an old boolean field, unrelated), with a dedicated regression test
  (`test_legacy_parasite_mode_boolean_does_not_collide_with_aggressiveness`)
  proving no collision through the `to_dict()`/`from_dict()` round-trip.
- Verdict: keep-as-is
- Rationale: each of the three changes is narrowly scoped to what Sasha
  asked for, tested at the layer where it's actually exercisable, and the
  one real correctness risk found in review (ALL mode's filter-bypass, a
  known/intentional design consequence but previously untested) was closed
  with a regression test before commit.

## app/domain/group.py, app/core/group_coordinator.py, app/core/account_runner.py — reviewed 2026-08-31, TODO P3 (group-mode logic audit + strike-tracking fix)
- Stability (1-5): 4 — a full end-to-end audit (no over-send, visibility
  gating, participation/token-cap, radius_basis, begun-first priority,
  partial-start ownership) found the existing logic sound; the one real gap
  (no strike-tracking, unlike solo dispatch — a chronically-uncrewed vehicle
  would fail every cycle forever, sinking any good vehicle bundled with it
  in the same batch) is now fixed and mirrors `DispatchRunner.run_cycle`
  exactly (per-vehicle retry on batch failure, `StrikeTracker`-based
  exclusion from the combined pool). Code-reviewer: APPROVE, 1 MEDIUM
  (the retry loop's `on_stat` call was firing once per vehicle instead of
  once per mission-send, inflating the dashboard's cumulative "missions"
  stat on any multi-vehicle partial retry) — fixed before commit, with a
  test (`stat_calls == [(1, 3, 1)]`) that would have caught it.
- Expandability (1-5): 4 — `_usable(v, aid)` in group.py is a direct mirror
  of dispatch.py's solo `_usable(v)`, so the two pool-filtering mechanisms
  stay easy to keep in sync; `AccountRunner.strikes` is now a clean public
  seam other future group-mode features can read (e.g. a "struggling to
  field" dashboard view scoped to the group).
- Maintainability (1-5): 4 — the retry/strike logic is a close structural
  copy of the already-understood solo-mode pattern, not a novel mechanism to
  learn; comments cross-reference `DispatchRunner.run_cycle` directly so a
  future reader can diff the two instead of re-deriving intent.
- Verdict: keep-as-is
- Rationale: this was audit-then-fix, not speculative hardening — every
  change traces to a concretely identified, real failure mode (chronic bad
  vehicle) rather than defensive code added "just in case."

## app/domain/requirements.py — reviewed 2026-08-31, TODO P1 (item 7, docstring only)
- Stability (1-5): 5 — code unchanged (only the module docstring was
  rewritten to describe the engine's new tertiary role); its own 5-ish unit
  tests in `tests/test_requirements.py` still pass untouched.
- Expandability (1-5): 3 — the regex/extend-rule matching (`VehicleType.
  matches`/`required_count`, in `assets.py`) is inherently harder to extend
  than a data-table lookup; that's exactly why P1 moved primary resolution
  off of it.
- Maintainability (1-5): 3 — still functional and tested, but now a
  legacy/rarely-hit code path; a future session could consider whether it's
  worth deleting outright once live confirmation shows the tertiary fallback
  never fires in practice (not decided this session — no evidence either way).
- Verdict: refactor-in-place (documentation only this round; a future
  candidate for deletion once live data shows it's dead weight)
- Rationale: kept alive deliberately (still wired through 3 call sites +
  its own tests) since removing it is a wider-blast-radius change than this
  TODO item asked for; downgraded its documented role to match reality.

## app/domain/dispatch.py — reviewed 2026-09-11, ad-hoc feature (SMART order)
- Feature: `DispatchOrder.SMART` — user request: "sort out whats the most
  amount of credits i can do with my currently available vehicles without
  sending to missions he cannot fully saturate." Ranks candidates by
  credits-per-vehicle-needed (a knapsack-style value-density heuristic)
  instead of raw distance/credits/age. The "finish already-begun missions
  first" half of the request needed no new code — the existing unconditional
  priority pass (begun-missions-first) already does this; SMART only changes
  ordering within the remaining fresh-mission pool.
- Stability (1-5): 4 — 306 tests pass incl. 2 new SMART-specific cases
  (credit-density beats raw-credits ordering; min_fire_personnel top-up folds
  into the vehicle-needed estimate). The estimate is deliberately approximate
  (doesn't run the full multi-role coverage matcher per candidate, just sums
  `Requirement.count` plus a ceil-divided fire-personnel top-up) — a
  code-reviewer MEDIUM finding confirmed this is an acceptable simplification
  given `plan_dispatches` already re-resolves requirements for real at actual
  dispatch time; SMART only needs a *relative* ranking, not an exact count.
  Not yet confirmed against live play (no way to observe "chose fewer trucks
  for near-equal credits" without watching several real cycles).
- Expandability (1-5): 3 — `_order()`'s tuple-shape sniffing
  (`len(t) == 5`) is a `ponytail:`-flagged deliberate shortcut so the 4
  existing non-SMART orders (3-tuple in solo mode, unchanged) didn't need a
  data-shape migration for one new order; flagged for a NamedTuple refactor
  if a 6th field is ever needed.
- Maintainability (1-5): 4 — isolated to the SMART branch of the
  candidate-gathering loop and `_order()`; doesn't touch any other order's
  code path.
- Verdict: keep-as-is
- Rationale: two of the three code-review findings were real bugs fixed
  before commit (min_fire_personnel omission; group mode silently accepting
  `order=smart` and no-oping instead of an explicit reject-to-NEAREST) — see
  `app/core/account_config.py` note below. The third (duplicate resolver
  calls between ranking and actual dispatch) was explicitly skipped per
  ponytail/YAGNI, per the reviewer's own "low priority" framing — the ranking
  pass is O(missions), not O(missions × vehicles), so it's not a hot path.

## app/core/account_config.py — reviewed 2026-09-11, ad-hoc feature (SMART order)
- Change: `GroupConfig.group_policy()` gained an explicit SMART→NEAREST
  reject-and-fallback guard, since `plan_group_dispatches` has no per-account
  requirement resolver to rank by (unlike solo `plan_dispatches`) — letting
  `order=smart` through would have silently degraded to raw-credits ordering
  inside the shared `_order()` sort, which is a silent-failure-shaped bug the
  code reviewer flagged as MEDIUM.
- Stability (1-5): 5 — one new regression test
  (`test_smart_order_rejected_falls_back_to_nearest`) locks in the reject
  behavior; `AccountConfig.dispatch_policy()` needed no equivalent guard
  since solo mode's SMART path is fully implemented.
- Verdict: keep-as-is

## app/web/templates/dashboard.html — reviewed 2026-09-11, ad-hoc fix (auto-refresh eating form input)
- Bug: user report — "the site frequently reloads itself... impossible to
  fill in the account fields." The dashboard's `<meta http-equiv="refresh"
  content="5">` (added for the live log console) hard-reloaded the whole
  page every 5s, wiping the "Add account" form (label/email/password) mid-
  keystroke since it's the only form on the page with free-text inputs.
- Fix: replaced the meta-refresh with an equivalent `setInterval(reload,
  5000)`, guarded to skip the reload whenever the add-account form has
  focus OR any of its inputs already hold a typed value — so a user who
  tabs briefly out of the form (or types slowly) doesn't lose work either.
  `details.html` keeps its plain meta-refresh unchanged (that page has no
  input fields to protect).
- Stability (1-5): 4 — pure client-side JS, no server/domain change, no new
  test surface (nothing in `tests/test_web.py` asserted on the meta tag).
  Manually reasoned through, not live-browser-verified this session.
- Verdict: keep-as-is
- Rationale: minimal, targeted fix — kept the 5s live-refresh behavior
  everywhere else on the page, only suppressed it exactly where it was
  destructive.

## app/web/templates/account.html, app/domain/dispatch.py — reviewed 2026-09-11, ad-hoc fix (SMART wording)
- User feedback: "smart isnt most cash per vehicles needed its more most cash
  with all currently available vehicles... is that just a wording issue in
  the settings page or have you built the algorithm wrongly." Investigated:
  the algorithm was already correct for the stated goal -- `plan_dispatches`'
  candidate-consuming loop (lines ~331-353) doesn't dispatch a single "best"
  mission and stop; it walks the ENTIRE credits-per-vehicle-sorted list,
  dispatching every mission the shrinking pool can still fully crew. The
  per-vehicle ranking only decides priority (who gets first claim on scarce
  vehicle types); the actual outcome of a full pass already is "the most
  total credits this fleet can earn right now" -- which is what was asked
  for. This was a pure labeling/comment problem: the settings dropdown and
  code comments described the internal sort KEY, not the actual goal/outcome,
  which read as if SMART only picked one efficient mission rather than
  greedily clearing everything affordable.
- Fix: reworded the account.html dropdown option and added a clarifying
  comment in dispatch.py tying the per-vehicle ranking back to the aggregate
  behavior of the loop that consumes it. No algorithm change -- behavior is
  identical (all 306 tests unchanged and pass).
- Verdict: keep-as-is (wording only)
- Rationale: worth flagging the honest limit even so -- greedy-by-density is
  the standard practical approximation for this NP-hard (multi-dimensional
  knapsack) problem, not a literal proven-optimal solver; it's not expected
  to matter in practice at typical fleet/mission scale, but it's not a
  mathematical guarantee either.

## app/domain/missingtext.py — reviewed 2026-09-11, live bug (NBSP breaks category matching)
- Bug: user shared a live "unresolved" diagnostic log line for mission
  #4397737525 "Straße unter Wasser" (mtid 173). Its `missing_text` was
  revealed and plainly needed a fire engine + pump, but the resolver came
  back with zero requirements, so the bot silently skipped it. Root cause:
  the game renders these category strings with U+00A0 (non-breaking space)
  everywhere a plain space would go -- e.g. `"1\xa0Löschfahrzeug\xa0(LF)"`
  -- both between the count and the category, and inside abbreviations like
  "z.\xa0B.". `_COUNT_RE`'s `\s` DOES match across the NBSP (confirmed via
  the real reference JSON), so the count strips correctly, but the leftover
  category string still CONTAINS an embedded NBSP, which fails an exact
  dict-key lookup against `MissingTextResolver._by_text` (built from the
  reference table's plain-space keys). Net effect: a "revealed" mission
  with real, visible needs was treated as needing nothing -- no error, no
  distinct log signal beyond the generic "no requirements found" line.
- Fix: `_normalize_spaces()` (new), called from `parse_missing_text()` on
  all three group strings right when they're extracted from the parsed
  JSON, before anything downstream (split/count-regex/dict-lookup) sees
  the text. Code review (async) flagged that the first version only
  handled U+00A0 specifically -- widened to `re.sub(r"\s", " ", ...)`,
  the SAME whitespace class `_COUNT_RE`/`_SPLIT_RE` already tolerate, so
  other invisible-space codepoints the game might also emit (thin space,
  narrow no-break space, etc. -- it's already shown to be inconsistent
  about which one it uses where) can't reintroduce the identical failure
  mode for a codepoint not yet observed live.
- Stability (1-5): 5 -- traced every caller (dispatch.py, resolver.py) and
  confirmed both go through `parse_missing_text` before ever reaching
  `MissingTextResolver.resolve`/`resolve_line`; no code constructs
  `MissingTextGroups` directly with live text bypassing this normalization.
  3 new regression tests incl. the exact live example, an end-to-end
  resolve-through-`default_resolver()` check, and a non-NBSP Unicode space
  variant. 309 tests pass.
- Verdict: keep-as-is
- Rationale: this is a real, live, silently-recurring dispatch bug (not a
  hypothetical) -- likely affected every multi-word missing_text category,
  not just this one mission, since the game appears to use NBSP
  systematically rather than for this specific mission type.

## app/domain/missingtext.py, app/domain/resolver.py — reviewed 2026-09-11, live bug (vehicle mislabeled under personnel)
- Bug: user shared another "unresolved" log line -- mission #4397704222
  "Person in Wasser" (mtid 244), `missing_text`
  `{"vehicles":null,"personnel":"2 GW-Wasserrettung","other":""}`. User
  clarified: "GW-Wasserrettung" is satisfied by the same-named VEHICLE, not
  a crew role. Confirmed against `data/de/vehicles/64.mscv`: type 64 (GW-W)
  already carries a legacy regex extend-rule keyed on exactly this text
  (`"([1-6]) GW-Wasserrettung": 1`, etc.), proving the game genuinely means
  "N GW-W vehicles" here, just filed under the wrong JSON key. Since
  "GW-Wasserrettung" wasn't in the LSSM `vehiclesByRequirement` table (only
  in the legacy engine's per-type regex, a different, unrelated code path)
  and every `personnel`-group entry was unconditionally run through
  headcount/crew-size conversion, this mission's real, catalog-confirmed
  need (`boats: 1`) silently resolved to nothing.
- Fix: added `{"texts": ["GW-Wasserrettung", "GW-Wasserrettungen"],
  "vehicles": [64]}` to the reference table. Split `MissingTextResolver`'s
  merged `_by_text` into `_vehicle_text` (vehicles/other groups, unchanged
  behavior) and `_staff_text` (crew roles); added `resolve_personnel()`
  which tries the staff table first (genuine headcount, e.g.
  "Feuerwehrleute"), falling back to the vehicle table (the game's
  mislabeling case) -- so the caller (`resolver.py`'s `_vehicle_requirements`)
  knows which bucket needs `_personnel_to_vehicles` conversion and which is
  already a vehicle count.
- Code review (async): APPROVE. Confirmed no other caller of
  `resolve`/`resolve_line` was affected by the table split (only the
  vehicles/other groups use them, both semantically vehicle-only already);
  confirmed the staff/vehicle text tables don't currently collide on any
  category name (today's precedence choice is inert, documented for if that
  ever changes); confirmed type 64 is correct against the .mscv source.
  One MEDIUM gap (no test for a personnel string mixing a staff chunk and a
  vehicle chunk together, e.g. "8 Feuerwehrmann, 2 GW-Wasserrettung") --
  closed with `test_resolve_personnel_mixed_staff_and_vehicle_chunks_route_separately`.
  315 tests pass.
- Verdict: keep-as-is
- Rationale: same class of live-data bug as the NBSP fix above -- the game's
  missing_text is the authoritative source but not perfectly structured;
  real fleet-relevant fixes, not speculative hardening.

NOTE: per explicit instruction, this fix is COMMITTED ONLY -- not pushed,
not merged to master, not deployed. Batched together with any further
findings from this live-testing session before the next rebuild.

## data/de/vehicles/Vehicle.mscv, data/reference/lssm-missing-requirements.json — reviewed 2026-09-11, live data gap (ELW substitution)
- User asked whether ELW2 (mobile command vehicle) and ELW2-Drohne are
  recognized substitutes for ELW1 (battalion chief vehicle), and mentioned
  SEG-ELW ("usable as a plain ELW") and ELW2-Drohne ("also serves as a
  drone"). Investigated both the catalog fallback class map and the live
  `missing_text` table:
  - ELW1 (3) / ELW2 (34) / ELW2-Drohne (78, 129) were ALREADY fully
    interchangeable in both paths -- no gap.
  - ELW2-Drohne (129) was ALREADY correctly in the live "Drohneneinheit(en)"
    category (`[125,126,127,128,129]`), but MISSING from the catalog
    fallback's `Vehicle.mscv` `"drone"` class (`[125,126,127,128]`) -- a
    real, if minor, inconsistency (only affects the pre-missing_text
    catalog estimate for an untouched drone-needing mission).
  - SEG-ELW (59) was ONLY mapped to its own dedicated "ELW 1 (SEG)"
    category, not to plain "ELW 1"/`battalion_chief_vehicles` -- confirmed
    by the user as a real gap ("usable as a ELW" too).
- Fix: added 129 to `Vehicle.mscv`'s `"drone"` class; added 59 to
  `Vehicle.mscv`'s `"battalion_chief_vehicles"` class AND to the live
  table's `"ELW 1"` vehicles list, while KEEPING the dedicated "ELW 1
  (SEG)" -> [59] entry unchanged (SEG-ELW satisfies either need).
- User flagged a NEXT step explicitly out of scope for this fix: SEG-ELW
  and ELW2-Drohne are "less preferable" for a GENERIC ELW slot since both
  have their own special use case (SEG-ELW: preferred on 20+ patient
  missions per the game's automatic patient-FSM handling; ELW2-Drohne: its
  drone role). The coverage matcher (`app/domain/coverage.py`) has NO
  concept of "prefer type A over type B within one requirement's eligible
  set" today -- `select_coverage`'s tie-break is only
  (most-requirements-covered, nearest, vehicle id). Implementing a
  specialization-avoidance preference is a real algorithm change to a
  shared, heavily-used function, not a data-table edit -- deliberately NOT
  attempted in this pass; flagged for a follow-up decision.
- Stability (1-5): 5 -- 3 new regression tests (real committed data), 317
  tests pass. No code-reviewer subagent dispatched for this one (unlike the
  NBSP/GW-Wasserrettung fixes): pure array-append changes to already
  well-tested data files, double-cross-referenced against both user
  domain confirmation and the .mscv source pattern data before editing --
  proportionate to the size of the change per ponytail.
- Verdict: keep-as-is

## app/domain/coverage.py, app/domain/dispatch.py — reviewed 2026-09-11, ad-hoc features (ELW preference + MZB escort)
- Feature 1: ELW specialist deprioritization. User: SEG-ELW and ELW2-Drohne
  are eligible ELW1/ELW2 substitutes but both have another valuable use
  (SEG-ELW: automatic patient handling on mass-casualty missions; ELW2-Drohne:
  also a drone) -- so both should be picked LAST among equally-eligible ELW
  candidates for an ordinary mission (never excluded -- still used if it's
  the only option), UNLESS the mission has 20+ patients, where SEG-ELW
  becomes the actively BETTER pick. Implemented as a new middle tiebreak
  dimension in `select_coverage()` (`specialist_type_ids`/`preferred_type_ids`,
  after coverage-count, before distance) plus `_ELW_SPECIALIST_TYPE_IDS =
  {59, 129}` / `_SEG_ELW_TYPE_ID = 59` constants and a `patient_load`
  computation in `_plan_one`.
- Feature 2: MZB water-rescue escort. User: an MZB always wants a GW-Taucher
  + GW-Wasserrettung riding along, preferably same station, never a hard
  blocker. Implemented as `_add_water_rescue_escort()`, called AFTER every
  gating check in `_plan_one` (satisfied / max_vehicles_per_mission), so it
  structurally cannot cause a mission to be skipped, capped, or marked
  unsatisfied.
- Code review (async): WARNING verdict, 3 MEDIUM + 2 LOW. Fixed:
  1. `patient_load` originally used `max(patients_count,
     possible_patients_count)` -- since `possible_patients_count` is a
     static per-mission ceiling that never drops, this never decayed: a
     mission that started as 25-casualty would keep "preferring" SEG-ELW
     for its entire lifetime even after most patients were already
     transported. Fixed to reuse `resolver.py`'s existing `_casualties()`
     revealed-vs-possible reconciliation (same one `_casualty_counts`
     already uses for ambulances), gated on `vehicle_state >= 1` -- now
     correctly drops to 0 once patients_count authoritatively clears.
  2. MZB escort's same-station check (`v.building_id == mzb.building_id`)
     let two UNKNOWN-station vehicles (`building_id=None` on both sides)
     falsely match each other, wrongly preferring one over a genuinely
     nearer, known-station candidate. Fixed with an explicit `is not None`
     guard.
  3. LOW: ELW2-Drohne (129) is only reachable via the missing_text path
     (not in `Vehicle.mscv`'s `battalion_chief_vehicles` catalog class) --
     had zero test coverage. Added
     `test_elw2_drohne_deprioritized_via_missing_text_path`.
  - NOT fixed, deliberately: MEDIUM #3 -- an opportunistic MZB escort pick
    consumes from the SAME shared per-cycle pool as every other mission
    `plan_dispatches` processes afterward, so it can (in principle) take a
    diver/GW-Wasser vehicle a LATER-processed mission genuinely needs. This
    is not unique to this feature: `plan_dispatches` already greedily
    consumes the shared pool in priority order for every vehicle type --
    any earlier mission can already "steal" a vehicle a later one needs.
    Adding reservation/look-ahead logic just for escort picks would be
    inconsistent special-casing (YAGNI) rather than a real fix for this
    feature specifically; flagging it here as a known, accepted, pre-existing
    system characteristic rather than building around it.
- Stability (1-5): 4 -- 12 new tests across `test_coverage.py`,
  `test_dispatch.py` covering both features' happy paths, the "not a hard
  blocker" cases, and the two fixed edge cases above. 333 tests pass.
- Verdict: keep-as-is

## app/core/runner.py — reviewed 2026-09-11, ad-hoc feature (dispatch log verbs)
- User: distinguish "these are the first vehicles to a mission" (dispatched)
  from "an alliance member is already on it" (sent to help) from "we're
  already there" (sent reinforcements), so the log reads more informatively
  during manual monitoring.
- New `_dispatch_verb(mission, assigned, mission_id)`: "sent reinforcements"
  if `assigned.get(mission_id)` (our own vehicle already en route this
  cycle), else "sent to help" if `mission.vehicle_state >= 1` (someone else
  is on it), else "dispatched". Wired into both the batch-success and
  per-vehicle-retry log lines in `run_cycle`.
- Code review confirmed: `by_id.get(mid)` can't realistically miss (same
  `missions` list backs both), and `getattr(None, ...)` degrades safely to
  "dispatched" even if it somehow did; `assigned` is a per-cycle snapshot
  with no staleness window since each mission is planned at most once per
  `run_cycle` call. No fix needed.
- Scope note: only the solo `DispatchRunner` path -- group mode's dispatch
  log (`group_coordinator.py`) already uses a different, bracket-prefixed
  format with no "dispatched" verb at all, so it wasn't touched (user's
  wording matched the solo format exactly).
- Stability (1-5): 5 -- 3 new tests, one per verb, 333 tests pass.
- Verdict: keep-as-is

NOTE: per the established pattern this session, this batch is COMMITTED
ONLY unless/until told to push/merge/rebuild.
