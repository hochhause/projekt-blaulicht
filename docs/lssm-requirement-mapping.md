# Requirement matching — LSSM V4 model (and how we'll use it)

Studied LSSM V4's "Enhanced Missing Vehicles" (Einsatzhelfer):
`src/modules/extendedCallWindow/assets/emv/getMissingRequirements.ts` + the data in
`src/modules/extendedCallWindow/i18n/de_DE.json → enhancedMissingVehicles`.
Saved the mapping to `data/reference/lssm-missing-requirements.json`.

## The model

A mission's `missing_text` (the `alert-missing-vehicles` box, also in `missions_json`) has
three `[data-requirement-type=...]` groups — matching the JSON blob's keys:

- **`vehicles`** — e.g. `"1 Polizeihubschrauber, 1 Boot"`
- **`staff`** (= `personnel` in the JSON) — e.g. `"8 GW-Wasserrettung"`, `"1 Einsatzleiter 2"`
- **`other`** — water / foam / pump (progress bars, not a vehicle count)

**Crucial:** the entries are **requirement CATEGORY names, not vehicle captions.** A fire mission
shows `"1 Löschfahrzeug (LF)"`, not `"1 LF 20"`. (That's why my earlier test of `"LF 20"` came back
unmatched — it's not what `missing_text` ever says.)

Each category → a set of vehicle **type ids** via `vehiclesByRequirement` (88 entries):

```json
{ "texts": ["Löschfahrzeug (LF)", "Löschfahrzeuge (LF)"],
  "vehicles": [0,1,6,7,8,9,17,...,30,37,87,88,89,90,107,119,121,163,166,167],
  "conditionalVehicles": { "allow_rw_instead_of_lf": [4,39,47] } }
```

Notes:
- **Multi-role is baked in** — an HLF (type 30) appears in `Löschfahrzeug (LF)` AND in
  `Rüstwagen oder HLF`, so one HLF satisfies both an LF and an RW slot (same as our coverage model,
  but this list is authoritative & complete).
- **Compound requirements** exist as their own entries — `"Löschfahrzeug oder Rüstwagen"`,
  `"Rüstwagen oder HLF"`, `"Löschfahrzeug, Rüstwagen oder Gerätewagen Öl"`.
- **`equipment`** (14 entries) — the vehicle only counts if it carries the module, e.g.
  `equipment: ["heavy_rescue"]` (RW module) or `["oil"]`. This is the **GW / AB / ANH** nuance:
  a carrier counts for a requirement only with the right Abrollbehälter/equipment.
- **`conditionalVehicles`** (3 entries) — extra types allowed under a game SETTING
  (`allow_rw_instead_of_lf`, `allow_arff_instead_of_lf`). Only count if that option is on.
- **`staff`** (4 entries) — personnel categories → the vehicle type ids whose **crew** provides them:
  `Feuerwehrmann` (fire crew), `Polizisten`, `THW-Einsatzkräfte`, `Rettungsdienstler`.

## The other half: en-route ("Noch benötigt = fehlend − Auf Anfahrt")

`missing_text` is **"Am Einsatzort fehlend"** — what's missing *at the scene*. It does **not**
subtract vehicles already **on the way**. LSSM's table (`EMVTable.vue`) shows per requirement:
`Am Einsatzort fehlend | Auf Anfahrt | Noch benötigt | Ausgewählt`, where
**Noch benötigt = fehlend − Auf Anfahrt**. So to know what to actually SEND we must subtract
en-route vehicles.

- **Our own en-route** we already subtract (the delta, `target_type=='mission'`).
- **Other players' en-route** on a shared mission is only on the **detail page** (the driving list).
  So for a shared mission, accurate "Noch benötigt" needs opening the detail — which ties into the
  user's rule for *when* to open one (skip yellow; open red-with-missing_text / not-participating).

## Plan for our bot (TODO)

- [ ] Build a requirement resolver from `data/reference/lssm-missing-requirements.json`:
      requirement-category text → type-id set (+ equipment + conditionalVehicles handling).
- [ ] Parse `missing_text` (vehicles + personnel) into requirements against that table — replacing
      the flaky regex engine for this purpose. Personnel → firefighter/other crew via `staff`.
- [ ] Subtract en-route: our delta always; for shared missions optionally read the detail's driving
      list (only for red/eligible missions, per the when-to-open rule) to get true "Noch benötigt".
- [ ] Handle `equipment`: only count a carrier if we know it has the module (needs the vehicle's
      equipment state — `/api/equipments` or the vehicle detail). Conservative fallback: treat as
      satisfiable and let a failed dispatch strike it.
- [ ] `conditionalVehicles`: ignore unless we detect the game setting is on (default: don't count).

Attribution: mapping data derived from LSS-Manager/LSSM-V.4 (open-source userscript).
