import unittest

from app.domain.transport import (
    TransportKind,
    TransportPolicy,
    TransportRequest,
    TransportTarget,
    route_transport,
    route_transports,
)

SCENE = (52.5200, 13.4050)
NEAR = (52.5300, 13.4100)   # ~1.2 km
MID = (52.5600, 13.4500)    # ~5 km
FAR = (53.5500, 9.9900)     # Hamburg, ~250 km


def patient(vid=1, specialty=None, origin=SCENE):
    return TransportRequest(vid, TransportKind.PATIENT, origin, required_specialty=specialty)


def prisoner(vid=1, origin=SCENE):
    return TransportRequest(vid, TransportKind.PRISONER, origin)


def hospital(bid, pos, beds=5, specialties=frozenset(), tax=0, own=True):
    return TransportTarget(bid, pos, beds, frozenset(specialties), tax, own)


class RouteTransportTests(unittest.TestCase):
    def test_picks_nearest_hospital_with_capacity(self):
        targets = [hospital(1, MID), hospital(2, NEAR)]
        d = route_transport(patient(), targets, TransportPolicy())
        self.assertEqual(d.target_building_id, 2)  # nearest

    def test_skips_full_hospital(self):
        targets = [hospital(1, NEAR, beds=0), hospital(2, MID, beds=3)]
        d = route_transport(patient(), targets, TransportPolicy())
        self.assertEqual(d.target_building_id, 2)

    def test_specialty_required(self):
        targets = [
            hospital(1, NEAR, specialties={"Chirurgie"}),
            hospital(2, MID, specialties={"Neurologie"}),
        ]
        d = route_transport(patient(specialty="Neurologie"), targets, TransportPolicy())
        self.assertEqual(d.target_building_id, 2)  # only #2 has Neurologie

    def test_out_of_range_returns_none(self):
        d = route_transport(patient(), [hospital(1, FAR)], TransportPolicy(max_distance_km=50))
        self.assertIsNone(d)

    def test_tax_ceiling_filters_alliance_hospital(self):
        targets = [hospital(1, NEAR, tax=20, own=False), hospital(2, MID, tax=0)]
        d = route_transport(patient(), targets, TransportPolicy(max_tax=10))
        self.assertEqual(d.target_building_id, 2)  # #1 exceeds tax cap

    def test_prefers_own_over_closer_alliance(self):
        targets = [hospital(1, NEAR, own=False, tax=0), hospital(2, MID, own=True)]
        d = route_transport(patient(), targets, TransportPolicy(prefer_own=True))
        self.assertEqual(d.target_building_id, 2)  # own wins despite being farther

    def test_prisoner_routing(self):
        cells = [TransportTarget(9, NEAR, free_capacity=1)]
        d = route_transport(prisoner(), cells, TransportPolicy())
        self.assertEqual(d.target_building_id, 9)
        self.assertIs(d.kind, TransportKind.PRISONER)


class RouteBatchTests(unittest.TestCase):
    def test_capacity_consumed_across_requests(self):
        # One near hospital with a single bed; two patients -> second goes to MID.
        targets = [hospital(1, NEAR, beds=1), hospital(2, MID, beds=5)]
        results = route_transports([patient(1), patient(2)], targets, TransportPolicy())
        chosen = [d.target_building_id for _, d in results]
        self.assertEqual(chosen, [1, 2])

    def test_unroutable_when_all_full(self):
        targets = [hospital(1, NEAR, beds=1)]
        results = route_transports([patient(1), patient(2)], targets, TransportPolicy())
        self.assertEqual(results[0][1].target_building_id, 1)
        self.assertIsNone(results[1][1])  # no bed left


if __name__ == "__main__":
    unittest.main()
