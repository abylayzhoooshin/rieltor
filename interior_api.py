"""Минимальный HTTP API выдачи временных фотооценок Cleaner."""

from __future__ import annotations

import hmac
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit

from interior_online_store import InteriorOnlineStore


def _public_result(row):
    return {
        "evaluation_id": row["evaluation_id"],
        "listing_id": row["listing_id"],
        "photo_set_hash": row["photo_set_hash"],
        "evaluator_version": row["evaluator_version"],
        "status": row["status"],
        "score": row["score"],
        "coverage": row["coverage"],
        "evaluation_key": row["evaluation_key"],
        "content_set_key": row["content_set_key"],
        "evaluated_at": row["evaluated_at"],
    }


def make_handler(store: InteriorOnlineStore, api_key: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "mainrieltor-interior/1"

        def log_message(self, fmt, *args):
            # ASCII prefix is deliberate: Render is UTF-8, but local Windows
            # consoles may use CP1251 and must not break the HTTP response.
            print("   [interior-api] " + (fmt % args))

        def _json(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            supplied = self.headers.get("X-API-Key", "")
            if api_key and not hmac.compare_digest(supplied, api_key):
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "неверный или отсутствующий X-API-Key"})
                return False
            return True

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                self._json(HTTPStatus.OK, {"status": "ok", "queue": store.stats()})
                return
            if parsed.path != "/interior/evaluations":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if not self._authorized():
                return
            try:
                query = parse_qs(parsed.query)
                after_id = int(query.get("after_id", ["0"])[0])
                limit = int(query.get("limit", ["200"])[0])
                rows = store.list_unacknowledged(after_id=after_id, limit=limit)
                results = [_public_result(row) for row in rows]
                self._json(HTTPStatus.OK, {
                    "results": results,
                    "count": len(results),
                    "has_more": len(results) == limit,
                    "next_after_id": (
                        results[-1]["evaluation_id"] if results else after_id
                    ),
                })
            except (TypeError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def do_POST(self):
            if urlsplit(self.path).path != "/interior/evaluations/ack":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if not self._authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 256 * 1024:
                    raise ValueError("невалидный Content-Length")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body должен быть объектом")
                result = store.acknowledge(
                    payload.get("evaluation_ids") or [],
                    baseline_version=str(payload.get("baseline_version") or ""),
                )
                self._json(HTTPStatus.OK, result)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    return Handler


def start_interior_api(
    store: InteriorOnlineStore, *, host: str, port: int, api_key: str,
) -> tuple[ThreadingHTTPServer, Thread]:
    if not api_key:
        raise ValueError("INTERIOR_EXCHANGE_API_KEY обязателен для запуска API")
    server = ThreadingHTTPServer((host, port), make_handler(store, api_key))
    thread = Thread(target=server.serve_forever, name="interior-api", daemon=True)
    thread.start()
    return server, thread
