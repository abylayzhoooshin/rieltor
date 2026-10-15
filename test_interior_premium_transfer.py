import unittest

from interior_premium_transfer import (
    STRICT_RADIUS_KM,
    build_premium_signals,
    strict_premium_evidence,
)
from interior_price_adjustment import EVALUATOR_VERSION


def row(listing_id, street, house, score, pm2, *, lon=76.94, rooms="2"):
    return {
        "id": listing_id,
        "_street_norm": street,
        "_house_num_norm": house,
        "_rooms": rooms,
        "_price_m2": pm2,
        "square_m2": 50,
        "_floor_total": 5,
        "_lat": 43.25,
        "_lon": lon,
        "_complex_key": "",
        "photo_set_hash": listing_id,
        "first_seen_at": "2026-01-01T00:00:00+00:00",
        "interior_status": "ready",
        "interior_score": score,
        "interior_coverage": "sufficient",
        "interior_evaluator_version": EVALUATOR_VERSION,
    }


def class_score(pm2, rooms, room_bounds, q25, q75):
    return pm2 / 10000


class InteriorPremiumTransferTests(unittest.TestCase):
    def setUp(self):
        self.target = row("target", "target", "1", 6.4, 9000)
        self.weak = row("weak", "target", "1", 4.4, 5000)
        self.pool = [
            self.weak,
            row("d1-low", "donor", "1", 4.3, 5000, lon=76.945),
            row("d1-high", "donor", "1", 6.5, 7500, lon=76.945),
            row("d2-low", "donor", "2", 4.2, 5100, lon=76.95),
            row("d2-high", "donor", "2", 6.6, 7140, lon=76.95),
        ]

    def test_two_buildings_create_strict_evidence(self):
        signals = build_premium_signals(self.pool, class_score, {}, 0, 1)
        evidence = strict_premium_evidence(
            self.target, self.pool, signals, class_score, {}, 0, 1
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["donor_buildings"], 2)
        self.assertEqual(evidence["transition"], "4_to_6")
        self.assertGreater(evidence["premium_pct"], 39)

    def test_one_building_is_not_evidence(self):
        one_donor_pool = self.pool[:3]
        signals = build_premium_signals(one_donor_pool, class_score, {}, 0, 1)
        evidence = strict_premium_evidence(
            self.target, one_donor_pool, signals, class_score, {}, 0, 1
        )
        self.assertIsNone(evidence)

    def test_donors_outside_one_kilometre_are_excluded_without_weights(self):
        self.assertEqual(STRICT_RADIUS_KM, 1.0)
        far_pool = [
            self.weak,
            row("d1-low", "donor", "1", 4.3, 5000, lon=76.96),
            row("d1-high", "donor", "1", 6.5, 7500, lon=76.96),
            row("d2-low", "donor", "2", 4.2, 5100, lon=76.97),
            row("d2-high", "donor", "2", 6.6, 7140, lon=76.97),
        ]
        signals = build_premium_signals(far_pool, class_score, {}, 0, 1)
        evidence = strict_premium_evidence(
            self.target, far_pool, signals, class_score, {}, 0, 1
        )
        self.assertIsNone(evidence)


if __name__ == "__main__":
    unittest.main()
