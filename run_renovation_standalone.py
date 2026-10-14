"""Один реальный прогон и проверка price_drop-кэша без orchestrator."""

from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from renovation_store import RenovationStore
from renovation_worker import process_listing


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("KRISHA_DATA_DIR", BASE_DIR))
DEFAULT_BASELINE = DATA_DIR / "baseline" / "krisha_almaty_baseline.csv"
DEFAULT_DB = DATA_DIR / "interior" / "renovation.sqlite3"


class ResponsesHTTPClient:
    """Минимальный Responses API adapter с интерфейсом AsyncOpenAI client."""

    def __init__(self, api_key: str, timeout: int = 120):
        self._api_key = api_key
        self._timeout = timeout
        self.responses = self
        self.calls = 0

    async def create(self, **payload: Any) -> dict[str, Any]:
        self.calls += 1
        return await asyncio.to_thread(self._post, payload)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=encoded,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = json.load(response)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI HTTP {exc.code}: {body[:500]}") from exc

        raw["output_text"] = "".join(
            part.get("text", "")
            for item in raw.get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        )
        return raw


def candidates(path: Path, listing_id: str | None):
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        rows = []
        for row in csv.DictReader(file):
            if listing_id is not None and str(row.get("id")) != listing_id:
                continue
            try:
                urls = json.loads(row.get("photo_urls") or "[]")
            except json.JSONDecodeError:
                continue
            if row.get("status") == "active" and 4 <= len(urls) <= 9:
                rows.append(row)
        rows.sort(key=lambda row: row.get("last_seen_at") or "", reverse=True)
        yield from rows


def read_api_key(from_stdin: bool) -> str:
    if from_stdin:
        key = sys.stdin.readline().strip()
    else:
        key = getpass.getpass("OpenAI API key: ").strip()
    if not key:
        raise ValueError("API key не передан")
    return key


async def run(args) -> dict[str, Any]:
    api_key = read_api_key(args.api_key_stdin)
    client = ResponsesHTTPClient(api_key)
    last_error: Exception | None = None
    with RenovationStore(args.db) as store:
        before = store.api_attempt_count()
        for number, row in enumerate(candidates(args.baseline, args.listing_id), 1):
            if number > args.max_candidates:
                break
            try:
                first = await process_listing(
                    store=store,
                    client=client,
                    listing_id=row["id"],
                    price=float(row["price"]),
                    photo_urls=row["photo_urls"],
                    event_reason="new",
                )
                lower_price = max(1.0, float(row["price"]) - 10_000)
                second = await process_listing(
                    store=store,
                    client=client,
                    listing_id=row["id"],
                    price=lower_price,
                    photo_urls=row["photo_urls"],
                    event_reason="price_drop",
                )
                after = store.api_attempt_count()
                if first["evaluation_key"] != second["evaluation_key"]:
                    raise AssertionError("price_drop изменил evaluation_key")
                if not second["cache_hit"] or second["api_called"]:
                    raise AssertionError("price_drop не использовал сохранённую оценку")
                if after - before != (0 if first["cache_hit"] else 1):
                    raise AssertionError("неожиданное число API-попыток")
                binding = store.get_listing_binding(row["id"])
                public_payload = dict(first["payload"])
                public_payload.pop("photo_manifest", None)
                return {
                    "listing_id": row["id"],
                    "title": row.get("title"),
                    "url": row.get("url"),
                    "photos": first["photo_count"],
                    "first_cache_hit": first["cache_hit"],
                    "second_cache_hit": second["cache_hit"],
                    "http_calls_this_run": client.calls,
                    "stored_api_attempts_added": after - before,
                    "price_before": float(row["price"]),
                    "price_after": binding["last_price"],
                    "payload": public_payload,
                }
            # Переходим к следующей карточке только при сбое скачивания CDN.
            # Ошибка схемы/модели после платного API-вызова должна остановить
            # standalone-прогон, иначе он может незаметно потратить деньги на
            # несколько следующих объявлений.
            except urllib.error.URLError as exc:
                last_error = exc
                print(f"Карточка {row.get('id')} недоступна: {type(exc).__name__}", file=sys.stderr)
                continue
        if last_error:
            raise RuntimeError("не удалось скачать полный набор фото выбранных карточек") from last_error
        raise RuntimeError("в baseline нет подходящей активной карточки")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--listing-id")
    parser.add_argument("--max-candidates", type=int, default=10)
    parser.add_argument("--api-key-stdin", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
