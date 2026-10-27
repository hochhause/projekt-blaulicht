# Dispatch pipeline rebuild — tracker

One commit per task, checkbox ticked in the same commit. No parallel tracks (single surface, one
file family: `app/domain/dispatch.py`, `app/domain/resolver.py`, `app/domain/requirements.py`,
`app/core/runner.py`). No ledger/progress-page tooling for this round (see plan header) — progress
is this file's checkboxes plus the commit log.

## Ordering constraints

- M1 before M2/M3: everything downstream depends on `Requirement.delta_scope` and the
  en-route/on-scene split existing first. Getting this wrong means redoing the plumbing in M2/M3's
  code, not just a test fix.
- Within M1: task 1 (the field) before task 2 (resolver wiring) before tasks 3-4 (dispatch.py
  split + `_apply_delta`) before task 5 (call-site plumbing) — each depends on the previous one's
  types existing.
- M3 (recall) last among the feature milestones, deliberately: it's the highest-risk piece
  (Sasha's explicit "if we over-recall we have a problem"), and should be built against an already
  stable, already-tested M1+M2 so a recall bug is never masked by a requirement-computation bug.
- M4's full-suite run and by-hand check are the PR gate — nothing merges before both are green.

## Models table

| Step(s) | Failure mode | Model/effort | Why |
| --- | --- | --- | --- |
| 1 (Requirement field) | Loud-if-wrong (typecheck/import) | sonnet/low | Mechanical dataclass field |
| 2 (resolver 3-way branch) | Silent-if-wrong — wrong branch quietly mis-derives a requirement | opus/high | Core correctness, the plan's headline fix |
| 3 (en_route/on_scene split) | Silent-if-wrong — a missed caller keeps using the old combined semantics unnoticed | opus/high | Touches every existing caller of `assigned_from_vehicles` |
| 4 (`_apply_delta` scoping) | Silent-if-wrong | opus/high | Money-path: wrong scoping = wrong dispatch, wrong recall |
| 5 (call-site plumbing) | Loud-if-wrong once 2-4 are right (missed site = test failure) | sonnet/medium | Mechanical once the core functions exist |
| 8-9 (D1/D2 rule changes) | Loud-if-wrong (acceptance tests) | sonnet/medium | Narrow, well-specified behavior changes |
| 10 (D7 unification) | Silent-if-wrong — a missed semantic (allow_partial, scarce-type pool, skip_patient_contribution) silently changes parasite/top-up behavior | opus/high | The architectural core of the original round's scope |
| 12-13 (recall core + wiring) | Silent-if-wrong, highest real-world cost | opus/high + adversarial review | "If we over-recall we have a problem" |
| 16 (SMART validation) | Silent-if-wrong only if a real gap exists | sonnet/medium to build the fixture, opus/high to judge the result | Judgment call on "good enough" |
| 17, 19 (dead code, docs) | Loud-if-wrong | haiku/sonnet, low | Mechanical |
| 21 (review gate) | — | opus (code-reviewer), security-reviewer only if a step touched auth/egress (none do) | Established project pattern |

## Milestone 1 — the delta fix + missing-text memory

- [x] 1. Add `delta_scope: str = "full"` to `Requirement` ([requirements.py](../app/domain/requirements.py)).
- [x] 2. Add `_missing_text_ever_revealed` set + `missing_text_ever_revealed(mission)` to
      `MissionRequirementResolver` ([resolver.py](../app/domain/resolver.py)); restructure
      `vehicle_requirements`'s branch into the three-way revealed / ever-revealed-now-gone / never-
      revealed shape from the plan's Contract section, tagging `delta_scope` accordingly.
- [x] 3. Add [`en_route_from_vehicles`](../app/domain/dispatch.py:352) alongside (not replacing)
      `assigned_from_vehicles` (filtered on `fms == 3`, `target_type == "mission"`). Design note
      (differs from the plan's original sketch): no separate `on_scene_from_vehicles`/`_merge` was
      needed in M1 — `_apply_delta`'s `"full"` branch keeps using the existing combined `assigned`
      dict directly (it's already the S3+S4 union `_apply_delta` wants for that scope); on-scene-
      only (`fms == 4`) extraction is deferred to M3, which is the first place that actually needs
      S4 in isolation (the recall core).
- [x] 4. Update [`_apply_delta`](../app/domain/dispatch.py:1344) to take `(requirements, en_route,
      assigned, mission_id)` and branch per-requirement on `delta_scope` (`"en_route_only"` ->
      `en_route`, `"full"` -> `assigned`).
- [x] 5. Update every IN-SCOPE call site (`plan_dispatches`'s reservation call, its SMART-efficiency
      estimate, both `_plan_one` calls, `_plan_one`'s own Rule-1 branch) to pass `en_route` through.
      `_outstanding` and `_reserve_vehicle_group` keep their original single-`assigned`-shaped
      public signature where only `app/domain/group.py` still calls them (out of scope this round)
      — `_reserve_vehicle_group` gained one positional `en_route` param since dispatch.py's own two
      call sites need the real split; group.py's one call site was updated to pass its existing
      `global_assigned` dict as both `en_route` and `assigned` (one line, behaviour-preserving by
      construction — verified: `test_group.py` unchanged, all green). `_outstanding` needed no
      signature change (its only remaining caller is group.py).
- [x] 6. Wrote AC-M1-01 through AC-M1-07 directly as passing tests (not stub-first — see plan
      header's note on adapting the skill's ceremony for a solo build); one existing test
      (`test_reservation_suppressed_when_assigned_already_covers_the_need`) needed updating since
      its synthetic `assigned`-only fixture no longer disambiguates en-route from on-scene.
- [x] 7. `python -m pytest tests/ -q --ignore=tests/test_web.py` — 501 passed, 12 subtests passed
      (494 pre-existing + 7 new). `tests/test_web.py` fails to even collect on a pre-existing,
      unrelated environment issue (installed `pydantic-core` version mismatch) — confirmed
      unrelated to this change (a raw import-time `SystemError`, nothing in this diff touches
      `app/web/`). Independent review: pending (see Milestone 2 — reviewing both together).

## Milestone 2 — VGE / top-up / priority-pass unification

- [ ] 8. D1: VGE branch in `_priority_pass` checks `missing_text` revealed → falls through to
      normal treatment; unrevealed → spare send gated on `parasite_scope` (not unconditional).
- [ ] 9. D2: top-up's "not begun" gate in the main loop switches from `effective_count_revealed` to
      the new missing-text-revealed memory (task 2).
- [ ] 10. D7: route `_priority_pass`'s on_it_here/is_parasite/patient_here coverage decision
      through `_plan_one` (preserving each caller's `allow_partial`/`patient_only`/scarce-type-pool
      semantics exactly — see the dispatch-pipeline-map artifact's Fig. 2 for the current shape
      being replaced); VGE's own spare-claim and `skip_patient_contribution` stay as direct,
      non-`_plan_one` paths (they never claim to satisfy a requirement).
- [ ] 11. Write + pass AC-M2-01 through AC-M2-09.
- [ ] 12. `python -m pytest tests/test_dispatch.py -x -q` green; independent review.

## Milestone 3 — automatic recall

- [ ] 13. Design and implement the reconciliation core (dispatch.py): S4-only counting (ours +
      others), surplus-our-S4 recall, "stop our S3 on an already-green mission."
- [ ] 14. Wire into `DispatchRunner.run_cycle` ([runner.py](../app/core/runner.py)): in-process
      `last_checked: dict[int, float]` + `fired_once: set[int]`, first-green-or-15-minutes cadence.
- [ ] 15. Rewire `cleanup()` to call the same core unconditionally (every mission, no cadence);
      remove `plan_cleanup`'s old S3/S4-blind implementation once the new core covers its cases.
- [ ] 16. Write + pass AC-M3-01 through AC-M3-06, including the explicit mutation check for
      AC-M3-05 (remove the "ours only" filter, confirm the test fails, put the filter back).
- [ ] 17. `python -m pytest tests/test_dispatch.py tests/test_runner.py -x -q` green; independent,
      adversarial review specifically targeting over-recall scenarios (opus, explicit prompt: "try
      to find an input that causes a needed vehicle to be recalled").

## Milestone 4 — SMART validation, cleanup, full regression, PR

- [ ] 18. Build the SMART validation fixture (AC-M4-01); compare against the theoretical best
      combination; document the result in the plan (confirm unchanged, or report a gap to Sasha
      before changing anything).
- [ ] 19. Remove the dead "awaiting arrival" relabel + its test (D8, AC-M4-02).
- [ ] 20. Update `WORKFLOW.md` with this round's changes (closing the gap left after the Rule 1
      hotfix, where this was skipped under an explicit scope limit).
- [ ] 21. Full suite: `python -m pytest tests/ -x -q`, record real output + exit code (AC-M4-03).
- [ ] 22. By-hand live check against the real account (AC-M4-04) — pause, run one cycle, compare
      shortfall/mission-breakdown before/after for a previously-affected mission if still open.
- [ ] 23. `code-reviewer` subagent pass over the full diff; address required changes.
- [ ] 24. Open the PR: human-readable body (what/why/what to watch), dense evidence (test output,
      the by-hand check's findings) in a follow-up comment, link this plan.

## Follow-ups (non-blocking, collected during the build)

- `app/domain/group.py` may share some of this plan's issues (duplicated coverage logic,
  S3/S4-blindness) — not mapped this round.
- `app/domain/transport.py` is a complete, tested routing engine never wired into the live
  transport path (`_handle_transports` just takes the game's first offered target) — surfaced in
  passing, unrelated to this plan.
- The Abrollbehälter-causes-FMS5 claim is unverified (no code/test/doc support found) — needs a
  concrete live example before any action.

## Out of scope (acknowledge-only, do not re-open mid-build)

- `app/domain/group.py`, `app/domain/transport.py` wiring, Abrollbehälter/FMS5 (see Follow-ups).
- Any change to `partial_starts`/`batch_size`/`max_vehicles_per_mission` default VALUES — this
  plan changes which rules apply where, not any policy default.
