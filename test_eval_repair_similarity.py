import unittest

from eval_repair_similarity import (
    adaptive_influence_cap,
    bucket_for,
    relation_factor,
    repair_similarity,
)


class RepairSimilarityRulesTests(unittest.TestCase):
    def test_uncertain_transition_does_not_cross_boundary(self):
        self.assertEqual(repair_similarity(5.8, 6.2), 0.0)
        self.assertEqual(repair_similarity(6.2, 5.8), 0.0)

    def test_stable_good_band_accepts_6_3_and_6_9(self):
        self.assertGreater(repair_similarity(6.3, 6.9), 0.0)
        self.assertGreater(repair_similarity(6.9, 6.3), 0.0)

    def test_good_and_premium_bands_do_not_mix_automatically(self):
        self.assertEqual(repair_similarity(6.9, 7.0), 0.0)

    def test_non_complex_buildings_with_two_floor_gap_are_compatible(self):
        target = {"floor_total": 5}
        candidate = {"floor_total": 7}
        relation, weight = relation_factor(target, candidate, "3")
        self.assertEqual(relation, "non_complex_similar_floors")
        self.assertEqual(weight, 1.0)

    def test_different_complexes_have_reduced_confidence(self):
        target = {"complex_id": "a", "floor_total": 12}
        candidate = {"complex_id": "b", "floor_total": 12}
        relation, weight = relation_factor(target, candidate, "3")
        self.assertEqual(relation, "different_complex_same_class")
        self.assertLess(weight, 1.0)

    def test_supply_bucket_separates_one_same_building_from_no_match(self):
        result = {"cohort_size": 7, "benchmark_confidence": 0.3}
        target = {"complex_id": ""}
        one = bucket_for(result, {"same_building_n": 1, "outside_n": 0}, target)
        none = bucket_for(result, {"same_building_n": 0, "outside_n": 0}, target)
        self.assertEqual(one["match_supply"], "same_building_1_only")
        self.assertEqual(none["match_supply"], "no_similar_match")

    def test_adaptive_cap_needs_shortage_and_external_evidence(self):
        self.assertEqual(adaptive_influence_cap(2, 10), 0.0)
        self.assertEqual(adaptive_influence_cap(0, 0), 0.0)
        self.assertEqual(adaptive_influence_cap(1, 2), 0.25)
        self.assertEqual(adaptive_influence_cap(0, 3), 0.75)


if __name__ == "__main__":
    unittest.main()
