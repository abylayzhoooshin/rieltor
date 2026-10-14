"""Online gateway and queue worker for interior evaluation.

The gateway is fed only by cards already fetched from Collector's existing
change feed.  It never scans Collector and never changes Stage 2/3 decisions.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import renovation_scorer
from interior_online_store import (
    CleanerBaselineIndex,
    InteriorOnlineStore,
    TERMINAL_STATUSES,
    validate_collector_photo_hash,
)
from renovation_store import RenovationStore
from renovation_worker import process_listing


class InteriorAuthenticationError(RuntimeError):
    pass


class InteriorGateway:
    def __init__(
        self, *, store: InteriorOnlineStore, baseline: CleanerBaselineIndex,
        evaluator_version: str = renovation_scorer.EVALUATOR_VERSION,
    ):
        self.store = store
        self.baseline = baseline
        self.evaluator_version = evaluator_version

    def consider_card(
        self, card: dict[str, Any], event_reason: str | None = None,
        *, priority: int = 0,
    ) -> dict[str, Any]:
        """Reuse an exact result or durably enqueue this Collector card."""
        listing_id = str(card.get("id") or "").strip()
        if not listing_id:
            raise ValueError("Collector card has no id")
        urls, photo_hash = validate_collector_photo_hash(
            card.get("photo_urls"), card.get("photo_set_hash"),
        )
        published = self.baseline.find(listing_id, photo_hash, self.evaluator_version)
        if published is not None:
            return {**published, "action": "reused_baseline"}

        existing = self.store.find(listing_id, photo_hash, self.evaluator_version)
        if existing is not None:
            return {**existing, "action": "reused_temporary"}

        row, _ = self.store.enqueue(
            listing_id=listing_id,
            photo_set_hash=photo_hash,
            evaluator_version=self.evaluator_version,
            photo_urls=urls,
            price=card.get("price"),
            event_reason=event_reason or card.get("reason"),
            priority=priority,
        )
        if not urls:
            self.store.complete(
                row["evaluation_id"], status="no_photos", score=None,
                coverage=None, evaluation_key_value=None,
                content_set_key=None, result=None,
            )
            row = self.store.find(listing_id, photo_hash, self.evaluator_version)
        return {**row, "action": "completed_no_photos" if not urls else "enqueued"}


class InteriorQueueWorker:
    def __init__(
        self, *, queue_store: InteriorOnlineStore,
        renovation_store: RenovationStore, client: Any,
        max_attempts: int = 3, retry_delay_seconds: int = 30,
    ):
        self.queue_store = queue_store
        self.renovation_store = renovation_store
        self.client = client
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds

    @staticmethod
    def _is_auth_error(exc: BaseException) -> bool:
        return getattr(exc, "status_code", None) in {401, 403}

    async def run_once(self) -> dict[str, Any] | None:
        row = self.queue_store.claim_next()
        if row is None:
            return None
        evaluation_id = int(row["evaluation_id"])
        try:
            outcome = await process_listing(
                store=self.renovation_store,
                client=self.client,
                listing_id=row["listing_id"],
                price=row["price"],
                photo_urls=json.loads(row["photo_urls_json"]),
                event_reason=row["event_reason"],
            )
            status = outcome["status"]
            if status not in TERMINAL_STATUSES:
                raise RuntimeError(f"worker returned non-terminal status: {status}")
            payload = outcome.get("payload")
            transient_gaps = status == "ready_with_download_gaps" and any(
                bool(item.get("retryable"))
                for item in ((payload or {}).get("download_failures") or [])
            )
            if transient_gaps and int(row["attempt_count"]) < self.max_attempts:
                self.queue_store.fail_attempt(
                    evaluation_id,
                    error="temporary photo download gaps",
                    max_attempts=self.max_attempts,
                    retry_delay_seconds=self.retry_delay_seconds,
                )
                return {
                    "evaluation_id": evaluation_id,
                    **outcome,
                    "status": "retry",
                    "retry_reason": "temporary_photo_download_gaps",
                }
            result = (payload or {}).get("result") or {}
            self.queue_store.complete(
                evaluation_id,
                status=status,
                score=result.get("score"),
                coverage=result.get("coverage"),
                evaluation_key_value=outcome.get("evaluation_key"),
                content_set_key=(payload or {}).get("content_set_key"),
                result=payload,
            )
            return {"evaluation_id": evaluation_id, **outcome}
        except Exception as exc:
            self.queue_store.fail_attempt(
                evaluation_id,
                error=f"{type(exc).__name__}: {exc}",
                max_attempts=self.max_attempts,
                retry_delay_seconds=self.retry_delay_seconds,
            )
            if self._is_auth_error(exc):
                raise InteriorAuthenticationError(str(exc)) from exc
            return {
                "evaluation_id": evaluation_id,
                "status": "failed_attempt",
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def run_until_idle(self) -> list[dict[str, Any]]:
        outcomes = []
        while True:
            outcome = await self.run_once()
            if outcome is None:
                return outcomes
            outcomes.append(outcome)
            await asyncio.sleep(0)
