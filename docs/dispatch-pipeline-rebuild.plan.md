# Dispatch pipeline rebuild — plan

**Tier: T3** (sized up from an initial T2 "merge two coverage paths" cleanup once the scope grew
to include a live correctness bug in the core delta math, a new automatic-recall feature, and
changed semantics for three existing modes — shared live-dispatch behavior where a mistake has
real in-game cost, several interacting pieces, no UI surface so the heavier `pr-review-fleet`
video-recording machinery doesn't apply). Adapted for a single-maintainer project: written
plan + acceptance + tracker, per-step implement → independent review (`code-reviewer` subagent,
`security-reviewer` only if a step touches auth/egress) → rework, full test suite before any PR.
No ledger/progress-page tooling, no recorded milestone walkthroughs (no UI changed) — the full
test suite plus a live dry-run read against the real account stands in for that.

## Provenance

Sasha, 26–27 Oct 2026, across several messages (quoted, not paraphrased):

- "Now lets get to the cleanup. Look for all decisions and their order / priority that happen
  between polling and missions and actually sending anything."
- On the dispatch-map artifact's findings: confirmed the `_priority_pass` / `_plan_one` split is
  real and live (`parasite_scope = "both"` today, verified against `data/blaulicht.db`).
- Reported two live symptoms: mission 4447662938 sent an HLF-Schiene and said "already covered"
  needing 2 LFs; mission 4447716684 (933/b) needed 2× ELW 1, sent 1× ELW2-Drohne, displayed
  "satisfied". Both traced to the delta/assigned staleness class of bug, not a double-counting bug
  in LSSM's own category data (independently verified against LSSM's upstream source).
- The target algorithm (his words, lightly reformatted, not altered):
  1. VGE: if it has revealed `missing_text`, treat like any other mission; if not, ignore (or send
     one spare if parasite mode is on).
  2. Top-up: only process missions with a revealed `missing_text`.
  3. Patient-only: unchanged (only missions with a patient; send only the required RD vehicles;
     recall once all patients treated/transported).
  4. Missions with revealed `missing_text`: try to fully satisfy each from the available pool; if
     not possible and partial sends are on, send what's possible; order by policy, SMART ranks by
     "how much credits-worth of missions can we turn green this cycle" (good-enough, not provably
     optimal).
  5. Missions with no revealed `missing_text` (nothing of ours there yet, or only en-route):
     handled the same way as 4, in the same pass.
  6. Finally: compare each mission's **S4 (on-scene)** vehicles against its requirement; recall
     surplus. Count other players' S4 vehicles too, but only ever recall our own. "This is
     extremely important to do right because if we over-recall we have a problem."
- Confirmed (quoted): "no need to subtract any S4 as they are already accounted for by the game" —
  and separately, that red missions never have an S3 vehicle, and a green mission's leftover S3
  vehicles can be recalled.
- On the missing-text-revealed signal: "missing text is live[sic], once its gone we have
  everything, if it shows back up we're missing something. How to solve that is yours to figure
  out, but it doesn't need to survive a restart."
- Recall cadence: "doesn't need to call every cycle. call it once the first time we see a mission
  green for that mission only and once every 15min otherwise."

## D0 — the headline finding: this closes a gap against our own written spec, not a new idea

[`docs/live-data-model.md:47-52`](../docs/live-data-model.md:47), written before this round of
conversation, already says:

> `missing_text` is "Am Einsatzort fehlend" — what's missing **at the scene**. It does **NOT**
> subtract vehicles already **on the way**. The true send list is
> **Noch benötigt = fehlend − Auf Anfahrt**.

