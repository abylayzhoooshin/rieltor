
"""
incoming_clean.py

Мягкая очистка НОВЫХ объявлений перед Stage 3.

Это НЕ baseline-cleaning:
- не строит когорты;
- не ищет статистические выбросы;
- не удаляет "слишком дешёвые/дорогие" объявления;
- не отбрасывает риелторов;
- не требует фото;
- red_flags не удаляют объявление.

Логика основана на исходных stage1_clean.py + stage2_llm_analyze.py:
Stage 1 даёт детерминированную нормализацию, Stage 2 извлекает
finish_type/red_flags/premium_markers/extra_attributes.
Но для incoming Stage 1 превращён в SOFT-режим: сомнительные данные
помечаются warning, а не выбрасываются.

Использование:
python incoming_clean.py --input <worker.csv> --output <clean.csv> --cache <cache.json>

Требуется запущенный llama-server для текстового Stage 2.
"""

import argparse
import asyncio
import csv
import json
import os
from datetime import datetime, timezone

# Используем именно существующий Stage 2 как библиотеку, но НЕ запускаем
# его CLI и НЕ применяем его когорты/фильтры baseline.
from stage2_llm_analyze import (
    analyze_all,
    load_cache,
    save_cache,
    OUTPUT_EXTRA_FIELDNAMES,
    requires_manual_review,
)

def to_float(x):
    try:
        if x is None or str(x).strip() == "":
            return None
        return float(str(x).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def load_rows(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def freshness(row):
    for field in ("scraped_at", "added_at", "updated_at"):
        raw = (row.get(field) or "").strip()
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except ValueError:
            pass
    return datetime.min


def dedupe_keep_freshest(rows):
    """
    Технический dedup по id — это не рыночная чистка.
    Если id повторяется, сохраняем наиболее свежую запись.
    """
    groups = {}
    order = []
    for row in rows:
        rid = str(row.get("id") or "").strip()
        if rid not in groups:
            groups[rid] = []
            order.append(rid)
        groups[rid].append(row)

    out = []
    for rid in order:
        group = sorted(groups[rid], key=freshness, reverse=True)
        out.append(dict(group[0]))
    return out


def soft_stage1_enrich(row):
    """
    Ничего не выбрасывает. Только нормализует очевидные числовые поля
    и формирует диагностические warnings для Stage 3.
    """
    out = dict(row)
    warnings = []

    rid = str(out.get("id") or "").strip()
    if not rid:
        warnings.append("missing_id")

    price = to_float(out.get("price") or out.get("new_price"))
    square = to_float(out.get("square_m2"))
    rooms = str(out.get("rooms") or "").strip()

    if price is None or price <= 0:
        warnings.append("invalid_or_missing_price")
    if square is None or square <= 0:
        warnings.append("invalid_or_missing_square")
    if not rooms:
        warnings.append("missing_rooms")

    storage = str(out.get("storage") or "").strip().lower()
    if storage and storage != "live":
        warnings.append(f"not_live_storage:{storage}")

    if not str(out.get("full_description") or "").strip():
        warnings.append("no_description")
    if not str(out.get("photo_count") or "").strip():
        warnings.append("photo_count_unknown")

    out["price"] = price if price is not None else out.get("price", "")
    out["square_m2"] = square if square is not None else out.get("square_m2", "")
    out["incoming_data_warnings"] = json.dumps(
        warnings, ensure_ascii=False
    )

    # Это НЕ verdict и НЕ фильтр. Stage 3 использует поле только для
    # снижения data_confidence/объяснения результата.
    # Only the actual Stage 2 red-flag decision is a manual-review flag.
    # Missing photo/rooms/etc. are warnings, not manual-review triggers.
    out["incoming_requires_manual_review"] = False
    return out


async def run(input_path, output_path, cache_path, concurrency):
    rows = load_rows(input_path)
    print(f"Incoming: загружено {len(rows)}")

    rows = dedupe_keep_freshest(rows)
    rows = [soft_stage1_enrich(r) for r in rows]

    # Stage 2: извлечение фактов, без удаления строк.
    cache = load_cache(cache_path)
    analyzed = await analyze_all(rows, cache, concurrency)
    save_cache(cache_path, cache)

    results = []
    for row, llm_result in analyzed:
        out = dict(row)
        out["finish_type"] = llm_result.get("finish_type") or ""
        out["red_flags"] = json.dumps(
            llm_result.get("red_flags") or [], ensure_ascii=False
        )
        out["premium_markers"] = json.dumps(
            llm_result.get("premium_markers") or [], ensure_ascii=False
        )
        out["extra_attributes"] = json.dumps(
            llm_result.get("extra_attributes") or {}, ensure_ascii=False
        )
        out["requires_manual_review"] = requires_manual_review(llm_result)
        out["llm_skipped_error"] = bool(llm_result.get("_skipped_error"))
        results.append(out)

    if results:
        fieldnames = list(results[0].keys())
    else:
        fieldnames = list(rows[0].keys()) if rows else []

    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    print(
        f"✅ Incoming clean: {len(results)} записей сохранено. "
        f"НИ ОДНА запись не удалена из-за цены, когорты, seller или red_flags."
    )
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cache", default="incoming_llm_analysis_cache.json")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency должен быть >= 1")

    asyncio.run(run(
        args.input,
        args.output,
        args.cache,
        args.concurrency,
    ))
