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

## app/domain/resolver.py — reviewed 2026-09-11, live gap (RTH per-patient request undetected)
- User asked: "does the app know the RTH from 'Wir benötigen: RTH' for
  patientcare?" Investigated: no. `_casualty_counts`'s per-patient text
  match only checked "nef"/"notarzt" -- a patient's revealed
  "Wir benötigen: RTH" matched neither, so that request was silently
  ignored entirely (no NEF, no helicopter, nothing added).
- Fix: detect "rth"/"rettungshubschrauber" as a separate signal, mapped to
  the dedicated `rescue_helicopter` class (type 31 only) rather than the
  broader `nef` class (29/31/74, includes ground Notarzt vehicles) --
  RTH-mentioning entries are excluded from the `nef` bucket so the same
  patient's need isn't double-counted across both.
- Code review (async): APPROVE, one MEDIUM residual-risk note (accepted,
  not fixed): substring matching ("rth" in text) is only proven safe
  against the two patient-need shapes ever observed live
  ("Wir benötigen: NEF"/"RTH") -- a hypothetical future third shape
  containing "rth" as an incidental substring of an unrelated word is
  unproven but not impossible. Not blocking; worth re-checking if a new
  patient-need string shape is ever observed.
- Stability (1-5): 5 -- 1 new end-to-end test
  (`test_rth_comes_from_patient_need_as_rescue_helicopter_not_nef`)
  confirms the count, the class, AND that it does NOT also land in `nef`;
  existing NEF tests (`test_nef_comes_from_patient_need_not_chance`,
  `test_two_patients_two_nefs`) confirmed unaffected. 334 tests pass.
- Verdict: keep-as-is

NOTE: commit only, per the established batching pattern -- no
push/merge/rebuild this round.

## app/domain/dispatch.py, app/domain/resolver.py — reviewed 2026-09-11, ad-hoc feature (vehicle-class reservation)
- User: "reinforcements and help is always to be prioritized... if ANY
  mission mentions some vehicle group as missing (e.g. 'missing a boat' or
  'missing personnel GW-Wasserrettung'), those vehicles cannot be sent out
  as [fresh] dispatch anymore, only help or reinforcement until those
  missing texts have been resolved." Investigated first: the existing
  priority pass already gives OUR OWN already-open missions first crack at
  the pool each cycle, but nothing previously prevented a FRESH send
  elsewhere from consuming a vehicle class an already-open (ours or an
  ally's) mission still needs -- confirmed via two scoping questions with
  the user before building: (1) only missions we could actually act on
  (in range, in scope) count toward reservation; (2) reservation is a
  BLUNT, WHOLE-CLASS hold, not an exact-count one -- deliberately, per the
  user's own stated rationale: a vehicle can look available/dispatchable
  yet keep silently failing to actually crew/tow (missing personnel,
  missing a trailer tug), so a same-cycle "looks satisfiable" coverage
  check is exactly the kind of signal that failure mode fools. Trust the
  mission's own revealed need instead of our own in-cycle guess.
- Implementation: `_reserve_vehicle_group()` (shared helper) adds a
  mission's outstanding VEHICLE-CATEGORY type IDs (via the now-public
  `resolver.vehicle_requirements()` -- excludes the casualty-count layer
  deliberately, since ambulances/NEF are needed near-constantly and would
  make reservation absurdly disruptive) to a per-cycle `reserved_type_ids`
  set, for any actionable mission with `vehicle_state >= 1` that isn't
  patient-governed. Called from both `_priority_pass` (our own/parasite
  missions) and `plan_dispatches`' main candidate loop (everyone else),
  UNCONDITIONALLY -- not gated on whether the current pool numerically
  looks sufficient. The reservation set is fully built before ANY
  consumption happens in the candidate-consuming loop, so a fresh mission
  processed EARLIER in policy order (e.g. NEAREST) still correctly loses
  out to an already-open mission evaluated later. Only a genuinely fresh
  send (`governing=="normal"` and `vehicle_state==0` -- what would log as
  "dispatched", not "sent to help"/"sent reinforcements") gets its pool
  filtered; help/reinforcement sends always see the full pool.
- Code review (async): APPROVE. Fixed both MEDIUM findings: (1) DRY
  duplication between the two call sites -- extracted the shared
  `_reserve_vehicle_group()` helper so the two reservation paths can't
  drift apart; (2) a private cross-module method call
  (`resolver._vehicle_requirements`) -- renamed to public
  `resolver.vehicle_requirements()` with an updated docstring documenting
  the new external caller. Closed the 3-part test-coverage gap: added
  tests for the `_priority_pass` reservation path specifically, multiple
  open missions reserving different classes simultaneously without
  clobbering each other, and reservation correctly NOT triggering once
  `assigned` (from the live vehicle feed, an independent, trusted signal)
  already nets the outstanding need to zero.
