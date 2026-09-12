import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.domain.assets import AssetLibrary
from app.domain.catalog import MissionCatalog
from app.domain.dispatch import (
    DispatchOrder,
    DispatchPolicy,
    MissionSource,
    ModeScope,
    ParasiteAggressiveness,
    assigned_from_vehicles,
    plan_dispatches,
)
from app.domain.resolver import MissionRequirementResolver

BASE = Path(__file__).resolve().parent
VEHICLES_DIR = BASE.parent / "data" / "de" / "vehicles"
FIXTURE = BASE / "fixtures" / "einsaetze_sample.json"

# Fixture mission types (see einsaetze_sample.json):
#   1 -> firetrucks:1               (avg 170)
#   3 -> firetrucks:1, police_cars:1 (avg 320)
LF = 0     # firetrucks
HLF = 30   # firetrucks + heavy_rescue
FUSTW = 32  # police_cars

# A building and nearby/far missions (small coordinate deltas ~ a few km).
BUILDING = (52.5200, 13.4050)
NEAR = (52.5300, 13.4100)   # ~1.2 km
NEAR2 = (52.5250, 13.4000)  # ~0.6 km
FAR = (53.5500, 9.9900)     # Hamburg, ~250 km


def mission(mid, mtype, pos, is_alliance=False, average_credits=0, created_at=None, vehicle_state=0,
            patients_count=0, possible_patients_count=0, missing_text="", is_event=False):
    return SimpleNamespace(
        id=mid, caption=f"M{mid}", mission_type_id=mtype, position=pos,
        is_alliance=is_alliance, average_credits=average_credits, created_at=created_at,
        vehicle_state=vehicle_state, missing_text=missing_text,
        patients_count=patients_count, possible_patients_count=possible_patients_count,
        is_event=is_event,
    )


def vehicle(vid, type_id, pos=BUILDING, available=True, building_id=None):
    return SimpleNamespace(id=vid, type_id=type_id, is_available=available, position=pos,
                            building_id=building_id)


def building(pos=BUILDING):
    return SimpleNamespace(position=pos)


class DispatchPlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        library = AssetLibrary.load(VEHICLES_DIR)
        cls.lib_for_engine = library
        catalog = MissionCatalog.from_payload(json.loads(FIXTURE.read_text(encoding="utf-8")))
        cls.resolver = MissionRequirementResolver(catalog, library)

    def plan(self, missions, vehicles, policy=None, assigned=None):
        return plan_dispatches(
            missions, vehicles, [building()], self.resolver,
            policy or DispatchPolicy(radius_km=40), assigned=assigned,
        )

    def test_simple_dispatch(self):
        plan = self.plan([mission(1, 1, NEAR)], [vehicle(100, LF)])
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].vehicle_ids, (100,))
        self.assertTrue(plan.planned[0].satisfied)

    def test_out_of_range_skipped(self):
        plan = self.plan([mission(1, 1, FAR)], [vehicle(100, LF)])
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "out of range")])

    def test_own_credit_floor_skips_cheap_mission(self):
        plan = plan_dispatches(
            [mission(1, 1, NEAR, average_credits=170)], [vehicle(100, LF)], [building()],
            self.resolver, DispatchPolicy(radius_km=40, own_credit_min=300),
        )
        self.assertEqual(plan.planned, [])
        self.assertTrue(plan.skipped[0][1].startswith("below credit floor"))

    def test_own_credit_ceiling_skips_expensive_mission(self):
        plan = plan_dispatches(
            [mission(1, 1, NEAR, average_credits=5000)], [vehicle(100, LF)], [building()],
            self.resolver, DispatchPolicy(radius_km=40, own_credit_max=1000),
        )
        self.assertEqual(plan.planned, [])
        self.assertTrue(plan.skipped[0][1].startswith("above credit ceiling"))

    def test_credit_bounds_are_per_source(self):
        # alliance min 1000 blocks a 500-credit alliance mission, but the same
        # 500 own mission passes (own has no min).
        alliance_m = mission(1, 1, NEAR, is_alliance=True, average_credits=500)
        own_m = mission(2, 1, NEAR2, is_alliance=False, average_credits=500)
        policy = DispatchPolicy(radius_km=40, alliance_credit_min=1000)
        plan = plan_dispatches([alliance_m, own_m], [vehicle(100, LF), vehicle(101, LF)],
                               [building()], self.resolver, policy)
        ids = [mp.mission_id for mp in plan.planned]
        self.assertIn(2, ids)       # own passed
        self.assertNotIn(1, ids)    # alliance blocked by its own min

    def test_alliance_age_window(self):
        now = 1_000_000.0
        # shared 30s ago; min age 60s -> too recent.
        recent = mission(1, 1, NEAR, is_alliance=True)
        recent.alliance_shared_at = now - 30
        policy = DispatchPolicy(radius_km=40, alliance_min_age_seconds=60)
        plan = plan_dispatches([recent], [vehicle(100, LF)], [building()],
                               self.resolver, policy, now=now)
        self.assertEqual(plan.skipped, [(1, "shared too recently")])

    def test_missing_text_fallback_when_catalog_empty(self):
        # Unknown mission type (999 not in fixture catalog) but missing_text
        # names an RTW -> the regex engine resolves it and we dispatch.
        from app.domain.requirements import RequirementEngine
        engine = RequirementEngine(self.lib_for_engine)
        m = mission(1, 999, NEAR)
        m.missing_text = "noch benötigt: 1 RTW"
        plan = plan_dispatches([m], [SimpleNamespace(id=100, type_id=28, is_available=True,
                                                     position=BUILDING, building_id=1)],
                               [building()], self.resolver, DispatchPolicy(radius_km=40),
                               text_engine=engine)
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].vehicle_ids, (100,))  # RTW (type 28)

    def test_missing_text_json_fallback_when_catalogue_empty(self):
        # Unknown mission type (no catalogue requirement) but the live missing_text
        # JSON reveals an RTW -> parsed from the JSON blob and dispatched.
        from app.domain.requirements import RequirementEngine
        engine = RequirementEngine(self.lib_for_engine)
        m = mission(1, 999, NEAR)  # 999 not in catalogue
        m.missing_text = '{"vehicles":"1 RTW","personnel":"","other":""}'
        plan = plan_dispatches([m], [SimpleNamespace(id=100, type_id=28, is_available=True,
                                                     position=BUILDING, building_id=1)],
                               [building()], self.resolver, DispatchPolicy(radius_km=40),
                               text_engine=engine)
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].vehicle_ids, (100,))  # RTW (type 28)

    def test_missing_text_overrides_catalogue_requirement(self):
        # P1 rework: missing_text is the AUTHORITATIVE remaining need once
        # revealed (docs/live-data-model.md) — it replaces the catalogue's
        # class/chance estimate rather than only filling gaps. Mission type 1
        # has a catalogue requirement of firetrucks:1, but once missing_text
        # reveals "1 RTW" (the game's own current "still missing" list), that
        # is what we send — not the stale catalogue guess.
        m = mission(1, 1, NEAR)  # firetrucks:1 (catalogue estimate, now stale)
        m.missing_text = '{"vehicles":"1 RTW","personnel":"","other":""}'
        plan = plan_dispatches([m], [vehicle(100, LF), vehicle(101, 28)], [building()],
                               self.resolver, DispatchPolicy(radius_km=40))
        self.assertEqual(plan.planned[0].vehicle_ids, (101,))  # the RTW, not the LF

    def test_pool_consumed_across_missions(self):
        # Two firetruck missions, only one LF -> nearest gets it, other short.
        missions = [mission(1, 1, NEAR), mission(2, 1, NEAR2)]  # m2 is nearer
        plan = self.plan(missions, [vehicle(100, LF)])
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].mission_id, 2)  # nearest first
        self.assertEqual(plan.skipped, [(1, "not enough vehicles: short firetrucks x1")])

    def test_parasite_and_topup_scopes_dont_double_dispatch(self):
        # TODO.md P2: "confirm parasite + top-up don't double-send". Both
        # scopes cover this alliance mission (begun, we're not on it yet) —
        # it must be handled ONCE, by the priority pass's parasite branch
        # (which runs first and claims the mission via `handled`), never
        # ALSO by the main loop's top-up branch.
        m = mission(1, 1, NEAR, is_alliance=True, vehicle_state=1)  # firetrucks:1, begun
        policy = DispatchPolicy(radius_km=40, parasite_scope=ModeScope.ALLIANCE,
                                 top_up_scope=ModeScope.ALLIANCE)
        plan = plan_dispatches([m], [vehicle(100, LF)], [building()], self.resolver, policy)
        mission_plans = [mp for mp in plan.planned if mp.mission_id == 1]
        self.assertEqual(len(mission_plans), 1)
        self.assertEqual(mission_plans[0].vehicle_ids, (100,))

    def test_multi_role_hlf_used_across_requirements(self):
        # type 3 needs firetrucks:1 + police_cars:1. HLF covers firetrucks; a
        # FuStW covers police. Two vehicles, HLF is the firetruck.
        plan = self.plan([mission(1, 3, NEAR)], [vehicle(100, HLF), vehicle(101, FUSTW)])
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(set(plan.planned[0].vehicle_ids), {100, 101})
        self.assertTrue(plan.planned[0].satisfied)

    def test_delta_already_covered_is_skipped(self):
        # A firetruck already en route to mission 1 (type 1) -> nothing to do.
        plan = self.plan([mission(1, 1, NEAR)], [vehicle(100, LF)], assigned={1: [LF]})
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "already covered")])

    def test_assigned_from_vehicles(self):
        vehicles = [
            SimpleNamespace(id=1, type_id=LF, target_type="mission", target_id=555),
            SimpleNamespace(id=2, type_id=FUSTW, target_type="mission", target_id=555),
            SimpleNamespace(id=3, type_id=LF, target_type=None, target_id=None),      # idle
            SimpleNamespace(id=4, type_id=LF, target_type="building", target_id=999),  # returning
        ]
        self.assertEqual(assigned_from_vehicles(vehicles), {555: [LF, FUSTW]})

    def test_delta_from_en_route_vehicle_prevents_redispatch(self):
        # Mission 1 (firetrucks:1) already has an LF en route (target_type=mission)
        # plus a spare idle LF -> the spare is NOT sent.
        enroute = SimpleNamespace(id=1, type_id=LF, is_available=False,
                                  position=BUILDING, building_id=1,
                                  target_type="mission", target_id=1)
        spare = vehicle(2, LF)
        assigned = assigned_from_vehicles([enroute, spare])
        plan = self.plan([mission(1, 1, NEAR)], [enroute, spare], assigned=assigned)
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "already covered")])

    def test_max_vehicles_cap_skips_big_mission(self):
        plan = plan_dispatches(
            [mission(1, 3, NEAR)], [vehicle(100, LF), vehicle(101, FUSTW)], [building()],
            self.resolver, DispatchPolicy(radius_km=40, max_vehicles_per_mission=1),
        )
        self.assertEqual(plan.planned, [])
        self.assertTrue(plan.skipped[0][1].startswith("needs 2 > cap 1"))

    def test_unavailable_vehicles_ignored(self):
        plan = self.plan([mission(1, 1, NEAR)], [vehicle(100, LF, available=False)])
        self.assertEqual(plan.planned, [])

    def test_order_credits_high_first(self):
        # type 1 avg 170, type 3 avg 320. Give each its own LF so both plan.
        missions = [mission(1, 1, NEAR), mission(2, 3, NEAR2)]
        vehicles = [vehicle(100, LF), vehicle(101, LF), vehicle(102, FUSTW)]
        plan = plan_dispatches(
            missions, vehicles, [building()], self.resolver,
            DispatchPolicy(radius_km=40, order=DispatchOrder.CREDITS_HIGH),
        )
        # The 320-credit mission (id 2) is planned before the 170 one.
        self.assertEqual(plan.planned[0].mission_id, 2)

    def test_order_smart_beats_credits_high_when_pool_is_shared(self):
        # SMART (Sasha, 2026-09-11): rank by credits PER VEHICLE NEEDED, not
        # raw credits -- the classic knapsack case where one expensive,
        # vehicle-hungry mission would otherwise starve several cheaper ones
        # worth more combined. Pool has exactly enough for EITHER the one
        # expensive mission OR both cheap ones, never both.
        cheap_a = mission(1, 1, NEAR, average_credits=100)   # firetrucks:1 -> needs 1 LF
        cheap_b = mission(2, 1, NEAR2, average_credits=90)   # firetrucks:1 -> needs 1 LF
        expensive = mission(3, 2, NEAR, average_credits=150)  # firetrucks:2+platform:1+battalion:1 -> 4 vehicles
        pool = [vehicle(100, LF), vehicle(101, LF), vehicle(102, 2), vehicle(103, 3)]

        greedy = plan_dispatches(
            [cheap_a, cheap_b, expensive], list(pool), [building()], self.resolver,
            DispatchPolicy(radius_km=40, order=DispatchOrder.CREDITS_HIGH),
        )
        self.assertEqual({mp.mission_id for mp in greedy.planned}, {3})     # picks the 150 alone

        smart = plan_dispatches(
            [cheap_a, cheap_b, expensive], list(pool), [building()], self.resolver,
            DispatchPolicy(radius_km=40, order=DispatchOrder.SMART),
        )
        self.assertEqual({mp.mission_id for mp in smart.planned}, {1, 2})    # picks both cheap ones: 190 > 150

    def test_order_smart_accounts_for_min_fire_personnel_topup(self):
        # SMART's efficiency estimate must fold in a mission's minimum
        # firefighter headcount (topped up with EXTRA firetrucks by _plan_one
        # beyond the base class count) -- not just the base requirement --
        # or a "1 firetruck" mission with a big personnel floor would rank
        # artificially efficient. Same avg credits; A needs a big personnel
        # top-up, B doesn't -> B must be ranked (and thus processed) first.
        from app.domain.catalog import MissionCatalog, MissionType
        from app.domain.resolver import MissionRequirementResolver

        heavy = MissionType(id=50, name="Heavy", average_credits=100,
                             requirements={"firetrucks": 1}, min_fire_personnel=20)
        light = MissionType(id=51, name="Light", average_credits=90,
                             requirements={"firetrucks": 1}, min_fire_personnel=0)
        resolver = MissionRequirementResolver(
            MissionCatalog({50: heavy, 51: light}), self.lib_for_engine,
        )
        plan = plan_dispatches(
            [mission(1, 50, NEAR), mission(2, 51, NEAR2)],
            [vehicle(100 + i, LF) for i in range(10)], [building()], resolver,
            DispatchPolicy(radius_km=40, order=DispatchOrder.SMART),
        )
        self.assertEqual(plan.planned[0].mission_id, 2)  # light (no topup) processed first

    def test_batch_size_limits_missions(self):
        missions = [mission(i, 1, NEAR) for i in range(1, 6)]  # 5 firetruck missions
        vehicles = [vehicle(100 + i, LF) for i in range(5)]
        plan = plan_dispatches(
            missions, vehicles, [building()], self.resolver,
            DispatchPolicy(radius_km=40, batch_size=2),
        )
        self.assertEqual(len(plan.planned), 2)
        self.assertIn((3, "beyond batch size"), [(mid, r) for mid, r in plan.skipped])

    # -- parasite mode --------------------------------------------------------
    def test_parasite_leeches_shared_mission_with_spare(self):
        # A shared mission others are on (vehicle_state=1), type our fleet can't
        # usefully fill (needs police, we only have an LF) -> send a spare LF to
        # claim participation. Filters/cash bypassed.
        para = mission(1, 32, FAR, is_alliance=True, vehicle_state=1)  # far, out of normal range
        # type 32 doesn't exist as a mission type in fixture -> resolve empty ->
        # but parasite still leeches. Use a real fixture type needing police.
        para = mission(1, 3, FAR, is_alliance=True, average_credits=99999, vehicle_state=1)
        plan = plan_dispatches(
            [para], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.ALLIANCE, own_credit_max=1000),
        )
        # Useful (LF covers firetrucks part) OR leech — either way it's actioned,
        # despite being far and over the cash cap.
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].mission_id, 1)

    def test_parasite_off_leaves_shared_mission_to_filters(self):
        para = mission(1, 3, FAR, is_alliance=True, vehicle_state=1)
        plan = plan_dispatches(
            [para], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.OFF),
        )
        self.assertEqual(plan.planned, [])  # out of range, not leeched

    def test_parasite_ignores_shared_mission_nobody_on(self):
        # vehicle_state=0 -> nobody contributed yet -> not a leech target.
        para = mission(1, 3, NEAR, is_alliance=True, vehicle_state=0)
        plan = plan_dispatches(
            [para], [vehicle(100, LF), vehicle(101, FUSTW)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.ALLIANCE, mission_source=frozenset({MissionSource.OWN})),
        )
        # mission_source OWN filters it out; parasite didn't grab it (vehicle_state 0)
        self.assertEqual(plan.planned, [])

    def test_parasite_on_patient_governed_mission_only_claims_participation(self):
        # Decision (Sasha, 2026-08-31): parasite + patient-governed together
        # must NEVER try a "useful" patient-only contribution — always just
        # claim participation with a plain spare, even when a real patient
        # need exists on the mission. patient_scope's own governance (for
        # missions we're actually assigned to) is what actually cares for
        # patients; a parasite leech isn't medically aware.
        para = mission(1, 1, NEAR, is_alliance=True, vehicle_state=1)  # others on it, we're not
        para.patients_count = 1  # a real medical need exists
        rtw = vehicle(100, 28)          # ambulances — the "useful" pick under the old behavior
        lfs = [vehicle(101, LF), vehicle(102, LF), vehicle(103, LF)]  # most-abundant type
        plan = plan_dispatches(
            [para], [rtw, *lfs], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.ALLIANCE,
                            patient_scope=ModeScope.ALLIANCE),
        )
        self.assertEqual(len(plan.planned), 1)
        sent = plan.planned[0]
        self.assertEqual(len(sent.vehicle_ids), 1)
        self.assertNotEqual(sent.vehicle_ids[0], 100)  # NOT the RTW
        self.assertIn(sent.vehicle_ids[0], {101, 102, 103})  # a plain spare LF
        self.assertFalse(sent.satisfied)  # a pure leech claim, not a real contribution

    def test_parasite_mode_all_grabs_untouched_mission_too(self):
        # ParasiteAggressiveness.ALL drops the "someone's already on it"
        # requirement entirely (Sasha, 2026-08-31).
        para = mission(1, 3, NEAR, is_alliance=True, vehicle_state=0)  # nobody on it yet
        plan = plan_dispatches(
            [para], [vehicle(100, LF), vehicle(101, FUSTW)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.ALLIANCE,
                            parasite_mode=ParasiteAggressiveness.ALL,
                            mission_source=frozenset({MissionSource.OWN})),
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].mission_id, 1)

    def test_parasite_mode_all_preempts_missions_the_main_loop_would_have_filtered(self):
        # ALL-mode's priority-pass claim bypasses cash/category/batch/order
        # filters entirely -- same documented bypass parasite has always had
        # for ACTIVE-mode leech targets, now also reachable on an untouched
        # mission the main (filtered/ordered) loop would otherwise have
        # skipped outright (here: below the own-credit floor).
        cheap = mission(1, 1, NEAR, is_alliance=False, average_credits=10)  # firetrucks:1
        policy = DispatchPolicy(radius_km=40, own_credit_min=1000,  # would filter it out normally
                                 parasite_scope=ModeScope.BOTH,
                                 parasite_mode=ParasiteAggressiveness.ALL)
        # Sanity: without ALL-mode parasite, the credit floor filters it out.
        plain = plan_dispatches([cheap], [vehicle(100, LF)], [building()], self.resolver,
                                 DispatchPolicy(radius_km=40, own_credit_min=1000))
        self.assertEqual(plain.planned, [])
        self.assertEqual(plain.skipped, [(1, "below credit floor (10)")])

        plan = plan_dispatches([cheap], [vehicle(100, LF)], [building()], self.resolver, policy)
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].mission_id, 1)

    def test_parasite_mode_needed_skips_active_mission_with_nothing_missing(self):
        # ParasiteAggressiveness.NEEDED adds "missing_text must be revealed"
        # on top of "someone's already on it" — a mission others are on but
        # that's already fully covered (empty missing_text) is skipped.
        covered = mission(1, 3, NEAR, is_alliance=True, vehicle_state=1)
        covered.missing_text = ""
        plan = plan_dispatches(
            [covered], [vehicle(100, LF), vehicle(101, FUSTW)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.ALLIANCE,
                            parasite_mode=ParasiteAggressiveness.NEEDED,
                            mission_source=frozenset({MissionSource.OWN})),
        )
        self.assertEqual(plan.planned, [])

    def test_parasite_mode_needed_grabs_active_mission_with_missing_text(self):
        needy = mission(1, 3, NEAR, is_alliance=True, vehicle_state=1)
        needy.missing_text = '{"vehicles":"1 ELW 1","personnel":"","other":""}'
        plan = plan_dispatches(
            [needy], [vehicle(100, LF), vehicle(101, FUSTW)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.ALLIANCE,
                            parasite_mode=ParasiteAggressiveness.NEEDED,
                            mission_source=frozenset({MissionSource.OWN})),
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].mission_id, 1)

    # -- per-mode scoping -----------------------------------------------------
    def test_mode_scope_lets_normal_own_and_patient_alliance_coexist(self):
        # Normal dispatch own-only, patient-only on alliance: an own fire mission
        # gets a full firetruck; an alliance fire mission with a patient gets an
        # RTW (patient-only), NOT a firetruck.
        own = mission(1, 1, NEAR, is_alliance=False)
        ally = mission(2, 1, NEAR2, is_alliance=True)
        ally.patients_count = 1
        policy = DispatchPolicy(radius_km=40, mission_source=frozenset({MissionSource.OWN}),
                                patient_scope=ModeScope.ALLIANCE)
        plan = plan_dispatches(
            [own, ally], [vehicle(100, LF), vehicle(101, 28)],  # LF + RTW(28)
            [building()], self.resolver, policy,
        )
        by = {mp.mission_id: set(mp.vehicle_ids) for mp in plan.planned}
        self.assertEqual(by.get(1), {100})   # own -> LF (normal)
        self.assertEqual(by.get(2), {101})   # alliance -> RTW (patient-only)

    def test_mode_scope_alliance_leaves_own_out_of_scope(self):
        # patient_scope=alliance and normal off for own -> an own mission nobody
        # covers is skipped "out of scope".
        own = mission(1, 1, NEAR, is_alliance=False)
        plan = plan_dispatches(
            [own], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, mission_source=frozenset({MissionSource.ALLIANCE}),
                           patient_scope=ModeScope.ALLIANCE),
        )
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "out of scope")])

    # -- mission_source multi-select (event tag) -------------------------------
    def test_mission_source_empty_selection_covers_nothing(self):
        # An empty selection is a valid "run no normal dispatch" setting, not
        # a silent fallback to own+alliance (Sasha, 2026-09-12).
        own = mission(1, 1, NEAR, is_alliance=False)
        plan = plan_dispatches(
            [own], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, mission_source=frozenset()),
        )
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "out of scope")])

    def test_mission_source_event_tag_covers_regardless_of_alliance_ownership(self):
        # An event mission can be own OR alliance-shared depending on who
        # bought it -- selecting only EVENT must cover both, and must NOT
        # cover a plain (non-event) alliance mission.
        own_event = mission(1, 1, NEAR, is_alliance=False, is_event=True)
        alliance_event = mission(2, 1, NEAR2, is_alliance=True, is_event=True)
        plain_alliance = mission(3, 1, NEAR2, is_alliance=True, is_event=False)
        plan = plan_dispatches(
            [own_event, alliance_event, plain_alliance],
            [vehicle(100, LF), vehicle(101, LF), vehicle(102, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, mission_source=frozenset({MissionSource.EVENT})),
        )
        planned_ids = {mp.mission_id for mp in plan.planned}
        self.assertEqual(planned_ids, {1, 2})
        self.assertIn((3, "out of scope"), plan.skipped)

    def test_mission_source_own_plus_event_also_covers_an_alliance_event(self):
        # The exact scenario Sasha described: "own" checked, "alliance" NOT
        # checked, but an alliance-shared event should still be worked
        # because "event" is an independent, additive tag.
        own_normal = mission(1, 1, NEAR, is_alliance=False, is_event=False)
        alliance_event = mission(2, 1, NEAR2, is_alliance=True, is_event=True)
        alliance_normal = mission(3, 1, NEAR2, is_alliance=True, is_event=False)
        plan = plan_dispatches(
            [own_normal, alliance_event, alliance_normal],
            [vehicle(100, LF), vehicle(101, LF), vehicle(102, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, mission_source=frozenset({MissionSource.OWN, MissionSource.EVENT})),
        )
        planned_ids = {mp.mission_id for mp in plan.planned}
        self.assertEqual(planned_ids, {1, 2})
        self.assertIn((3, "out of scope"), plan.skipped)

    # -- top-up / symbiont mode ----------------------------------------------
    def test_top_up_skips_fresh_mission(self):
        # A brand-new mission nobody is on yet (vehicle_state=0) is left alone.
        fresh = mission(1, 1, NEAR, vehicle_state=0)
        plan = plan_dispatches(
            [fresh], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, top_up_scope=ModeScope.BOTH),
        )
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "not begun (top-up)")])

    def test_top_up_partial_fills_begun_mission(self):
        # A begun mission (vehicle_state=1) needs fire + police; we only have an
        # LF. Top-up sends it and reports the police shortfall — no full crew
        # required, unlike normal dispatch.
        begun = mission(1, 3, NEAR, vehicle_state=1)
        plan = plan_dispatches(
            [begun], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, top_up_scope=ModeScope.BOTH),
        )
        self.assertEqual(len(plan.planned), 1)
        mp = plan.planned[0]
        self.assertEqual(mp.vehicle_ids, (100,))
        self.assertFalse(mp.satisfied)
        self.assertIn("police_cars", mp.shortfall)

    def test_top_up_does_not_strike(self):
        # Same partial situation, but with a strike tracker: top-up must NOT
        # record ANY strike (it keeps helping, it doesn't give up on a class).
        from app.domain.strikes import StrikeTracker

        strikes = StrikeTracker(threshold=1)  # would strike on the first miss
        begun = mission(1, 3, NEAR, vehicle_state=1)
        plan_dispatches(
            [begun], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, top_up_scope=ModeScope.BOTH),
            strikes=strikes, now=1000.0,
        )
        self.assertEqual(strikes.struck_classes(1000.0), set())

    def test_partial_starts_sends_what_it_has(self):
        # type 3 needs fire + police; we only have an LF. With partial_starts on,
        # send the LF now and report the police gap (instead of skipping).
        begun = mission(1, 3, NEAR)
        plan = plan_dispatches(
            [begun], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, partial_starts=True),
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].vehicle_ids, (100,))
        self.assertFalse(plan.planned[0].satisfied)
        self.assertIn("police_cars", plan.planned[0].shortfall)

    def test_normal_mode_still_requires_full_crew(self):
        # Sanity: with top-up OFF, the same begun mission we can't fully crew is
        # skipped (not partial-sent).
        begun = mission(1, 3, NEAR, vehicle_state=1)
        plan = plan_dispatches(
            [begun], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, top_up_scope=ModeScope.OFF),
        )
        self.assertEqual(plan.planned, [])
        self.assertTrue(plan.skipped[0][1].startswith("not enough vehicles"))

    def test_plan_records_shortfall_classes(self):
        # Mission needs fire + police; we only have an LF -> not enough, and the
        # missing class is tallied on plan.shortfalls (for the dashboard list).
        m = mission(1, 3, NEAR)
        plan = plan_dispatches(
            [m], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40),
        )
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.shortfalls.get("police_cars"), 1)

    def test_top_up_partial_also_records_shortfall(self):
        begun = mission(1, 3, NEAR, vehicle_state=1)
        plan = plan_dispatches(
            [begun], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, top_up_scope=ModeScope.BOTH),
        )
        self.assertEqual(plan.shortfalls.get("police_cars"), 1)

    def test_spread_mission_topped_up_first(self):
        # We're already on mission 1 (assigned an LF) but it still needs police;
        # priority pass tops it up before the batch even with batch_size 1.
        m1 = mission(1, 3, NEAR)  # firetrucks + police; LF en route, needs FuStW
        plan = plan_dispatches(
            [m1], [vehicle(200, FUSTW)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, batch_size=1), assigned={1: [LF]},
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].vehicle_ids, (200,))  # FuStW tops it up

    def test_absent_class_never_strikes(self):
        # A firetruck mission with NO firetruck available: short EVERY cycle, but
        # a class we simply don't have must NOT accrue strikes (it's not a
        # phantom that failed to send — we just don't own one).
        from app.domain.strikes import StrikeTracker

        strikes = StrikeTracker(threshold=1)  # would strike on the first count
        for _ in range(3):
            plan = plan_dispatches(
                [mission(1, 1, NEAR)], [vehicle(100, FUSTW)], [building()], self.resolver,
                DispatchPolicy(radius_km=40), strikes=strikes, now=1000.0,
            )
            self.assertTrue(plan.skipped[0][1].startswith("not enough vehicles"))
        self.assertEqual(strikes.struck_classes(1000.0), set())      # no strikes
        self.assertEqual(plan.shortfalls.get("firetrucks"), 1)        # but still tallied

    def test_struck_vehicle_excluded_from_pool(self):
        # A specific vehicle struck out for repeated dispatch failures is dropped
        # from the fleet, so the planner won't pick it (nor is anything else here,
        # so the mission is simply short).
        from app.domain.strikes import StrikeTracker

        strikes = StrikeTracker(threshold=1)
        strikes.record(["100"], 1000.0)  # vehicle #100 struck out (keyed by id)
        plan = plan_dispatches(
            [mission(1, 1, NEAR)], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40), strikes=strikes, now=1000.0,
        )
        self.assertEqual(plan.planned, [])
        self.assertTrue(plan.skipped[0][1].startswith("not enough vehicles"))

    def test_partial_coverage_is_not_dispatched(self):
        # Mission needs firetrucks + police; only a firetruck is available ->
        # not fully crewable -> skipped, nothing sent.
        plan = plan_dispatches(
            [mission(1, 3, NEAR)], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40),
        )
        self.assertEqual(plan.planned, [])
        self.assertTrue(plan.skipped[0][1].startswith("not enough vehicles"))

    def test_skipped_missions_do_not_consume_batch_slot(self):
        # batch_size=1. Mission A (processed first, nearest) is already covered
        # -> skipped, no slot used. Mission B then still gets dispatched.
        a = mission(1, 1, NEAR2)  # nearest -> first
        b = mission(2, 1, NEAR)
        plan = plan_dispatches(
            [a, b], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, batch_size=1), assigned={1: [LF]},
        )
        self.assertEqual([mp.mission_id for mp in plan.planned], [2])
        self.assertIn((1, "already covered"), plan.skipped)

    def test_category_filter(self):
        # type 1 is category "fire"; only allow "police" -> filtered out.
        plan = plan_dispatches(
            [mission(1, 1, NEAR)], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, allowed_categories=frozenset({"police"})),
        )
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "category filtered")])

    def test_category_filter_allows_matching(self):
        plan = plan_dispatches(
            [mission(1, 1, NEAR)], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, allowed_categories=frozenset({"fire"})),
        )
        self.assertEqual(len(plan.planned), 1)

    def test_alliance_mission_worked_by_default(self):
        plan = self.plan([mission(1, 1, NEAR, is_alliance=True)], [vehicle(100, LF)])
        self.assertEqual(len(plan.planned), 1)

    def test_own_only_filter_skips_alliance(self):
        plan = plan_dispatches(
            [mission(1, 1, NEAR, is_alliance=True)], [vehicle(100, LF)], [building()],
            self.resolver, DispatchPolicy(radius_km=40, mission_source=frozenset({MissionSource.OWN})),
        )
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "out of scope")])

    def test_alliance_only_filter_skips_own(self):
        plan = plan_dispatches(
            [mission(1, 1, NEAR, is_alliance=False)], [vehicle(100, LF)], [building()],
            self.resolver, DispatchPolicy(radius_km=40, mission_source=frozenset({MissionSource.ALLIANCE})),
        )
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "out of scope")])

    def test_order_oldest_first(self):
        missions = [
            mission(1, 1, NEAR, created_at="2026-08-17T10:00:00"),
            mission(2, 1, NEAR2, created_at="2026-08-17T08:00:00"),  # older
        ]
        vehicles = [vehicle(100, LF), vehicle(101, LF)]
        plan = plan_dispatches(
            missions, vehicles, [building()], self.resolver,
            DispatchPolicy(radius_km=40, order=DispatchOrder.OLDEST),
        )
        self.assertEqual(plan.planned[0].mission_id, 2)  # oldest first

    def test_order_newest_first(self):
        missions = [
            mission(1, 1, NEAR, created_at="2026-08-17T08:00:00"),
            mission(2, 1, NEAR2, created_at="2026-08-17T10:00:00"),  # newer
        ]
        vehicles = [vehicle(100, LF), vehicle(101, LF)]
        plan = plan_dispatches(
            missions, vehicles, [building()], self.resolver,
            DispatchPolicy(radius_km=40, order=DispatchOrder.NEWEST),
        )
        self.assertEqual(plan.planned[0].mission_id, 2)  # newest first

    def test_alliance_radius_applied(self):
        # Mission ~1.2 km away, own radius 40 (in range) but alliance radius 0.5
        # (out of range) -> alliance mission skipped for distance.
        plan = plan_dispatches(
            [mission(1, 1, NEAR, is_alliance=True)], [vehicle(100, LF)], [building()],
            self.resolver, DispatchPolicy(radius_km=40, alliance_radius_km=0.5),
        )
        self.assertEqual(plan.skipped, [(1, "out of range")])

    # -- fire-personnel requirement (average_min_fire_personnel) --------------
    def _fire_resolver(self, min_fire, base_reqs=None):
        from app.domain.catalog import MissionCatalog, MissionType
        from app.domain.resolver import MissionRequirementResolver

        mt = MissionType(id=900, name="Fire personnel", average_credits=100,
                         requirements=base_reqs or {}, min_fire_personnel=min_fire)
        return MissionRequirementResolver(MissionCatalog({900: mt}), self.lib_for_engine)

    def test_fire_personnel_only_mission_sends_one_lf(self):
        # "3 Feuerwehrleute mind." with no vehicle class -> one LF (crew 9) covers it.
        resolver = self._fire_resolver(3)
        plan = plan_dispatches([mission(1, 900, NEAR)], [vehicle(100, LF)],
                               [building()], resolver, DispatchPolicy(radius_km=40))
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].vehicle_ids, (100,))
        self.assertTrue(plan.planned[0].satisfied)

    def test_fire_personnel_needs_two_when_crew_short(self):
        # 12 firefighters; one LF (9) is short, so a second is added.
        resolver = self._fire_resolver(12)
        plan = plan_dispatches([mission(1, 900, NEAR)], [vehicle(100, LF), vehicle(101, LF)],
                               [building()], resolver, DispatchPolicy(radius_km=40))
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(set(plan.planned[0].vehicle_ids), {100, 101})
        self.assertTrue(plan.planned[0].satisfied)

    def test_fire_personnel_shortfall_when_no_fire_vehicle(self):
        # Only a police car -> no firefighter crew -> shortfall on "firefighters".
        resolver = self._fire_resolver(3)
        plan = plan_dispatches([mission(1, 900, NEAR)], [vehicle(100, FUSTW)],
                               [building()], resolver, DispatchPolicy(radius_km=40))
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.shortfalls.get("firefighters"), 1)
        self.assertTrue(plan.skipped[0][1].startswith("not enough vehicles"))

    def test_fire_personnel_counts_en_route_crew(self):
        # An LF (9) already en route already covers a 3-firefighter mission.
        resolver = self._fire_resolver(3)
        plan = plan_dispatches([mission(1, 900, NEAR)], [vehicle(100, LF)],
                               [building()], resolver, DispatchPolicy(radius_km=40),
                               assigned={1: [LF]})
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "already covered")])

    def test_fire_personnel_hlf_plans_at_half_crew(self):
        # Live-verified 2026-09-11: HLF is capped at 9 crew but usually
        # staffed at roughly half that -- a lone HLF (planning_personnel=4)
        # must NOT look sufficient for a 5-firefighter floor (it would have,
        # pre-fix, using the nominal 9); a second HLF gets added to actually
        # cover it.
        resolver = self._fire_resolver(5)
        plan = plan_dispatches([mission(1, 900, NEAR)], [vehicle(100, HLF), vehicle(101, HLF)],
                               [building()], resolver, DispatchPolicy(radius_km=40))
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(set(plan.planned[0].vehicle_ids), {100, 101})
        self.assertTrue(plan.planned[0].satisfied)

    def test_fire_personnel_plain_lf_still_plans_at_full_crew(self):
        # Sanity: the halving is specific to HLF -- a plain LF (also 9
        # nominal) still plans at full capacity and alone covers a
        # 9-firefighter floor, unlike the HLF case above.
        resolver = self._fire_resolver(9)
        plan = plan_dispatches([mission(1, 900, NEAR)], [vehicle(100, LF)],
                               [building()], resolver, DispatchPolicy(radius_km=40))
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].vehicle_ids, (100,))
        self.assertTrue(plan.planned[0].satisfied)

    # -- cleanup: recall surplus vehicles ------------------------------------
    def _assigned(self, vid, type_id, mid, fms=4):
        return SimpleNamespace(id=vid, type_id=type_id, target_type="mission",
                               target_id=mid, fms=fms, is_available=False,
                               position=BUILDING, building_id=1)

    def test_cleanup_recalls_surplus(self):
        from app.domain.dispatch import plan_cleanup
        m = mission(1, 1, NEAR)  # needs firetrucks:1
        vehs = [self._assigned(100, LF, 1), self._assigned(101, LF, 1)]  # 2 LFs, 1 needed
        recalls = plan_cleanup([m], vehs, [building()], self.resolver)
        self.assertEqual(len(recalls), 1)
        self.assertIn(recalls[0][0], {100, 101})

    def test_cleanup_keeps_needed(self):
        from app.domain.dispatch import plan_cleanup
        m = mission(1, 1, NEAR)
        recalls = plan_cleanup([m], [self._assigned(100, LF, 1)], [building()], self.resolver)
        self.assertEqual(recalls, [])

    def test_cleanup_skips_unknown_requirement(self):
        from app.domain.dispatch import plan_cleanup
        m = mission(1, 4242, NEAR)  # not in catalogue -> can't judge -> leave alone
        recalls = plan_cleanup([m], [self._assigned(100, LF, 1)], [building()], self.resolver)
        self.assertEqual(recalls, [])

    def test_cleanup_keeps_transporting_vehicle(self):
        from app.domain.dispatch import plan_cleanup
        m = mission(1, 1, NEAR)  # needs 1 firetruck; the RTW is extra but transporting
        vehs = [self._assigned(100, LF, 1, fms=4), self._assigned(101, 28, 1, fms=5)]
        recalls = plan_cleanup([m], vehs, [building()], self.resolver)
        self.assertEqual(recalls, [])

    def test_cleanup_recalls_idle_extra(self):
        from app.domain.dispatch import plan_cleanup
        m = mission(1, 1, NEAR)
        vehs = [self._assigned(100, LF, 1, fms=4), self._assigned(101, 28, 1, fms=4)]
        recalls = plan_cleanup([m], vehs, [building()], self.resolver)
        self.assertEqual([r[0] for r in recalls], [101])  # idle RTW not needed

    def test_cleanup_fire_personnel_keeps_minimum(self):
        from app.domain.dispatch import plan_cleanup
        resolver = self._fire_resolver(3)  # 3 firefighters -> one LF suffices
        m = mission(1, 900, NEAR)
        vehs = [self._assigned(100, LF, 1), self._assigned(101, LF, 1)]
        recalls = plan_cleanup([m], vehs, [building()], resolver)
        self.assertEqual(len(recalls), 1)

    def test_vehicle_position_derived_from_building(self):
        # Vehicles carry no coordinates (real API); distance uses their building.
        near_b = SimpleNamespace(id=1, position=NEAR)
        far_b = SimpleNamespace(id=2, position=(52.90, 13.90))
        v_near = SimpleNamespace(id=100, type_id=LF, is_available=True, position=None, building_id=1)
        v_far = SimpleNamespace(id=101, type_id=LF, is_available=True, position=None, building_id=2)
        plan = plan_dispatches(
            [mission(1, 1, NEAR)], [v_far, v_near], [near_b, far_b], self.resolver,
            DispatchPolicy(radius_km=200),
        )
        # The LF at the nearer building is chosen despite list order.
        self.assertEqual(plan.planned[0].vehicle_ids, (100,))

    # -- ELW specialist deprioritization (Sasha, 2026-09-11) --------------

    def _mission2_support(self):
        # Mission type 2 needs firetrucks:2, platform_trucks:1,
        # battalion_chief_vehicles:1 -- give it everything except the ELW
        # slot itself so the coverage matcher's choice there is isolated.
        return [vehicle(1, LF), vehicle(2, LF), vehicle(3, 2)]  # 2=platform_trucks

    def test_seg_elw_deprioritized_for_a_plain_elw_slot(self):
        m = mission(1, 2, NEAR)
        vehicles = self._mission2_support() + [vehicle(10, 59), vehicle(11, 3)]  # SEG-ELW, ELW1
        plan = self.plan([m], vehicles)
        self.assertTrue(plan.planned[0].satisfied)
        self.assertIn(11, plan.planned[0].vehicle_ids)   # plain ELW1
        self.assertNotIn(10, plan.planned[0].vehicle_ids)  # SEG-ELW held back

    def test_seg_elw_preferred_on_mass_casualty_mission(self):
        # 20+ patients also layers on an ambulance-per-patient requirement
        # plus incident-command scaling (always-on, unrelated to this
        # feature) -- supply enough ambulances (28), an LNA (55) and OrgL
        # (56) so the mission stays satisfiable and the ELW pick is isolated
        # to just the SEG-ELW-vs-plain-ELW1 choice being tested.
        m = mission(1, 2, NEAR, possible_patients_count=20)
        ambulances = [vehicle(200 + i, 28) for i in range(20)]
        command = [vehicle(300, 55), vehicle(301, 56)]
        vehicles = self._mission2_support() + ambulances + command + [vehicle(10, 59), vehicle(11, 3)]
        plan = self.plan([m], vehicles)
        self.assertTrue(plan.planned[0].satisfied)
        self.assertIn(10, plan.planned[0].vehicle_ids)   # SEG-ELW preferred
        self.assertNotIn(11, plan.planned[0].vehicle_ids)

    def test_seg_elw_still_used_when_it_is_the_only_elw(self):
        # Not a hard blocker -- a lone SEG-ELW still satisfies a plain ELW slot.
        m = mission(1, 2, NEAR)
        vehicles = self._mission2_support() + [vehicle(10, 59)]
        plan = self.plan([m], vehicles)
        self.assertTrue(plan.planned[0].satisfied)
        self.assertIn(10, plan.planned[0].vehicle_ids)

    def test_seg_elw_preference_decays_once_patients_are_already_cleared(self):
        # possible_patients_count is a static ceiling that never drops; the
        # preference must track the RECONCILED (revealed) patient count, not
        # just that ceiling, once vehicle_state shows units already on scene
        # and patients_count authoritatively reads 0 (already transported) --
        # otherwise SEG-ELW stays "preferred" for a mission whose casualty
        # burden has already resolved.
        m = mission(1, 2, NEAR, vehicle_state=1, patients_count=0, possible_patients_count=20)
        vehicles = self._mission2_support() + [vehicle(10, 59), vehicle(11, 3)]
        plan = self.plan([m], vehicles)
        self.assertTrue(plan.planned[0].satisfied)
        self.assertIn(11, plan.planned[0].vehicle_ids)     # back to plain ELW1
        self.assertNotIn(10, plan.planned[0].vehicle_ids)  # no longer preferred

    def test_elw2_drohne_deprioritized_via_missing_text_path(self):
        # The catalogue class map doesn't include 129 for battalion_chief (it
        # only appears there via the revealed missing_text "ELW 1" category,
        # a different code path than the other ELW tests above) -- confirm
        # the specialist penalty still applies once it becomes eligible.
        m = mission(1, 1, NEAR, missing_text='{"vehicles":"1 ELW 1","personnel":"","other":""}')
        plan = self.plan([m], [vehicle(10, 129), vehicle(11, 3)])
        self.assertTrue(plan.planned[0].satisfied)
        self.assertIn(11, plan.planned[0].vehicle_ids)
        self.assertNotIn(10, plan.planned[0].vehicle_ids)

    # -- MZB water-rescue escort (Sasha, 2026-09-11) ----------------------

    def _mzb_mission(self, **kw):
        return mission(1, 1, NEAR, missing_text='{"vehicles":"1 Boot","personnel":"","other":""}', **kw)

    def test_mzb_escort_sent_from_same_station(self):
        mzb = vehicle(100, 70, building_id=1)
        same_station_diver = vehicle(101, 63, building_id=1)
        same_station_water = vehicle(102, 64, building_id=1)
        other_station_diver = vehicle(103, 63, building_id=2)
        plan = self.plan([self._mzb_mission()], [mzb, same_station_diver, same_station_water, other_station_diver])
        ids = set(plan.planned[0].vehicle_ids)
        self.assertEqual(ids, {100, 101, 102})

    def test_mzb_escort_falls_back_to_nearest_when_no_same_station_escort(self):
        mzb = vehicle(100, 70, building_id=1)
        diver_elsewhere = vehicle(101, 63, building_id=2)
        plan = self.plan([self._mzb_mission()], [mzb, diver_elsewhere])
        self.assertIn(101, plan.planned[0].vehicle_ids)

    def test_mzb_unknown_station_does_not_falsely_match_another_unknown_station(self):
        # Both building_id=None (unknown station) must NOT be treated as
        # "same station" just because None == None -- that would wrongly
        # prefer this candidate over a genuinely nearer, known-station one.
        mzb = vehicle(100, 70, pos=NEAR, building_id=None)
        unknown_station_far = vehicle(101, 63, pos=FAR, building_id=None)
        known_station_near = vehicle(102, 63, pos=NEAR2, building_id=5)
        plan = self.plan([self._mzb_mission()], [mzb, unknown_station_far, known_station_near])
        self.assertIn(102, plan.planned[0].vehicle_ids)
        self.assertNotIn(101, plan.planned[0].vehicle_ids)

    def test_mzb_dispatch_not_blocked_when_no_escort_available(self):
        # Not a hard blocker -- the MZB itself still goes, and the mission is
        # still fully satisfied, when no diver/water-rescue unit is idle.
        mzb = vehicle(100, 70, building_id=1)
        plan = self.plan([self._mzb_mission()], [mzb])
        self.assertTrue(plan.planned[0].satisfied)
        self.assertEqual(plan.planned[0].vehicle_ids, (100,))

    def test_no_escort_added_when_no_mzb_is_dispatched(self):
        # Sanity: a mission needing an LF doesn't pick up water-rescue escorts.
        plan = self.plan([mission(1, 1, NEAR)], [vehicle(100, LF), vehicle(101, 63), vehicle(102, 64)])
        self.assertEqual(plan.planned[0].vehicle_ids, (100,))

    # -- vehicle-class reservation for known shortfalls (Sasha, 2026-09-11) --

    def test_reservation_blocks_fresh_dispatch_when_an_already_open_mission_is_short(self):
        # Mission 1 (ally-started, vehicle_state=1) still needs a boat; mission
        # 2 is a completely fresh mission that also needs one, and is CLOSER
        # (NEAR2 < NEAR) so it's processed first under default NEAREST order.
        # Without reservation it would steal the only boat; with it, the boat
        # must be held for the already-open mission instead.
        ally_started = mission(1, 1, NEAR, vehicle_state=1,
                                missing_text='{"vehicles":"1 Boot","personnel":"","other":""}')
        fresh = mission(2, 1, NEAR2, vehicle_state=0,
                        missing_text='{"vehicles":"1 Boot","personnel":"","other":""}')
        boat = vehicle(100, 70)
        plan = self.plan([ally_started, fresh], [boat])
        planned_ids = {p.mission_id for p in plan.planned}
        self.assertIn(1, planned_ids)
        self.assertNotIn(2, planned_ids)
        skip_reasons = dict(plan.skipped)
        self.assertTrue(skip_reasons[2].startswith("not enough vehicles"))

    def test_reservation_applies_even_with_surplus_supply(self):
        # Deliberately UNCONDITIONAL (Sasha, 2026-09-11): reservation isn't
        # lifted just because the pool happens to look "enough" for everyone
        # -- a vehicle can look available yet keep silently failing to
        # actually crew/tow, so a same-cycle "looks satisfiable" count isn't
        # trusted. As long as mission 1 still MENTIONS a boat as needed, the
        # WHOLE class stays held back from fresh dispatch, even with 2 boats
        # on hand for a 1-boat need.
        ally_started = mission(1, 1, NEAR, vehicle_state=1,
                                missing_text='{"vehicles":"1 Boot","personnel":"","other":""}')
        fresh = mission(2, 1, NEAR2, vehicle_state=0,
                        missing_text='{"vehicles":"1 Boot","personnel":"","other":""}')
        boats = [vehicle(100, 70), vehicle(101, 70)]
        plan = self.plan([ally_started, fresh], boats)
        planned_ids = {p.mission_id for p in plan.planned}
        self.assertIn(1, planned_ids)
        self.assertNotIn(2, planned_ids)

    def test_reservation_lifted_once_the_mention_is_gone(self):
        # Once mission 1's missing_text no longer mentions anything needed
        # (revealed and fully satisfied -- empty groups), the class is free
        # for fresh dispatch again.
        resolved = mission(1, 1, NEAR, vehicle_state=1,
                            missing_text='{"vehicles":"","personnel":"","other":""}')
        fresh = mission(2, 1, NEAR2, vehicle_state=0,
                        missing_text='{"vehicles":"1 Boot","personnel":"","other":""}')
        plan = self.plan([resolved, fresh], [vehicle(100, 70)])
        planned_ids = {p.mission_id for p in plan.planned}
        self.assertIn(2, planned_ids)

    def test_reservation_does_not_apply_to_unrelated_vehicle_classes(self):
        # The shortfall is for boats; a fresh mission needing an unrelated
        # class (firetrucks) is completely unaffected.
        ally_started = mission(1, 1, NEAR, vehicle_state=1,
                                missing_text='{"vehicles":"1 Boot","personnel":"","other":""}')
        fresh_unrelated = mission(2, 1, NEAR2, vehicle_state=0)  # plain firetrucks:1
        plan = self.plan([ally_started, fresh_unrelated], [vehicle(100, LF)])
        planned_ids = {p.mission_id for p in plan.planned}
        self.assertIn(2, planned_ids)

    def test_reservation_contributed_via_priority_pass_for_our_own_open_mission(self):
        # Mission 1 is OUR OWN already-open mission (on_it, via `assigned`) --
        # handled entirely inside _priority_pass, never reaching the main
        # loop's own reservation block. A SECOND boat is idle and unclaimed
        # by mission 1's own dispatch -- reservation must still hold it back
        # from the fresh mission (not just "whatever's left over").
        ours = mission(1, 1, NEAR, vehicle_state=1,
                       missing_text='{"vehicles":"1 Boot","personnel":"","other":""}')
        fresh = mission(2, 1, NEAR2, vehicle_state=0,
                        missing_text='{"vehicles":"1 Boot","personnel":"","other":""}')
        boats = [vehicle(100, 70), vehicle(101, 70)]
        assigned = {1: [999]}  # an irrelevant already-assigned type -- doesn't satisfy the boat need
        plan = self.plan([ours, fresh], boats, assigned=assigned)
        planned_ids = {p.mission_id for p in plan.planned}
        self.assertIn(1, planned_ids)     # priority pass grabs a boat for our own mission
        self.assertNotIn(2, planned_ids)  # fresh mission blocked despite an idle second boat

    def test_reservation_accumulates_across_multiple_open_missions(self):
        # Mission 1 needs a boat, mission 2 needs a GW-Wasserrettung -- both
        # reservations must coexist in the same cycle without one clobbering
        # the other.
        boat_mission = mission(1, 1, NEAR, vehicle_state=1,
                               missing_text='{"vehicles":"1 Boot","personnel":"","other":""}')
        gw_mission = mission(2, 1, NEAR2, vehicle_state=1,
                             missing_text='{"vehicles":null,"personnel":"1 GW-Wasserrettung","other":""}')
        fresh_boat = mission(3, 1, NEAR, vehicle_state=0,
                             missing_text='{"vehicles":"1 Boot","personnel":"","other":""}')
        fresh_gw = mission(4, 1, NEAR2, vehicle_state=0,
                           missing_text='{"vehicles":null,"personnel":"1 GW-Wasserrettung","other":""}')
        boat = vehicle(100, 70)
        gw = vehicle(101, 64)
        plan = self.plan([boat_mission, gw_mission, fresh_boat, fresh_gw], [boat, gw])
        planned_ids = {p.mission_id for p in plan.planned}
        self.assertIn(1, planned_ids)
        self.assertIn(2, planned_ids)
        self.assertNotIn(3, planned_ids)
        self.assertNotIn(4, planned_ids)

    def test_reservation_suppressed_when_assigned_already_covers_the_need(self):
        # If we've ALREADY sent a matching vehicle (tracked via `assigned`,
        # from the live vehicle feed) that fully covers the mentioned
        # category, the outstanding need nets to zero and nothing is
        # reserved -- even though missing_text hasn't caught up yet.
        ours = mission(1, 1, NEAR, vehicle_state=1,
                       missing_text='{"vehicles":"1 Boot","personnel":"","other":""}')
        fresh = mission(2, 1, NEAR2, vehicle_state=0,
                        missing_text='{"vehicles":"1 Boot","personnel":"","other":""}')
        boat_for_fresh = vehicle(100, 70)
        assigned = {1: [70]}  # a boat already assigned to mission 1
        plan = self.plan([ours, fresh], [boat_for_fresh], assigned=assigned)
        planned_ids = {p.mission_id for p in plan.planned}
        self.assertIn(2, planned_ids)


if __name__ == "__main__":
    unittest.main()
