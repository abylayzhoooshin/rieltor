"""
Stage 2 — текстовый треш-фильтр (методология v3, Этап 2), через Claude
API. Отдельный файл от Stage 1 (чистка), по договорённости — чистка и
анализ разделены, каждый можно гонять и переиспользовать независимо.

ЧТО ДЕЛАЕТ:
    Для каждой записи из krisha_astana_clean.csv (выход Stage 1) читает
    full_description (+ структурные текстовые поля furniture,
    rent_renovation, suited_for) и просит Claude вытащить структурированный
    JSON:
        - finish_type: черновая / предчистовая / чистовая / null
        - red_flags: список из фиксированного набора (плесень, залив,
          пожар, судебные споры, аварийное состояние, требует капремонта,
          несогласованная перепланировка)
        - premium_markers: список текстовых маркеров премиум-ремонта
          (дизайнерский ремонт, евроремонт, конкретные бренды техники/
          мебели и т.п.)
        - extra_attributes: свободный словарь доп. атрибутов, которые
          явно есть в тексте, но не разложены по колонкам парсера —
          животные разрешены/запрещены, парковка, вид из окна, рядом
          метро/остановка и т.п. Что нашёл, то и кладёт; ничего не
          придумывает, если в тексте не сказано — просто не добавляет
          ключ.

ВАЖНОЕ ДОПУЩЕНИЕ (по договорённости с пользователем):
    Все извлечённые атрибуты берутся как есть, БЕЗ проверки
    достоверности. Объявление вполне может врать про "евроремонт" или
    "рядом метро" — мы это не валидируем на этом этапе, задача Stage 2
    только прочитать, что НАПИСАНО, а не что правда. Это тот же принцип,
    что и во всей методологии: red_flags не отбрасывают объявление
    молча, а уводят в "требует ручной проверки" — то есть Stage 2 не
    претендует на истину в последней инстанции, а размечает сигнал для
    человека и для Этапа 3.

ВЕТВЛЕНИЕ ПО МЕТОДОЛОГИИ:
    - finish_type == "черновая" → отдельный пул (это дело Stage 3,
      здесь только проставляется флаг)
    - серьёзные red_flags ИЛИ несогласованная перепланировка →
      requires_manual_review = True (вне скоринга, а не просто скидка)

КЭШ (llm_analysis_cache.json, {id: {"text_hash":..., "result": {...}}}):
    Перед каждым вызовом API считается хэш от текста, который реально
    уходит в промпт (full_description + furniture + rent_renovation +
    suited_for). Если хэш не изменился с прошлого прогона — запись НЕ
    отправляется в API повторно, берётся закэшированный результат.
    Экономит деньги и время на каждом повторном прогоне Stage 2 по той
    же в целом базе (методология явно говорит: "текстовый фильтр — при
    каждом обновлении базы", то есть Stage 2 будет гоняться регулярно
    по растущей базе, а не один раз).

МОДЕЛЬ: claude-haiku-4-5-20251001 — дешёвая модель, ровно то, что и
    предполагает методология ("дешёвая LLM Haiku-класса").

ЗАВИСИМОСТИ: pip install anthropic
    Ключ по умолчанию берётся из переменной окружения ANTHROPIC_API_KEY
    (стандартно для SDK). Если не хочешь возиться с переменной
    окружения — впиши ключ прямо в API_KEY ниже (см. CONFIG) и он
    будет использован вместо переменной окружения. Это менее безопасно
    (ключ окажется в файле — если этот файл попадёт в git/куда-то ещё,
    ключ утечёт), но для локальной разработки на своей машине это
    рабочий вариант.

Запуск:
    python stage2_llm_analyze.py \
        --input krisha_astana_clean.csv \
        --output krisha_astana_analyzed.csv \
        --cache llm_analysis_cache.json \
        --concurrency 5
"""

import argparse
import asyncio
import csv
import hashlib
import json
import os
import sys

from anthropic import AsyncAnthropic

# ============================== CONFIG ==============================

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 500

# Если оставить None — SDK сам возьмёт ключ из переменной окружения
# ANTHROPIC_API_KEY. Если хочешь вписать ключ прямо тут — просто
# впиши строку вместо None. НЕ коммить этот файл с реальным ключом
# внутри в публичный/общий репозиторий — если работаешь в одиночку
# локально, это не критично, но привычка так себе.
API_KEY = "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"  # например: API_KEY = "sk-ant-api03-..."

RED_FLAG_OPTIONS = [
    "плесень",
    "залив",
    "пожар",
    "судебные_споры",
    "аварийное_состояние",
    "требует_капремонта",
    "несогласованная_перепланировка",
]

SYSTEM_PROMPT = f"""Ты помогаешь риелтору быстро прочитать текст объявления об аренде
квартиры и вытащить структурированные факты. НЕ проверяй правдивость —
просто извлекай, что написано в тексте, как есть.

Верни СТРОГО валидный JSON без каких-либо пояснений до/после, по схеме:
{{
  "finish_type": "черновая" | "предчистовая" | "чистовая" | null,
  "red_flags": [список из {RED_FLAG_OPTIONS}, только те, что реально упомянуты],
  "premium_markers": [список коротких текстовых маркеров премиум-ремонта,
                       если есть: "дизайнерский ремонт", "евроремонт",
                       конкретные бренды техники/мебели и т.п. Пустой
                       список, если ничего такого нет],
  "extra_attributes": {{произвольные ключ:значение — то, что явно
                        упомянуто в тексте, но не про ремонт: животные,
                        парковка, вид из окна, близость метро/остановки,
                        балкон/лоджия текстом, и т.п. Не выдумывай, если
                        не упомянуто — просто не добавляй ключ.}}
}}

Если описание пустое или бессмысленное — верни все поля пустыми/null."""

