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
    _infer_own_user_id,
    _reserve_vehicle_group,
    _TOWED_WITH_SELF_PROPELLED_ALTERNATIVE,
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
            patients_count=0, possible_patients_count=0, missing_text="", is_event=False,
            user_id=None, caption=None):
    return SimpleNamespace(
        id=mid, caption=caption if caption is not None else f"M{mid}", mission_type_id=mtype,
        position=pos,
        is_alliance=is_alliance, average_credits=average_credits, created_at=created_at,
        vehicle_state=vehicle_state, missing_text=missing_text,
        patients_count=patients_count, possible_patients_count=possible_patients_count,
        is_event=is_event, user_id=user_id,
    )


def vehicle(vid, type_id, pos=BUILDING, available=True, building_id=None):
    return SimpleNamespace(id=vid, type_id=type_id, is_available=available, position=pos,
                            building_id=building_id)


def building(pos=BUILDING):
    return SimpleNamespace(position=pos)


class DispatchPlannerTests(unittest.TestCase):
    def setUp(self):
        # A fresh resolver per test (not setUpClass/cls): MissionRequirementResolver
        # now carries small per-mission-id sticky state (effective_count_revealed,
        # resolver.py) that must not leak between tests reusing the same literal
        # mission ids for unrelated scenarios.
        library = AssetLibrary.load(VEHICLES_DIR)
        self.lib_for_engine = library
        catalog = MissionCatalog.from_payload(json.loads(FIXTURE.read_text(encoding="utf-8")))
        self.resolver = MissionRequirementResolver(catalog, library)

    def plan(self, missions, vehicles, policy=None, assigned=None, en_route=None):
        return plan_dispatches(
            missions, vehicles, [building()], self.resolver,
            policy or DispatchPolicy(radius_km=40), assigned=assigned, en_route=en_route,
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
        # missing_text revealed: required for top-up eligibility under D2
        # (Sasha, 2026-10-27 -- missing_text_ever_revealed, not vehicle_state).
        m = mission(1, 1, NEAR, is_alliance=True, vehicle_state=1,
                    missing_text='{"vehicles":"1 Löschfahrzeuge (LF)","personnel":"","other":""}')
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

    # -- alarm-counter tagging (Sasha, 2026-10-27: "how often we need to
    # alarm to each mission" -- a parasite send or S5 processing doesn't
    # count, only a real send/reinforcement does) --------------------------
    def test_normal_dispatch_counts_as_an_alarm_by_default(self):
        plan = plan_dispatches(
            [mission(1, 1, NEAR)], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40),
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertTrue(plan.planned[0].counts_as_alarm)

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
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.ALLIANCE, own_credit_max=1000,
                            parasite_scarce_threshold=0),
        )
        # Useful (LF covers firetrucks part) OR leech — either way it's actioned,
        # despite being far and over the cash cap.
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].mission_id, 1)
        self.assertFalse(plan.planned[0].counts_as_alarm)  # a parasite send, never OUR alarm

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
                            patient_scope=ModeScope.ALLIANCE, parasite_scarce_threshold=0),
        )
        self.assertEqual(len(plan.planned), 1)
        sent = plan.planned[0]
        self.assertEqual(len(sent.vehicle_ids), 1)
        self.assertNotEqual(sent.vehicle_ids[0], 100)  # NOT the RTW
        self.assertIn(sent.vehicle_ids[0], {101, 102, 103})  # a plain spare LF
        self.assertFalse(sent.satisfied)  # a pure leech claim, not a real contribution
        self.assertFalse(sent.counts_as_alarm)

    def test_parasite_mode_all_grabs_untouched_mission_too(self):
        # ParasiteAggressiveness.ALL drops the "someone's already on it"
        # requirement entirely (Sasha, 2026-08-31).
        para = mission(1, 3, NEAR, is_alliance=True, vehicle_state=0)  # nobody on it yet
        plan = plan_dispatches(
            [para], [vehicle(100, LF), vehicle(101, FUSTW)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.ALLIANCE,
                            parasite_mode=ParasiteAggressiveness.ALL,
                            mission_source=frozenset({MissionSource.OWN}),
                            parasite_scarce_threshold=0),
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
                                 parasite_mode=ParasiteAggressiveness.ALL,
                                 parasite_scarce_threshold=0)
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
                            mission_source=frozenset({MissionSource.OWN}),
                            parasite_scarce_threshold=0),
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].mission_id, 1)

    # -- Parasite scarce-vehicle threshold ---------------------------------

    def test_parasite_scarce_threshold_withholds_a_scarce_type_from_useful_contribution(self):
        # We own exactly 1 FUSTW fleet-wide -> scarce at threshold=2. 3 LFs
        # owned -> comfortably abundant at the same threshold. Parasite must
        # send an LF for the firetrucks slot but withhold the scarce FUSTW
        # from the police_cars slot, leaving it unsatisfied.
        para = mission(1, 3, NEAR, is_alliance=True, vehicle_state=1)
        lfs = [vehicle(101, LF), vehicle(102, LF), vehicle(103, LF)]
        plan = plan_dispatches(
            [para], [vehicle(100, FUSTW), *lfs], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.ALLIANCE,
                            parasite_scarce_threshold=2),
        )
        self.assertEqual(len(plan.planned), 1)
        sent = plan.planned[0]
        self.assertEqual(len(sent.vehicle_ids), 1)
        self.assertIn(sent.vehicle_ids[0], {101, 102, 103})  # an LF, never the scarce FUSTW
        self.assertFalse(sent.satisfied)
        self.assertIn("police_cars", sent.shortfall)
        # A parasite's "useful" _plan_one-routed contribution is retagged too
        # -- not just the plain claim-a-spare fallback (Sasha, 2026-10-27).
        self.assertFalse(sent.counts_as_alarm)

    def test_parasite_scarce_threshold_zero_means_no_floor(self):
        # Same fleet as above, but the floor is off (default-equivalent) ->
        # both classes get filled, including the "scarce" FUSTW.
        para = mission(1, 3, NEAR, is_alliance=True, vehicle_state=1)
        plan = plan_dispatches(
            [para], [vehicle(100, FUSTW), vehicle(101, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.ALLIANCE,
                            parasite_scarce_threshold=0),
        )
        self.assertEqual(len(plan.planned), 1)
        sent = plan.planned[0]
        self.assertEqual(set(sent.vehicle_ids), {100, 101})
        self.assertTrue(sent.satisfied)

    def test_parasite_scarce_threshold_falls_back_to_a_non_scarce_spare(self):
        # Every type the mission could actually use (FUSTW, LF) is scarce (1
        # owned each) -> the useful-contribution pick finds nothing, so the
        # plain claim-a-spare fallback must also skip them and reach for the
        # abundant, functionally-irrelevant ambulances instead.
        para = mission(1, 3, NEAR, is_alliance=True, vehicle_state=1)
        ambulances = [vehicle(102, 28), vehicle(103, 28), vehicle(104, 28)]
        plan = plan_dispatches(
            [para], [vehicle(100, FUSTW), vehicle(101, LF), *ambulances], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.ALLIANCE,
                            parasite_scarce_threshold=2),
        )
        self.assertEqual(len(plan.planned), 1)
        sent = plan.planned[0]
        self.assertEqual(len(sent.vehicle_ids), 1)
        self.assertIn(sent.vehicle_ids[0], {102, 103, 104})
        self.assertFalse(sent.satisfied)  # a pure leech claim, not a real contribution

    def test_parasite_scarce_threshold_does_not_restrict_our_own_topup_reinforcement(self):
        # Scoped to parasite only (Sasha, 2026-10-12): a mission we're
        # genuinely on, reinforced via top_up_scope, must still be able to use
        # a scarce vehicle to actually finish it -- top-up already can't waste
        # one on an unsatisfiable mission (full-satisfaction gated).
        ours = mission(1, 3, NEAR, is_alliance=False, vehicle_state=1)
        plan = plan_dispatches(
            [ours], [vehicle(100, FUSTW), vehicle(101, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.OFF,
                            top_up_scope=ModeScope.OWN, parasite_scarce_threshold=2),
            assigned={1: [LF]},  # already on it with the LF; FUSTW still needed
        )
        self.assertEqual(len(plan.planned), 1)
        sent = plan.planned[0]
        self.assertEqual(sent.vehicle_ids, (100,))  # the scarce FUSTW, sent anyway
        self.assertTrue(sent.satisfied)
        self.assertTrue(sent.counts_as_alarm)  # OUR reinforcement, not a parasite's

    # -- Verbandsgroßeinsatz (VGE) compliance override -------------------------

    def test_vge_mission_ignored_while_unrevealed_and_parasite_off(self):
        # D1 (Sasha, 2026-10-27): "adopt the new VGE logic" -- an UNREVEALED
        # VGE mission now gets no spare at all unless parasite_scope actually
        # covers it (previously unconditional, regardless of any mode).
        # "[Verband]" caption (confirmed live, 2026-10-07): the real signal a
        # genuinely foreign alliance mission carries -- see
        # _confidently_alliance, which the VGE override deliberately requires
        # over the plain is_alliance flag alone.
        vge = mission(1, 41, NEAR, is_alliance=True, vehicle_state=1,
                      caption="[Verband] VGE")
        plan = plan_dispatches(
            [vge], [vehicle(100, LF), vehicle(101, FUSTW)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.OFF,
                            mission_source=frozenset({MissionSource.OWN, MissionSource.ALLIANCE})),
        )
        self.assertEqual(plan.planned, [])

    def test_vge_mission_gets_a_spare_while_unrevealed_if_parasite_covers_it(self):
        # Same unrevealed VGE mission, but parasite_scope now covers it --
        # exactly one free participation spare (the old unconditional
        # behaviour's one surviving case).
        vge = mission(1, 41, NEAR, is_alliance=True, vehicle_state=1,
                      caption="[Verband] VGE")
        plan = plan_dispatches(
            [vge], [vehicle(100, LF), vehicle(101, FUSTW)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.ALLIANCE,
                            mission_source=frozenset({MissionSource.OWN, MissionSource.ALLIANCE})),
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertFalse(plan.planned[0].satisfied)  # pure leech, free
        self.assertFalse(plan.planned[0].counts_as_alarm)

    def test_vge_mission_graduates_to_normal_dispatch_once_revealed(self):
        # D1: once missing_text is revealed, a VGE mission is treated like
        # any other mission -- full dispatch, no one-vehicle cap, even with
        # parasite entirely off. (A minimal ad-hoc catalog entry for type 41
        # is needed here -- the shared fixture catalog only carries the small
        # sample types 1-4, not the real VGE ids.)
        catalog = MissionCatalog.from_payload([{"id": "41", "name": "VGE", "requirements": {}}])
        resolver = MissionRequirementResolver(catalog, self.lib_for_engine)
        vge = mission(1, 41, NEAR, is_alliance=True, vehicle_state=1, caption="[Verband] VGE",
                      missing_text='{"vehicles":"1 Löschfahrzeuge (LF), 1 FuStW",'
                                   '"personnel":"","other":""}')
        plan = plan_dispatches(
            [vge], [vehicle(100, LF), vehicle(101, FUSTW)], [building()], resolver,
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.OFF,
                            mission_source=frozenset({MissionSource.OWN, MissionSource.ALLIANCE})),
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(set(plan.planned[0].vehicle_ids), {100, 101})
        self.assertTrue(plan.planned[0].satisfied)
        self.assertTrue(plan.planned[0].counts_as_alarm)  # graduated -- a real send now

    def test_vge_mission_gets_nothing_more_once_already_on_it(self):
        # Once our one participation vehicle is in (tracked via `assigned`),
        # later cycles must send NOTHING further -- not even a top-up, unlike
        # a normal on_it mission's "always finish what's started" rule.
        vge = mission(1, 41, NEAR, is_alliance=True, vehicle_state=1,
                      caption="[Verband] VGE")
        plan = plan_dispatches(
            [vge], [vehicle(100, LF), vehicle(101, FUSTW)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, top_up_scope=ModeScope.ALLIANCE),
            assigned={1: [0]},  # already claimed participation with an LF
        )
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [])  # handled silently, not reported as skipped

    def test_vge_mission_handled_even_with_no_spare_available(self):
        # parasite_scope covers it (so the spare WOULD be attempted), but no
        # usable vehicle at all exists for the claim -- still marked handled
        # (no fallback to a real dispatch attempt), just silently does
        # nothing this cycle.
        vge = mission(1, 41, NEAR, is_alliance=True, vehicle_state=1,
                      caption="[Verband] VGE")
        plan = plan_dispatches(
            [vge], [], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.ALLIANCE),
        )
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [])

    def test_vge_override_does_not_apply_to_own_missions(self):
        # The restriction is about the ALLIANCE's Leitungsteam controlling a
        # shared incident -- an own (not alliance-shared) mission of the same
        # type id dispatches completely normally.
        own_vge_type = mission(1, 41, NEAR, is_alliance=False)
        plan = plan_dispatches(
            [own_vge_type], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40),
        )
        # mission type 41 isn't in the test fixture catalog, so this resolves
        # to "no requirements found" rather than a real crew -- the point is
        # only that it reaches NORMAL handling, not the VGE override.
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "no requirements found")])

    def test_vge_override_does_not_misfire_on_an_own_hilferuf_shared_mission(self):
        # Code-review finding, 2026-10-07: a player's OWN mission of a
        # VGE-matching type (here: 41, a real id in _VGE_MISSION_TYPE_IDS),
        # shared via Hilferuf, with own_user_id still unresolved (no other
        # unshared own mission around to infer it from) -- raw is_alliance is
        # now True, but it carries no "[Verband]" tag (never does, it's
        # genuinely ours) and no user_id to cross-check. _confidently_alliance
        # must treat this as "not confidently alliance" rather than guessing,
        # so the VGE override does NOT fire and this still gets a real, full
        # dispatch. (Type 41 isn't in the shared test fixture catalog, so a
        # small dedicated one is built here, mirroring the one-off
        # MissionCatalog pattern used elsewhere in these tests.)
        from app.domain.catalog import MissionCatalog, MissionType
        from app.domain.resolver import MissionRequirementResolver

        mt = MissionType(id=41, name="VGE-type", average_credits=100,
                          requirements={"firetrucks": 1}, chances={})
        resolver = MissionRequirementResolver(MissionCatalog({41: mt}), self.lib_for_engine)
        own_shared = mission(1, 41, NEAR, is_alliance=True, caption="M1")  # no [Verband], own_user_id unknown
        plan = plan_dispatches(
            [own_shared], [vehicle(100, LF)], [building()], resolver,
            DispatchPolicy(radius_km=40),
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].vehicle_ids, (100,))
        self.assertTrue(plan.planned[0].satisfied)  # a real crew, not a lone spare

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

    # -- own vs alliance reclassification (own_user_id) ------------------------
    def test_own_mission_shared_to_alliance_stays_own_scoped_when_user_id_matches(self):
        # Live-reported bug (Sasha, 2026-09-14): mission_source=OWN + hilferuf
        # on -- a mission Hilferuf itself shares flips is_alliance True from
        # the next poll, silently dropping it out of an own-only scope even
        # though it's still ours. own_user_id must keep it own-scoped.
        shared_but_mine = mission(1, 1, NEAR, is_alliance=True, user_id=42)
        plan = plan_dispatches(
            [shared_but_mine], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, mission_source=frozenset({MissionSource.OWN})),
            own_user_id=42,
        )
        self.assertEqual([mp.mission_id for mp in plan.planned], [1])
        self.assertNotIn((1, "out of scope"), plan.skipped)

    def test_genuinely_alliance_mission_still_excluded_from_own_only_scope(self):
        # A mission actually owned by someone ELSE must still be excluded --
        # own_user_id must not just blanket-include everything alliance-shared.
        someone_elses = mission(1, 1, NEAR, is_alliance=True, user_id=999)
        plan = plan_dispatches(
            [someone_elses], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, mission_source=frozenset({MissionSource.OWN})),
            own_user_id=42,
        )
        self.assertEqual(plan.planned, [])
        self.assertIn((1, "out of scope"), plan.skipped)

    def test_own_user_id_unknown_falls_back_to_is_alliance_proxy(self):
        # Without own_user_id (e.g. userinfo fetch failed) or without the
        # mission carrying user_id, behavior must match the pre-fix proxy
        # exactly: a shared mission reads as alliance, full stop.
        shared = mission(1, 1, NEAR, is_alliance=True, user_id=None)
        plan = plan_dispatches(
            [shared], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, mission_source=frozenset({MissionSource.OWN})),
            own_user_id=42,  # known, but the mission itself has no user_id
        )
        self.assertEqual(plan.planned, [])
        self.assertIn((1, "out of scope"), plan.skipped)

    def test_verband_caption_tag_classifies_as_alliance_even_without_own_user_id(self):
        # User (2026-09-14): "shared, not mine tend to have [Verband] in the
        # name" -- a strong signal that needs no own_user_id inference at
        # all, so a genuinely foreign mission classifies correctly from the
        # very first cycle.
        foreign = mission(1, 1, NEAR, is_alliance=True, caption="[Verband] Waldbrand")
        plan = plan_dispatches(
            [foreign], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, mission_source=frozenset({MissionSource.OWN})),
        )
        self.assertEqual(plan.planned, [])
        self.assertIn((1, "out of scope"), plan.skipped)

    def test_verband_caption_tag_overrides_a_user_id_coincidence(self):
        # Even if user_id somehow matched (shouldn't happen live, but the
        # caption tag is the stronger, more direct signal per the user),
        # "[Verband]" must still win -- it means "not mine" unconditionally.
        foreign = mission(1, 1, NEAR, is_alliance=True, caption="[Verband] Waldbrand", user_id=42)
        plan = plan_dispatches(
            [foreign], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, mission_source=frozenset({MissionSource.OWN})),
            own_user_id=42,
        )
        self.assertEqual(plan.planned, [])

    def test_own_mission_shared_still_uses_own_credit_bounds_not_alliance(self):
        # A genuinely-own mission that's been shared must still be judged by
        # OWN credit bounds, not alliance ones, now that ownership (not
        # sharing status) decides which bound applies.
        shared_but_mine = mission(1, 1, NEAR, is_alliance=True, user_id=42, average_credits=50)
        plan = plan_dispatches(
            [shared_but_mine], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, own_credit_min=1000, alliance_credit_min=0),
            own_user_id=42,
        )
        # Below the OWN floor (which now correctly applies) -> filtered, not planned.
        self.assertEqual(plan.planned, [])
        self.assertTrue(any(r.startswith("below credit floor") for _, r in plan.skipped))

    def test_priority_pass_parasite_scope_also_respects_true_ownership(self):
        # Same fix, the OTHER call site (Sasha, 2026-09-14, code review):
        # _priority_pass computes its own is_alliance for parasite_scope --
        # a shared-but-owned mission must still count as OWN there too, not
        # just in plan_dispatches' main loop.
        shared_but_mine = mission(1, 1, NEAR, is_alliance=True, user_id=42, vehicle_state=1)
        plan = plan_dispatches(
            [shared_but_mine], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.OWN,
                            mission_source=frozenset(),  # normal dispatch off -- isolate parasite
                            parasite_scarce_threshold=0),
            own_user_id=42,
        )
        self.assertEqual([mp.mission_id for mp in plan.planned], [1])

    def test_priority_pass_parasite_scope_still_excludes_genuine_alliance(self):
        someone_elses = mission(1, 1, NEAR, is_alliance=True, user_id=999, vehicle_state=1)
        plan = plan_dispatches(
            [someone_elses], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, parasite_scope=ModeScope.OWN,
                            mission_source=frozenset()),
            own_user_id=42,
        )
        self.assertEqual(plan.planned, [])

    def test_infer_own_user_id_from_the_first_unshared_mission(self):
        # An unshared mission can only ever be one of OUR OWN -- the same
        # invariant the pre-fix is_alliance proxy already relied on for the
        # unshared case -- so its user_id, if present, IS the account's id.
        shared = mission(1, 1, NEAR, is_alliance=True, user_id=999)
        unshared_no_id = mission(2, 1, NEAR, is_alliance=False, user_id=None)
        unshared_with_id = mission(3, 1, NEAR, is_alliance=False, user_id=42)
        self.assertEqual(_infer_own_user_id([shared, unshared_no_id, unshared_with_id]), 42)

    def test_infer_own_user_id_none_when_nothing_usable_is_visible(self):
        shared_only = mission(1, 1, NEAR, is_alliance=True, user_id=999)
        self.assertIsNone(_infer_own_user_id([shared_only]))
        self.assertIsNone(_infer_own_user_id([]))

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

    def test_top_up_skips_begun_mission_it_cannot_fully_crew(self):
        # A begun mission (vehicle_state=1, not already ours -- see on_it's own
        # unconditional spread path below) needs fire + police; we only have an
        # LF. Top-up's job is to PRIORITIZE an already-begun mission ahead of
        # the normal order/batch, not to relax the full-crew requirement --
        # Sasha, 2026-10-05: "TopUp ... ONLY prioritizes a mission if we're
        # certain we can feed it." So this is a skip, same as normal dispatch
        # without partial_starts, not a partial send.
        # missing_text revealed: required for top-up eligibility under D2
        # (Sasha, 2026-10-27). Revealing switches the requirement source from
        # the catalogue's class names (firetrucks/police_cars) to these LSSM
        # category names (Löschfahrzeuge (LF) / FuStW).
        begun = mission(1, 3, NEAR, vehicle_state=1,
                        missing_text='{"vehicles":"1 Löschfahrzeuge (LF), 1 FuStW",'
                                     '"personnel":"","other":""}')
        plan = plan_dispatches(
            [begun], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, top_up_scope=ModeScope.BOTH),
        )
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "not enough vehicles: short FuStW x1")])

    def test_top_up_sends_once_it_can_fully_crew(self):
        # Same begun mission, but the pool now has both an LF and a FuStW --
        # top-up sends the full set rather than withholding it.
        begun = mission(1, 3, NEAR, vehicle_state=1,
                        missing_text='{"vehicles":"1 Löschfahrzeuge (LF), 1 FuStW",'
                                     '"personnel":"","other":""}')
        plan = plan_dispatches(
            [begun], [vehicle(100, LF), vehicle(101, FUSTW)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, top_up_scope=ModeScope.BOTH),
        )
        self.assertEqual(len(plan.planned), 1)
        mp = plan.planned[0]
        self.assertEqual(set(mp.vehicle_ids), {100, 101})
        self.assertTrue(mp.satisfied)

    def test_top_up_does_not_strike(self):
        # Same uncrewable situation, but with a strike tracker: a top-up skip
        # (short a class, not a failed send) must NOT record ANY strike.
        from app.domain.strikes import StrikeTracker

        strikes = StrikeTracker(threshold=1)  # would strike on the first miss
        begun = mission(1, 3, NEAR, vehicle_state=1,
                        missing_text='{"vehicles":"1 Löschfahrzeuge (LF), 1 FuStW",'
                                     '"personnel":"","other":""}')
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
        begun = mission(1, 3, NEAR, vehicle_state=1,
                        missing_text='{"vehicles":"1 Löschfahrzeuge (LF), 1 FuStW",'
                                     '"personnel":"","other":""}')
        plan = plan_dispatches(
            [begun], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, top_up_scope=ModeScope.BOTH),
        )
        self.assertEqual(plan.shortfalls.get("FuStW"), 1)

    def test_spread_mission_topped_up_first(self):
        # We're already on mission 1 (assigned an LF) but it still needs police;
        # the outstanding need (police) CAN be fully met this cycle, so it's
        # sent -- whether that happens via the priority pass or (now) the main
        # loop's ordinary "normal" governing, the visible result is the same.
        m1 = mission(1, 3, NEAR)  # firetrucks + police; LF en route, needs FuStW
        plan = plan_dispatches(
            [m1], [vehicle(200, FUSTW)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, batch_size=1), assigned={1: [LF]},
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].vehicle_ids, (200,))  # FuStW tops it up

    def test_on_it_mission_that_cannot_be_fully_crewed_is_skipped_not_trickled(self):
        # Sasha, 2026-10-05 (live log): an already-on mission that can NEVER
        # be fully crewed this cycle (e.g. a mass-casualty Dammbruch needing
        # dozens of specialist vehicles) must not jump the queue ahead of
        # ordering, nor get a partial vehicle sent every single cycle, just
        # because we already have one vehicle there -- that's top_up_scope's
        # job (requires full satisfaction too) or partial_starts' job
        # (explicit partial opt-in), and both are off by default here. The
        # outstanding need (after subtracting the already-assigned LF) is
        # police_cars:1, which the pool can't supply -> skipped, same as any
        # other normal-governed mission that can't be fully crewed.
        m1 = mission(1, 3, NEAR)  # firetrucks:1 (met by assigned LF) + police_cars:1
        plan = plan_dispatches(
            [m1], [vehicle(100, LF)], [building()], self.resolver,  # no FuStW anywhere
            DispatchPolicy(radius_km=40), assigned={1: [LF]},
        )
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "not enough vehicles: short police_cars x1")])

    def test_on_it_mission_respects_least_cash_ordering_once_fallen_through(self):
        # Same point from the ordering angle: an on_it mission (we have SOME
        # vehicle tracked against it, via `assigned`'s key -- a realistic
        # stand-in for "we sent something here a prior cycle") with no
        # top_up_scope/patient_scope coverage competes on equal footing for
        # the one scarce LF in the pool -- it doesn't jump the "least cash
        # first" queue ahead of a cheaper fresh mission just because we're
        # already on it. (Before this fix, the priority pass handled on_it
        # missions unconditionally, BEFORE this ordering ever ran, so the
        # expensive on_it mission would have won the LF instead.)
        on_it_expensive = mission(1, 1, NEAR, average_credits=9999)  # firetrucks:1, nothing of ours counts yet
        fresh_cheap = mission(2, 1, NEAR, average_credits=1)         # firetrucks:1, fresh
        plan = plan_dispatches(
            [on_it_expensive, fresh_cheap], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40, order=DispatchOrder.CREDITS_LOW, batch_size=1),
            assigned={1: []},  # "on_it" (a key exists) but contributes no actual delta
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].mission_id, 2)  # the cheap FRESH mission wins the LF
        # mission 1 doesn't bypass batch_size via the priority pass either --
        # it's capped out just like any other normal-governed candidate.
        self.assertIn((1, "beyond batch size"), plan.skipped)

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
        # 20+ patients also layers on an ambulance-per-patient requirement,
        # a NEF-generosity requirement (0% modeled chance here -> half of all
        # 20 patients, 10 NEFs -- Sasha, 2026-09-16), plus incident-command
        # scaling (always-on, unrelated to this feature) -- supply enough
        # ambulances (28), NEFs (29), an LNA (55) and OrgL (56) so the
        # mission stays satisfiable and the ELW pick is isolated to just the
        # SEG-ELW-vs-plain-ELW1 choice being tested.
        m = mission(1, 2, NEAR, possible_patients_count=20)
        ambulances = [vehicle(200 + i, 28) for i in range(20)]
        nefs = [vehicle(400 + i, 29) for i in range(10)]
        command = [vehicle(300, 55), vehicle(301, 56)]
        vehicles = self._mission2_support() + ambulances + nefs + command + [vehicle(10, 59), vehicle(11, 3)]
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
        # Mission 1 is OUR OWN already-open mission (on_it, via `assigned`).
        # With no patient/top-up scope covering it, its actual DISPATCH now
        # happens via the main loop's ordinary "normal" governing (it falls
        # through _priority_pass's send gate) -- but _priority_pass still
        # reserves its vehicle type UNCONDITIONALLY for every on_it mission
        # regardless of that gate, so this still proves what its name claims:
        # the reservation itself is contributed via the priority pass. A
        # SECOND boat is idle and unclaimed by mission 1's own dispatch --
        # reservation must still hold it back from the fresh mission (not
        # just "whatever's left over").
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

    def test_reservation_holds_even_when_the_on_it_mission_itself_is_out_of_scope(self):
        # Mission 1 is on_it (we have a vehicle assigned) but ALLIANCE, and
        # mission_source here only covers OWN -- with no patient/top-up scope
        # covering it either, it gets zero special treatment: it falls
        # through _priority_pass's send gate, then the main loop skips it as
        # "out of scope" before ever reaching THAT loop's own reservation
        # call. Its type must still be reserved regardless (an on_it mission
        # is reserved UNCONDITIONALLY in the priority pass, independent of
        # whether it goes on to actually get dispatched anywhere) -- a fresh
        # OWN mission needing the same boat must not poach the only spare.
        ours_alliance = mission(1, 1, NEAR, is_alliance=True, vehicle_state=1,
                                 missing_text='{"vehicles":"1 Boot","personnel":"","other":""}')
        fresh_own = mission(2, 1, NEAR2, vehicle_state=0,
                            missing_text='{"vehicles":"1 Boot","personnel":"","other":""}')
        boats = [vehicle(100, 70), vehicle(101, 70)]
        assigned = {1: [999]}  # on_it via an irrelevant type -- doesn't satisfy the boat need
        policy = DispatchPolicy(radius_km=40, mission_source=frozenset({MissionSource.OWN}))
        plan = plan_dispatches([ours_alliance, fresh_own], boats, [building()], self.resolver,
                               policy, assigned=assigned)
        self.assertEqual(plan.planned, [])  # neither mission gets anything
        self.assertEqual(plan.skipped, [
            (1, "out of scope"),                        # mission 1 gets nothing itself...
            (2, "not enough vehicles: short Boot x1"),   # ...but its reservation still blocks mission 2
        ])

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
        # If we've ALREADY sent a matching vehicle that's still EN ROUTE (S3)
        # and fully covers the mentioned category, the outstanding need nets
        # to zero and nothing is reserved. missing_text's own "1 Boot" is
        # already net of on-scene (S4) vehicles (docs/live-data-model.md), so
        # it keeps showing "1 Boot" needed the whole time our boat is en
        # route -- that's expected, not "hasn't caught up yet" -- and the
        # en-route boat is what nets it to zero on OUR side.
        ours = mission(1, 1, NEAR, vehicle_state=1,
                       missing_text='{"vehicles":"1 Boot","personnel":"","other":""}')
        fresh = mission(2, 1, NEAR2, vehicle_state=0,
                        missing_text='{"vehicles":"1 Boot","personnel":"","other":""}')
        boat_for_fresh = vehicle(100, 70)
        assigned = {1: [70]}       # a boat already assigned (S3+S4) to mission 1
        en_route = {1: [70]}       # ... specifically still en route (S3)
        plan = self.plan([ours, fresh], [boat_for_fresh], assigned=assigned, en_route=en_route)
        planned_ids = {p.mission_id for p in plan.planned}
        self.assertIn(2, planned_ids)

    # -- D7: _priority_pass routes through _plan_one (Sasha, 2026-10-27) ------
    # Closes the four asymmetries the dispatch-pipeline-map artifact found
    # between _priority_pass's old inline coverage computation and _plan_one:
    # a parasite/top-up/patient reinforcement used to get none of _plan_one's
    # rules. See docs/dispatch-pipeline-rebuild.{plan,acceptance}.md D7.

    def test_parasite_reinforcement_on_red_mission_gets_rule1_bypass(self):
        # AC-M2-06: a parasite contribution to a RED mission now routes
        # through _plan_one, so Rule 1's red-bypass applies -- a stale/wrong
        # `assigned` entry no longer silently zeroes the requirement.
        m = mission(1, 1, NEAR, is_alliance=True, vehicle_state=1)  # firetrucks:1, begun
        m.status = "red"
        spare_lf = vehicle(100, LF)
        assigned = {1: [LF]}  # stale: claims an LF is already there; nothing real is
        policy = DispatchPolicy(radius_km=40, parasite_scope=ModeScope.ALLIANCE,
                                 parasite_scarce_threshold=0)
        plan = plan_dispatches([m], [spare_lf], [building()], self.resolver, policy, assigned=assigned)
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].vehicle_ids, (100,))
        # NOTE: `assigned={1: [LF]}` makes this mission `on_it`, which forces
        # is_parasite False (parasite requires NOT on_it) -- it's handled by
        # the main loop's own normal governing, not _priority_pass's parasite
        # branch, so it's OUR reinforcement and DOES count as a real alarm.
        self.assertTrue(plan.planned[0].counts_as_alarm)

    def test_topup_reinforcement_tops_up_firefighter_minimum(self):
        # AC-M2-07: an on_it, top-up-covered mission below its
        # min_fire_personnel floor now gets extra firetrucks via _plan_one's
        # fire-personnel top-up -- previously only a main-loop send got this.
        catalog = MissionCatalog.from_payload([
            {"id": "900", "name": "Test", "requirements": {},
             "additional": {"average_min_fire_personnel": 9}},  # == one LF's crew exactly
        ])
        resolver = MissionRequirementResolver(catalog, self.lib_for_engine)
        m = mission(1, 900, NEAR)
        policy = DispatchPolicy(radius_km=40, top_up_scope=ModeScope.BOTH)
        lf = vehicle(100, LF)
        # on_it via a type that isn't a firetruck, so _fire_crew doesn't count
        # it as already-assigned crew and trivially zero out fire_needed.
        plan = plan_dispatches([m], [lf], [building()], resolver, policy, assigned={1: [900000]})
        self.assertEqual(len(plan.planned), 1)
        self.assertIn(100, plan.planned[0].vehicle_ids)
        self.assertTrue(plan.planned[0].counts_as_alarm)  # OUR reinforcement

    def test_topup_reinforcement_prefers_seg_elw_on_mass_casualty(self):
        # AC-M2-08: an on_it, top-up-covered mass-casualty mission now gets
        # the SEG-ELW preference through _plan_one -- _priority_pass's old
        # inline select_coverage call never passed specialist_type_ids/
        # preferred_type_ids at all.
        m = mission(1, 2, NEAR, possible_patients_count=20)
        ambulances = [vehicle(200 + i, 28) for i in range(20)]
        nefs = [vehicle(400 + i, 29) for i in range(10)]
        command = [vehicle(300, 55), vehicle(301, 56)]
        vehicles = self._mission2_support() + ambulances + nefs + command + [vehicle(10, 59), vehicle(11, 3)]
        policy = DispatchPolicy(radius_km=40, top_up_scope=ModeScope.BOTH)
        # on_it via a type that isn't part of this mission's own requirements,
        # so the delta doesn't trivially zero anything out -- just forces
        # on_it_here routing through _priority_pass.
        plan = plan_dispatches([m], vehicles, [building()], self.resolver, policy, assigned={1: [900000]})
        self.assertTrue(plan.planned)
        mp = plan.planned[0]
        self.assertTrue(mp.satisfied)
        self.assertIn(10, mp.vehicle_ids)      # SEG-ELW preferred
        self.assertNotIn(11, mp.vehicle_ids)
        self.assertTrue(mp.counts_as_alarm)  # OUR reinforcement

    def test_parasite_mzb_send_gets_water_rescue_escort(self):
        # AC-M2-09: a parasite-sent MZB now gets its opportunistic
        # water-rescue escort through _plan_one -- _priority_pass's old
        # inline select_coverage call never ran _add_water_rescue_escort.
        m = self._mzb_mission(is_alliance=True, vehicle_state=1)  # "1 Boot" revealed, begun
        mzb = vehicle(100, 70, building_id=1)
        diver = vehicle(101, 63, building_id=1)
        water = vehicle(102, 64, building_id=1)
        policy = DispatchPolicy(radius_km=40, parasite_scope=ModeScope.ALLIANCE,
                                 parasite_scarce_threshold=0)
        plan = plan_dispatches([m], [mzb, diver, water], [building()], self.resolver, policy)
        self.assertTrue(plan.planned)
        self.assertEqual(set(plan.planned[0].vehicle_ids), {100, 101, 102})
        self.assertFalse(plan.planned[0].counts_as_alarm)  # still a parasite send

    # -- S3/S4 delta scoping (Sasha, 2026-10-27) -------------------------------
    # missing_text is already net of on-scene (S4) vehicles
    # (docs/live-data-model.md: "Noch benötigt = fehlend − Auf Anfahrt") --
    # _apply_delta must subtract only en-route (S3) against a revealed
    # requirement, never S4 again on top, or it silently under-reports what's
    # still needed. See docs/dispatch-pipeline-rebuild.{plan,acceptance}.md.

    def test_revealed_requirement_subtracts_en_route_only_not_on_scene_too(self):
        # AC-M1-01: missing_text says "1 ELW 1" (already net of the one ELW1
        # on scene). The SECOND, en-route ELW1 is what nets this to zero --
        # the on-scene one must NOT also be subtracted a second time.
        m = mission(1, 1, NEAR, vehicle_state=1,
                    missing_text='{"vehicles":"1 ELW 1","personnel":"","other":""}')
        spare_elw1 = vehicle(200, 3)
        assigned = {1: [3, 3]}   # one on-scene + one en-route ELW1
        en_route = {1: [3]}      # ... only the en-route one
        plan = self.plan([m], [spare_elw1], assigned=assigned, en_route=en_route)
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "already covered")])

    def test_revealed_requirement_without_en_route_still_dispatches(self):
        # AC-M1-02 (the original live symptom, reproduced without any stale-
        # tracking bug): same revealed need, one ELW1 on scene, but nothing
        # actually en route -- one more ELW1 must still be sent.
        m = mission(1, 1, NEAR, vehicle_state=1,
                    missing_text='{"vehicles":"1 ELW 1","personnel":"","other":""}')
        spare_elw1 = vehicle(200, 3)
        assigned = {1: [3]}      # the one on-scene ELW1 only
        plan = self.plan([m], [spare_elw1], assigned=assigned, en_route={})
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].vehicle_ids, (200,))

    def test_catalogue_fallback_still_subtracts_en_route_and_on_scene(self):
        # AC-M1-03, regression guard: a mission that has NEVER revealed
        # missing_text uses the raw catalogue total (un-netted) -- both
        # en-route and on-scene vehicles must still come off it, unchanged
        # from before the S3/S4 split existed.
        m = mission(1, 3, NEAR)  # type 3: firetrucks:1, police_cars:1, no missing_text
        spare = vehicle(200, FUSTW)
        assigned = {1: [LF, FUSTW]}   # one firetruck (on scene) + one police car (en route)
        en_route = {1: [FUSTW]}
        plan = self.plan([m], [spare], assigned=assigned, en_route=en_route)
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "already covered")])

    def test_casualty_layer_keeps_subtracting_en_route_and_on_scene(self):
        # AC-M1-07, non-regression: the casualty-driven layer (ambulances,
        # NEF, etc.) is NOT scoped by delta_scope -- patients_count reflects
        # real remaining casualties, which neither an en-route nor an
        # on-scene ambulance changes on its own, so both must still count.
        # (Mission type 1 also needs a firetruck and the NEF-generosity
        # heuristic adds NEFs -- neither is provisioned here, so the mission
        # stays short on THOSE; the point is ambulances specifically must
        # NOT also show up short, which would mean an on-scene RTW got
        # double-subtracted against.)
        m = mission(1, 1, NEAR, patients_count=2, possible_patients_count=2)
        spare_rtw = vehicle(200, 28)
        assigned = {1: [28, 28]}   # one on-scene + one en-route RTW
        en_route = {1: [28]}
        plan = self.plan([m], [spare_rtw], assigned=assigned, en_route=en_route)
        self.assertEqual(plan.planned, [])
        [(_mid, reason)] = plan.skipped
        self.assertNotIn("ambulances", reason)

    # -- unstick-first ordering (Sasha, 2026-10-07) ----------------------------
    # "a 30 vehicle mission that waits for 1 single LF can be unstuck, freeing
    # up 29 more vehicles" -- any mission with vehicles already ARRIVED is
    # ranked by closeness to completion (still_missing/arrived, ascending)
    # and dispatched -- fully, never partially -- before any fresh candidate,
    # regardless of `order`, and exempt from `batch_size`.

    def _fillable_resolver(self, *type_specs):
        """A resolver over just the given (id, requirements, credits) mission
        types -- these tests need real, known totals to compute ratios
        against, not the shared fixture's."""
        from app.domain.catalog import MissionCatalog, MissionType
        from app.domain.resolver import MissionRequirementResolver

        types = {
            tid: MissionType(id=tid, name=f"T{tid}", average_credits=credits,
                              requirements=reqs, chances={})
            for tid, reqs, credits in type_specs
        }
        return MissionRequirementResolver(MissionCatalog(types), self.lib_for_engine)

    def test_unstick_first_outranks_order_for_a_fillable_mission(self):
        # Mission 1 wants 5 firetrucks total, missing_text now reveals only 1
        # still missing -> 4 arrived, ratio 1/4. Mission 2 is a fresh, cheap
        # mission CREDITS_LOW would normally put first. The pool only has one
        # LF -- it must go to the almost-finished mission, not the cheaper
        # fresh one.
        resolver = self._fillable_resolver((950, {"firetrucks": 5}, 1000), (951, {"firetrucks": 1}, 10))
        almost_done = mission(1, 950, NEAR, vehicle_state=1,
                              missing_text='{"vehicles":"1 Löschfahrzeuge (LF)","personnel":"","other":""}')
        fresh_cheap = mission(2, 951, NEAR2)
        plan = plan_dispatches(
            [almost_done, fresh_cheap], [vehicle(100, LF)], [building()], resolver,
            DispatchPolicy(radius_km=40, order=DispatchOrder.CREDITS_LOW),
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].mission_id, 1)
        self.assertTrue(plan.planned[0].satisfied)
        self.assertIn((2, "not enough vehicles: short firetrucks x1"), plan.skipped)

    def test_unstick_unfillable_mission_is_skipped_not_partially_sent(self):
        # Needs 2 more LF to finish; the pool only has 1 LF anywhere -- ruled
        # unfillable and skipped (no partial trickle). Mission 2 needs a
        # DIFFERENT class (police, not firetrucks) so it isn't blocked by
        # mission 1's own (correct, pre-existing) reservation of the LF type
        # it's still legitimately waiting on -- it still gets handled normally.
        resolver = self._fillable_resolver((950, {"firetrucks": 5}, 1000), (951, {"police_cars": 1}, 10))
        almost_done = mission(1, 950, NEAR, vehicle_state=1,
                              missing_text='{"vehicles":"2 Löschfahrzeuge (LF)","personnel":"","other":""}')
        fresh_cheap = mission(2, 951, NEAR2)
        plan = plan_dispatches(
            [almost_done, fresh_cheap], [vehicle(100, LF), vehicle(101, FUSTW)], [building()], resolver,
            DispatchPolicy(radius_km=40),
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].mission_id, 2)
        # The shortfall is reported as what's still missing AFTER using the
        # one LF coverage could already find toward it (2 wanted, 1 on
        # hand -> 1 further short), not the full original count.
        self.assertIn((1, "not enough vehicles: short Löschfahrzeuge (LF) x1"), plan.skipped)

    def test_unstick_fills_are_exempt_from_batch_size(self):
        resolver = self._fillable_resolver((950, {"firetrucks": 5}, 1000))
        almost_done = mission(1, 950, NEAR, vehicle_state=1,
                              missing_text='{"vehicles":"1 Löschfahrzeuge (LF)","personnel":"","other":""}')
        plan = plan_dispatches(
            [almost_done], [vehicle(100, LF)], [building()], resolver,
            DispatchPolicy(radius_km=40, batch_size=0),
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].mission_id, 1)

    def test_unstick_ranks_closest_to_complete_first_among_several_fillable(self):
        # Mission 1: 30 wanted, 1 missing -> arrived 29, ratio ~0.034 (nearly
        # done). Mission 2: 2 wanted, 1 missing -> arrived 1, ratio 1.0
        # (barely started), and worth far more credits. Only one LF exists
        # anywhere -- closeness to completion must win over both credits and
        # list order (mission 2 listed first here, on purpose).
        resolver = self._fillable_resolver(
            (950, {"firetrucks": 30}, 100), (951, {"firetrucks": 2}, 9999),
        )
        barely_started = mission(2, 951, NEAR2, vehicle_state=1,
                                 missing_text='{"vehicles":"1 Löschfahrzeuge (LF)","personnel":"","other":""}')
        almost_done = mission(1, 950, NEAR, vehicle_state=1,
                              missing_text='{"vehicles":"1 Löschfahrzeuge (LF)","personnel":"","other":""}')
        plan = plan_dispatches(
            [barely_started, almost_done], [vehicle(100, LF)], [building()], resolver,
            DispatchPolicy(radius_km=40, order=DispatchOrder.CREDITS_HIGH),
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].mission_id, 1)

    def test_unstick_does_not_apply_to_an_unknown_mission_type(self):
        # No catalogue entry at all -> no computable total -> the fillable
        # check's `mtype is not None` guard simply doesn't fire, falling
        # through to the normal fresh-candidate path unchanged. That path
        # itself can't resolve anything for an unknown type either (same as
        # today, unrelated to this feature) -- the point here is only that
        # it's handled as "no requirements found", not mis-ranked or errored.
        unknown = mission(1, 999, NEAR, vehicle_state=1,
                          missing_text='{"vehicles":"1 Löschfahrzeuge (LF)","personnel":"","other":""}')
        plan = plan_dispatches(
            [unknown], [vehicle(100, LF)], [building()], self.resolver,
            DispatchPolicy(radius_km=40),
        )
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "no requirements found")])

    def test_unstick_excludes_a_fresh_untouched_mission_even_with_a_known_total(self):
        # Code-review finding, 2026-10-08: vehicle_state == 0 alone must keep
        # a mission out of the fillable bucket -- nothing could have arrived
        # yet, whatever the catalogue total says. Pool scarce enough that
        # only one of two equal-cost candidates gets the LF; if the untouched
        # mission wrongly jumped the queue it would win despite listed second.
        resolver = self._fillable_resolver((950, {"firetrucks": 1}, 100), (951, {"firetrucks": 1}, 100))
        fresh_first = mission(2, 951, NEAR2)
        untouched_second = mission(1, 950, NEAR)  # vehicle_state=0 default
        plan = plan_dispatches(
            [untouched_second, fresh_first], [vehicle(100, LF)], [building()], resolver,
            DispatchPolicy(radius_km=40),
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].mission_id, 2)  # order (nearest), not a bogus unstick jump

    def test_unstick_excludes_a_mission_with_an_unresolvable_catalogue_class(self):
        # Code-review finding, 2026-10-08: vehicle_requirements() silently
        # DROPS a catalogue class it can't map to any vehicle type (a real,
        # tested, documented behavior -- see tests/test_resolver.py's
        # test_unknown_class_is_reported_not_dropped) -- so total_wanted
        # (every declared class) and still_missing_raw (only the resolvable
        # ones) would otherwise enumerate different class sets, fabricating
        # an "arrived" count. Even WITH vehicle_state=1, this must not be
        # classified fillable -- proven here via batch_size=0: a fillable
        # dispatch is exempt from it (see the batch-exemption test above), so
        # if this mission wrongly entered that bucket it would still be
        # dispatched despite batch_size=0; correctly excluded, it must go
        # through the normal batch-respecting path and get capped out.
        resolver = self._fillable_resolver((950, {"totally_unknown_class": 3, "firetrucks": 1}, 9999))
        buggy = mission(1, 950, NEAR, vehicle_state=1)
        plan = plan_dispatches(
            [buggy], [vehicle(100, LF)], [building()], resolver,
            DispatchPolicy(radius_km=40, batch_size=0),
        )
        self.assertEqual(plan.planned, [])
        self.assertEqual(plan.skipped, [(1, "beyond batch size")])

    def test_unstick_never_partial_even_with_partial_starts_on(self):
        # partial_starts is scoped to fresh sends only (established earlier
        # this session) -- confirm it does NOT leak into the unstick pass:
        # an unfillable-this-cycle mission still gets nothing, not a partial.
        resolver = self._fillable_resolver((950, {"firetrucks": 5}, 1000))
        almost_done = mission(1, 950, NEAR, vehicle_state=1,
                              missing_text='{"vehicles":"2 Löschfahrzeuge (LF)","personnel":"","other":""}')
        plan = plan_dispatches(
            [almost_done], [vehicle(100, LF)], [building()], resolver,
            DispatchPolicy(radius_km=40, partial_starts=True),
        )
        self.assertEqual(plan.planned, [])
        self.assertIn((1, "not enough vehicles: short Löschfahrzeuge (LF) x1"), plan.skipped)

    def test_unstick_applies_to_topup_governed_missions_too(self):
        # The only exclusion is governing == "patient" -- a topup-governed
        # fillable candidate still jumps ahead of a fresh one, same as normal.
        resolver = self._fillable_resolver((950, {"firetrucks": 5}, 1000), (951, {"firetrucks": 1}, 9999))
        almost_done = mission(1, 950, NEAR, is_alliance=True, vehicle_state=1,
                              missing_text='{"vehicles":"1 Löschfahrzeuge (LF)","personnel":"","other":""}')
        fresh_expensive = mission(2, 951, NEAR2, is_alliance=False)
        plan = plan_dispatches(
            [almost_done, fresh_expensive], [vehicle(100, LF)], [building()], resolver,
            DispatchPolicy(radius_km=40, top_up_scope=ModeScope.ALLIANCE, order=DispatchOrder.CREDITS_HIGH),
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].mission_id, 1)  # topup-governed, but still unstick-first

    # -- Sticky vehicle_state survives a reverted live flag (Sasha, 2026-10-22) -
    # vehicle_state is transient on the GAME's side (confirmed live, 2026-10-01):
    # it reverts to 0 the instant nobody is CURRENTLY on scene, even on a
    # mission that's genuinely already open and just had its responders finish
    # a shift and step away (observed live on a riot mission whose FuStW had
    # already contributed and moved on, still needing more BePol). Every
    # dispatch.py call site that cares "has this mission ever been responded
    # to" must use resolver.effective_count_revealed (sticky), not the raw
    # live flag, or it loses reservation/unstick-first/topup/parasite
    # eligibility the moment the live flag reverts.

    def test_topup_eligibility_persists_after_missing_text_goes_quiet(self):
        # D2 (Sasha, 2026-10-27): top-up eligibility now keys off
        # missing_text_ever_revealed, not vehicle_state -- same sticky
        # protection against a transient flicker, just a different signal
        # (missing_text going quiet again means "fully covered", not "never
        # began" -- see missing_text_ever_revealed's own docstring).
        policy = DispatchPolicy(radius_km=40, top_up_scope=ModeScope.OWN)
        cycle1 = mission(1, 3, NEAR, vehicle_state=1,
                         missing_text='{"vehicles":"1 Löschfahrzeuge (LF), 1 FuStW",'
                                      '"personnel":"","other":""}')
        self.plan([cycle1], [vehicle(100, LF), vehicle(101, FUSTW)], policy)
        cycle2 = mission(1, 3, NEAR, vehicle_state=0, missing_text="")
        plan = self.plan([cycle2], [vehicle(102, FUSTW)], policy)
        self.assertNotIn((1, "not begun (top-up)"), plan.skipped)

    def test_unstick_first_eligibility_persists_after_vehicle_state_reverts_to_zero(self):
        resolver = self._fillable_resolver((950, {"firetrucks": 5}, 1000), (951, {"firetrucks": 1}, 10))
        cycle1 = mission(1, 950, NEAR, vehicle_state=1,
                         missing_text='{"vehicles":"1 Löschfahrzeuge (LF)","personnel":"","other":""}')
        plan_dispatches([cycle1], [], [building()], resolver, DispatchPolicy(radius_km=40))
        cycle2 = mission(1, 950, NEAR, vehicle_state=0,
                         missing_text='{"vehicles":"1 Löschfahrzeuge (LF)","personnel":"","other":""}')
        fresh_cheap = mission(2, 951, NEAR2)
        plan = plan_dispatches(
            [cycle2, fresh_cheap], [vehicle(100, LF)], [building()], resolver,
            DispatchPolicy(radius_km=40, order=DispatchOrder.CREDITS_LOW),
        )
        self.assertEqual(len(plan.planned), 1)
        self.assertEqual(plan.planned[0].mission_id, 1)  # unstick-first priority preserved

    def test_parasite_others_on_persists_after_vehicle_state_reverts_to_zero(self):
        # parasite_scarce_threshold=0: this test's tiny 1-vehicle pool would
        # otherwise read as "scarce" under the default threshold and get
        # withheld from parasite entirely -- an unrelated feature, isolated
        # here so it doesn't mask what's actually under test.
        policy = DispatchPolicy(radius_km=40, parasite_scope=ModeScope.ALLIANCE,
                                parasite_scarce_threshold=0)
        cycle1 = mission(1, 3, NEAR, is_alliance=True, vehicle_state=1)
        self.plan([cycle1], [vehicle(100, LF), vehicle(101, FUSTW)], policy)
        cycle2 = mission(1, 3, NEAR, is_alliance=True, vehicle_state=0)
        plan = self.plan([cycle2], [vehicle(102, FUSTW)], policy)
        # A parasite leech send only fires when `others_on` reads true; a
        # plain normal-governed candidate wouldn't be actioned here at all
        # (own_user_id unset makes is_alliance=True fall outside the default
        # mission_source) -- any planned entry proves the parasite path fired.
        self.assertEqual([mp.mission_id for mp in plan.planned], [1])

    def test_reservation_persists_after_vehicle_state_reverts_to_zero(self):
        mtype = self.resolver.catalog.get(3)
        cycle1 = mission(1, 3, NEAR, vehicle_state=1)
        self.resolver.vehicle_requirements(cycle1, mtype)  # unrelated to stickiness, just realistic use
        self.resolver.effective_count_revealed(cycle1)  # cycle 1: establishes sticky memory
        cycle2 = mission(1, 3, NEAR, vehicle_state=0)
        reserved: set[int] = set()
        _reserve_vehicle_group(reserved, cycle2, mtype, self.resolver, en_route={}, assigned={},
                               patient_governed=False)
        self.assertTrue(reserved, "reservation should still fire after vehicle_state reverts to 0")

    # -- towed (AB/Anh) distance penalty (Sasha, 2026-10-08) -------------------
    # A WLF (or similar) that tows an AB/Anh is locked to it for the whole job
    # -- prefer an equally-eligible self-propelled alternative unless the
    # towed one is meaningfully closer (by more than the 7km penalty).

    def test_towed_variant_loses_the_tiebreak_at_equal_distance(self):
        # Dekon-P (53, self-propelled) vs AB-Dekon-P (54, towed) -- same
        # position, so only the penalty decides.
        m = mission(1, 1, NEAR, vehicle_state=1,
                   missing_text='{"vehicles":"1 Dekon-P","personnel":"","other":""}')
        self_propelled = vehicle(100, 53, pos=NEAR)
        towed = vehicle(101, 54, pos=NEAR)
        plan = self.plan([m], [towed, self_propelled])  # towed listed first, on purpose
        self.assertEqual(plan.planned[0].vehicle_ids, (100,))

    def test_towed_variant_still_wins_when_meaningfully_closer(self):
        # Towed one is right at the scene; the self-propelled alternative is
        # far away -- the penalty (7km) must not override a real, larger gap.
        m = mission(1, 1, NEAR, vehicle_state=1,
                   missing_text='{"vehicles":"1 Dekon-P","personnel":"","other":""}')
        towed_here = vehicle(101, 54, pos=NEAR)
        self_propelled_far = vehicle(100, 53, pos=FAR)
        plan = self.plan([m], [towed_here, self_propelled_far])
        self.assertEqual(plan.planned[0].vehicle_ids, (101,))

    def test_towed_variant_without_a_self_propelled_sibling_is_unpenalized(self):
        # AB-Tank (117) has no self-propelled alternative anywhere in our
        # data -- confirm it's genuinely excluded from the penalty set, not
        # just untested (picked at equal distance against a DIFFERENT, also
        # towed type with no penalty either, to isolate the exclusion itself).
        self.assertNotIn(117, _TOWED_WITH_SELF_PROPELLED_ALTERNATIVE)
        self.assertIn(54, _TOWED_WITH_SELF_PROPELLED_ALTERNATIVE)

    def test_towed_penalty_set_matches_a_fresh_derivation_from_committed_data(self):
        # Drift canary (code review, 2026-10-08): _TOWED_WITH_SELF_PROPELLED_
        # ALTERNATIVE is a hand-derived snapshot of data/reference/lssm-
        # missing-requirements.json + data/de/vehicles/*.mscv's own vehicle
        # names, hardcoded rather than loaded at runtime (that data changes
        # only on a deliberate, reviewed refresh -- this project's own
        # precedent for the LAST such refresh, this same session, was a
        # manual diff-and-update, not an auto-reload). This test re-derives
        # the set fresh from the committed files and fails loudly if a future
        # data refresh changes which AB/Anh types have a self-propelled
        # sibling, instead of the hardcoded set silently drifting stale.
        import glob
        import re

        ab_anh: dict[int, str] = {}
        for f in glob.glob(str(VEHICLES_DIR / "*.mscv")):
            m = re.search(r"(\d+)\.mscv$", f)
            if not m:
                continue
            tid = int(m.group(1))
            text = Path(f).read_text(encoding="utf-8")
            bm = re.search(r'"base"\s*:\s*\[(.*?)\]', text, re.S)
            if not bm:
                continue
            names = re.findall(r'"([^"]+)"', bm.group(1))
            first = names[0] if names else ""
            if first.startswith("AB-") or first.startswith("Anh "):
                ab_anh[tid] = first

        lssm_path = BASE.parent / "data" / "reference" / "lssm-missing-requirements.json"
        lssm = json.loads(lssm_path.read_text(encoding="utf-8"))
        derived: set[int] = set()
        for entry in lssm["vehiclesByRequirement"]:
            vids = set(entry.get("vehicles", []))
            present = vids & set(ab_anh)
            if present and (vids - set(ab_anh)):  # at least one non-AB/Anh sibling in the SAME entry
                derived |= present

        self.assertEqual(derived, _TOWED_WITH_SELF_PROPELLED_ALTERNATIVE)


if __name__ == "__main__":
    unittest.main()
