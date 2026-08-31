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
