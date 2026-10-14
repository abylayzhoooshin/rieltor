import json
import os
import tempfile
import unittest

from telegram_access import TelegramAccessStore, TelegramDetailsStore


class TelegramAccessStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "telegram_access.json")
        self.store = TelegramAccessStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_request_grant_revoke_and_reload(self):
        status, entry = self.store.request_access(
            chat_id=123, user_id=123, username="alice", display_name="Alice",
        )
        self.assertEqual(status, "requested")
        self.assertEqual(entry["username"], "alice")
        self.assertFalse(self.store.is_authorized(123))

        granted = self.store.grant(123, granted_by=999)
        self.assertEqual(granted["granted_by"], "999")
        self.assertTrue(self.store.is_authorized(123))
        self.assertEqual(TelegramAccessStore(self.path).recipients(), ["123"])

        self.assertTrue(self.store.revoke(123))
        self.assertFalse(self.store.is_authorized(123))
        self.assertEqual(self.store.revoked(), {"123"})

        self.store.grant(123, granted_by=999)
        self.assertEqual(self.store.revoked(), set())

    def test_settings_and_offset_are_persistent(self):
        self.store.set_setting("paused", True)
        self.store.set_setting("send_mode", "useful")
        self.store.set_setting("cohort_mode", "important")
        self.store.advance_offset(42)
        self.store.advance_offset(10)

        loaded = TelegramAccessStore(self.path)
        self.assertEqual(loaded.update_offset, 42)
        self.assertEqual(loaded.settings(), {
            "paused": True,
            "send_mode": "useful",
            "cohort_mode": "important",
        })

    def test_state_contains_no_message_history(self):
        self.store.request_access(chat_id=123, user_id=123, username="u")
        with open(self.path, encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertNotIn("messages", raw)
        self.assertNotIn("updates", raw)


class TelegramDetailsStoreTests(unittest.TestCase):
    def test_cache_is_bounded_and_replaces_same_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramDetailsStore(
                os.path.join(tmp, "details.sqlite3"), max_entries=2,
            )
            store.put("1", "one")
            store.put("2", "two")
            store.put("1", "one-new")
            store.put("3", "three")
            self.assertEqual(store.get("1"), "one-new")
            self.assertEqual(store.get("3"), "three")
            self.assertIsNone(store.get("2"))
            self.assertEqual(store.count(), 2)

    def test_expired_details_are_not_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramDetailsStore(
                os.path.join(tmp, "details.sqlite3"), ttl_seconds=0,
            )
            store.put("1", "one")
            self.assertIsNone(store.get("1"))


if __name__ == "__main__":
    unittest.main()
