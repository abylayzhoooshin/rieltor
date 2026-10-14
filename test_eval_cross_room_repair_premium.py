import unittest
from datetime import datetime, timezone

from eval_cross_room_repair_premium import build_signals, cross_room_donors


class CrossRoomRepairPremiumTests(unittest.TestCase):
    def test_cross_room_donor_is_same_address_but_different_rooms(self):
        target = {
            "address": ("street", "1"),
            "rooms": "2",
            "transition": "5_to_6",
        }
        rows = [
            {"address": ("street", "1"), "rooms": "1", "transition": "5_to_6"},
            {"address": ("street", "1"), "rooms": "2", "transition": "5_to_6"},
            {"address": ("other", "1"), "rooms": "1", "transition": "5_to_6"},
            {"address": ("street", "1"), "rooms": "3", "transition": "6_to_7plus"},
        ]

        strict = cross_room_donors(target, rows, same_transition=True)
        relaxed = cross_room_donors(target, rows, same_transition=False)

        self.assertEqual(["1"], [row["rooms"] for row in strict])
        self.assertEqual(["1", "3"], [row["rooms"] for row in relaxed])

    def test_build_signals_keeps_auditable_representative_ids(self):
        moment = datetime(2026, 10, 1, tzinfo=timezone.utc)
        pairs = [
            {
                "address": ("street", "1"),
                "rooms": "1",
                "transition": "5_to_6",
                "low_id": "low",
                "high_id": "high",
                "low_score": 5.4,
                "high_score": 6.2,
                "log_premium": 0.10,
                "available_at": moment,
            }
        ]

        signal = build_signals(pairs)[0]

        self.assertEqual("low", signal["low_id"])
        self.assertEqual("high", signal["high_id"])
        self.assertAlmostEqual(0.8, signal["score_gap"])


if __name__ == "__main__":
    unittest.main()