[`_apply_delta`'s](../app/domain/dispatch.py:1344) own docstring says "vehicles already **en
route**" — the intent was always S3-only. But its input,
[`assigned_from_vehicles`](../app/domain/dispatch.py:352), collects every vehicle with
`target_type == "mission"` with no `fms` check — S3 and S4 together. So today, once
`missing_text` is revealed (already net of S4 per the game's own accounting), we subtract S4 a
**second time**, understating what's still needed. This is a real bug against the project's own
documented design, independent of everything else in this plan, and is likely the single biggest
contributor to the "says covered, isn't" symptom class chased earlier this session.

## Deviations (numbered, each a deliberate behavior change — not a bug being reintroduced)

- **D1 — VGE graduates once `missing_text` is revealed.** Today: a confidently-foreign VGE mission
  always gets exactly one free spare vehicle, forever, unconditionally (not gated on
  `parasite_scope` at all), regardless of `missing_text`. New: once `missing_text` is revealed,
  treat it like any other mission — full normal dispatch, no special one-vehicle cap. While
  unrevealed, send a spare **only if `parasite_scope` covers it** (today it's unconditional).
  **This removes the existing command-vehicle reservation implied by
  [`_VGE_MISSION_TYPE_IDS`'s](../app/domain/dispatch.py:98) original compliance comment** (the
  alliance's §4/§6 rule reserving ELW 1/2 and AB-Einsatzleitung for the Leitungsteam for 12h) —
  Sasha confirmed "adopt the new VGE logic" without a carve-out for those types, so a revealed VGE
  mission may now receive command vehicles like any other mission. Recorded here so it's visible
  and revisable, not silently dropped.
- **D2 — top-up's "has it begun" gate changes from sticky `vehicle_state` to live `missing_text`
  revealed.** Reason (Sasha): this automatically covers mission types the catalogue doesn't know
  about yet, since `missing_text` reveal doesn't depend on our own catalogue completeness.
- **D3 — SMART's ranking goal is restated, not necessarily changed.** Today: credits ÷
  vehicles-needed (a greedy value-density heuristic). Sasha's framing ("how much credits-worth of
  missions can we turn green this cycle... good enough is good enough") describes the SAME
  emergent goal the existing comment in `plan_dispatches` already claims for this heuristic. No
  rewrite of the formula is planned; Build Step 6 re-validates the existing heuristic against this
  explicit goal and only adjusts it if a concrete gap is found (documented if so).
- **D4 — a new, automatic, S4-only recall feature.** Does not exist today (`plan_cleanup` is
  manual-only and S3/S4-blind — see D6). New: for every mission, once (the first time it's
  observed green) and then at most every 15 minutes thereafter *per mission*, compare **our own
  S4 vehicles only** against the mission's current requirement (counting other players' S4
  vehicles toward the total, but only ever recalling ours) and recall surplus. State is in-process
  only (a dict of `mission_id -> last_checked_epoch` plus a `fired_once` set) — explicitly does
  not need to survive a restart (Sasha's words).
- **D5 — the vehicle-class delta becomes scope-aware.** Against a `missing_text`-revealed
  requirement (already net of S4): subtract **S3 only**. Against the catalogue-fallback
  requirement (mission never revealed anything, raw total): subtract **S3+S4 together**, unchanged
  from today. The casualty-driven layer (`_casualty_counts`: ambulances, NEF, rescue helicopter,
  police cars, `kdow_lna`/`kdow_orgl`) is **not** touched by this — `patients_count`/
  `prisoners_count` reflect actual remaining casualties, not vehicle progress, so neither an S3
  nor an S4 vehicle changes that count on its own; the existing S3+S4 subtraction there stays
  correct and unchanged. See Contract below for how a `Requirement` carries which rule applies.
- **D6 — `plan_cleanup` (the manual cleanup button) is superseded, not fixed in place.** It has
  the same S3/S4-blindness this plan fixes elsewhere (its minimal-covering-subset selection draws
  from `target_type == "mission"` vehicles with no `fms` filter). Rather than patch two recall
  implementations in parallel, the manual `cleanup()` action is rewired to call the same new S4-only
  reconciliation core the automatic recall uses (Build Step 7), on demand, for every mission
  (ignoring the 15-minute/first-green cadence — a manual click always runs the check).
- **D7 — `_priority_pass`'s inline coverage path is retired in favour of routing through
  `_plan_one`.** (This is the ORIGINAL scope of the round, still in: see the dispatch-pipeline-map
  artifact's §7 "Asymmetries" for the four concrete gaps this closes — Rule 1 coverage, SEG-ELW
  preference, fire-personnel top-up, and the water-rescue escort all currently missing from
  parasite/top-up/patient reinforcement sends.) All of `_plan_one`'s rules now apply uniformly,
  including `max_vehicles_per_mission` and the opportunistic escort — confirmed by Sasha
  ("adopt uniformly").
- **D8 (ruling, mechanical)** — the now-dead "awaiting arrival" relabel in
  [`runner._mission_breakdown`](../app/core/runner.py:339) is removed (confirmed by Sasha): Rule 1
  made the case it was built for structurally unreachable.
- **Out of scope this round (confirmed):** `app/domain/group.py` (the parallel alliance/group
  dispatch path) is not touched, even though it may share some of these issues — flagged as a
  separate follow-up. Wiring the unused `app/domain/transport.py` routing engine into the live
  transport path is also out of scope (unrelated finding, surfaced in passing) — to be raised as
  its own follow-up. The Abrollbehälter-causes-FMS5 claim could not be grounded in code, tests, or
  docs; no action taken without a concrete live example.

## User-visible behaviour

- The shortfall log and mission-breakdown panel should show fewer false "already covered"/"fully
  satisfied" missions that are actually still short (D0/D5's fix).
- VGE missions with real, revealed needs get properly crewed instead of one token vehicle (D1).
- Vehicles are no longer left idling on a mission that's already green, or piled onto one beyond
  what it needs — both corrected automatically, without a manual cleanup click (D4/D6).
- No visible change intended for patient-only mode, for the main batch/partial-send mechanics, or
  for SMART's actual dispatch outcomes (D3 is a restatement, not a rewrite, pending Step 6).

## Contract

**`Requirement`** (`app/domain/requirements.py`) gains one field:

```python
delta_scope: str = "full"   # "full" (subtract S3+S4) | "en_route_only" (subtract S3 only)
```

Default `"full"` preserves every existing caller (catalogue-fallback path, casualty-count layer,
patient-only mode, the regex tertiary fallback) with zero behaviour change. Only
`resolver.vehicle_requirements`'s `missing_text`-revealed branch sets `"en_route_only"`.

**`assigned_from_vehicles`** (`app/domain/dispatch.py`) is replaced by two functions returning the
same `dict[int, list[int]]` shape, split by `fms`:

```python
def en_route_from_vehicles(vehicles) -> dict[int, list[int]]: ...   # fms == 3, target_type == "mission"
def on_scene_from_vehicles(vehicles) -> dict[int, list[int]]: ...   # fms == 4, target_type == "mission"
```

`assigned` (today's combined dict, used wherever `delta_scope` doesn't matter — e.g. casualty
counts, `_reserve_vehicle_group`, patient-only mode) becomes `_merge(en_route, on_scene)`, built
once per cycle, so every existing non-vehicle-class caller is unaffected.

**`_apply_delta`** signature becomes:

```python
def _apply_delta(requirements, en_route, on_scene, mission_id) -> list[Requirement]:
    # per requirement: pick en_route if delta_scope == "en_route_only", else en_route + on_scene
```

**Missing-text "ever revealed" memory** (`MissionRequirementResolver`, mirrors the existing
`_ever_revealed` set): a new in-process-only `_missing_text_ever_revealed: set[int]`, exposed as
`missing_text_ever_revealed(mission) -> bool`. `vehicle_requirements` becomes:

```
if groups.revealed:               # currently showing a parseable blob
    record mission id as ever-revealed; resolve from groups (as today), delta_scope="en_route_only"
elif missing_text_ever_revealed(mission):  # was revealed before, now gone -> done
    return [], []                 # no leftover requirement; NOT the catalogue fallback
else:                              # never revealed -> untouched, use catalogue
    resolve from mtype.requirements/chances (as today), delta_scope="full"
```

**New recall core** (`app/domain/dispatch.py`, name TBD at implementation, e.g.
`plan_reconciliation`): takes `missions`, `vehicles`, `resolver`, a `last_checked: dict[int,
float]` and `fired_once: set[int]` (owned by the caller — `DispatchRunner`, in-process, not
DB-backed), `now: float`. For each mission: if not yet in `fired_once` and `status == "green"`, OR
`now - last_checked.get(mid, 0) >= 900`, build the mission's current requirement, count **every**
player's S4 vehicles against it (own + others, via the same `target_type/target_id/fms` fields —
other players' vehicles are visible in the feed but not ours to recall), and if our own S4 count
exceeds what our own S4 vehicles are still needed for, recall the surplus (least-useful first —
exact tie-break TBD at implementation, e.g. farthest-from-home or most-redundant-type). Also
recalls our own S3 vehicles still en route to an already-green mission (per Sasha's explicit
"green mission... all S3 can be stopped"). Returns `list[(vehicle_id, mission_id, reason)]` for
the runner to execute and log, mirroring `plan_cleanup`'s existing return shape.

## Architecture rules

- **No parallel "what to send" implementations.** `_priority_pass` must not re-derive coverage
  inline (D7) — every send decision goes through `_plan_one` (or the new reconciliation core for
  recalls). Enforced by review, not a lint rule (too small a codebase to justify tooling for this).
- **`delta_scope` lives on the `Requirement`, not as an out-of-band flag threaded through call
  sites.** Keeps `_apply_delta` a pure function of its inputs and keeps the scope decision exactly
  where it's made (the resolver, which is the only thing that knows which source produced a given
  requirement).
- **The recall core never mutates state outside its return value.** `plan_reconciliation` (like
  `plan_cleanup` before it) only computes; `DispatchRunner` executes the actual recalls and updates
  `last_checked`/`fired_once`, so the core stays unit-testable without a fake client.

## Tests

Command: `python -m pytest tests/ -x -q` (verified runnable from the worktree root; project's
existing convention — area-scoped runs while building, full suite before the PR per CLAUDE.md).

- **D0/D5 (the delta fix)** — the highest-stakes test in this plan. Hand-computed cases:
  revealed `missing_text` showing "1 ELW 1 missing" + one S4 ELW1 already on scene + one S3 ELW1
  en route → must NOT re-send (fully covered: missing_text already nets S4, S3 covers the rest).
  Same setup but the S3 ELW1 is actually a DIFFERENT, unrelated vehicle incorrectly recorded as
  targeting this mission (the original stale-assigned symptom) → must re-send per Rule 1's
  existing red-bypass, now for the right reason. Catalogue-fallback case (never revealed) with one
  S3 and one S4 vehicle already assigned → both still subtracted from the raw total, unchanged
  behaviour, regression-guarded.
- **Missing-text "ever revealed, now gone" case** — a mission that WAS revealed (categories
  non-empty), now shows no `missing_text` at all (reverted/absent) → treated as fully covered, not
  re-derived from the catalogue. A mission that was NEVER revealed and still shows nothing → still
  uses the catalogue fallback (regression guard against conflating the two "not revealed" states).
- **D1 (VGE)** — revealed VGE mission dispatches normally (including command vehicle types, no
  special cap); unrevealed VGE mission with `parasite_scope` off sends nothing; same mission with
  `parasite_scope` on sends exactly one spare.
- **D2 (top-up)** — a mission with revealed `missing_text` but `vehicle_state` still 0 (a type the
  catalogue doesn't know) is now eligible for top-up; a mission with `vehicle_state >= 1` but no
  revealed `missing_text` is not (regression guard that the OLD signal is actually replaced, not
  additionally required).
- **D4/D6 (recall)** — mission just turned green this cycle (not in `fired_once`) with surplus S4
  → recalls on first sight. Same mission, still green, checked again 5 minutes later → no recall
  (cadence respected). Checked again at 16 minutes → recall re-evaluated. A green mission with an
  S3 vehicle still approaching → that vehicle recalled. **Mutation-check this one explicitly**
  (CLAUDE.md's money-path rule): break the "only ours" filter and confirm a test fails — this is
  the single highest-consequence line in the whole plan.
- **D7 (priority-pass unification)** — the four gap tests already scoped in the dispatch-map
  artifact: parasite send on a red mission now gets Rule 1; top-up reinforcement tops up the
  firefighter minimum; a heavy-casualty top-up reinforcement gets the SEG-ELW preference; a
  parasite-sent MZB gets its water-rescue escort.
- Full existing suite (494 tests as of this plan) stays green — explicit regression gate.

## Risks and non-goals

- **Highest risk: the recall feature over-recalling a genuinely-needed vehicle** (Sasha's own
  words: "if we over-recall we have a problem"). Mitigated by: S4-only counting (never touches an
  en-route vehicle's progress toward requirement satisfaction except the explicit green-mission-S3
  case, which is a distinct, narrower rule), the 15-minute/once-on-green cadence (not every cycle),
  counting other players' S4 vehicles before computing our own surplus, and the explicit
  mutation-check test above.
- **D1's removal of the Leitungsteam command-vehicle reservation** is a real compliance-adjacent
  behaviour change, recorded so it's easy to revert if it causes a real-world issue with the
  alliance.
- **Non-goal:** rewriting SMART's actual ranking formula (D3) unless Step 6 finds a concrete case
  the existing heuristic gets wrong against the stated goal.
- **Non-goal:** `app/domain/group.py`, `app/domain/transport.py` wiring, and the
  Abrollbehälter/FMS5 claim — all explicitly deferred (see Deviations' "out of scope" note).

## Open questions

None blocking — every judgment call above was either ruled on by Sasha directly or recorded as a
small, reversible, delegated decision (D3's "no rewrite unless a gap is found," the S4/en-route
tie-break for recall ordering, the fms==5-while-still-targeting-the-original-mission edge case,
treated as "already arrived, counts toward satisfaction" since the vehicle has already done its
job at that scene — ASSUMPTION, safe reading, flagged here for visibility rather than blocking).
