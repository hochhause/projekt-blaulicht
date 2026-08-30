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
- [ ] **top-up vs partial_starts**: overlapping ("send what you have"). Keep both? partial_starts is
      the general one; top-up adds the begun-only restriction. Document that top-up ⊂ partial + begun.
- [ ] **parasite + top-up** both act on begun/shared missions — confirm no double-send (the delta
      should prevent it, but verify).
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

- [ ] **Share-before-send**: instead of restricting to currently-visible missions, share all
      members' own missions FIRST, then RE-FETCH missions, then plan/dispatch — so everything is
      visible in the same cycle (user's suggested fix). Keep the visibility guard as a safety net.
- [ ] Combined group stats on the dashboard while running (optional).
- [ ] Decide which per-account modes (if any) should be selectable per member inside a group, vs a
      single group-wide policy (current = group-wide).

---

## 6. Decisions log
(record final rulings here as we make them)
- …
