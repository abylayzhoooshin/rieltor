import csv
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from interior_api import start_interior_api
from interior_online_store import (
    CleanerBaselineIndex,
    InteriorOnlineStore,
    collector_photo_set_hash,
    validate_collector_photo_hash,
)


VERSION = "simple-decimal-v4-original-gpt6-luna"


class InteriorOnlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = InteriorOnlineStore(self.root / "online.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def enqueue(self, listing_id="1", urls=None, photo_hash=None):
        urls = urls if urls is not None else ["https://krisha-photos.kcdn.online/1.jpg"]
        photo_hash = collector_photo_set_hash(urls) if photo_hash is None else photo_hash
        return self.store.enqueue(
            listing_id=listing_id, photo_set_hash=photo_hash,
            evaluator_version=VERSION, photo_urls=urls,
            price=300000, event_reason="new",
        )

    def complete(self, evaluation_id, score=6.2, coverage="sufficient"):
        self.store.complete(
            evaluation_id, status="ready", score=score, coverage=coverage,
            evaluation_key_value="eval-key", content_set_key="content-key",
            result={"result": {"score": score, "coverage": coverage}},
        )

    def test_collector_hash_exact_contract(self):
        urls = ["b", "a", "a"]
        expected = hashlib.sha256("a|a|b".encode()).hexdigest()
        self.assertEqual(collector_photo_set_hash(urls), expected)
        self.assertEqual(collector_photo_set_hash(list(reversed(urls))), expected)
        self.assertIsNone(collector_photo_set_hash([]))
        self.assertEqual(validate_collector_photo_hash(json.dumps(urls), expected), (urls, expected))
        with self.assertRaisesRegex(ValueError, "collector_photo_hash_mismatch"):
            validate_collector_photo_hash(urls, "wrong")

    def test_existing_database_is_migrated_without_losing_rows(self):
        legacy = self.root / "legacy.sqlite3"
        with sqlite3.connect(legacy) as connection:
            connection.executescript(
                """
                CREATE TABLE interior_deliveries (
                    evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_key TEXT NOT NULL UNIQUE,
                    listing_id TEXT NOT NULL,
                    photo_set_hash TEXT,
                    evaluator_version TEXT NOT NULL,
                    photo_urls_json TEXT NOT NULL,
                    price REAL,
                    event_reason TEXT,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    error TEXT,
                    score REAL,
                    coverage TEXT,
                    evaluation_key TEXT,
                    content_set_key TEXT,
                    result_json TEXT,
                    evaluated_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    acknowledged_baseline_version TEXT
                );
                INSERT INTO interior_deliveries (
                    delivery_key, listing_id, evaluator_version, photo_urls_json,
                    status, created_at, updated_at
                ) VALUES ('legacy-key', 'legacy-id', 'v', '[]', 'pending', 'now', 'now');
                """
            )
        migrated = InteriorOnlineStore(legacy)
        with migrated._connect() as connection:
            columns = {row["name"] for row in connection.execute(
                "PRAGMA table_info(interior_deliveries)"
            )}
            row = connection.execute(
                "SELECT listing_id, priority FROM interior_deliveries"
            ).fetchone()
            decisions = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='interior_decisions'"
            ).fetchone()
        self.assertIn("priority", columns)
        self.assertEqual((row["listing_id"], row["priority"]), ("legacy-id", 0))
        self.assertIsNotNone(decisions)

    def test_same_key_is_idempotent_and_price_does_not_requeue(self):
        first, created_first = self.enqueue()
        second, created_second = self.store.enqueue(
            listing_id="1", photo_set_hash=first["photo_set_hash"],
            evaluator_version=VERSION,
            photo_urls=json.loads(first["photo_urls_json"]),
            price=250000, event_reason="price_drop",
        )
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first["evaluation_id"], second["evaluation_id"])
        self.assertEqual(second["price"], 250000)

    def test_new_photo_hash_creates_new_delivery(self):
        first, _ = self.enqueue(urls=["https://krisha-photos.kcdn.online/1.jpg"])
        second, created = self.enqueue(urls=["https://krisha-photos.kcdn.online/2.jpg"])
        self.assertTrue(created)
        self.assertNotEqual(first["evaluation_id"], second["evaluation_id"])

    def test_fresh_delivery_priority_overtakes_backfill(self):
        old, _ = self.enqueue(listing_id="old")
        fresh, _ = self.store.enqueue(
            listing_id="fresh",
            photo_set_hash=collector_photo_set_hash(["fresh"]),
            evaluator_version=VERSION,
            photo_urls=["fresh"], price=1, event_reason="new", priority=100,
        )
        claimed = self.store.claim_next()
        self.assertEqual(claimed["evaluation_id"], fresh["evaluation_id"])

    def test_decision_waits_for_result_and_is_deleted_after_completion(self):
        delivery, _ = self.enqueue(listing_id="decision")
        decision, created = self.store.enqueue_decision(
            event_id=99, listing_id="decision",
            delivery_key_value=delivery["delivery_key"],
            row={"id": "decision", "price": 200000},
        )
        self.assertTrue(created)
        self.assertEqual(self.store.claim_decisions(max_wait_seconds=3600), [])
        self.complete(delivery["evaluation_id"])
        jobs = self.store.claim_decisions(max_wait_seconds=3600)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["interior_score_now"], 6.2)
        self.store.complete_decisions([decision["decision_id"]])
        self.assertEqual(self.store.claim_decisions(max_wait_seconds=0), [])

    def test_decision_with_embedded_baseline_result_is_ready_immediately(self):
        self.store.enqueue_decision(
            event_id=100, listing_id="baseline-result",
            delivery_key_value=None,
            row={"id": "baseline-result", "interior_status": "ready", "interior_score": 7.1},
        )
        jobs = self.store.claim_decisions(max_wait_seconds=3600)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(json.loads(jobs[0]["row_json"])["interior_score"], 7.1)

    def test_gc_keeps_delivery_while_decision_still_references_it(self):
        delivery, _ = self.enqueue(listing_id="protected")
        self.complete(delivery["evaluation_id"])
        decision, _ = self.store.enqueue_decision(
            event_id=101, listing_id="protected",
            delivery_key_value=delivery["delivery_key"], row={"id": "protected"},
        )
        old = datetime.now(timezone.utc) - timedelta(hours=30)
        self.store.acknowledge(
            [delivery["evaluation_id"]], baseline_version="v1",
            acknowledged_at=old.isoformat(),
        )
        self.assertEqual(self.store.garbage_collect(now=datetime.now(timezone.utc)), 0)
        self.assertIsNotNone(self.store.find("protected", delivery["photo_set_hash"], VERSION))
        self.store.complete_decisions([decision["decision_id"]])
        self.assertEqual(self.store.garbage_collect(now=datetime.now(timezone.utc)), 1)

    def test_baseline_lookup_uses_exact_triple(self):
        baseline = self.root / "baseline.csv"
        photo_hash = collector_photo_set_hash(["u"])
        fields = [
            "id", "photo_set_hash", "interior_status", "interior_score",
            "interior_coverage", "interior_evaluator_version",
            "interior_evaluation_key", "interior_evaluated_at",
        ]
        with baseline.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow({
                "id": "7", "photo_set_hash": photo_hash,
                "interior_status": "ready", "interior_score": "6.2",
                "interior_coverage": "sufficient",
                "interior_evaluator_version": VERSION,
                "interior_evaluation_key": "ek", "interior_evaluated_at": "now",
            })
        index = CleanerBaselineIndex(baseline)
        self.assertEqual(index.find("7", photo_hash, VERSION)["score"], 6.2)
        self.assertIsNone(index.find("7", "different", VERSION))
        self.assertIsNone(index.find("7", photo_hash, "new-version"))

    def test_old_baseline_without_interior_columns_is_empty_index(self):
        baseline = self.root / "old.csv"
        baseline.write_text("id,price\n1,300000\n", encoding="utf-8")
        self.assertIsNone(CleanerBaselineIndex(baseline).find("1", None, VERSION))

    def test_ack_is_idempotent_and_gc_never_deletes_unacknowledged(self):
        row, _ = self.enqueue()
        self.complete(row["evaluation_id"])
        first = self.store.acknowledge([row["evaluation_id"]], baseline_version="build-1")
        second = self.store.acknowledge([row["evaluation_id"]], baseline_version="build-1")
        self.assertEqual(first["acknowledged"], 1)
        self.assertEqual(second["already_acknowledged"], 1)

        unacked, _ = self.enqueue(listing_id="2")
        self.complete(unacked["evaluation_id"])
        future = datetime.now(timezone.utc) + timedelta(hours=25)
        self.assertEqual(self.store.garbage_collect(now=future), 1)
        self.assertIsNotNone(self.store.find("2", unacked["photo_set_hash"], VERSION))

    def test_late_older_result_is_not_skipped_and_pending_cannot_be_acked(self):
        older, _ = self.enqueue(listing_id="older")
        newer, _ = self.enqueue(listing_id="newer")
        self.complete(newer["evaluation_id"])

        batch = self.store.list_unacknowledged(limit=1)
        self.assertEqual(batch[0]["evaluation_id"], newer["evaluation_id"])
        not_ready = self.store.acknowledge(
            [older["evaluation_id"]], baseline_version="build-1",
        )
        self.assertEqual(not_ready["not_ready"], 1)
        self.assertEqual(not_ready["acknowledged"], 0)

        self.store.acknowledge([newer["evaluation_id"]], baseline_version="build-1")
        self.complete(older["evaluation_id"])
        batch = self.store.list_unacknowledged(limit=1)
        self.assertEqual(batch[0]["evaluation_id"], older["evaluation_id"])

    def test_exhausted_technical_error_is_requeued_after_cooldown(self):
        row, _ = self.enqueue()
        claimed = self.store.claim_next()
        self.assertEqual(claimed["evaluation_id"], row["evaluation_id"])
        self.store.fail_attempt(
            row["evaluation_id"], error="temporary network failure", max_attempts=1,
        )
        self.assertEqual(
            self.store.find("1", row["photo_set_hash"], VERSION)["status"], "error",
        )
        now = datetime.now(timezone.utc)
        self.assertEqual(self.store.requeue_errors(older_than_hours=6, now=now), 0)
        future = now + timedelta(hours=7)
        self.assertEqual(self.store.requeue_errors(older_than_hours=6, now=future), 1)
        retried = self.store.find("1", row["photo_set_hash"], VERSION)
        self.assertEqual(retried["status"], "retry")
        self.assertEqual(retried["attempt_count"], 0)

    def test_http_api_auth_pagination_and_ack(self):
        for number in (1, 2):
            row, _ = self.enqueue(listing_id=str(number))
            self.complete(row["evaluation_id"])
        server, thread = start_interior_api(
            self.store, host="127.0.0.1", port=0, api_key="test-secret",
        )
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + "/interior/evaluations")
            self.assertEqual(caught.exception.code, 401)

            request = urllib.request.Request(
                base + "/interior/evaluations?limit=1",
                headers={"X-API-Key": "test-secret"},
            )
            with urllib.request.urlopen(request) as response:
                page = json.load(response)
            self.assertEqual(page["count"], 1)
            evaluation_id = page["results"][0]["evaluation_id"]
            self.assertEqual(page["next_after_id"], evaluation_id)
            next_request = urllib.request.Request(
                base + f"/interior/evaluations?limit=1&after_id={evaluation_id}",
                headers={"X-API-Key": "test-secret"},
            )
            with urllib.request.urlopen(next_request) as response:
                next_page = json.load(response)
            self.assertEqual(next_page["count"], 1)
            self.assertGreater(
                next_page["results"][0]["evaluation_id"], evaluation_id,
            )

            body = json.dumps({
                "evaluation_ids": [evaluation_id], "baseline_version": "build-http",
            }).encode()
            ack_request = urllib.request.Request(
                base + "/interior/evaluations/ack", data=body, method="POST",
                headers={"X-API-Key": "test-secret", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(ack_request) as response:
                ack = json.load(response)
            self.assertEqual(ack["acknowledged"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_api_does_not_log_successful_health_checks(self):
        server, thread = start_interior_api(
            self.store, host="127.0.0.1", port=0, api_key="test-secret",
        )
        output = io.StringIO()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with redirect_stdout(output):
                with urllib.request.urlopen(base + "/health") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.load(response)["status"], "ok")
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(base + "/missing")
            logs = output.getvalue()
            self.assertNotIn('GET /health HTTP/1.1" 200', logs)
            self.assertIn('GET /missing HTTP/1.1" 404', logs)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