DEFAULT_RESULT = {
    "finish_type": None,
    "red_flags": [],
    "premium_markers": [],
    "extra_attributes": {},
}

SEVERE_FLAGS = {
    "плесень", "залив", "пожар", "судебные_споры",
    "аварийное_состояние", "несогласованная_перепланировка",
}

OUTPUT_EXTRA_FIELDNAMES = [
    "finish_type", "red_flags", "premium_markers", "extra_attributes",
    "requires_manual_review",
]

# ============================== ХЕЛПЕРЫ ==============================


def load_rows(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_cache(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(path, cache):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def build_prompt_text(row):
    """То, что реально уходит модели — от этого же текста считается хэш
    для кэша, так что если хоть один из этих кусков поменяется в базе
    (например, объявление отредактировали), запись переанализируется."""
    parts = [
        row.get("full_description") or "",
        f"Мебель: {row.get('furniture') or '—'}",
        f"Ремонт (как есть на сайте): {row.get('rent_renovation') or '—'}",
        f"Кому подходит: {row.get('suited_for') or '—'}",
    ]
    return "\n".join(parts).strip()


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def requires_manual_review(result):
    flags = set(result.get("red_flags") or [])
    return bool(flags & SEVERE_FLAGS)


def parse_model_json(raw_text):
    """Модель иногда оборачивает JSON в ```json ... ``` несмотря на промпт
    — подчищаем на всякий случай перед парсингом."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return dict(DEFAULT_RESULT)

    return {
        "finish_type": parsed.get("finish_type"),
        "red_flags": parsed.get("red_flags") or [],
        "premium_markers": parsed.get("premium_markers") or [],
        "extra_attributes": parsed.get("extra_attributes") or {},
    }


# ============================== ВЫЗОВ API ==============================


async def analyze_one(client, row, semaphore):
    prompt_text = build_prompt_text(row)
    if not prompt_text or prompt_text.count("—") == 3:  # только заглушки, реального текста нет
        return dict(DEFAULT_RESULT)

    async with semaphore:
        try:
            response = await client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt_text}],
            )
        except Exception as e:  # сетевые/API ошибки — не роняем весь прогон из-за одной записи
            print(f"   ⚠️  id={row.get('id')}: ошибка API ({e}) — оставляю пустой результат")
            return dict(DEFAULT_RESULT)

        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        return parse_model_json(raw_text)


async def analyze_all(rows, cache, concurrency):
    client = AsyncAnthropic(api_key=API_KEY) if API_KEY else AsyncAnthropic()
    semaphore = asyncio.Semaphore(concurrency)

    to_call = []       # (row, hash) — реально пойдут в API
    from_cache = 0

    prepared = []
    for row in rows:
        prompt_text = build_prompt_text(row)
        h = text_hash(prompt_text)
        cached = cache.get(row["id"])
        if cached and cached.get("text_hash") == h:
            prepared.append((row, h, cached["result"]))
            from_cache += 1
        else:
            prepared.append((row, h, None))
            to_call.append(row)

    print(f"Всего записей: {len(rows)}, из кэша (текст не менялся): {from_cache}, идёт в API: {len(to_call)}")

    tasks = {
        row["id"]: asyncio.create_task(analyze_one(client, row, semaphore))
        for row in to_call
    }
    if tasks:
        await asyncio.gather(*tasks.values())

    results = []
    for row, h, cached_result in prepared:
        if cached_result is not None:
            result = cached_result
        else:
            result = tasks[row["id"]].result()
            cache[row["id"]] = {"text_hash": h, "result": result}
        results.append((row, result))

    await client.close()
    return results


# ============================== ВЫВОД ==============================


def write_output(path, rows_with_results, base_fieldnames):
    fieldnames = base_fieldnames + OUTPUT_EXTRA_FIELDNAMES
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row, result in rows_with_results:
            out_row = dict(row)
            out_row["finish_type"] = result.get("finish_type") or ""
            out_row["red_flags"] = json.dumps(result.get("red_flags") or [], ensure_ascii=False)
            out_row["premium_markers"] = json.dumps(result.get("premium_markers") or [], ensure_ascii=False)
            out_row["extra_attributes"] = json.dumps(result.get("extra_attributes") or {}, ensure_ascii=False)
            out_row["requires_manual_review"] = requires_manual_review(result)
            writer.writerow(out_row)


# ============================== MAIN ==============================


async def run(input_path, output_path, cache_path, concurrency):
    rows = load_rows(input_path)
    if not rows:
        print("Нет записей для анализа.")
        return

    cache = load_cache(cache_path)
    results = await analyze_all(rows, cache, concurrency)
    save_cache(cache_path, cache)

    base_fieldnames = list(rows[0].keys())
    write_output(output_path, results, base_fieldnames)

    manual_review_count = sum(1 for _, r in results if requires_manual_review(r))
    print(f"✅ {output_path} — {len(results)} записей")
    print(f"⚠️  Требует ручной проверки (серьёзные red_flags): {manual_review_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="krisha_astana_clean.csv")
    parser.add_argument("--output", default="krisha_astana_analyzed.csv")
    parser.add_argument("--cache", default="llm_analysis_cache.json")
    parser.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.input, args.output, args.cache, args.concurrency))
    except FileNotFoundError as e:
        print(f"❌ Файл не найден: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        sys.exit(0)