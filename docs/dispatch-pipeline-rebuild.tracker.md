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

- [x] 8. D1: VGE branch in `_priority_pass` checks `missing_text` revealed → falls through to
      normal treatment; unrevealed → spare send gated on `parasite_scope` (not unconditional).
- [x] 9. D2: top-up's "not begun" gate in the main loop switches from `effective_count_revealed` to
      the new missing-text-revealed memory (task 2).
- [x] 10. D7: routed `_priority_pass`'s on_it_here/is_parasite/patient_here coverage decision
      through `_plan_one`. `allow_partial = is_parasite or patient_here` reproduces the old
      `allow_send` rule exactly; a plain top-up miss (allow_partial=False) is deliberately left
      un-handled so it still falls through to the main loop's second attempt (unstick-first
      eligible there) — its shortfall is only tallied once (`shortfalls=None` passed for that one
      case) to avoid double-counting when the second attempt also misses. VGE's own spare-claim and
      `skip_patient_contribution` stay as direct, non-`_plan_one` paths, unchanged.
- [x] 11. Wrote + passed AC-M2-01 through AC-M2-09 (9 new tests). Five pre-existing tests needed
      updating for D1/D2's deliberate behaviour changes: `test_vge_mission_gets_only_a_single_spare_vehicle`
      split into three (unrevealed+parasite-off, unrevealed+parasite-on, revealed-graduates) since
      its one scenario no longer covers the now-three-way VGE behaviour; four top-up tests needed a
      `missing_text` added (they relied on `vehicle_state` alone, the signal D2 replaces) and their
      expected shortfall-key strings changed from catalogue class names ("police_cars") to LSSM
      category names ("FuStW") once missing_text became the requirement source.
- [x] 12. `python -m pytest tests/ -q --ignore=tests/test_web.py` — 507 passed, 12 subtests passed.
      Independent review: pending (deferred to before the PR, covering M1+M2+M3 together — see
      Milestone 4).

## Milestone 3 — automatic recall

