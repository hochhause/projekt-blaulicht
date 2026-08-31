# Modes & toggles — compatibility / precedence / UI review (TODO)

We've grown a lot of modes and toggles. This is a working doc to (a) inventory them,
(b) pin down which combine / conflict / override which, and (c) redesign how the web
UI presents them. Nothing here is built yet — it's the plan.

Status: DRAFT for discussion. Check items off as we settle/implement them.

---

## 1. Inventory

### Per-account (single-account dispatch)
| Setting | Values | What it does |
|---|---|---|
| `mission_source` | both / own / alliance | Which missions **normal** dispatch works. |
| `parasite_scope` | off / own / alliance / both | Leech missions others are already on (priority pass, additive). |
| `top_up_scope` | off / own / alliance / both | Only reinforce **already-begun** missions; partial ok. |
| `patient_scope` | off / own / alliance / both | Dispatch **only** patient units (RTW/NEF/RTH + LNA/OrgL); recall idle after. |
| `hilferuf` | on / off | Share own missions we can't crew to the alliance (add-on). |
| `partial_starts` | on / off | Send what we have even if we can't fully crew (add-on). |
| `transport_enabled` | on / off | Handle Sprechwünsche (FMS 5) each cycle. |
| `transport_only` | on / off | ONLY transport, no dispatch. |
| Filters | — | radius, alliance_radius, order, own/alliance cash min/max, alliance age, batch_size, allowed_categories, max_vehicles_per_mission, max_vehicles, poll/jitter. |
| Strikes | (automatic) | A vehicle that repeatedly fails to send is dropped. No toggle. |

### Group ("symbiotic") mode — separate config, replaces per-account while running
`radius`, `radius_basis` (any/all), `order`, cash min/max, `batch_size`, categories,
poll/jitter, `transport_enabled`, `participation`, `max_token_per_account`,
`partial_starts`, `share_min_credits`, `member_ids`.

---

## 2. Current precedence & interaction rules (as built)

