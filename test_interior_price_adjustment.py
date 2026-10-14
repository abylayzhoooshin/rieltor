import math
import unittest

from interior_price_adjustment import (
    EVALUATOR_VERSION,
    MAX_ABS_LOG_ADJUSTMENT,
    adjust_price_for_interior,
)


def scored(
    value,
    *,
    coverage="sufficient",
    status="ready",
    listing_id="1",
    price_m2=None,
):
    row = {
        "id": listing_id,
        "interior_status": status,
        "interior_score": value,
        "interior_coverage": coverage,
        "interior_evaluator_version": EVALUATOR_VERSION,
    }
    if price_m2 is not None:
        row["_price_m2"] = price_m2
    return row


class InteriorPriceAdjustmentTests(unittest.TestCase):
    def test_unknown_target_never_changes_price(self):
        cohorts = [{"weight": 1.0, "rows": [scored(5, listing_id="a"), scored(6, listing_id="b")]}]
        result = adjust_price_for_interior(5000, {}, cohorts)
        self.assertEqual(result["base_price_m2_adjusted"], 5000)
        self.assertFalse(result["interior_adjustment_applied"])

    def test_better_interior_raises_fair_price_and_worse_lowers_it(self):
        cohorts = [{"weight": 1.0, "rows": [scored(5, listing_id="a"), scored(5, listing_id="b")]}]
        better = adjust_price_for_interior(5000, scored(8), cohorts)
        worse = adjust_price_for_interior(5000, scored(2), cohorts)
        self.assertGreater(better["base_price_m2_adjusted"], 5000)
        self.assertLess(worse["base_price_m2_adjusted"], 5000)

    def test_adjustment_is_capped(self):
        cohorts = [{"weight": 1.0, "rows": [scored(1, listing_id="a"), scored(1, listing_id="b")]}]
        result = adjust_price_for_interior(5000, scored(10), cohorts)
        self.assertLessEqual(
            result["base_price_m2_adjusted"], 5000 * math.exp(MAX_ABS_LOG_ADJUSTMENT) + 1e-9,
        )

    def test_one_comparable_is_not_enough(self):
        cohorts = [{"weight": 1.0, "rows": [scored(5, listing_id="a")]}]
        result = adjust_price_for_interior(5000, scored(8), cohorts)
        self.assertEqual(result["base_price_m2_adjusted"], 5000)
        self.assertFalse(result["interior_adjustment_applied"])

    def test_other_evaluator_version_is_unknown(self):
        cohorts = [{"weight": 1.0, "rows": [scored(5, listing_id="a"), scored(5, listing_id="b")]}]
        target = scored(8)
        target["interior_evaluator_version"] = "changed-prompt"
        result = adjust_price_for_interior(5000, target, cohorts)
        self.assertEqual(result["base_price_m2_adjusted"], 5000)
        self.assertFalse(result["interior_adjustment_applied"])

    def test_partial_coverage_has_smaller_effect(self):
        cohorts = [{"weight": 1.0, "rows": [scored(5, listing_id="a"), scored(5, listing_id="b")]}]
        sufficient = adjust_price_for_interior(5000, scored(7), cohorts)
        partial = adjust_price_for_interior(5000, scored(7, coverage="partial"), cohorts)
        self.assertGreater(
            sufficient["base_price_m2_adjusted"], partial["base_price_m2_adjusted"],
        )

    def test_bad_interior_discount_is_stronger_than_equal_good_premium(self):
        cohorts = [{"weight": 1.0, "rows": [scored(5, listing_id="a"), scored(5, listing_id="b")]}]
        worse = adjust_price_for_interior(5000, scored(3), cohorts)
        better = adjust_price_for_interior(5000, scored(7), cohorts)
        worse_log = abs(math.log(worse["base_price_m2_adjusted"] / 5000))
        better_log = abs(math.log(better["base_price_m2_adjusted"] / 5000))
        self.assertGreater(worse_log, better_log)
        self.assertEqual(worse["interior_adjustment_beta"], 0.12)
        self.assertEqual(better["interior_adjustment_beta"], 0.04)

    def test_thin_sample_uses_individual_comparables(self):
        cohorts = [{
            "weight": 1.0,
            "rows": [
                scored(5.0, listing_id="a", price_m2=4000),
                scored(7.2, listing_id="b", price_m2=6000),
            ],
        }]
        result = adjust_price_for_interior(5000, scored(6.4), cohorts)
        self.assertEqual(result["interior_adjustment_method"], "individual_thin_sample")
        self.assertEqual(result["interior_individual_n"], 2)
        self.assertIsNotNone(result["interior_individual_price_m2"])

    def test_mid_sample_uses_individual_comparables(self):
        cohorts = [{
            "weight": 1.0,
            "rows": [
                scored(6.2, listing_id=str(index), price_m2=8000)
                for index in range(4)
            ],
        }]
        result = adjust_price_for_interior(5000, scored(6.4), cohorts)
        self.assertEqual(result["interior_adjustment_method"], "individual_mid_sample")
        self.assertGreater(
            result["base_price_m2_adjusted"],
            result["interior_legacy_price_m2"],
        )
        self.assertLessEqual(
            result["base_price_m2_adjusted"],
            5000 * math.exp(MAX_ABS_LOG_ADJUSTMENT) + 1e-9,
        )

    def test_mid_sample_uses_nearby_similar_repairs_despite_weaker_same_house(self):
        cohorts = [
            {
                "weight": 0.8306,
                "rows": [scored(4.4, listing_id="same-house", price_m2=6925)],
            },
            {
                "weight": 0.1694,
                "rows": [
                    scored(4.3, listing_id="near-1", price_m2=6732.6),
                    scored(4.2, listing_id="near-2", price_m2=6732.6),
                    scored(4.2, listing_id="near-3", price_m2=6925.0),
                    scored(6.2, listing_id="near-4", price_m2=7425.7),
                    scored(6.4, listing_id="near-5", price_m2=10000.0),
                    scored(6.1, listing_id="near-6", price_m2=10082.9),
                ],
            },
        ]
        result = adjust_price_for_interior(6729, scored(6.4), cohorts)
        self.assertEqual(result["interior_adjustment_method"], "individual_mid_sample")
        self.assertEqual(result["interior_individual_n"], 7)
        self.assertGreater(
            result["base_price_m2_adjusted"],
            result["interior_legacy_price_m2"],
        )
        self.assertLess(
            result["base_price_m2_adjusted"],
            result["interior_individual_price_m2"],
        )

    def test_many_comparables_blend_legacy_and_individual_estimates(self):
        cohorts = [{
            "weight": 1.0,
            "rows": [
                scored(6.2, listing_id=str(index), price_m2=6500)
                for index in range(16)
            ],
        }]
        result = adjust_price_for_interior(5000, scored(6.4), cohorts)
        self.assertEqual(result["interior_adjustment_method"], "individual_many_blend")
        self.assertGreater(
            result["base_price_m2_adjusted"],
            result["interior_legacy_price_m2"],
        )
        self.assertLess(
            result["base_price_m2_adjusted"],
            result["interior_individual_price_m2"],
        )

    def test_individual_similarity_cannot_override_cohort_priority(self):
        close_building = [scored(4.5, listing_id="close", price_m2=4000)]
        distant = [
            scored(4.5, listing_id=f"far-{index}", price_m2=10000)
            for index in range(10)
        ]
        cohorts = [
            {"weight": 0.9, "rows": close_building},
            {"weight": 0.1, "rows": distant},
        ]
        result = adjust_price_for_interior(5000, scored(4.5), cohorts)
        self.assertEqual(result["interior_adjustment_method"], "individual_weak_interior")
        self.assertLess(result["interior_individual_price_m2"], 5000)

    def test_partial_target_stays_on_legacy_formula(self):
        cohorts = [{
            "weight": 1.0,
            "rows": [
                scored(6.0, listing_id="a", price_m2=4000),
                scored(6.0, listing_id="b", price_m2=8000),
            ],
        }]
        result = adjust_price_for_interior(
            5000,
            scored(6.5, coverage="partial"),
            cohorts,
        )
        self.assertEqual(result["interior_adjustment_method"], "aggregate_reference")
        self.assertIsNone(result["interior_individual_price_m2"])

    def test_new_model_keeps_global_adjustment_cap(self):
        cohorts = [{
            "weight": 1.0,
            "rows": [
                scored(10, listing_id="a", price_m2=20000),
                scored(10, listing_id="b", price_m2=20000),
            ],
        }]
        result = adjust_price_for_interior(5000, scored(10), cohorts)
        self.assertLessEqual(
            result["base_price_m2_adjusted"],
            5000 * math.exp(MAX_ABS_LOG_ADJUSTMENT) + 1e-9,
        )

    def test_strict_external_evidence_may_exceed_ordinary_cap(self):
        cohorts = [{"weight": 1.0, "rows": [
            scored(4.2, listing_id="a", price_m2=5000),
            scored(4.4, listing_id="b", price_m2=5100),
        ]}]
        evidence = {
            "transition": "4_to_6",
            "premium_log": math.log(1.5),
            "premium_pct": 50.0,
            "donor_buildings": 2,
            "weak_base_n": 1,
            "confidence": 0.35,
        }
        result = adjust_price_for_interior(
            5000, scored(6.4), cohorts,
            external_premium_evidence=evidence,
        )
        self.assertAlmostEqual(result["base_price_m2_adjusted"], 7500)
        self.assertEqual(result["interior_adjustment_method"], "strict_external_premium")
        self.assertGreater(result["interior_price_adjustment_pct"], 16)

    def test_one_external_donor_cannot_bypass_cap(self):
        cohorts = [{"weight": 1.0, "rows": [
            scored(4.2, listing_id="a", price_m2=5000),
            scored(4.4, listing_id="b", price_m2=5100),
        ]}]
        evidence = {
            "transition": "4_to_6",
            "premium_log": math.log(1.5),
            "premium_pct": 50.0,
            "donor_buildings": 1,
            "weak_base_n": 1,
            "confidence": 0.2,
        }
        result = adjust_price_for_interior(
            5000, scored(6.4), cohorts,
            external_premium_evidence=evidence,
        )
        self.assertNotEqual(result["interior_adjustment_method"], "strict_external_premium")
        self.assertLessEqual(result["base_price_m2_adjusted"], 5000 * math.exp(MAX_ABS_LOG_ADJUSTMENT) + 1e-9)


if __name__ == "__main__":
    unittest.main()
