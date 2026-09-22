
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
red_flags/premium_markers/extra_attributes.
Но для incoming Stage 1 превращён в SOFT-режим: сомнительные данные
помечаются warning, а не выбрасываются.

Использование:
python incoming_clean.py --input <worker.csv> --output <clean.csv> --cache <cache.json>

Для текстового Stage 2 нужен OPENAI_API_KEY (модель — OPENAI_MODEL).
"""

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime, timezone

# Используем именно существующий Stage 2 как библиотеку, но НЕ запускаем
# его CLI и НЕ применяем его когорты/фильтры baseline.
from stage2_llm_analyze import (
    Stage2ConfigError,
    analyze_all,
    load_cache,
    save_cache,
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

    Возвращает (out, dropped_no_id). Строки с пустым id раньше попадали
    в общую группу "" и схлопывались в ОДНУ — то есть терялись молча, в
    файле, который декларирует "ничего не выбрасывает". Пустой id у
    объявления означает поломку парсера, а не свойство рынка, поэтому
    такие строки удаляем, но возвращаем счётчик наверх, чтобы поломка
    была видна в логе.
    """
    groups = {}
    order = []
    dropped_no_id = []
    for row in rows:
        rid = str(row.get("id") or "").strip()
        if not rid:
            dropped_no_id.append(row)
            continue
        if rid not in groups:
            groups[rid] = []
            order.append(rid)
        groups[rid].append(row)

    out = []
    for rid in order:
        group = sorted(groups[rid], key=freshness, reverse=True)
        out.append(dict(group[0]))
    return out, dropped_no_id


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

    if not str(out.get("full_description") or "").strip():
        warnings.append("no_description")
    if not str(out.get("photo_count") or "").strip():
        warnings.append("photo_count_unknown")

    out["price"] = price if price is not None else out.get("price", "")
    out["square_m2"] = square if square is not None else out.get("square_m2", "")
    # Diagnostic only — a human reading the CSV can see why a row looked
    # thin before Stage 2. Nothing downstream currently parses this
    # column (the previous comment here claimed Stage 3 reads it to
    # lower data_confidence; it does not — Stage 3 computes its own
    # data_warnings from the enriched row directly, independent of this
    # field).
    out["incoming_data_warnings"] = json.dumps(
        warnings, ensure_ascii=False
    )
    return out


class Stage2Unavailable(RuntimeError):
    """Stage 2 не отдал факты ни по одной строке — скорить нечем."""


async def run(input_path, output_path, cache_path, concurrency):
    rows = load_rows(input_path)
    print(f"Incoming: загружено {len(rows)}")

    rows, dropped_no_id = dedupe_keep_freshest(rows)
    if dropped_no_id:
        # Не тихое "so be it": пустой id — это симптом поломки парсера,
        # и он должен быть виден в логе оркестратора.
        print(f"⚠️  Удалено {len(dropped_no_id)} строк без id (ошибка парсера?)")
    rows = [soft_stage1_enrich(r) for r in rows]

    # Stage 2: извлечение фактов, без удаления строк.
    cache = load_cache(cache_path)
    analyzed = await analyze_all(rows, cache, concurrency)
    save_cache(cache_path, cache)

    results = []
    llm_failed = 0
    for row, llm_result in analyzed:
        out = dict(row)
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
        if out["llm_skipped_error"]:
            llm_failed += 1
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

    # Если Stage 2 не отработал ни по одной строке, значит OpenAI API
    # недоступен (ключ, сеть, лимиты). Молча продолжать нельзя: premium_markers пуст у всех, из-за
    # чего проседает quality_evidence_score и data_confidence, а red_flags
    # пуст у всех, из-за чего вердикт РУЧНАЯ ПРОВЕРКА не сработает ни разу.
    # Результат выглядит валидным, но систематически смещён. CSV выше уже
    # записан намеренно — он пригодится для разбора, — но цикл считается
    # проваленным, и оркестратор не должен по нему скорить и рассылать.
    if results and llm_failed == len(results):
        raise Stage2Unavailable(
            f"Stage 2 не отработал ни по одной из {len(results)} строк "
            f"(OpenAI API недоступен?). Цикл провален: скоринг был бы "
            f"систематически смещён."
        )
    if llm_failed:
        print(f"⚠️  Stage 2 не отработал по {llm_failed} из {len(results)} строк")

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

    try:
        asyncio.run(run(
            args.input,
            args.output,
            args.cache,
            args.concurrency,
        ))
    except Stage2Unavailable as e:
        # Ненулевой код — сигнал оркестратору прервать цикл.
        print(f"❌ {e}")
        sys.exit(2)
    except Stage2ConfigError as e:
        print(f"❌ {e}")
        sys.exit(3)
