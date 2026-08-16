# Leitstellenspiel.de — official read APIs

Source: the game's own API listing (see <https://www.leitstellenspiel.de/api-infos>
for the changelog). Reproduced here so the bot's assumptions are documented.

> **All official APIs are read-only (GET). There is no write API.**
> Therefore *dispatching* — a write — cannot use these; it stays the form POST
> to `/missions/{id}/alarm` (capture-verified, see `app/game/client.py`).
>
> **Poll as rarely as possible.** These endpoints are provided for tools on the
> understanding that they aren't hammered. Cache aggressively; only mission +
> vehicle state is time-sensitive.

Legend: 🔏 needs login · 🔓 public (no auth)

## Read state (authenticated)

| Endpoint | Purpose | Bot use |
| --- | --- | --- |
| 🔏 `/api/vehicles`, `/api/v2/vehicles` | Your fleet (v2 = newer shape) | poll — availability |
| 🔏 `/api/vehicles/{id}`, `/api/v2/vehicles/{id}` | Single vehicle | on demand |
| 🔏 `/api/v1/vehicle_distances` | Vehicle mileage | — |
| 🔏 `/api/vehicle_states` | FMS summary (counts per status) | quick availability check |
| 🔏 `/api/equipments`, `/api/equipments/{id}` | Rollcontainer modules, winches, … | later (special missions) |
| 🔏 `/api/buildings`, `/api/buildings/{id}` | Your buildings | radius origins (cache ~1 h) |
| 🔏 `/api/buildings/{id}/vehicles`, `/api/v2/…` | Vehicles in a building | on demand |
| 🔏 `/api/alliance_buildings`, `/{id}` | Alliance-funded buildings | transport targets (alliance) |
| 🔏 `/api/allianceinfo` | Alliance info | alliance features |
| 🔏 `/api/userinfo` (alias `/credits`) | Credits / user info | credit gate + stats |
| 🔏 `/api/settings` | Account settings | — |
| 🔏 `/api/schoolings`, `/api/alliance_schoolings` | Your / alliance courses | personnel training (later) |
| 🔏 `/api/v1/aao_categories` | AAO categories | respect player presets (later) |
| 🔏 `/api/v1/aaos`, `/api/v1/aaos/{id}` | AAO entries (Ausrückeordnung = the player's own preset dispatch orders) | respect player presets (later) |

## Reference data (public)

| Endpoint | Purpose | Bot use |
| --- | --- | --- |
| 🔓 `/einsaetze.json` | **Mission-type catalogue with structured `requirements`** | PRIMARY requirement source (cache ~24 h) |
| 🔓 `/alliance_event_types.json` | Alliance events + their missions | alliance events (later) |
| 🔓 `/api/mission_type_data` | Meta-schema: mission attribute keys, POIs, icons, building types, awards, filters | reference only (not per-mission reqs) |

## Community metadata (unofficial, via the dev forum)

The [developer forum thread](https://forum.leitstellenspiel.de/index.php?thread/8406-infos-für-entwickler/)
points to LSSM's community catalogs — described there as "not official" but
"pretty up-to-date". These are static **type** metadata (not your account):

| Endpoint | Purpose |
| --- | --- |
| `https://api.lss-manager.de/de_DE/vehicles` | Vehicle-**type** catalogue — the canonical type id ↔ caption table |
| `https://api.lss-manager.de/de_DE/buildings` | Building-**type** catalogue (types, extensions) |

Candidate source for a vehicle **type-id ↔ name** bridge (which would let the
training map key on type id instead of name). Naive fetches 403 — send a proper
browser `User-Agent`/`Accept`. Not yet integrated.

Also noted from the thread: `/api/allianceinfo` carries a `role_flags` object
(`owner`, `admin`, `coadmin`, `schooling`, `finance`, `staff`,
`transport_requests`, `view_logs`) — what the account may do in its alliance.

## Not officially listed but required

| Endpoint | Purpose | Note |
| --- | --- | --- |
| `/map/missions_json` | **Current open missions + coordinates** | Undocumented; the official list has no open-missions endpoint. Capture-verified via ShiftCaptain's userscript; used for the dispatch loop. |

## `einsaetze.json` shape (verified 2026-08-16)

Top level is a JSON **array**. One entry:

```json
{
  "id": "1",
  "name": "Brennender Abfallcontainer",
  "average_credits": 170,
  "requirements": { "firetrucks": 1 },
  "prerequisites": { "main_building": 0, "fire_stations": 1 },
  "additional": { "expansion_missions_ids": [2, 3, 26, ...], "filter_id": "firehouse_missions" },
  "mission_categories": ["fire"],
  "base_mission_id": 1
}
```

The `requirements` keys are vehicle-**class** names — the *same* names used by the
salvaged `Vehicle.mscv` (`firetrucks`, `police_cars`, `platform_trucks`,
`battalion_chief_vehicles`, `hazmat_vehicles`, …). So base requirements resolve
with **no text parsing**: `einsaetze.requirements` (class→count) joined with
`Vehicle.mscv` (class→type IDs). The `.mscv` regex engine is only needed for
per-instance scaling that isn't in this static data (exact crew/water counts,
patient/prisoner transports) — read off the live mission page.
