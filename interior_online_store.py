"""Временная очередь и outbox online-фотооценок.

Это не постоянная база всех оценок: опубликованные Cleaner записи удаляются
через grace period. Первоначальные и уже опубликованные оценки читаются из
clean baseline Cleaner.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


TERMINAL_STATUSES = {
    "ready", "ready_with_download_gaps", "no_photos", "photos_unavailable",
}
ACTIVE_STATUSES = {"pending", "processing", "retry", "error"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_photo_set_hash(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def collector_photo_set_hash(photo_urls: list[str]) -> str | None:
    """Точный алгоритм Collector; дубли сохраняются, порядок не влияет."""
    if not photo_urls:
        return None
    return hashlib.sha256("|".join(sorted(photo_urls)).encode("utf-8")).hexdigest()


def parse_raw_photo_urls(value: Any) -> list[str]:
    """Разбирает поле до dedupe: Collector включает дубли в свой hash."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("photo_urls не является JSON-массивом") from exc
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("photo_urls должен быть массивом строк")
    return value


def validate_collector_photo_hash(photo_urls: Any, photo_set_hash: Any) -> tuple[list[str], str | None]:
    urls = parse_raw_photo_urls(photo_urls)
    supplied = normalize_photo_set_hash(photo_set_hash)
    expected = collector_photo_set_hash(urls)
    if supplied != expected:
        raise ValueError(
            "collector_photo_hash_mismatch: "
            f"получен={supplied or 'null'}, ожидается={expected or 'null'}"
        )
    return urls, supplied