- **transport_only overrides everything** — if on, the cycle only does transport; no dispatch.
- **Priority order each cycle:** transport → finish-started (missions we're on) → normal loop.
- **Spread** (top up missions we already have vehicles on) **always** runs, regardless of modes.
- **Parasite is ADDITIVE, not a governing mode** — it runs in the priority pass (leech shared
  missions others are on that we're not) *in addition to* normal dispatch. `mission_source` still
  governs normal dispatch.
- **Governing mode per mission** (for the normal candidate loop), precedence
  **patient > top-up > normal(mission_source)**. A mission no scope covers → "out of scope".
- `hilferuf`, `partial_starts` are add-ons layered on the above.

---

## 3. Compatibility matrix (to confirm / fix)

Legend: ✅ combine cleanly · ⚠️ works but questionable · ❌ conflict / undefined · ⤵️ one overrides.

| A \ B | normal | parasite | top-up | patient | transport-only | hilferuf | partial |
|---|---|---|---|---|---|---|---|
| **parasite** | ✅ additive | — | ⚠️ overlap (both touch begun/shared) | ⚠️ leech sends a NON-medical spare → breaks patient-only intent | ⤵️ transport-only wins | ✅ | ✅ |
| **top-up** | ⚠️ precedence: top-up wins on covered missions | ⚠️ | — | ⤵️ patient wins (patient>top-up) | ⤵️ | ✅ | ⚠️ both partial (redundant) |
| **patient** | ⤵️ patient wins on covered missions | ❌ parasite spare undermines "medical only" | ⤵️ patient wins | — | ⤵️ | ✅ (share medical mission we can't crew) | ⚠️ patient already sends what it can |
| **transport-only** | ⤵️ | ⤵️ | ⤵️ | ⤵️ | — | ⚠️ nothing to share (no dispatch) | ⤵️ |

**Open questions to resolve:**
- [ ] **patient + parasite**: parasite may throw a random spare at a mission patient-only is on →
      contradicts "only medical". Decide: parasite should skip missions governed by patient scope,
      OR block the combo in the UI.
- [x] **top-up vs partial_starts**: overlapping ("send what you have"). Keep both? partial_starts is
      the general one; top-up adds the begun-only restriction. Document that top-up ⊂ partial + begun.
      DOCUMENTED (2026-08-31, see §6) — keeping both as-is, they're not redundant: top_up_scope governs
      WHICH missions get the partial-send treatment (only begun ones we're not yet on), independently
      of whether `partial_starts` is even on; `partial_starts` extends the SAME "send what you have"
      behavior to `normal`-governed missions regardless of begun-state.
- [x] **parasite + top-up** both act on begun/shared missions — confirm no double-send (the delta
      should prevent it, but verify). CONFIRMED (2026-08-31): a mission both scopes cover is handled
      exactly once. Parasite runs in `dispatch.py`'s priority pass (`_priority_pass`), which fires
      BEFORE the main candidate loop and adds the mission to `handled` — so top-up's main-loop branch
      never sees it. No delta-dependent race; it's structural (mutually exclusive by construction),
      confirmed by `tests/test_dispatch.py::test_parasite_and_topup_scopes_dont_double_dispatch`.
- [ ] Should enabling **transport_only** grey out every dispatch mode in the UI?

---

## 4. Proposed UI layout (redesign)

Current: a "Modes" card (parasite/top-up/patient as scope dropdowns) + Hilferuf/partial as add-ons,
plus Dispatch filters and a Transport card. Proposed cleanup:

- [ ] **Section A — Dispatch scope & filters** (always relevant): mission_source, radius(+alliance),
      order, cash bounds, age, batch, categories, vehicle caps, poll/jitter.
- [ ] **Section B — Strategy mode (pick the primary behaviour)**: a single selector
      `normal · top-up · patient-only` (mutually-exclusive *governing* mode) with its scope, since
      precedence already makes them mutually exclusive per mission. Makes the "which wins" explicit.
- [ ] **Section C — Add-ons (independent, layer on top)**: parasite scope, hilferuf, partial dispatch.
      Show a warning when an add-on conflicts with the chosen mode (e.g. parasite + patient).
- [ ] **Section D — Transport**: enabled, transport-only (transport-only visually disables A–C).
- [ ] Inline compatibility warnings + auto-disable clearly-incompatible combos.
- [ ] Mirror the same grouping on the **Group** config page (only the applicable subset).

---

## 5. Group-mode follow-ups (from discussion)

- [x] **Share-before-send**: instead of restricting to currently-visible missions, share all
      members' own missions FIRST, then RE-FETCH missions, then plan/dispatch — so everything is
      visible in the same cycle (user's suggested fix). Keep the visibility guard as a safety net.
      Implemented in `app/core/group_coordinator.py::run_cycle` (2026-08-31).
- [ ] Combined group stats on the dashboard while running (optional).
- [ ] Decide which per-account modes (if any) should be selectable per member inside a group, vs a
      single group-wide policy (current = group-wide).

---

## 6. Decisions log
(record final rulings here as we make them)
- **2026-08-31 — top-up ⊂ partial + begun-only, keep both settings.** `top_up_scope`'s main-loop
  branch always sets `allow_partial=True` (`app/domain/dispatch.py::plan_dispatches`,
  `governing == "topup"`) and is gated to missions with `vehicle_state >= 1` (skipped otherwise as
  "not begun (top-up)"). `partial_starts` independently sets `allow_partial=True` for
  `normal`-governed missions with NO begun-state gate. So top-up's behavior is a strict subset of
  "partial send" restricted to begun missions — not a duplicate setting. Decision: keep both;
  top_up_scope answers "which missions get top-up treatment" (a governing-mode choice, mutually
  exclusive with normal/patient per mission), `partial_starts` answers "should normal dispatch settle
  for less than a full crew" (an add-on). No code change needed, only this clarification.
- **2026-08-31 — parasite + top-up never double-send.** Confirmed structurally, not by luck: parasite
  is entirely handled in `dispatch.py`'s priority pass (`_priority_pass`), which runs before the main
  candidate loop and marks a handled mission id so the main loop (where top-up's branch lives) skips
  it outright. See `tests/test_dispatch.py::test_parasite_and_topup_scopes_dont_double_dispatch`.
- **2026-08-31 — Share-before-send implemented as specified.** `app/core/group_coordinator.py`'s
  `run_cycle` now shares each member's own missions before the mission fetch used for planning, then
  re-fetches so the same-cycle share is reflected; `visible_to` (from the re-fetch) remains the safety
  net for the game not having propagated a share instantly. See `tests/test_group_coordinator.py`.
