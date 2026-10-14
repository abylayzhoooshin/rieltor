import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import orchestrator_v7 as orchestrator


class _RecordingGateway:
    def __init__(self, error=None):
        self.cards = []
        self.error = error

    def consider_card(self, card, reason, *, priority=0):
        self.cards.append((card, reason, priority))
        if self.error is not None:
            raise self.error
        return {"action": "queued", "status": "pending", "delivery_key": "key"}


class _RecordingDecisionStore:
    def __init__(self):
        self.rows = []

    def enqueue_decision(self, **kwargs):
        self.rows.append(kwargs)
        return kwargs, True


class _DecisionLoopStore:
    def __init__(self, jobs):
        self.jobs = jobs
        self.completed = []
        self.retried = []

    def claim_decisions(self, **_kwargs):
        if self.jobs is None:
            raise asyncio.CancelledError
        jobs, self.jobs = self.jobs, None
        return jobs

    def complete_decisions(self, ids):
        self.completed.extend(ids)

    def retry_decisions(self, ids, **_kwargs):
        self.retried.extend(ids)


class CollectorInteriorFeedTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _old_event():
        return {
            "event_id": 8,
            "id": "100001",
            "reason": "new",
            "observed_at": "2020-01-01T00:00:00+00:00",
        }

    @staticmethod
    def _card():
        return {
            "id": "100001",
            "price": 250000,
            "photo_urls": ["https://photos.example/1.jpg"],
        }

    def test_interior_result_is_copied_into_stage3_contract(self):
        row = orchestrator._attach_interior_result({}, {
            "status": "ready", "score": 7.2, "coverage": "sufficient",
            "evaluator_version": "simple-decimal-v4-original-gpt6-luna",
        })
        self.assertEqual(row["interior_status"], "ready")
        self.assertEqual(row["interior_score"], 7.2)
        self.assertEqual(row["interior_coverage"], "sufficient")

    def test_collector_event_metadata_survives_adapter(self):
        event = self._old_event()
        row = orchestrator.adapt_collector_listing(self._card(), event)
        self.assertEqual(row["collector_event_id"], 8)
        self.assertEqual(row["collector_observed_at"], event["observed_at"])

    def test_prediction_audit_is_tiny_idempotent_event_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "predictions.sqlite3")
            first = {
                "source": "collector", "collector_event_id": 8,
                "id": "100001", "reason": "new",
                "collector_observed_at": "2026-10-01T10:00:00+00:00",
                "price": 300000, "square_m2": 50,
                "base_price_m2": 5000, "benchmark_confidence": 0.7,
                "verdict": "СПРАВЕДЛИВАЯ",
            }
            dropped = {
                **first, "collector_event_id": 9,
                "reason": "price_drop", "price": 280000,
            }
            inserted = orchestrator.append_prediction_audit(
                [first, first, dropped], path,
            )
            self.assertEqual(inserted, 2)
            self.assertEqual(orchestrator.append_prediction_audit([first], path), 0)

            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                rows = [dict(row) for row in conn.execute(
                    "SELECT * FROM predictions ORDER BY collector_event_id"
                )]
            finally:
                conn.close()

            self.assertEqual([row["collector_event_id"] for row in rows], [8, 9])
            self.assertEqual([row["listing_id"] for row in rows], ["100001", "100001"])
            self.assertEqual(rows[0]["predicted_price"], 250000)
            self.assertEqual(rows[1]["asking_price"], 280000)
            self.assertEqual(rows[0]["model_version"], orchestrator.PREDICTION_MODEL_VERSION)

    def test_telegram_explains_applied_interior_adjustment(self):
        row = {field: "" for field in orchestrator.NOTIFICATIONS_FIELDNAMES}
        row.update({
            "title": "Квартира", "price": 300000, "verdict": "СПРАВЕДЛИВАЯ",
            "interior_score": 7.2, "interior_reference_score": 5.5,
            "interior_price_adjustment_pct": 6.4,
        })
        text = orchestrator.format_telegram_message(row)
        self.assertIn("Ремонт: <b>7.2/10</b>", text)
        self.assertIn("у аналогов 5.5", text)
        self.assertIn("+6.4%", text)

    def test_telegram_uses_colored_direction_without_cohort_details(self):
        row = {field: "" for field in orchestrator.NOTIFICATIONS_FIELDNAMES}
        row.update({
            "title": "Квартира", "price": 300000,
            "verdict": "ТРЕБУЕТ ПРОВЕРКИ", "diff_pct": 0.136,
            "benchmark_confidence": 0.437,
        })
        text = orchestrator.format_telegram_message(row)
        self.assertIn("🟢⬇️ Отклонение: 13.6%", text)
        self.assertIn("🎯 Уверенность: 0.437 (низкая)", text)
        self.assertNotIn("Когорта:", text)
        self.assertNotIn("дешевле базы", text)
        self.assertNotIn("дороже базы", text)

        row["diff_pct"] = -0.2
        text = orchestrator.format_telegram_message(row)
        self.assertIn("🔴⬆️ Отклонение: 20.0%", text)

    async def test_old_event_is_queued_for_interior_but_not_sent_to_stage2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline.csv"
            baseline.write_text("id\n", encoding="utf-8")
            state = root / "collector_state.json"
            gateway = _RecordingGateway()
            process = AsyncMock(return_value=[])

            changes = {
                "events": [self._old_event()],
                "count": 1,
                "next_since": 8,
            }
            with (
                patch.object(orchestrator, "BASELINE_CSV", str(baseline)),
                patch.object(orchestrator, "COLLECTOR_STATE_FILE", str(state)),
                patch.object(orchestrator, "COLLECTOR_START_FROM", "beginning"),
                patch.object(orchestrator, "_interior_gateway", gateway),
                patch.object(orchestrator, "fetch_changes", AsyncMock(return_value=changes)),
                patch.object(orchestrator, "fetch_listing", AsyncMock(return_value=self._card())),
                patch.object(orchestrator, "process_and_notify", process),
                patch("builtins.print"),
            ):
                await orchestrator.collector_job()

            self.assertEqual(len(gateway.cards), 1)
            process.assert_not_awaited()
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["last_event_id"], 8)

    async def test_cursor_does_not_advance_if_durable_interior_enqueue_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline.csv"
            baseline.write_text("id\n", encoding="utf-8")
            state = root / "collector_state.json"
            gateway = _RecordingGateway(RuntimeError("sqlite write failed"))
            changes = {
                "events": [self._old_event()],
                "count": 1,
                "next_since": 8,
            }

            with (
                patch.object(orchestrator, "BASELINE_CSV", str(baseline)),
                patch.object(orchestrator, "COLLECTOR_STATE_FILE", str(state)),
                patch.object(orchestrator, "COLLECTOR_START_FROM", "beginning"),
                patch.object(orchestrator, "_interior_gateway", gateway),
                patch.object(orchestrator, "fetch_changes", AsyncMock(return_value=changes)),
                patch.object(orchestrator, "fetch_listing", AsyncMock(return_value=self._card())),
                patch("builtins.print"),
            ):
                with self.assertRaisesRegex(RuntimeError, "sqlite write failed"):
                    await orchestrator.collector_job()

            self.assertFalse(state.exists())

    async def test_fresh_event_is_durably_deferred_until_interior_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline.csv"
            baseline.write_text("id\n", encoding="utf-8")
            state = root / "collector_state.json"
            gateway = _RecordingGateway()
            decisions = _RecordingDecisionStore()
            process = AsyncMock(return_value=[])
            event = {
                **self._old_event(),
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
            changes = {"events": [event], "count": 1, "next_since": 8}
            with (
                patch.object(orchestrator, "BASELINE_CSV", str(baseline)),
                patch.object(orchestrator, "COLLECTOR_STATE_FILE", str(state)),
                patch.object(orchestrator, "COLLECTOR_START_FROM", "beginning"),
                patch.object(orchestrator, "_interior_gateway", gateway),
                patch.object(orchestrator, "_interior_store", decisions),
                patch.object(orchestrator, "fetch_changes", AsyncMock(return_value=changes)),
                patch.object(orchestrator, "fetch_listing", AsyncMock(return_value=self._card())),
                patch.object(orchestrator, "process_and_notify", process),
                patch("builtins.print"),
            ):
                await orchestrator.collector_job()

            self.assertEqual(len(decisions.rows), 1)
            self.assertEqual(decisions.rows[0]["event_id"], 8)
            self.assertEqual(decisions.rows[0]["delivery_key_value"], "key")
            process.assert_not_awaited()
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["last_event_id"], 8)

    async def test_llm_retry_limit_is_per_decision_not_per_batch(self):
        jobs = [
            {
                "decision_id": 1, "listing_id": "old-failure",
                "attempt_count": orchestrator.LLM_FAIL_MAX_ATTEMPTS - 1,
                "row_json": json.dumps({"id": "old-failure"}),
                "interior_status_now": None,
            },
            {
                "decision_id": 2, "listing_id": "new-failure",
                "attempt_count": 0,
                "row_json": json.dumps({"id": "new-failure"}),
                "interior_status_now": None,
            },
        ]
        store = _DecisionLoopStore(jobs)
        failure = orchestrator.LlmRowsFailed(["old-failure", "new-failure"])
        with (
            patch.object(orchestrator, "process_and_notify", AsyncMock(side_effect=failure)),
            patch("builtins.print"),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await orchestrator.interior_decision_loop(store)

        self.assertEqual(store.completed, [1])
        self.assertEqual(store.retried, [2])


if __name__ == "__main__":
    unittest.main()
