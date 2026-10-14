import unittest

from eval_repair_premium_v2 import repair_transition, score_band, seller_pair_type


class RepairPremiumV2RulesTests(unittest.TestCase):
    def test_score_bands_are_non_overlapping(self):
        self.assertEqual(score_band(4.0), "4")
        self.assertEqual(score_band(4.99), "4")
        self.assertEqual(score_band(5.0), "5")
        self.assertEqual(score_band(6.99), "6")
        self.assertEqual(score_band(7.0), "7plus")

    def test_transition_is_oriented_from_weaker_to_better(self):
        self.assertEqual(repair_transition(4.4, 6.4), ("4_to_6", True))
        self.assertEqual(repair_transition(6.4, 4.4), ("4_to_6", False))
        self.assertIsNone(repair_transition(6.2, 6.8))

    def test_seller_pairs_do_not_mix_owner_and_agent(self):
        owner = {"_seller_class": "owner"}
        agent = {"_seller_class": "agent"}
        unknown = {"_seller_class": "unknown"}
        self.assertEqual(seller_pair_type(owner, owner), "owner_owner")
        self.assertEqual(seller_pair_type(agent, agent), "agent_agent")
        self.assertEqual(seller_pair_type(owner, agent), "mixed")
        self.assertEqual(seller_pair_type(owner, unknown), "unknown")


if __name__ == "__main__":
    unittest.main()