- Known, accepted, NOT-fixed limitation: parasite mode's ALL aggressiveness
  can send a fresh vehicle to a completely untouched mission via
  `_priority_pass`'s parasite branch, which does not currently respect
  `reserved_type_ids` (that branch uses the full, unfiltered pool). A
  narrower, opt-in edge case; disclosed, not built around in v1. Also
  noted but not fixed: the SMART-order efficiency estimate doesn't account
  for reservation when ranking, so a mission whose only path requires a
  reserved class can rank artificially high and then get skipped anyway --
  reviewed and confirmed negligible (a skipped mission costs nothing but
  one wasted `_plan_one` call; it doesn't consume a batch slot or starve
  another candidate).
- Stability (1-5): 4 -- 7 new tests (4 initial + 3 closing the review's
  gaps) covering the core mechanism, multi-mission accumulation, the
  `assigned`-netting suppression case, and the priority-pass path
  specifically. 341 tests pass.
- Verdict: keep-as-is
- Rationale: this is an explicit, deliberate over-reservation trade-off
  the user chose with full awareness of the cost (idle vehicles sitting
  unused rather than helping a fresh mission) in exchange for a much
  simpler mental model when manually diagnosing a stuck dispatch -- not a
  bug to "optimize away" later without checking with them first.

## app/domain/resolver.py, app/domain/coverage.py — reviewed 2026-09-11, verification (prisoner count vs base FuStW requirement)
- User asked: "sometimes a mission requests X FuStW but has the option for
  up to Y prisoners. if Y>X then send Y FustW instead." Verified via a
  concrete check (mission type 3: base police_cars=1, possible_prisoners=3):
  the resolver already produces TWO separate `police_cars` Requirement rows
  (base=1, casualty-layer prisoner-derived=3) sharing the same type_ids
  {32,95}. The coverage matcher's existing multi-role "max, not sum"
  semantics (a vehicle satisfies every open requirement it qualifies for
  simultaneously, coverage.py's own documented design) means a single
  vehicle knocks down BOTH rows at once until the smaller is exhausted,
  then keeps going for the larger -- converging to exactly max(X, Y) = 3
  vehicles sent, not 1 (undercounting) and not 1+3=4 (double-counting).
  No code change needed -- this was already correct by construction.
- Added `test_prisoner_count_above_base_police_requirement_sends_the_larger_count`
  to lock this in permanently (an easy behavior to accidentally break via a
  future refactor that, say, merges same-source Requirement rows before
  coverage matching). 342 tests pass.
- Verdict: keep-as-is (verification only, no behavior change)

## app/domain/group.py, app/core/group_coordinator.py — reviewed 2026-09-11, feature parity (extend 4 solo-mode features to group mode)
- User asked for an audit: "have all of the behavioral changes we did today
  made it into the group mode as well?" Audit found: data/parsing fixes
  (NBSP normalization, GW-Wasserrettung split, SEG-ELW/ELW2-Drohne
  eligibility, RTH detection) were automatically shared via the common
  resolver/asset-library machinery, but 4 newer DECISION-LAYER features
  built directly into the solo-only code paths were NOT: ELW specialist
  deprioritization, MZB water-rescue escort, dispatch log verbs, and
  vehicle-class reservation. User confirmed: "yes extend all changes to
  group mode."
- Implementation: `plan_group_dispatches` gained a `GroupPlan.participating`
  field (mission id -> account ids already on it before this cycle, for the
  log-verb feature) and a reservation pre-scan (unconditional, NOT
  range/scope-filtered -- a deliberate v1 simplification since group mode's
  own "begun" pass already bypasses those filters by design, and
  over-reserving is the user's stated preference for this feature anyway).
  `action()`'s nested function gained `is_fresh_send`-gated `call_pool`/
  `token_pool` (the latter for the participation "spare" token pick too, so
  a reserved vehicle can't be handed out as a token either), the ELW
  specialist/preferred tiebreak params on `select_coverage`, and an
  `_add_water_rescue_escort` call placed after every gating check (same
  guarantee as solo: can't affect accept/reject). `group_coordinator.py`
  gained its own `_dispatch_verb` keyed on the per-mission `participating`
  set instead of solo's per-account `assigned` dict (group mode dispatches
  per-account-per-mission, so the verb is computed per (mission, sender)
  pair).
- Code review (async): APPROVE, no correctness bugs. 3 MEDIUM test-coverage
  gaps closed: (a) the pre-scan's "not range-filtered" claim was only
  asserted by comment -- added a test with an out-of-range already-open
  mission that still reserves for an in-range fresh one; (b) the
  `token_pool` participation-pick filtering had zero coverage (all
  prior tests used `participation=False`) -- added a 2-account
  `participation=True` test confirming a reserved vehicle isn't handed out
  as a token; (c) MZB-escort-with-multiple-owners was untested -- added a
  test with the MZB and its escorts owned by different accounts, confirming
  correct `per_owner` bucketing.
- Stability (1-5): 4 -- 8 new tests in `test_group.py` (5 initial + 3
  closing review gaps) plus 6 new tests in `test_group_coordinator.py`
  (4 direct `_dispatch_verb` unit tests + 2 log-integration tests). 356
  tests pass.
- Verdict: keep-as-is

## app/domain/assets.py, app/domain/dispatch.py, app/domain/resolver.py — reviewed 2026-09-11, live gap (HLF crew over-assumed)
- User reported live under-sending on "Kleine Feldbrand" (mission type 14):
  it requests 9 firefighter personnel, and the bot treated a single HLF
  (nominally capped at 9 crew) as fully satisfying that need -- but real
  HLFs are typically staffed at "4-7" crew in practice, not the full 9.
  Clarified scope: "usually max is fine but for HLF because they carry a
  lot go for only half" -- ONLY HLF gets discounted, every other firetruck
  type keeps its full nominal capacity.
- Investigated: identified HLF 20 (type 30) and HLF 10 (type 90) as the two
  HLF variants via their `.mscv` pattern text, both nominally capped at 9
  crew. Added `VehicleType.planning_personnel` (assets.py) -- same as
  `personnel` for every type except 30/90, which return half (floor,
  minimum 1). `personnel` itself is UNCHANGED (still the raw nominal
  capacity, used elsewhere e.g. for display). Switched 4 call sites from
  `.personnel` to `.planning_personnel`: `_fire_crew`/`_fire_personnel_topup`
  (dispatch.py, the actual dispatch-time crew math), the SMART order's
  `avg_fire_crew` estimate (dispatch.py, a ranking approximation), and
  `_personnel_to_vehicles` (resolver.py, missing_text personnel-headcount
  conversion).
- Code review (async): APPROVE, 0 blocking findings. Confirmed via a full
  185-file sweep of `data/de/vehicles/*.mscv` that no other firetruck-class
  type has "HLF" in its pattern text (4 other files mention "HLF" in
  disjunctive text like "Rüstwagen oder HLF" but are heavy_rescue_vehicles,
  not HLF themselves, uncrewed in the data either way). Confirmed the
  SMART-order `avg_fire_crew` change is a complete no-op today (shifts
  ~5.7->~5.3, both truncate to 5 via the estimate's own `int()`) -- not a
  bug, just currently inert; added a comment explaining why that's fine
  (only needs relative ranking, not an exact count). Confirmed
  `_personnel_to_vehicles` is ALSO currently inert -- the real committed
  "Feuerwehrmann"/"Feuerwehrleute" staff category has 62 eligible type IDs
  including 6 other non-HLF types at personnel=9, so `max(crews)` still
  resolves to 9 regardless; not worth a forced synthetic test since no live
  category is HLF-only. Confirmed the `_fire_personnel_topup` nearest-first
  tie-break is unaffected (distance is still the primary sort key; the
  crew-based tie-break only applies at equal distance, and correctly now
  prefers an equal-distance non-HLF truck over an HLF, which is the
  intended effect).
- Stability (1-5): 5 -- 2 new tests (`test_planning_personnel_halves_only_hlf`
  in test_assets.py; `test_fire_personnel_hlf_plans_at_half_crew` +
  `test_fire_personnel_plain_lf_still_plans_at_full_crew` in
  test_dispatch.py, confirming the discount is HLF-specific not a general
  derating). 359 tests pass.
- Verdict: keep-as-is

## app/game/client.py, app/domain/strikes.py, app/core/runner.py, app/core/group_coordinator.py — reviewed 2026-09-11, live gap (confirmed dispatch rejections never detected)
- User reported (via manual testing, then confirmed the exact live evidence):
  a vehicle missing crew or a towing vehicle silently fails to dispatch
  forever. Root cause: the alarm-dispatch endpoint returns HTTP 200 even
  when it can't use a specific vehicle -- the response page just carries a
  Bootstrap alert-danger div naming the vehicle and why
  (`<a href="/vehicles/125521931">🟢 Anh TeSi</a>: Das Zugfahrzeug befindet
  sich nicht auf der Wache.`, live-captured), which the client already
  receives (follows redirects) but never reads. The existing strike system
  only reacts to an outright HTTP-level failure, which this never produces.
- User made two explicit design calls: (1) a CONFIRMED rejection (the game
  itself named the vehicle) strikes it out IMMEDIATELY, bypassing the
  normal 3-in-24h threshold -- stronger evidence than an ambiguous HTTP
  failure; (2) don't try to model the two different real lockout durations
  mentioned in passing (1h tow-vehicle vs. "until restart" crew) -- reuse
  the existing StrikeTracker window uniformly for both.
- Implementation: `StrikeTracker.record_confirmed_failure()` (front-loads
  enough strikes to cross threshold in one call, reusing `record()`'s
  `on_struck` machinery). `_parse_dispatch_failures()` + new
  `DispatchResult(ok, rejected)` NamedTuple in client.py --
  `dispatch()`/`dispatch_many()` now read the response body they already
  had. Both `runner.py` and `group_coordinator.py`'s dispatch loops
  adjusted to only count non-rejected vehicles as dispatched and strike
  rejected ones immediately with the reason surfaced in the log -- this
  ALSO fixed a related, previously-undiscovered bug: a "successful" batch
  that silently dropped one vehicle was being counted/logged as if every
  vehicle in it succeeded.
- Code review (async): BLOCK on a CRITICAL finding, since fixed. The
  regex's `.*?` gaps (under DOTALL) weren't bounded to the current div --
  reviewer proved via direct execution that an alert-danger div with NO
  vehicle link (a plausible generic error banner) would let the match run
  on PAST its own `</div>` and latch onto the FIRST unrelated
  `<a href="/vehicles/…">…</a>: …</div>` found anywhere later on the page --
  e.g. a perfectly healthy vehicle's own status row -- misreporting it as a
  confirmed rejection and striking it out immediately on zero real
  evidence. Fixed by bounding every gap with `(?:(?!</?div\b).)*?` so the
  anchor search can't cross a div boundary; verified against both the real
  live example and the reviewer's adversarial case before and after the
  fix. Also closed two LOW-priority test gaps from the same review: an
  `on_struck`-fires-exactly-once test for `record_confirmed_failure`, and a
  group_coordinator-level confirmed-rejection test (the FakeClient's
  `.rejections` mechanism existed but no test ever used it).
- Stability (1-5): 4 -- 373 tests pass, including the adversarial
  cross-div-boundary regression test built directly from the reviewer's
  proof-of-concept. New dedicated `tests/test_client_dispatch.py` covers
  the parser and `GameClient.dispatch_many`/`dispatch` end-to-end via a
  fake session; `test_runner.py`/`test_group_coordinator.py` cover both
  dispatch loops' handling of a confirmed rejection.
- Verdict: keep-as-is

## app/domain/dispatch.py, app/core/account_config.py, app/core/runner.py, app/web/* — reviewed 2026-09-11, ad-hoc feature (Hilferuf share-everything mode)
- User asked: "add a 3rd option to hilferuf to share everything or do we
  already have such an option." Investigated: Hilferuf was a plain
  on/off toggle -- on shared only own missions genuinely uncrewable this
  cycle. No "share everything" option existed.
- Implementation: `HilferufAggressiveness(str, Enum)` -- OFF / UNCREWABLE
  (original ON behavior) / ALL (every own, not-yet-shared mission,
  regardless of crew status -- lets allies pile on for extra participation
  credit even on missions already handled fine). Follows the exact
  established precedent from `ParasiteAggressiveness`: legacy boolean
  `hilferuf` kept for migration only, new `hilferuf_mode` field added
  distinctly, `from_dict()` migrates `hilferuf: true` -> `hilferuf_mode:
  "uncrewable"` only when the new key is absent (explicit key always wins).
  `runner.py`'s `_hilferuf` branches on mode: UNCREWABLE keeps the original
  `plan.skipped`-filtered candidate list; ALL iterates every mission in
  `by_id` (every mission the account currently sees this cycle, already
  fetched before this call), same `is_alliance`/`self._shared` exclusions
  applied either way.
- Code review (async): APPROVE, zero findings across all 5 points checked
  (migration ordering/precedence, no orphaned old-boolean references
  anywhere in app/, ALL-mode candidate scope correctness, full
  backward-compatibility trace for both `hilferuf: true` and `false`/absent
  stored configs, and a broad search for any other `DispatchPolicy(hilferuf=
  ...)` construction site that might have been missed by the rename).
- Stability (1-5): 5 -- 4 new tests in test_account_config.py (migration,
  roundtrip, invalid-value fallback, explicit-key-wins-over-legacy) + 2 new
  tests in test_runner.py (ALL mode shares a fully-crewable mission too;
  ALL mode still excludes alliance missions). 379 tests pass.
- Verdict: keep-as-is

## app/domain/group.py, app/domain/dispatch.py, app/core/account_config.py, app/web/templates/group.html — reviewed 2026-09-11, feature parity (SMART order for group mode)
- User: "a lot of the settings seem missing from group mode, such as smart
  planning." Investigated: `AccountConfig.dispatch_policy()` fully supported
  SMART (solo `plan_dispatches`), but `GroupConfig.group_policy()` explicitly
  rejected `order=smart` and silently fell back to NEAREST (see the
  2026-09-11 SMART-order entry above) since `plan_group_dispatches` had no
  per-candidate requirement-resolution estimate to rank by.
- Implementation: `_smart_efficiency()` (new, group.py) -- same
  credits-per-vehicle-needed heuristic as solo's inline SMART branch, reusing
  `_outstanding()` (the SAME requirement derivation `action()` uses for the
  real dispatch decision, not a separate estimate) plus the same
  min_fire_personnel ceil-division top-up. A group-wide `avg_fire_crew` is
  computed once per cycle (mean `planning_personnel` across all firetruck
  types in the resolver's library) instead of solo's simpler constant, since
  group mode has no single "this account's fleet" to average over. Both the
  `begun_c` and `fresh_c` candidate lists now build 5-tuples
  `(dist, credits, mission, "begun"/"fresh", efficiency)` under SMART
  (3-tuples otherwise), consumed by the SAME shared `_order()` in
  dispatch.py solo already used -- no group-specific sort logic added.
  Removed `GroupConfig.group_policy()`'s SMART-reject guard; added the
  dropdown option to group.html.
- Also fixed in the same batch (found while investigating "the group
  dispatch log messages are weird"): `group_coordinator.py`'s per-account
  dispatch log line was missing the actual vehicle COUNT
  (`f"[#{aid}] {verb} → #{gmp.mission_id}..."` never interpolated
  `len(succeeded)`, unlike solo's equivalent line) -- fixed to
  `f"[#{aid}] {verb} {len(succeeded)} → #{gmp.mission_id}..."`. And
  `classify_log()` (web/app.py) only matched solo's un-bracketed log-line
  vocabulary (`"dispatched "`, `"sent to help "`, etc. as literal prefixes),
  so every group-mode line -- always prefixed `[#<id>] ...` -- fell through
  to the default/uncolored CSS class. Fixed by stripping a leading
  `[#<digits>] ` tag (`_ACCOUNT_TAG` regex) before running the existing
  classification checks, so solo and group lines share one vocabulary table
  instead of needing a duplicate bracketed copy.
- Code review (async): APPROVE, 1 MEDIUM (fixed before commit) + 1 LOW
  (acknowledged, not blocking). MEDIUM: the comment inside `_order()`'s
  SMART branch (dispatch.py) still claimed group mode "rejects SMART before
  ever calling here" and that the 5-tuple check was a "never expected to
  trigger" no-op -- both now false since group mode genuinely produces and
  consumes 5-tuples as its normal SMART path. Fixed by rewriting the comment
  to state group mode now also builds 5-tuples, mirroring solo's shape. LOW:
  solo's SMART `avg_fire_crew` is a per-account fleet average while group's
  is a library-wide average across every firetruck type that exists in the
  data (not just types any member actually owns) -- a pre-existing
  asymmetry in what "average" means between the two modes, immaterial in
  practice since the estimate only needs relative ranking, not an exact
  count (same reasoning already accepted for solo's HLF-derating no-op
  entry above); not fixed, flagged for awareness only.
- Stability (1-5): 4 -- 1 new test
  (`test_smart_order_beats_credits_high_when_pool_is_shared_in_group_mode`,
  mirrors solo's identical SMART-vs-credits_high scenario) plus the existing
  `test_smart_order_maps_through` (renamed from the old reject-test, now
  asserts SMART passes through instead of falling back). 382 tests pass.
- Verdict: keep-as-is
- Rationale: closes a real settings-parity gap (group mode's config page
  offered a SMART option that never should have been silently degraded to
  NEAREST once the reject guard was removed as part of this fix -- now it's
  simply gone, replaced by a real implementation) by reusing the exact same
  requirement-resolution and sort code solo dispatch already relies on,
  rather than building a second heuristic to maintain.

NOTE: commit only, per the established batching pattern -- no
push/merge/rebuild this round. Still open, awaiting explicit user direction
before building: (1) whether group-mode sharing/dispatching should run
concurrently across members (asyncio.gather) rather than sequentially, and
if so whether dispatch POSTs themselves should also parallelize across
accounts (trading away the "one mission fully staffed before moving on"
guarantee) or only the read/share phases; (2) whether other solo-only
settings (mission_source scope, parasite/top_up/patient_scope, per-source
credit bounds) should also be extended to group mode.

## app/core/group_coordinator.py, app/domain/group.py, app/web/app.py + dashboard.html — reviewed 2026-09-12, feature (group-mode concurrency rewrite + same-cycle backfill + per-account log view)
- User outlined a full concurrency redesign for group mode: concurrent
  per-member reads, concurrent sharing, concurrent dispatch with each
  account on its own delay/jitter, and an end-of-cycle "another account
  covers a failed send if possible" backfill pass. Also asked for a way to
  view either the compound (merged) log or one account's slice of it.
- Investigated two premises in the outline before building (per "don't
  guess live-game facts"): (1) whether missions have a separate cheap
  "list" endpoint vs. a richer "details" one -- confirmed NO: `/map/
  missions_json` (`fetch_missions()`) already returns full mission data
  (missing_text, patients, vehicle_state) in one call; `/missions/{id}`
  (the OTHER real endpoint the user asked about) exists but is only ever
  fetched transactionally, immediately before a dispatch POST, purely to
  grab a fresh CSRF token -- its body is otherwise discarded, never used as
  a data source. So "one random member fetches all details" was both
  infeasible (no such bulk endpoint) and undesirable (every member's own
  fetch is what builds `visible_to` -- the per-account gate that stops a
  mission being assigned to an account that can't act on it; centralizing
  the fetch would have silently broken that). (2) how literal "concurrent"
  should be, given this is a multi-accounting setup already flagged
  elsewhere as ban-risk if detected -- user confirmed "overlapping but
  individually jittered" over a literal simultaneous burst.
- Implementation: `GroupCoordinator.run_cycle` rewritten so every read
  phase (fleet fetch, first-pass mission fetch for sharing, the share
  itself, the post-share re-fetch, transport) launches one coroutine per
  account via a new `_gather_jittered()` helper -- each preceded by its own
  small random lead-in (`_lead_jitter()`, reusing the existing
  `dispatch_gap` dial as the jitter ceiling) so accounts overlap in wall
  time without firing in lockstep. Login (`connect()`) deliberately stays
  sequential and unstaggered -- it only ever happens ONCE per member for
  the group's whole lifetime (a no-op on every later cycle), so there's no
  recurring cost to leaving it alone, and it's the single most
  scrutiny-sensitive action an account takes. Dispatch itself was
  restructured from "one mission at a time, cycling through its senders"
  into one independent async worker per account (`_dispatch_account`),
  sequential within an account (own gap/jitter between sends) but
  concurrent across accounts via `asyncio.gather` -- an explicit,
  user-approved trade against the old "mission fully staffed before moving
  on" guarantee, justified because an interrupted cycle's shortfall is
  naturally re-derived and topped up by the next cycle's fresh replan, not
  lost. New `_backfill()` pass: every vehicle that failed this cycle
  (confirmed-rejected or plain-failed) gets ONE same-type substitute
  attempt from whichever member still has a free one, once every account's
  dispatch worker has finished.
- Web: `_tag_account_id()` (app.py) parses the existing `[#<id>]` group-mode
  prefix (already used by `classify_log` for coloring) back out to the real
  account a line concerns, since every group line lives under one shared
  `GROUP_KEY` pseudo-account bucket. The dashboard's compound log gained a
  `filter_id` per row and a dropdown (persisted in sessionStorage,
  mirroring the existing "maximise log" toggle) to show all accounts or
  just one. `_account_log()` also merges group-mode lines tagged to an
  account into that account's own `/accounts/{id}/details` page (previously
  invisible there entirely, since they never lived under that account's own
  log bucket), tag stripped since it's redundant on an already-scoped page.
- Code review (async): WARNING, 2 HIGH + 2 MEDIUM + 1 LOW, both HIGH fixed
  before commit. HIGH #1: `_backfill` never checked mission visibility
  before picking a substitute -- could dispatch a vehicle from an account
  outside `visible_to[mission_id]`, which is a silent no-op at best (the
  mission isn't in that account's own feed) and directly contradicted the
  module's own stated invariant. The reviewer proved it via the very test
  meant to cover backfill (`test_backfill_covers_a_failed_vehicle_with_
  another_accounts_same_type_vehicle`): the substitute account never
  actually had the mission in its feed, and only the fake client's
  not-modeling-visibility masked it. Fixed by threading `visible_to`
  through to `_backfill` and gating candidates on it (mirroring
  `plan_group_dispatches`'s own `_visible()` helper exactly); fixed the
  masking test to pre-share the mission so it's genuinely visible, and
  added `test_backfill_never_uses_a_vehicle_from_an_account_that_cannot_
  see_the_mission` as the regression the original test should have been.
  HIGH #2: `_run_account`'s catch-all `except Exception` around
  `_dispatch_account` discarded the WHOLE account's accumulated
  `(dispatched, failures, consumed)` tuple on any exception -- including
  every vehicle that account had ALREADY genuinely dispatched earlier in
  its own loop before the error, since `GameClient.dispatch_many`'s
  token-fetch `get_text()` call is unwrapped and can raise. Losing those
  ids from `consumed` opened a real double-dispatch window: `_backfill`
  could later pick an already-busy vehicle as a "substitute" and send it to
  a second, unrelated mission the same cycle. Fixed by moving exception
  handling down to per-mission scope INSIDE `_dispatch_account` (both the
  batch `dispatch_many` call and each per-vehicle retry's `dispatch` call
  now have their own try/except, logging and continuing rather than
  propagating) -- the outer `_run_account` catch is now documented as a
  last-resort net for a genuinely unexpected bug, not the expected-failure
  path. Added `test_exception_mid_account_dispatch_does_not_lose_earlier_
  consumed_ids`. MEDIUM (fixed): `building_pos`'s building_id->position
  merge was duplicated verbatim between `group_coordinator.py` and
  `plan_group_dispatches` -- extracted the shared
  `merge_building_positions()` helper into `app/domain/group.py`, called
  from both. MEDIUM (addressed via the two HIGH fixes' new tests): the
  reviewer's flagged test-coverage gaps were exactly the two HIGH bugs
  above, closed by the same tests that lock in the fixes. LOW (fixed): a
  stale `GroupPlan.missions` field comment in `group.py` still described
  the old "one mission at a time" sequencing the coordinator no longer
  does; reworded to describe the current per-account-worker priority
  ordering.
- Also proactively wrapped `_backfill`'s own `dispatch()` call in the same
  per-candidate try/except pattern (not explicitly flagged by the reviewer,
  same class of bug as HIGH #2) so one substitute's transient failure can't
  abort backfill for the rest of that cycle's failures list.
- Stability (1-5): 4 -- 389 tests pass (7 new: 2 backfill tests, the
  exception-isolation regression, `_tag_account_id`/`_account_log`
  unit tests, the dashboard filter_id integration test, the per-account
  details-page merge test). The concurrency restructuring itself has no
  live-account confirmation yet of actual wall-clock speedup or of the
  intended "overlapping but individually jittered" request pattern being
  visibly less bot-like than the old strictly-sequential loop -- both are
  inherently hard to verify without watching real traffic over several
  cycles; flagged for a first-live-run sanity check.
- Verdict: keep-as-is
- Rationale: both HIGH findings were genuine, reproducible correctness bugs
  in newly-added code (not stylistic), fixed with regression tests that
  fail on the pre-fix code and pass after; the DRY finding matches this
  org's own stated policy and was a trivial, safe extraction with no
  behavior change (confirmed via the unchanged 43-test group+group_
  coordinator suite passing identically before and after).

## app/game/models.py, app/domain/dispatch.py, app/core/account_config.py, app/web/app.py + account.html/details.html — reviewed 2026-09-12, ad-hoc feature (event-mission tag + mission_source multi-select)
- User asked: "can you make it that the bot can distinguish between my own,
  alliance shared (already does) and event." Clarified upfront: shared
  missions carry "[Verband]" in the caption per the user (though the code
  already detects alliance-ness structurally via `alliance_shared_at`, not
  caption text -- no change needed there), while event missions carry
  "[Event]" -- and "it can fall under my own or other ones depending on who
  buys/sets up the event," meaning EVENT is an independent, additive tag,
  not a third exclusive value alongside own/alliance. Asked what the
  distinction should actually change: user chose "mostly for the setting of
  what missions to work... a dropdown with multiselect" -- turning the
  existing single-choice `mission_source` (own/alliance/both) into a
  multi-select where a mission is worked if it matches ANY selected tag.
- Implementation: `Mission.is_event` (models.py) -- `"[Event]" in caption`.
  `MissionSource` (dispatch.py) dropped `BOTH`, gained `EVENT`.
  `_source_covers()` reworked to `(source: frozenset[MissionSource],
  is_alliance, is_event) -> bool`: `tag in source or (is_event and EVENT in
  source)`, where `tag` is OWN or ALLIANCE by `is_alliance` -- so selecting
  {own, event} covers own missions (any) AND alliance-shared events, but not
  plain alliance missions, exactly as the user described.
  `DispatchPolicy.mission_source` is now `frozenset[MissionSource]`
  (`field(default_factory=...)`, since a frozenset can't be a plain
  dataclass default) defaulting to `{OWN, ALLIANCE}` (same effective
  behavior as the old BOTH default). `AccountConfig.mission_source` is now
  `tuple[str, ...]`; `from_dict()` migrates an old plain-string value
  ("both"/"own"/"alliance") to the equivalent tuple, a new-format
  list/tuple passing through untouched; `dispatch_policy()` silently drops
  any entry that doesn't parse as a `MissionSource` rather than falling
  back to a default -- an all-empty/all-garbage selection deliberately maps
  to "normal dispatch works nothing" (a valid setting, e.g. a pure
  parasite/top-up account), not a silent default reinstatement. Web:
  `mission_source` form field is now `list[str] = Form(default=[])`;
  account.html's single `<select>` replaced with three checkboxes
  (own/alliance/event).
- Code review (async): APPROVE, 0 CRITICAL/HIGH, 1 MEDIUM + 2 LOW, all
  fixed before commit. MEDIUM: `details.html`'s Source row rendered the new
  tuple field with Jinja's bare `str()` (`('own', 'alliance')`, quotes and
  parens included) instead of following the sibling `allowed_categories`
  row's established `| join(', ')` + empty-case-label pattern in the SAME
  file -- fixed to match, with a new fallback label for the empty-selection
  case ("none (normal dispatch works nothing)"). LOW: `docs/MODES-TODO.md`'s
  inventory row for `mission_source` still described the old exclusive
  three-value shape -- reworded to describe the multi-select + the event
  tag's additive semantics. LOW: no in-UI signal when all three checkboxes
  are unchecked (a real but fully-intentional state per the code's own
  comments) -- added a conditional warning paragraph under the checkboxes,
  shown only when `cfg.mission_source` is empty.
- Also closed, proactively (not reviewer-flagged, found while fixing the
  MEDIUM above): neither `account.html` nor `details.html` was rendered by
  ANY existing test -- `test_config_save_roundtrips` and friends only POST
  to the config route, never GET the pages back, so a template syntax error
  in either would have shipped undetected. Added
  `test_account_config_page_renders_with_mission_source_checkboxes`,
  `test_account_config_page_warns_when_no_source_is_selected`, and
  `test_account_details_page_shows_mission_source_as_a_joined_list` to close
  that gap for both pages going forward, not just for this one field.
- Live-data note: the `"[Event]"` caption marker is owner-confirmed (not
  independently probed against a live account this session) --
  `docs/live-data-model.md` and `docs/MODES-TODO.md` both updated in the
  same pass so the finding isn't siloed in code comments alone, per the
  project's own verify-before-shipping convention.
- Stability (1-5): 5 -- 403 tests pass, including the exact scenario the
  user described end-to-end through `plan_dispatches`
  (`test_mission_source_own_plus_event_also_covers_an_alliance_event`), the
  event-tag-regardless-of-ownership case, the empty-selection-covers-
  nothing case, the legacy string migration for all three old values plus
  garbage, and the two new template-rendering regression tests above.
- Verdict: keep-as-is
- Rationale: a straightforward, well-scoped extension of an already-
  established enum/scope pattern (mirrors `ModeScope`'s own off/own/
  alliance/both shape, just multi-select instead of exclusive); the
  reviewer's findings were real but cosmetic (display formatting, stale
  doc, missing UI hint), not correctness bugs in the core `_source_covers`
  logic, which the reviewer traced through every `(is_alliance, is_event)`
  combination and found already correct.

NOTE: commit only, per the established batching pattern -- no
push/merge/rebuild this round without asking first.
