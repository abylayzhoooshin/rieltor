import asyncio
import csv
import hashlib
import tempfile
import unittest
from pathlib import Path

from interior_online_store import CleanerBaselineIndex, InteriorOnlineStore, collector_photo_set_hash
from interior_service import InteriorGateway, InteriorQueueWorker
from renovation_scorer import EVALUATOR_VERSION
from renovation_store import RenovationStore
from renovation_worker import DownloadedPhoto


def _write_baseline(path: Path, rows):
    fields = [
        "id", "photo_set_hash", "interior_status", "interior_score",
        "interior_coverage", "interior_evaluator_version",
        "interior_evaluation_key", "interior_evaluated_at",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class _Responses:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        return type("Response", (), {
            "status": "completed", "id": "r1", "model": "gpt-6-luna",
            "usage": {},
            "output_text": '{"score":6.5,"coverage":"sufficient","confidence":0.9,'
                           '"evidence_photo_ids":["p01"],"summary":"ok"}',
        })()


class _Client:
    def __init__(self):
        self.responses = _Responses()


class InteriorServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.baseline_path = root / "baseline.csv"
        _write_baseline(self.baseline_path, [])
        self.queue = InteriorOnlineStore(root / "queue.sqlite3")
        self.gateway = InteriorGateway(
            store=self.queue, baseline=CleanerBaselineIndex(self.baseline_path),
        )
        self.cache = RenovationStore(root / "cache.sqlite3")

    def tearDown(self):
        self.cache.close()
        self.temp.cleanup()

    @staticmethod
    def card(listing_id="1", urls=None, price=100):
        urls = [] if urls is None else urls
        return {
            "id": listing_id, "price": price, "photo_urls": urls,
            "photo_set_hash": collector_photo_set_hash(urls),
        }

    def test_baseline_result_wins_without_temporary_row(self):
        urls = ["https://krisha-photos.kcdn.online/a.jpg"]
        _write_baseline(self.baseline_path, [{
            "id": "7", "photo_set_hash": collector_photo_set_hash(urls),
            "interior_status": "ready", "interior_score": "7.1",
            "interior_coverage": "sufficient",
            "interior_evaluator_version": EVALUATOR_VERSION,
            "interior_evaluation_key": "ek", "interior_evaluated_at": "now",
        }])
        result = self.gateway.consider_card(self.card("7", urls), "price_drop")
        self.assertEqual(result["action"], "reused_baseline")
        self.assertEqual(self.queue.stats(), {})

    def test_price_only_change_reuses_temporary_key(self):
        urls = ["https://krisha-photos.kcdn.online/a.jpg"]
        first = self.gateway.consider_card(self.card("7", urls, 100), "new")
        second = self.gateway.consider_card(self.card("7", urls, 80), "price_drop")
        self.assertEqual(first["evaluation_id"], second["evaluation_id"])
        self.assertEqual(second["action"], "reused_temporary")
        self.assertEqual(self.queue.stats(), {"pending_unacked": 1})

    def test_empty_photos_are_terminal_without_worker(self):
        result = self.gateway.consider_card(self.card("8", []), "new")
        self.assertEqual(result["action"], "completed_no_photos")
        self.assertEqual(result["status"], "no_photos")

    def test_hash_mismatch_is_rejected_without_queue_row(self):
        card = self.card("9", ["https://krisha-photos.kcdn.online/a.jpg"])
        card["photo_set_hash"] = "bad"
        with self.assertRaisesRegex(ValueError, "collector_photo_hash_mismatch"):
            self.gateway.consider_card(card, "new")
        self.assertEqual(self.queue.stats(), {})

    def test_worker_completes_and_second_price_event_does_not_call_api(self):
        url = "https://krisha-photos.kcdn.online/a.jpg"
        self.gateway.consider_card(self.card("10", [url]), "new")
        client = _Client()
        worker = InteriorQueueWorker(
            queue_store=self.queue, renovation_store=self.cache, client=client,
            retry_delay_seconds=0,
        )

        import renovation_worker
        original = renovation_worker.download_photo

        # process_listing's default fetcher is bound at definition time, so patch
        # the worker method through a tiny deterministic queue-worker override.
        async def fake_process(**kwargs):
            body = b"\xff\xd8\xff" + b"x" * 50
            photo = DownloadedPhoto(url=url, body=body, mime_type="image/jpeg",
                                    sha256=hashlib.sha256(body).hexdigest(), source_index=0)
            async def fetch(_):
                return photo
            # The downloader callback is synchronous; call the real scorer path
            # using a local equivalent below is unnecessary for gateway behavior.
            return None

        # Use the existing worker with its module-level default replaced by
        # constructing an outcome in a patched service symbol.
        import interior_service
        real_process = interior_service.process_listing

        async def stub_process(**kwargs):
            evaluation_key = "e" * 64
            payload = {
                "evaluator_version": EVALUATOR_VERSION,
                "content_set_key": "c" * 64,
                "evaluation_key": evaluation_key,
                "result": {"score": 6.5, "coverage": "sufficient"},
            }
            return {"listing_id": "10", "status": "ready", "api_called": True,
                    "evaluation_key": evaluation_key, "payload": payload}

        interior_service.process_listing = stub_process
        try:
            outcome = asyncio.run(worker.run_once())
        finally:
            interior_service.process_listing = real_process
            renovation_worker.download_photo = original
        self.assertEqual(outcome["status"], "ready")
        again = self.gateway.consider_card(self.card("10", [url], 80), "price_drop")
        self.assertEqual(again["action"], "reused_temporary")
        self.assertEqual(self.queue.stats(), {"ready_unacked": 1})

    def test_temporary_partial_download_is_retried_then_saved(self):
        url = "https://krisha-photos.kcdn.online/a.jpg"
        self.gateway.consider_card(self.card("11", [url]), "new")
        worker = InteriorQueueWorker(
            queue_store=self.queue, renovation_store=self.cache, client=_Client(),
            max_attempts=2, retry_delay_seconds=0,
        )
        import interior_service
        real_process = interior_service.process_listing

        async def partial(**kwargs):
            payload = {
                "content_set_key": "c" * 64,
                "result": {"score": 5.5, "coverage": "partial"},
                "download_failures": [{"retryable": True}],
            }
            return {
                "listing_id": "11", "status": "ready_with_download_gaps",
                "evaluation_key": "e" * 64, "payload": payload,
            }

        interior_service.process_listing = partial
        try:
            first = asyncio.run(worker.run_once())
            second = asyncio.run(worker.run_once())
        finally:
            interior_service.process_listing = real_process
        self.assertEqual(first["status"], "retry")
        self.assertEqual(second["status"], "ready_with_download_gaps")
        self.assertEqual(self.queue.stats(), {"ready_with_download_gaps_unacked": 1})


if __name__ == "__main__":
    unittest.main()
