# Projekt Blaulicht

A private, multi-account auto-dispatcher for **[leitstellenspiel.de](https://www.leitstellenspiel.de/)**
(the German server of Mission Chief). Python, headless HTTP, configured through a
small Docker-hosted web panel. German server only, for now.

> [!WARNING]
> **Automating the game violates its terms of service.** Detection means losing
> the account — and keeping the code private does nothing about that, because the
> game sees your account's *traffic pattern* regardless of where the code lives.
> Run it only on accounts you are willing to lose. The cadence and per-account
> identity measures in the design reduce the odds; they do not remove the risk.

The full design lives in the implementation plan (the "Projekt Blaulicht"
artifact). This README covers what exists in the repo today and how to work on it.

---

## Status

Built and **tested** (pure-stdlib, runs with no third-party packages — 63 tests):

| Area | Module | State |
| --- | --- | --- |
| Lenient JSON loader (trailing commas) | `app/domain/jsonc.py` | ✅ tested |
| .NET → Python regex translation | `app/domain/regexcompat.py` | ✅ tested |
| Vehicle assets — 185 types, crew, trailers | `app/domain/assets.py` | ✅ tested |
| Mission catalogue (`einsaetze.json`) | `app/domain/catalog.py` | ✅ tested |
| **Requirement resolver** (primary, structured) | `app/domain/resolver.py` | ✅ tested |
| Requirement scaling engine (regex, secondary) | `app/domain/requirements.py` | ✅ tested |
| **Multi-role coverage matcher** (1 HLF = LF+RW) | `app/domain/coverage.py` | ✅ tested |
| Building **layouts** (desired state) | `app/domain/layout.py` | ✅ tested |
| Estate state model | `app/domain/estate.py` | ✅ tested |
| Crew / trailer staffing math | `app/domain/crew.py` | ✅ tested |
| **Reconciliation** (actual vs desired → actions) | `app/domain/reconcile.py` | ✅ tested |
| Training requirements (wiki-sourced) | `app/domain/training.py` | ✅ tested |
| **Dispatch planner** (poll→plan, delta, coverage) | `app/domain/dispatch.py` | ✅ tested |
| **Dispatch runner** (one poll→plan→send cycle) | `app/core/runner.py` | ✅ tested |
| Great-circle distance / radius | `app/domain/geo.py` | ✅ tested |

Framework in place, awaiting live captures:

| Area | Module | State |
| --- | --- | --- |
| Estate write **capture seams** | `app/game/writes.py` | ⏳ placeholders, capture-ready |
| Action executor (perform vs report) | `app/core/executor.py` | ✅ tested logic |

Written, **not yet exercised against the live server** (needs `httpx` + a real
account — see the smoke test):

| Area | Module | State |
| --- | --- | --- |
| Auth session (login, CSRF, cookies) | `app/game/session.py` | ⚠️ needs live check |
| Ten-endpoint client + dispatch | `app/game/client.py` | ⚠️ needs live check |
| Typed game entities | `app/game/models.py` | ⚠️ field names provisional |
| Settings / server table | `app/config.py` | ✅ loads |

Not started (see the plan's roadmap): transport routing (patient/prisoner), the
multi-account orchestrator, the web panel, secret sealing, and Docker. The
dispatch decision + single-account cycle are built and tested; they still need a
live account to confirm the provisional model field names (run `scripts/smoke.py`,
which prints a **dry-run plan** — nothing is sent).

### The dispatch loop

[`dispatch.py`](app/domain/dispatch.py) is the pure planner — `plan_dispatches`
turns the current missions + fleet + buildings into a `DispatchPlan`: filter by
radius (nearest first), resolve requirements, subtract vehicles already en route
(**delta**), run the **multi-role coverage** matcher, and consume the pool so two
missions never claim the same vehicle. [`runner.py`](app/core/runner.py) wraps it
in one async poll→plan→dispatch cycle, decoupled from `httpx` (duck-typed client)
so it's unit-tested with a fake — jitter and back-off live there, not in the
decision logic.

### How requirements resolve (two paths)

1. **Primary — structured, no parsing.** `einsaetze.json` gives each mission
   type a `requirements` map of *vehicle-class → count*
   (`{"firetrucks": 2, "police_cars": 1}`). Those class names are exactly the
   keys in the salvaged `Vehicle.mscv`, so `MissionRequirementResolver` joins
   them into concrete vehicle-type IDs directly. This is what the dispatch loop
   will use.
2. **Secondary — regex on live text.** `RequirementEngine` parses a mission's
   displayed German requirement text for per-instance detail the static
   catalogue doesn't carry: exact crew/water/pump scaling and patient/prisoner
   cues. Applied on top of the base resolution, later.

**Multi-role matching.** A role is served by many vehicles (a class → many type
IDs) *and* a vehicle serves many roles (a type ID in many classes — an **HLF**
is both a `firetruck` and a `heavy_rescue_vehicle`). So a mission needing
`1 LF + 1 RW` is satisfied by a single HLF. [`coverage.py`](app/domain/coverage.py)
(`select_coverage`) solves this as a greedy set-multicover: fewest vehicles such
that every requirement's count is met, counting each vehicle toward *all* roles
it fills at once. This is the core of the dispatch matcher (Phase 3).

The endpoint catalogue and verified `einsaetze.json` shape are documented in
[`data/reference/api-endpoints.md`](data/reference/api-endpoints.md).

### Estate management (build / hire / train / staff)

The bot also manages your estate against a declared **layout** — "every fire
station is level 20, has extension 0, and holds 4 LF + 1 DLK". You describe the
desired state ([`layout.py`](app/domain/layout.py)); the reconciler
([`reconcile.py`](app/domain/reconcile.py)) diffs it against actual state read
from the API and emits the actions to close the gap: level-ups, extensions,
vehicle purchases, hires, and staffing reviews (crew math, incl. trailers, from
[`crew.py`](app/domain/crew.py)).

**Critical constraint:** the game has *no write API*, so — exactly like dispatch
— every management action (build, hire, train, assign) is a browser-mimicking
form POST whose request shape must be **captured from a live account** before it
can be automated. None are captured yet. So each action is classified:

- `AUTO_READY` — a captured executor exists (none yet);
- `AUTO_PENDING_CAPTURE` — automatable in principle, currently **reported** for
  you to do by hand;
- `MANUAL` — needs your judgement (e.g. selling surplus), always reported.

This is why "tell me when action is required" is the core, not a fallback:
`ReconcileResult.report()` is the always-available output, and each write flips
to `AUTO_READY` as its request is captured — no change to the reconciler.

**Wiring a captured write** (the whole seam):

1. Fill the real request into that action's `WriteSpec` in
   [`app/game/writes.py`](app/game/writes.py) and set `verified=True`.
2. Implement its branch in `EstateWriter._perform_verified`.
3. Pass its `ActionKind` in the `capabilities` set to `reconcile(...)`.

The reconciler then emits it as `AUTO_READY`, and `ActionRunner`
([`app/core/executor.py`](app/core/executor.py)) performs it instead of
reporting it. Nothing else changes. `MANUAL` kinds (e.g. selling surplus) never
auto-enable.

### Training (who needs which Lehrgang)

[`training.py`](app/domain/training.py) reads the wiki-sourced
[`data/de/training.json`](data/de/training.json) (vehicle type name → required
course) and reports, for a fleet, what training is needed — matched normalised
against each vehicle's `vehicle_type_caption`. Refresh instructions and the raw
scrape are in [`data/reference/training_wiki_source.md`](data/reference/training_wiki_source.md).
*Starting* a course is a write (capture-pending); showing the requirement is
live now.

---

## The salvaged data

`data/de/vehicles/` holds **186 files recovered from the old closed-source bot's
git history** — a ~60-class vehicle map (`Vehicle.mscv`) plus 185 per-type files
of German requirement regexes. This is the hardest part of the project and it
already exists; the requirement engine is built directly on it.

`data/reference/` keeps `Server.json` (all 19 regional base URLs) and
`config.mscc` (the old bot's fully-commented feature spec — the de-facto
requirements document).

To re-salvage from a fresh clone of the old repo:

```bash
git archive HEAD Assets/Vehicle/de/ | tar -x -C data/de/vehicles --strip-components=3
git show HEAD:Assets/Server.json  > data/reference/Server.json
git show HEAD:config.mscc          > data/reference/config.mscc
```

> The umlauts in these files are UTF-8 (`ö` = `0xC3 0xB6`). Always read them as
> UTF-8 — the loaders do — or the regexes won't match live German mission text.

---

## Layout

```
app/
  config.py            infrastructure settings + server table
  domain/              pure logic — no network, no framework, fully unit-tested
    jsonc.py           lenient JSON (trailing commas)
    regexcompat.py     .NET named-group regex → Python
    assets.py          load Vehicle.mscv + *.mscv into typed structures
    requirements.py    requirement text → {vehicle types, count}
    geo.py             haversine + radius filter
  game/                game I/O — needs httpx
    session.py         Devise login, CSRF, cookie jar
    client.py          the ten endpoints (read + dispatch/recall/transport)
    models.py          Mission / Vehicle / Building
data/
  de/vehicles/         salvaged German assets (186 files)
  reference/           Server.json, config.mscc
scripts/
  smoke.py             Phase 1 end-to-end login + read check
tests/                 stdlib unittest suite for the domain layer
```

---

## Working on it

**Run the tests** (no dependencies required):

```bash
cd projekt-blaulicht
python -m unittest discover -s tests -t . -v
# or, if you have pytest:  pip install -e ".[dev]" && pytest
```

**Run the live smoke test** (needs a throwaway account):

```bash
pip install -e .                     # installs httpx
export BLAULICHT_EMAIL=...  BLAULICHT_PASSWORD=...
python scripts/smoke.py
```

### Design notes for the next contributor

- **The domain layer must stay dependency-free.** It's what makes the crux
  testable in 0.1 s. Keep `httpx`/framework imports out of `app/domain`.
- **`app/game/models.py` field names are provisional.** They're defensive
  (multiple aliases per field, missing-key tolerant) but unverified against a
  live capture. The first job with a real account is to confirm them via the
  smoke test and tighten the parsers.
- **The requirement-engine scaling is reconstructed from data**, not from
  original source. It reproduces the three canonical cases (LF water/crew, RTW,
  FuStW police) under test. Feed real missions through it and extend the German
  patterns where `Resolution.unmatched` reports misses — that list is the
  maintenance signal, by design.
- **Dispatch is one vehicle per POST**, on purpose — the only shape confirmed
  against a live capture. Don't "optimise" it into a combined POST without a new
  capture proving it works.