- [x] 13. Design and implement the reconciliation core (dispatch.py): S4-only counting (ours +
      others), surplus-our-S4 recall, "stop our S3 on an already-green mission." **Design note,
      significant deviation from the plan's original Contract sketch**: the Contract assumed other
      players' vehicles are visible in the same `vehicles` feed ours come from — FALSE, confirmed in
      code (`GameClient.fetch_vehicles`'s own docstring: "Your **entire** fleet") and live
      (Sasha logged the bot into a browser session, 2026-10-27, to check). The combined "everyone's
      on-scene" count instead comes from a NEW `GameClient.fetch_on_scene_vehicles(mission_id)`,
      parsing the mission detail page's `#mission_vehicle_at_mission` table (live-verified: real
      vehicle id + `vehicle_type_id` per row, server-rendered not AJAX — see
      [client.py](../app/game/client.py)'s `_parse_on_scene_vehicles`/`_ON_SCENE_ROW`). A mission
      absent from that fetch (not due, or the fetch failed) gets NO S4 check at all — never treated
      as "confirmed surplus" (see `plan_reconciliation`'s docstring). `select_coverage` is reused
      over the combined (ours + others, real ids) pool with a custom `distance_of` bias that prefers
      keeping non-owned vehicles as "needed" first, so our own surplus is what gets freed —
      intentionally conservative (may miss a safe recall if the tie-break goes the other way; never
      over-recalls). Only a vehicle id BOTH our own fleet AND the fetched roster confirm present is
      ever a recall candidate (`verified_our_ids`) — a parsing miss or stale fetch for one of our own
      vehicles simply excludes it, never defaults it to "surplus".
- [x] 14. Wire into `DispatchRunner.run_cycle` ([runner.py](../app/core/runner.py)):
      `RecallCadence` (dispatch.py) replaces the plan's raw dict+set sketch — same semantics
      (`last_checked`/`fired_once`), owned by `AccountRunner` like `StrikeTracker`/`AlarmTracker` so
      a reconnect doesn't reset it. `due_for_reconciliation` is a separate pure function (not
      folded into the core) so the runner knows which missions to spend an extra page-fetch on
      BEFORE fetching. **Bug caught by testing, fixed in `due_for_reconciliation`**: the "never
      checked" default must be `now` itself, not `0.0` — a 0.0 sentinel made the 15-minute-elapsed
      branch trivially true for ANY real epoch `now` on a mission's very first sighting, firing an
      immediate check for every non-green candidate instead of only once-on-green.
- [x] 15. Rewired `cleanup()` to call `plan_reconciliation` unconditionally (every mission we have a
      vehicle on, no cadence gate) — `plan_cleanup` was fully deleted (confirmed unused anywhere
      else) along with its 6 dedicated tests, rewritten as `plan_reconciliation` equivalents plus new
      coverage for the roster/ownership edge cases the old function never had to consider.
- [x] 16. AC-M3-01 through AC-M3-06 covered across `tests/test_dispatch.py` (`plan_reconciliation`
      unit tests + `DueForReconciliationTests`), `tests/test_runner.py` (automatic-pass wiring +
      cleanup rewiring), `tests/test_client_dispatch.py` (`_parse_on_scene_vehicles` +
      `fetch_on_scene_vehicles`). **Mutation check performed and recorded**: changed the recall
      loop's `for vid in verified_our_ids:` to `for vid in {p.id for p in pool}:` (iterating every
      on-scene id, not just verified-ours) — the first test scenario written did NOT catch this (a
      real gap: the scenario could never produce a non-owned "surplus" id in the first place, since
      the ownership bias always kept a non-owned vehicle "needed" when there was only one of each).
      Rewrote `test_reconciliation_never_recalls_another_players_vehicle` with 3 on-scene LFs (2
      others' + 1 ours, mission needs 1) so a non-owned vehicle genuinely ends up marked surplus;
      confirmed the mutation now fails it, reverted the mutation, confirmed green again.
- [x] 17. `python -m pytest tests/ -q --ignore=tests/test_web.py` — 548 passed, 12 subtests passed.
      Independent adversarial review: pending, folded into Milestone 4's `code-reviewer` pass.

## Milestone 4 — SMART validation, cleanup, full regression, PR

- [x] 18. SMART validation (AC-M4-01): `test_order_smart_beats_credits_high_when_pool_is_shared`
      (tests/test_dispatch.py, pre-existing) already IS the theoretical-best-combination fixture the
      plan asked for — the classic knapsack counter-example (one expensive vehicle-hungry mission vs.
      two cheaper ones worth more combined), confirming SMART picks the combination worth MORE total
      credits (190 > 150), not just the single highest-credit mission (what `CREDITS_HIGH` picks
      instead, confirmed by the same test). No gap found against "maximize credits-worth of missions
      turned green this cycle" — no rewrite, per the plan's own D3 ruling.
- [x] 19. Removed the dead "awaiting arrival" relabel (`app/core/runner.py::_mission_breakdown`) —
      confirmed no dedicated test still existed for it (only a stale comment reference, fixed to point
      at `test_red_mission_ignores_stale_assigned_tracking_and_re_requests_in_full`, which already
      proves the red+stale-assigned case now correctly resolves to "not enough vehicles").
- [x] 20. Updated `WORKFLOW.md`: superseded the stale §4/§6 Leitungsteam-reservation entry (D1 removed
      it for a revealed VGE mission) and added a new top-level log entry for this whole round.
- [x] 21. Full suite: `python -m pytest tests/ -q --ignore=tests/test_web.py` — 548 passed, 12
      subtests passed. `tests/test_web.py` still excluded (pre-existing, unrelated pydantic-core
      version mismatch in this environment — confirmed unrelated again, no `app/web/` diff this
      round touches FastAPI's own import chain; the Jinja template addition was verified separately
      by rendering it directly with `jinja2.Environment`, both the populated and empty-state paths).
- [x] 22. By-hand live check against the real account (AC-M4-04) — read-only: fetched real missions/
      vehicles/mission-detail pages via an authenticated browser session (no `recall()`/write calls
      made), then ran the real, unmodified `_parse_on_scene_vehicles` regex against genuinely live
      HTML. **Caught a real bug this way, not by inspection**: the `#mission_vehicle_at_mission`
      table's name is misleading — it also lists a vehicle actively transporting a patient away
      (FMS 5, confirmed on a real mission: an NAW mid-transport), not just FMS 4. Counting that
      vehicle toward "everyone's on-scene coverage" could have misjudged one of OUR genuinely-still-
      needed vehicles as surplus once it left. Fixed by parsing each row's own
      `building_list_fms_N` span and filtering to FMS 4 only (`app/game/client.py`) — re-verified
      against the same live page afterward: the FMS-5 NAW is now correctly excluded, the 3 genuinely
      on-scene vehicles are kept.
- [ ] 23. `code-reviewer` subagent pass over the full diff; address required changes.
- [x] 24. No PR: Sasha's explicit instruction this round — push straight to `master` himself (via the
      session), then redeploy the running container directly. Superseded by that decision.

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
