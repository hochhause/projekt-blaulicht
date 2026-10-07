# Build workflow — how to execute TODO.md

This file is the **process manual** for working through `TODO.md` P0→P4 in one
continuous iterative session: build a task, review it, test it, commit it,
move to the next. `TODO.md` (+ the docs it cites) still owns *what* to build
and *why* — this file only owns *how to work* and the standing rules that
apply to every single task, so they don't need to be re-stated per task.

**Read this file in full before starting. Re-read it if your context gets
summarized/compacted mid-run — it's your memory of the rules, and `TODO.md`'s
checkboxes are your memory of progress.**

---

## 0. Non-negotiables

- **ToS risk.** Automating this game violates its terms of service (see
  README.md's warning). Never run a script that could DISPATCH, SEND, share,
  or otherwise mutate state against a **real, non-throwaway** account. The
  read-only inspection tools (`scripts/probe_*.py`,
  `scripts/inspect_live_playwright.py`) are for a throwaway account only.
- **Verify, don't guess — in this order:**
  1. The repo's own docs first: `docs/live-data-model.md`,
     `docs/lssm-requirement-mapping.md`, `docs/MODES-TODO.md`,
     `data/reference/api-endpoints.md`, `data/reference/*.json`. These already
     encode a lot of hard-won live verification — read the relevant one fully
     before writing code against an assumption.
  2. If still uncertain and it's a live-game behavior question, use the
     read-only probes. They need `BLAULICHT_EMAIL` / `BLAULICHT_PASSWORD` env
     vars for a throwaway account. Check for them first.
  3. If (1) and (2) both dead-end — no doc covers it, no probe can confirm it,
     no credentials available — **stop that specific task**, log it under
     "Blocked — needs Sasha" below (don't just skip silently), and move on to
     the next task. Don't ship a guessed field name or assumed game-setting
     state as if it were verified. Cite which doc or probe output confirmed
     anything you mark as confirmed.
- **Architecture.** `app/domain/` stays dependency-free — no `httpx`/framework
  imports; it's what makes the test suite run in ~0.1s. Network I/O only in
  `app/game/`. Dispatch is one vehicle per POST, deliberately (a live-capture-
  confirmed shape) — don't "optimize" it into a combined POST without a new
  capture proving it works.
- **Git.** Commit each finished task separately with a clear message — don't
  batch multiple TODO items into one commit. Do **not** push or merge to main
  without asking Sasha first.

---

## 1. The loop (repeat per task)

1. **Pick the next task.** Work `TODO.md` top to bottom: P0 → P1 → P2 → P3 →
   P4, in the order items appear. Skip anything already checked `[x]` or
   marked already-resolved/done (e.g. P0.2, P4's "already resolved" note).
2. **Build it.** Implement against the verification rules in §0. Extend the
   existing stdlib `unittest` suite for anything touching `app/domain/`.
3. **Review it.** Before committing, dispatch the `code-reviewer` subagent
   (via the `Agent` tool) against your diff. If the task touched
   `app/core/secrets.py`, session/auth (`app/game/session.py`), or anything
   handling the game credentials, also dispatch `security-reviewer`. Address
   what comes back — don't commit past a real finding without either fixing it
   or writing down why it doesn't apply.
4. **Test it.** `python -m unittest discover -s tests -t . -v` must pass in
   full, not just the new test. Fix or revert before moving on if it doesn't.
5. **Log code quality.** Append an entry to `docs/CODE_QUALITY_NOTES.md`
   (create it if missing; **append**, never overwrite prior entries) for every
   module you touched or read closely this task:

   ```
   ## <module path> — reviewed <YYYY-MM-DD>, TODO <item #>
   - Stability (1-5): X — <works as-is? crashes on edge cases? provisional/unverified?>
   - Expandability (1-5): X — <easy to extend with a new mode/vehicle class/endpoint?>
   - Maintainability (1-5): X — <readability, coupling, test coverage, comment quality>
   - Verdict: keep-as-is / refactor-in-place / rebuild-candidate
   - Rationale: <1-3 sentences, concrete, not diplomatic>
   ```

6. **Commit.** One task = one commit (or a few tightly-scoped ones). Reference
   the TODO item in the message (e.g. `P1.3: subtract en-route for shared
   missions`).
7. **Check the box in `TODO.md`** for the item you just finished, and update
   any doc TODO.md tells you to update for that item (e.g. P2's task asks you
   to update `docs/MODES-TODO.md`'s decisions log — do that as part of the
   same commit).
8. **Go to step 1.**

If a task requires a product decision only Sasha can make (flagged as such in
TODO.md — e.g. P3.3's per-member-vs-group-wide question, or P2's
parasite+patient combo policy), don't guess: log it below and either wait for
an answer before continuing that item, or skip to the next task and come back.

---

## 2. Blocked — needs Sasha

Running log. Add an entry every time step 1's verification order dead-ends or
a task needs a product decision. Don't delete entries — mark them resolved
inline once Sasha answers.

```
- [ ] <TODO item #> — <what's blocked and what's needed from Sasha>
```

- [x] P0.1 — RESOLVED 2026-08-31. Live-shared mission #4387674698 on throwaway account HyperCoderio: `alliance_shared_at` went `None` → real timestamp, `is_alliance` flipped `False` → `True`. Confirmed working.
- [x] P0.3 — RESOLVED 2026-08-31. Vehicle 81048893 on HyperCoderio is `"FuStW (DGL) #I - Polizei 01"`, `vehicle_type=103` — matches our local mapping exactly.
- [x] P4 (timed-mission expiry-aware priority) — RESOLVED 2026-08-31. `tv` confirmed as a nonzero "timing mechanic applies" marker, not a countdown itself; two countdown shapes confirmed (`sw`/`sw_start_in`/`sw_start_date` vs a plain `date_end`), cross-checked against the mission page's own `CountdownTimer(...)` JS. Sasha confirmed the mechanic (a scheduled-event deadline — all requested vehicles must be on scene by the timer's end) and explicitly decided AGAINST expiry-aware priority sorting — dispatch timed missions identically to normal ones, current behavior already correct. Also confirmed live and locked in with a regression test: missing_text reveals from creation for timed missions (unlike normal missions), which `resolve_mission` already handles correctly (never gated on `vehicle_state`). See docs/live-data-model.md's "Timed missions" section.
- [ ] P1 (en-route subtraction, other-players' half) — our OWN en-route was already subtracted before this session (`assigned_from_vehicles`/`_apply_delta` in `app/domain/dispatch.py`, unchanged). The remaining half — reading a SHARED mission's detail page for other players' "Auf Anfahrt" — needs a mission that's shared AND actively being worked by someone else. Credentials now exist (HyperCoderio, throwaway, real alliance "Leitstelle Sommerblüte") and Sasha has since gotten en-route/FMS-5 activity on OWN missions, but a mission shared this session (#4387674698) completed and dropped off the feed before anyone else picked it up, and the one alliance-shared mission seen since was already green/completed (no requirement table on its detail page — confirmed by inspection). Needs: re-run `scripts/probe_mission_detail.py` (or similar) against a shared mission that's RED and actually being worked by someone else, to capture the detail page's real markup/JSON shape.
- [x] P2 (patient + parasite conflict) — RESOLVED 2026-08-31 (Sasha): "if both are active, patients are removed AND one vehicle is sent to stay for claiming the cash" — parasite on a patient-governed mission never computes a patient-only "useful" pick, always claims participation with a plain spare instead. See `app/domain/dispatch.py::_priority_pass` and TODO.md's P2 entry.
- [x] P2 (UI regroup) — UNBLOCKED (the patient+parasite decision above resolved the "which combos are incompatible" question this was waiting on) but the full redesign itself is NOT YET BUILT this session — see TODO.md's P2 entry. Marking this specific WORKFLOW blocker resolved since there's nothing further to ask Sasha; the remaining work is a normal (large) build task for a future session.
- [ ] P3.3 (per-member modes vs single group-wide policy) — still an open product-scope decision for Sasha (unresolved). TODO.md phrases this as "Decide:", not a bug or verification gap. Current behavior (single `GroupPolicy` for the whole group, see `app/domain/group.py`) works and is tested; changing it needs Sasha's call.
- [ ] "min." Ausbildung-prefix question (found 2026-10-01, systematic .mscv sweep): four vehicles' own extend-rule patterns use a "min. N Person(en) mit X Ausbildung" phrasing — type 34 (ELW-2), type 63 (GW-Taucher), type 69 (GW-Wasserrettung-Ausbildung), type 54 (Dekon-P, the TRAILER variant specifically — type 53, the non-trailer Dekon-P, has NO "min." prefix in its own rule). Our committed `lssm-missing-requirements.json` staff-table entries for all 4 of these categories have no "min."-prefixed text variant registered at all. Unknown whether "min." is literally part of the game's revealed text (in which case any mission needing one of these qualifications via the trailer/ELW-2/Taucher/Wasserrettung variant would currently fall through as unmatched — same failure shape as Betreuungshelfer/Polizisten before their fixes) or just a documentation convention in the `.mscv` regex authored by whoever built that data (in which case nothing is actually broken). Not applying the same "two independent sources agree" confidence test the Polizeipferde fix used, since only ONE source (the `.mscv` file itself) shows this pattern at all — genuinely needs either a live capture of one of these 4 categories' real missing_text, or Sasha confirming/denying from memory whether he's seen a bare "min." in an actual mission.
- [x] Ambulance/NEF over-dispatch by MAX (raised 2026-10-01, Sasha) — RESOLVED 2026-10-05 (commit `d95bab7`). Sasha confirmed live: his medical vehicles DO leave the scene after handling each patient, by design, while a "patients appear at end" mission stays open, and the over-dispatch he saw happens specifically once ALL patients have already been worked through — confirming `vehicle_state` is TRANSIENT (reverts to 0 once nobody's currently on scene), not sticky. Fixed via `MissionRequirementResolver.effective_count_revealed()`: a mission id that's ever shown `vehicle_state>=1` is remembered for the resolver's lifetime (backed by `AccountRunner._ever_revealed`, surviving reconnects), so a later revealed-zero stays authoritative instead of re-triggering the max-possible fallback once the last vehicle leaves.
  - STILL OPEN (unchanged by the above fix, not made worse either): no "patients appear at end" (Hitzschlag-style) mission-type signal exists anywhere in the codebase/catalog/data — confirmed absent, not just unconfirmed. The window between a vehicle's FIRST arrival (before any patient has appeared yet) and the patient actually appearing still requests 0 ambulances, same as before this fix (once a patient genuinely appears, `patients_count>0` wins immediately regardless of the sticky memory, so this is a narrow pre-existing under-dispatch gap, not a regression). Genuinely needs a mission-type-level signal to fix properly — would need either a live-data field distinguishing these mission types, or Sasha flagging which mission type IDs behave this way from experience.
- [x] Alliance compliance (BKB 2.0 rules, read live 2026-10-07 from `verband/regeln/50220`) — §3 (Einsatzfreigabe) and §4/§6 (Leitungsteam command-vehicle restriction on Verbandsgroßeinsätze) implemented, commit `eaf7590`: `DispatchPolicy.hilferuf_min_credits` floor (applies under every `hilferuf_mode`, including ALL) and `_VGE_MISSION_TYPE_IDS`-driven parasite-only override in `_priority_pass`, gated by the new, deliberately-stricter `_confidently_alliance` check (not the general `is_alliance` proxy — see its docstring for why).
  - ACKNOWLEDGED, NOT SOLVED: §7's "custom" (player-started) Großschadenslagen have no fixed mission_type_id at all ("because they are... custom" — Sasha), so they aren't and can't be caught by `_VGE_MISSION_TYPE_IDS`. No workaround proposed or requested; flagging so a future session doesn't assume this is covered.
  - OPEN, Sasha's call pending: whether to also collapse `patient_scope`/`top_up_scope`/`parasite_scope`/`transport_only` into one single mutually-exclusive "mode" selector in the web UI — flagged as a real behavior change (would remove the already-tested ability to combine scopes, e.g. normal-own + patient-only-alliance simultaneously), not just a relabel. Awaiting clarification on whether that combinability should actually go away, or whether a lighter "preset picker" alongside the existing detailed fields is what's wanted instead.

---

## 3. Scope reminders

- Estate/building automation is **removed on purpose** (Sasha has his own
  script) — don't resurrect `layout.py`/`estate.py`/`crew.py`/`reconcile.py`/
  `training.py`/`executor.py`/`writes.py`. If you notice `README.md` still
  describes them as built, that's a known stale-doc issue — leave a one-line
  note in your session summary but don't fix it mid-run (separate task).
- Don't touch the P4 "already resolved" items listed in `TODO.md`
  (`additive_overlays`, `participating` semantics, FMS-5 cadence) — they're
  done, not open work.

---

## 4. End of session

When every P0–P4 item is either checked off or logged as blocked in §2,
summarize: what shipped (with commit refs), what's blocked and on what,
and a rollup of the `docs/CODE_QUALITY_NOTES.md` verdicts — which modules are
trending toward rebuild-candidate vs. holding up fine. Don't push/merge
without asking first.