def delivery_key(listing_id: Any, photo_set_hash: Any, evaluator_version: str) -> str:
    listing = str(listing_id or "").strip()
    if not listing:
        raise ValueError("listing_id обязателен")
    if not evaluator_version:
        raise ValueError("evaluator_version обязателен")
    canonical = json.dumps(
        [listing, normalize_photo_set_hash(photo_set_hash), evaluator_version],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CleanerBaselineIndex:
    """Индекс опубликованных Cleaner оценок по согласованному ключу."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._stamp: tuple[int, int] | None = None
        self._values: dict[str, dict[str, Any]] = {}

    def _reload_if_needed(self) -> None:
        stat = self.path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        values: dict[str, dict[str, Any]] = {}
        with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"id", "photo_set_hash", "interior_evaluator_version"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                # Старый baseline без фотооценок штатно означает пустой индекс.
                self._values = {}
                self._stamp = stamp
                return
            for row in reader:
                version = str(row.get("interior_evaluator_version") or "").strip()
                status = str(row.get("interior_status") or "").strip()
                if not version or status not in TERMINAL_STATUSES:
                    continue
                key = delivery_key(row.get("id"), row.get("photo_set_hash"), version)
                if key in values:
                    raise ValueError(f"дублирующий ключ фотооценки в baseline: id={row.get('id')}")
                score_text = str(row.get("interior_score") or "").strip()
                values[key] = {
                    "listing_id": str(row.get("id") or ""),
                    "photo_set_hash": normalize_photo_set_hash(row.get("photo_set_hash")),
                    "evaluator_version": version,
                    "status": status,
                    "score": float(score_text) if score_text else None,
                    "coverage": str(row.get("interior_coverage") or "").strip() or None,
                    "evaluation_key": str(row.get("interior_evaluation_key") or "").strip() or None,
                    "evaluated_at": str(row.get("interior_evaluated_at") or "").strip() or None,
                    "source": "cleaner_baseline",
                }
        self._values = values
        self._stamp = stamp

    def find(self, listing_id: Any, photo_set_hash: Any, evaluator_version: str) -> dict[str, Any] | None:
        self._reload_if_needed()
        value = self._values.get(delivery_key(listing_id, photo_set_hash, evaluator_version))
        return dict(value) if value else None


class InteriorOnlineStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._create_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _create_schema(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS interior_deliveries (
                    evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_key TEXT NOT NULL UNIQUE,
                    listing_id TEXT NOT NULL,
                    photo_set_hash TEXT,
                    evaluator_version TEXT NOT NULL,
                    photo_urls_json TEXT NOT NULL,
                    price REAL,
                    event_reason TEXT,
                    priority INTEGER NOT NULL DEFAULT 0,
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
                CREATE TABLE IF NOT EXISTS interior_decisions (
                    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL UNIQUE,
                    listing_id TEXT NOT NULL,
                    delivery_key TEXT,
                    row_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS interior_deliveries_pending
                    ON interior_deliveries(status, next_attempt_at, evaluation_id);
                CREATE INDEX IF NOT EXISTS interior_deliveries_outbox
                    ON interior_deliveries(acknowledged_at, evaluation_id);
                CREATE INDEX IF NOT EXISTS interior_decisions_pending
                    ON interior_decisions(status, next_attempt_at, decision_id);
                """
            )
            columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(interior_deliveries)"
                )
            }
            if "priority" not in columns:
                connection.execute(
                    "ALTER TABLE interior_deliveries ADD COLUMN priority INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def find(self, listing_id: Any, photo_set_hash: Any, evaluator_version: str) -> dict[str, Any] | None:
        key = delivery_key(listing_id, photo_set_hash, evaluator_version)
        with self._connect() as connection:
            return self._row(connection.execute(
                "SELECT * FROM interior_deliveries WHERE delivery_key = ?", (key,)
            ).fetchone())

    def enqueue(
        self, *, listing_id: Any, photo_set_hash: Any, evaluator_version: str,
        photo_urls: list[str], price: float | int | None, event_reason: str | None,
        priority: int = 0,
    ) -> tuple[dict[str, Any], bool]:
        listing = str(listing_id or "").strip()
        key = delivery_key(listing, photo_set_hash, evaluator_version)
        now = utc_now()
        encoded_urls = json.dumps(photo_urls, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO interior_deliveries (
                    delivery_key, listing_id, photo_set_hash, evaluator_version,
                    photo_urls_json, price, event_reason, priority, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (key, listing, normalize_photo_set_hash(photo_set_hash), evaluator_version,
                 encoded_urls, price, event_reason, int(priority), now, now),
            )
            created = cursor.rowcount == 1
            if not created:
                # Цена/причина обновляются для диагностики, но ключ и фото нет.
                connection.execute(
                    """UPDATE interior_deliveries SET price = ?, event_reason = ?,
                              priority = MAX(priority, ?), updated_at = ?
                       WHERE delivery_key = ?""",
                    (price, event_reason, int(priority), now, key),
                )
            row = connection.execute(
                "SELECT * FROM interior_deliveries WHERE delivery_key = ?", (key,)
            ).fetchone()
        return dict(row), created

    def enqueue_decision(
        self, *, event_id: int, listing_id: Any, delivery_key_value: str | None,
        row: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Durably save a fresh card before the Collector cursor advances."""
        now = utc_now()
        encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO interior_decisions (
                       event_id, listing_id, delivery_key, row_json,
                       status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                (int(event_id), str(listing_id), delivery_key_value, encoded, now, now),
            )
            created = cursor.rowcount == 1
            saved = connection.execute(
                "SELECT * FROM interior_decisions WHERE event_id = ?", (int(event_id),)
            ).fetchone()
        return dict(saved), created

    def resume_decisions(self) -> int:
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE interior_decisions
                   SET status = 'retry', next_attempt_at = NULL, updated_at = ?
                   WHERE status = 'processing'""",
                (now,),
            )
        return cursor.rowcount

    def claim_decisions(
        self, *, max_wait_seconds: int, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Claim decisions whose photo result is ready or wait limit expired."""
        if max_wait_seconds < 0 or not 1 <= limit <= 100:
            raise ValueError("invalid decision claim limits")
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        cutoff = (now_dt - timedelta(seconds=max_wait_seconds)).isoformat()
        terminal = sorted(TERMINAL_STATUSES)
        placeholders = ",".join("?" for _ in terminal)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"""SELECT d.*, i.status AS interior_status_now,
                           i.score AS interior_score_now,
                           i.coverage AS interior_coverage_now,
                           i.evaluator_version AS interior_evaluator_version_now
                    FROM interior_decisions d
                    LEFT JOIN interior_deliveries i ON i.delivery_key = d.delivery_key
                    WHERE d.status IN ('pending', 'retry')
                      AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= ?)
                      AND (
                           d.delivery_key IS NULL
                           OR i.status IN ({placeholders})
                           OR d.created_at <= ?
                      )
                    ORDER BY d.decision_id
                    LIMIT ?""",
                [now, *terminal, cutoff, limit],
            ).fetchall()
            ids = [int(row["decision_id"]) for row in rows]
            if ids:
                marks = ",".join("?" for _ in ids)
                connection.execute(
                    f"""UPDATE interior_decisions
                        SET status='processing', attempt_count=attempt_count+1, updated_at=?
                        WHERE decision_id IN ({marks})""",
                    [now, *ids],
                )
        return [dict(row) for row in rows]

    def complete_decisions(self, decision_ids: Iterable[int]) -> None:
        ids = list(dict.fromkeys(int(value) for value in decision_ids))
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        with self._connect() as connection:
            connection.execute(
                f"DELETE FROM interior_decisions WHERE decision_id IN ({marks})",
                ids,
            )

    def retry_decisions(
        self, decision_ids: Iterable[int], *, error: str, delay_seconds: int = 60,
    ) -> None:
        ids = list(dict.fromkeys(int(value) for value in decision_ids))
        if not ids:
            return
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        next_attempt = (now_dt + timedelta(seconds=delay_seconds)).isoformat()
        marks = ",".join("?" for _ in ids)
        with self._connect() as connection:
            connection.execute(
                f"""UPDATE interior_decisions
                    SET status='retry', error=?, next_attempt_at=?, updated_at=?
                    WHERE decision_id IN ({marks})""",
                [str(error)[:1000], next_attempt, now, *ids],
            )

    def complete(
        self, evaluation_id: int, *, status: str, score: float | None,
        coverage: str | None, evaluation_key_value: str | None,
        content_set_key: str | None, result: dict[str, Any] | None,
        evaluated_at: str | None = None,
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"нетерминальный status для complete: {status}")
        if status in {"no_photos", "photos_unavailable"} and (score is not None or coverage is not None):
            raise ValueError(f"{status} требует score=NULL и coverage=NULL")
        if status in {"ready", "ready_with_download_gaps"} and coverage not in {
            "insufficient", "partial", "sufficient",
        }:
            raise ValueError(f"{status} требует допустимый coverage")
        if coverage == "insufficient" and score is not None:
            raise ValueError("insufficient требует score=NULL")
        if coverage in {"partial", "sufficient"} and score is None:
            raise ValueError("partial/sufficient требуют числовой score")
        now = utc_now()
        encoded = None if result is None else json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE interior_deliveries
                SET status = ?, score = ?, coverage = ?, evaluation_key = ?,
                    content_set_key = ?, result_json = ?, evaluated_at = ?,
                    error = NULL, next_attempt_at = NULL, updated_at = ?
                WHERE evaluation_id = ?
                """,
                (status, score, coverage, evaluation_key_value, content_set_key,
                 encoded, evaluated_at or now, now, evaluation_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"evaluation_id не найден: {evaluation_id}")

    def resume_incomplete(self) -> int:
        """Return interrupted work to the queue when a process starts."""
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE interior_deliveries
                   SET status = 'retry', attempt_count = 0,
                       next_attempt_at = NULL, updated_at = ?
                   WHERE status IN ('processing', 'error')""",
                (now,),
            )
        return cursor.rowcount

    def requeue_errors(
        self, *, older_than_hours: float = 6, now: datetime | None = None,
    ) -> int:
        """Give exhausted technical failures another bounded run later.

        A terminal ``error`` is deliberately not exposed to Cleaner, but it
        must not become a permanent graveyard in a long-running web service.
        Attempts are reset only after a cooling-off interval. Authentication
        failures still stop all workers; fixing the key and restarting the
        service requeues them through ``resume_incomplete``.
        """
        if older_than_hours <= 0:
            raise ValueError("older_than_hours должен быть > 0")
        moment = now or datetime.now(timezone.utc)
        cutoff = (moment - timedelta(hours=older_than_hours)).isoformat()
        timestamp = moment.isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE interior_deliveries
                   SET status = 'retry', attempt_count = 0,
                       next_attempt_at = NULL, updated_at = ?
                   WHERE status = 'error' AND updated_at <= ?""",
                (timestamp, cutoff),
            )
        return cursor.rowcount

    def claim_next(self) -> dict[str, Any] | None:
        """Atomically claim one due row so parallel workers cannot duplicate it."""
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM interior_deliveries
                   WHERE status IN ('pending', 'retry')
                     AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                   ORDER BY priority DESC, evaluation_id
                   LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """UPDATE interior_deliveries
                   SET status = 'processing', attempt_count = attempt_count + 1,
                       error = NULL, updated_at = ?
                   WHERE evaluation_id = ?""",
                (now, row["evaluation_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM interior_deliveries WHERE evaluation_id = ?",
                (row["evaluation_id"],),
            ).fetchone()
        return dict(claimed)

    def fail_attempt(
        self, evaluation_id: int, *, error: str, max_attempts: int = 3,
        retry_delay_seconds: int = 30,
    ) -> str:
        """Schedule another try, or leave an explicit error after this run's limit."""
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempt_count FROM interior_deliveries WHERE evaluation_id = ?",
                (evaluation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"evaluation_id не найден: {evaluation_id}")
            exhausted = int(row["attempt_count"]) >= max_attempts
            status = "error" if exhausted else "retry"
            next_attempt = None if exhausted else (
                now_dt + timedelta(seconds=retry_delay_seconds)
            ).isoformat()
            connection.execute(
                """UPDATE interior_deliveries
                   SET status = ?, error = ?, next_attempt_at = ?, updated_at = ?
                   WHERE evaluation_id = ?""",
                (status, str(error)[:1000], next_attempt, now, evaluation_id),
            )
        return status

    def list_unacknowledged(
        self, *, after_id: int = 0, limit: int = 200,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit должен быть от 1 до 500")
        if after_id < 0:
            raise ValueError("after_id не может быть отрицательным")
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        params: list[Any] = [*sorted(TERMINAL_STATUSES), after_id, limit]
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM interior_deliveries
                WHERE status IN ({placeholders})
                  AND acknowledged_at IS NULL
                  AND evaluation_id > ?
                ORDER BY evaluation_id
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def acknowledge(
        self, evaluation_ids: Iterable[int], *, baseline_version: str,
        acknowledged_at: str | None = None,
    ) -> dict[str, int]:
        ids = list(dict.fromkeys(int(value) for value in evaluation_ids))
        if not ids or len(ids) > 500:
            raise ValueError("нужно передать от 1 до 500 evaluation_id")
        if not str(baseline_version or "").strip():
            raise ValueError("baseline_version обязателен")
        now = acknowledged_at or utc_now()
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            existing = connection.execute(
                f"SELECT evaluation_id, status, acknowledged_at FROM interior_deliveries "
                f"WHERE evaluation_id IN ({placeholders})", ids,
            ).fetchall()
            found = {int(row["evaluation_id"]): row for row in existing}
            terminal = {
                value for value in ids
                if value in found and found[value]["status"] in TERMINAL_STATUSES
            }
            newly = [
                value for value in ids
                if value in terminal and not found[value]["acknowledged_at"]
            ]
            if newly:
                new_placeholders = ",".join("?" for _ in newly)
                connection.execute(
                    f"""UPDATE interior_deliveries
                        SET acknowledged_at = ?, acknowledged_baseline_version = ?, updated_at = ?
                        WHERE evaluation_id IN ({new_placeholders})
                          AND acknowledged_at IS NULL""",
                    [now, baseline_version, now, *newly],
                )
        return {
            "requested": len(ids),
            "acknowledged": len(newly),
            "already_acknowledged": sum(
                1 for value in ids if value in terminal and found[value]["acknowledged_at"]
            ),
            "missing": sum(value not in found for value in ids),
            "not_ready": sum(value in found and value not in terminal for value in ids),
        }

    def garbage_collect(self, *, now: datetime | None = None, grace_hours: int = 24) -> int:
        return self.garbage_collect_details(now=now, grace_hours=grace_hours)["deleted"]

    def garbage_collect_details(
        self, *, now: datetime | None = None, grace_hours: int = 24,
    ) -> dict[str, Any]:
        moment = now or datetime.now(timezone.utc)
        cutoff = (moment - timedelta(hours=grace_hours)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT listing_id, evaluation_key FROM interior_deliveries
                   WHERE acknowledged_at IS NOT NULL AND acknowledged_at < ?
                     AND NOT EXISTS (
                         SELECT 1 FROM interior_decisions d
                         WHERE d.delivery_key = interior_deliveries.delivery_key
                           AND d.status IN ('pending', 'retry', 'processing')
                     )""",
                (cutoff,),
            ).fetchall()
            connection.execute(
                """DELETE FROM interior_deliveries
                   WHERE acknowledged_at IS NOT NULL AND acknowledged_at < ?
                     AND NOT EXISTS (
                         SELECT 1 FROM interior_decisions d
                         WHERE d.delivery_key = interior_deliveries.delivery_key
                           AND d.status IN ('pending', 'retry', 'processing')
                     )""",
                (cutoff,),
            )
            remaining_listing_ids = {
                row["listing_id"] for row in connection.execute(
                    "SELECT DISTINCT listing_id FROM interior_deliveries"
                )
            }
            remaining_evaluation_keys = {
                row["evaluation_key"] for row in connection.execute(
                    """SELECT DISTINCT evaluation_key FROM interior_deliveries
                       WHERE evaluation_key IS NOT NULL"""
                )
            }
        return {
            "deleted": len(rows),
            "orphaned_listing_ids": sorted({
                row["listing_id"] for row in rows
                if row["listing_id"] not in remaining_listing_ids
            }),
            "orphaned_evaluation_keys": sorted({
                row["evaluation_key"] for row in rows
                if row["evaluation_key"]
                and row["evaluation_key"] not in remaining_evaluation_keys
            }),
        }

    def stats(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, acknowledged_at, COUNT(*) AS n FROM interior_deliveries "
                "GROUP BY status, acknowledged_at IS NOT NULL"
            ).fetchall()
            decisions = connection.execute(
                "SELECT status, COUNT(*) AS n FROM interior_decisions GROUP BY status"
            ).fetchall()
        result: dict[str, int] = {}
        for row in rows:
            suffix = "acked" if row["acknowledged_at"] else "unacked"
            result[f"{row['status']}_{suffix}"] = int(row["n"])
        for row in decisions:
            result[f"decision_{row['status']}"] = int(row["n"])
        return result
