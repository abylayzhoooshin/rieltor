r"""
Stage 2 — текстовый треш-фильтр (методология v3, Этап 2), через
локальный Qwen3-14B GGUF + llama.cpp/llama-server OpenAI-compatible API.

Что делает:
    Для каждой записи из krisha_astana_clean.csv (выход Stage 1) читает
    full_description и текстовые поля furniture, rent_renovation, suited_for,
    после чего просит модель вернуть строго структурированный JSON:
        - finish_type: черновая / предчистовая / чистовая / null
        - red_flags: только фиксированный набор проблемных признаков
        - premium_markers: явно заявленные маркеры отделки/материалов/техники/мебели
        - extra_attributes: явно упомянутые дополнительные характеристики

Методологический принцип:
    Извлекаются только факты, которые явно следуют из текста объявления.
    Достоверность заявлений не проверяется. Red flags не удаляют запись,
    а выставляют requires_manual_review для последующего этапа.

Кэш:
    Хэш считается от реального prompt-текста + SYSTEM_PROMPT + PROMPT_VERSION.
    Если текст и инструкция не изменились, повторный запрос к модели не нужен.
    Ошибочные/невалидные ответы НЕ кэшируются.

Локальный API:
    llama-server должен быть запущен заранее и слушать http://127.0.0.1:8080/v1.
    Groq, дневные лимиты, RPM/TPM и budget-файл в этом варианте НЕ используются.

Зависимость Python:
    pip install openai

Пример запуска llama-server:
    .\llama-server.exe -m "D:\llama\models\Qwen3-14B-Q5_K_M.gguf" -ngl 99 -c 8192 -fa on --parallel 4 --reasoning off --alias qwen3-14b

Пример запуска Stage 2:
    python stage2_llm_analyze_local_qwen.py --input krisha_astana_clean.csv --output krisha_astana_analyzed.csv --cache llm_analysis_cache.json --concurrency 4
"""

import argparse
import asyncio
import csv
import hashlib
import json
import os
import re
import sys
from openai import AsyncOpenAI

# ============================== CONFIG ==============================

MODEL = "qwen3-14b"
# 300 было мало: часть объявлений с длинными premium_markers/extra_attributes
# не помещались в лимит, и JSON обрывался посреди строки
# ("Unterminated string..."). 600 даёт запас с учётом того, что thinking
# теперь отключён и токены тратятся только на сам ответ.
MAX_TOKENS = 600

LOCAL_API_URL = "http://127.0.0.1:8080/v1"
API_KEY = "local"

# Для llama-server, запущенного с --parallel/-np 4, начинаем с 4 одновременных запросов.
# Реальную оптимальную величину лучше подбирать по строкам/минуту.
LOCAL_CONCURRENCY = 4

# Локальный сервер не имеет Groq-лимитов. Эти повторы нужны только на случай
# временной ошибки сервера/HTTP или невалидного JSON.
RETRY_MAX_ATTEMPTS = 5
RETRY_DEFAULT_BACKOFF = 1.0

RED_FLAG_OPTIONS = [
    "плесень",
    "залив",
    "пожар",
    "судебные_споры",
    "аварийное_состояние",
    "требует_капремонта",
    "несогласованная_перепланировка",
]

# Меняется при любом изменении SYSTEM_PROMPT. Входит в hash кэша, чтобы
# старые результаты, полученные по другой инструкции, автоматически
# переанализировались.
PROMPT_VERSION = "stage2-v2-factual-extraction"

SYSTEM_PROMPT = f"""Ты выполняешь ТОЛЬКО извлечение фактов из объявления об аренде квартиры.

Цель: преобразовать текст объявления в строго структурированные признаки
для последующего статистического анализа стоимости аренды.

КРИТИЧЕСКИЙ ПРИНЦИП:
Извлекай только то, что явно следует из текста. Не оценивай квартиру,
не определяй её рыночную стоимость, не решай, является ли она дорогой
или дешёвой и не делай выводов по общему впечатлению.

НЕ ПРОВЕРЯЙ ДОСТОВЕРНОСТЬ:
если продавец/арендодатель пишет "дизайнерский ремонт", это фиксируется
как текстовый маркер, даже если это невозможно проверить. Если пишет
"рядом метро", это фиксируется как заявленный факт.

ОДНАКО НЕ ДОДУМЫВАЙ:
если признак не указан явно, оставляй null/пустой список.
Синоним или косвенный намёк не является доказательством другого признака,
если из текста нельзя сделать однозначный вывод.

КРИТИЧЕСКИ ВАЖНЫ ОТРИЦАНИЯ:
- "плесени нет" → НЕ ставить red_flag "плесень".
- "заливов не было" → НЕ ставить red_flag "залив".
- "перепланировка согласована" → НЕ ставить "несогласованная_перепланировка".
- "не требует ремонта" → НЕ ставить "требует_капремонта".
- "не аварийная" → НЕ ставить "аварийное_состояние".
Red flag ставится только если текст сообщает о наличии соответствующей
проблемы именно у этой квартиры. Упоминание проблемы в отрицательной форме,
в общем контексте или как того, чего нет, не является red flag.

Верни СТРОГО валидный JSON без Markdown, комментариев и пояснений:
{{
  "finish_type": "черновая" | "предчистовая" | "чистовая" | null,
  "red_flags": [],
  "premium_markers": [],
  "extra_attributes": {{}}
}}

finish_type:
- "черновая": нет финишной отделки; квартира явно в черновом/послестроительном состоянии.
- "предчистовая": основные строительные работы выполнены, квартира подготовлена
  под финишную отделку.
- "чистовая": финишная отделка уже присутствует; квартира отделана и пригодна
  для проживания или почти готова к нему.
- null: тип нельзя определить однозначно по тексту.
Наличие мебели само по себе НЕ определяет finish_type.

red_flags:
Используй ТОЛЬКО эти значения: {RED_FLAG_OPTIONS}.
Ставь флаг только при явном наличии проблемы у объекта.
Не ставь его за отрицание, гипотетическое условие, общую информацию
или упоминание проблемы, не относящейся к данной квартире.

Определения:
- "плесень": прямо указаны плесень/грибок или проблема с ними.
- "залив": прямо указано, что квартиру заливало/затапливало или она пострадала от затопления.
- "пожар": прямо указано, что квартира пострадала от пожара.
- "судебные_споры": прямо указан существующий судебный/юридический спор, связанный с квартирой.
- "аварийное_состояние": квартира прямо названа аварийной/опасной.
- "требует_капремонта": прямо сказано, что нужен капитальный ремонт.
- "несогласованная_перепланировка": прямо указано, что перепланировка не согласована/не узаконена/требует узаконивания.

Если проблема была в прошлом, но текст явно говорит, что она устранена,
не ставь текущий red_flag. Если из текста нельзя понять, устранена проблема
или нет, считай факт наличия проблемы упомянутым.

premium_markers:
Несмотря на название поля, НЕ оценивай премиальность. Это список
НАБЛЮДАЕМЫХ текстовых маркеров отделки, материалов, техники и мебели,
которые явно заявлены в объявлении и потенциально могут быть полезны
для будущей оценки.
Примеры: "дизайнерский ремонт", "евроремонт", "мраморная столешница",
"паркет", "встроенная техника Miele", "кухня Nolte".
Не добавляй собственные оценки вроде "дорогой ремонт", "премиум-объект",
"очень качественная квартира", если это не написано явно.
Сохраняй короткие формулировки, близкие к тексту объявления.

extra_attributes:
Сохраняй только явно упомянутые дополнительные характеристики, которые
не относятся напрямую к finish_type/red_flags/premium_markers.
Используй понятные и стабильные ключи. Предпочтительные ключи:
- "pets" — животные: true/false/краткое значение
- "parking" — парковка/паркинг: true/false/краткое значение
- "view" — явно описанный вид из окна
- "metro" — явно указанная близость метро/остановки
- "balcony" — балкон/лоджия: true/false/краткое значение
- "security" — охрана/консьерж/видеонаблюдение и т.п.
- другие ключи разрешены только если это явно упомянутая полезная характеристика,
  для которой нет подходящего ключа выше.
Не создавай разные названия одного и того же признака без необходимости:
например, используй "parking", а не "parking_available", "parking_lot" и т.п.
Не добавляй отсутствующие признаки.

Если описание пустое или бессмысленное — верни DEFAULT-пустой результат.
"""

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
    "requires_manual_review", "llm_skipped_error",
]

PROGRESS_STEP = 50

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
    # Кэш зависит не только от текста объявления, но и от версии инструкции.
    # Иначе после изменения prompt старые, уже размеченные записи ошибочно
    # считались бы актуальными.
    payload = f"{PROMPT_VERSION}\n{SYSTEM_PROMPT}\n{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def requires_manual_review(result):
    flags = set(result.get("red_flags") or [])
    return bool(flags & SEVERE_FLAGS)


def parse_model_json(raw_text):
    """Парсит и валидирует ответ модели. Невалидный результат НЕ превращается
    в пустышку: вызывающий код должен повторить запрос и не закэшировать мусор."""
    text = (raw_text or "").strip()
    if text.startswith("```") and text.endswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"ответ модели не является JSON: {e}") from e

    if not isinstance(parsed, dict):
        raise ValueError("корень JSON должен быть объектом")

    finish_type = parsed.get("finish_type")
    if finish_type not in {None, "черновая", "предчистовая", "чистовая"}:
        raise ValueError(f"недопустимый finish_type: {finish_type!r}")

    red_flags = parsed.get("red_flags", [])
    premium_markers = parsed.get("premium_markers", [])
    extra_attributes = parsed.get("extra_attributes", {})

    if not isinstance(red_flags, list) or not all(isinstance(x, str) for x in red_flags):
        raise ValueError("red_flags должен быть списком строк")
    if not isinstance(premium_markers, list) or not all(isinstance(x, str) for x in premium_markers):
        raise ValueError("premium_markers должен быть списком строк")
    if not isinstance(extra_attributes, dict):
        raise ValueError("extra_attributes должен быть объектом")

    unknown_flags = sorted(set(red_flags) - set(RED_FLAG_OPTIONS))
    if unknown_flags:
        raise ValueError(f"неизвестные red_flags: {unknown_flags}")

    # Убираем дубли, сохраняя порядок. Это делает CSV стабильнее и не меняет смысл.
    red_flags = list(dict.fromkeys(red_flags))
    premium_markers = list(dict.fromkeys(x.strip() for x in premium_markers if x.strip()))

    return {
        "finish_type": finish_type,
        "red_flags": red_flags,
        "premium_markers": premium_markers,
        "extra_attributes": extra_attributes,
    }


# ============================== ЛОКАЛЬНЫЙ API ==============================


def cache_key(row, prompt_text):
    """Стабильный ключ кэша: обычно id объявления, иначе хэш prompt-текста."""
    row_id = str(row.get("id") or "").strip()
    return row_id or text_hash(prompt_text)


# ============================== ВЫЗОВ API ==============================


async def analyze_one(client, row, semaphore):
    prompt_text = build_prompt_text(row)
    source_texts = [
        row.get("full_description") or "",
        row.get("furniture") or "",
        row.get("rent_renovation") or "",
        row.get("suited_for") or "",
    ]

    if not any(str(x).strip() for x in source_texts):
        return dict(DEFAULT_RESULT), False

    async with semaphore:
        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            try:
                response = await client.chat.completions.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    temperature=0,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt_text},
                    ],
                    # Qwen3 по умолчанию генерирует скрытые <think>...</think>
                    # рассуждения перед ответом. При MAX_TOKENS=300 модель часто
                    # не успевает выйти из "размышлений" до конца ответа, и
                    # content обрывается пустым (finish_reason="length").
                    # Явно просим шаблон chat не включать thinking — это дублирует
                    # --reasoning off на сервере на случай, если тот флаг не сработал.
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )

                raw_text = response.choices[0].message.content or ""
                finish_reason = response.choices[0].finish_reason

                # Страховка: если thinking всё же просочился (например, старая
                # версия llama-server игнорирует enable_thinking), вырезаем блок
                # <think>...</think> перед парсингом JSON.
                if "<think>" in raw_text:
                    raw_text = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()

                if not raw_text.strip():
                    raise ValueError(
                        f"пустой ответ модели (finish_reason={finish_reason!r}); "
                        "похоже, модель не уложилась в MAX_TOKENS "
                        "с учётом <think>-рассуждений"
                    )

                if finish_reason == "length":
                    raise ValueError(
                        "ответ модели обрезан по лимиту MAX_TOKENS "
                        f"({MAX_TOKENS}) — увеличьте MAX_TOKENS или "
                        "проверьте контекст сервера (-c / -np)"
                    )

                try:
                    result = parse_model_json(raw_text)
                    return result, False
                except ValueError as e:
                    if attempt < RETRY_MAX_ATTEMPTS:
                        wait_s = RETRY_DEFAULT_BACKOFF * attempt
                        print(
                            f"   ⚠️ id={row.get('id')}: невалидный ответ модели ({e}) — "
                            f"повторяю через {wait_s:.1f}s "
                            f"(попытка {attempt}/{RETRY_MAX_ATTEMPTS})"
                        )
                        await asyncio.sleep(wait_s)
                        continue

                    print(
                        f"   ⚠️ id={row.get('id')}: модель {RETRY_MAX_ATTEMPTS} раз "
                        "вернула невалидный JSON — не кэширую"
                    )
                    result = dict(DEFAULT_RESULT)
                    result["_skipped_error"] = True
                    return result, True

            except Exception as e:
                if attempt < RETRY_MAX_ATTEMPTS:
                    wait_s = RETRY_DEFAULT_BACKOFF * attempt
                    print(
                        f"   ⚠️ id={row.get('id')}: ошибка локального API ({e}) — "
                        f"повторяю через {wait_s:.1f}s "
                        f"(попытка {attempt}/{RETRY_MAX_ATTEMPTS})"
                    )
                    await asyncio.sleep(wait_s)
                    continue

                print(
                    f"   ⚠️ id={row.get('id')}: ошибка локального API после "
                    f"{RETRY_MAX_ATTEMPTS} попыток — не кэширую: {e}"
                )
                result = dict(DEFAULT_RESULT)
                result["_skipped_error"] = True
                return result, True

    result = dict(DEFAULT_RESULT)
    result["_skipped_error"] = True
    return result, True


async def analyze_one_with_progress(client, row, semaphore, counter, total):
    """Обёртка над analyze_one, которая печатает прогресс каждые
    PROGRESS_STEP завершённых запросов. Инкремент счётчика безопасен без
    lock: между await-точками корутины не прерываются."""
    result = await analyze_one(client, row, semaphore)
    counter[0] += 1
    if counter[0] % PROGRESS_STEP == 0 or counter[0] == total:
        print(f"   ... прогнано {counter[0]}/{total}")
    return result


async def analyze_all(rows, cache, concurrency=LOCAL_CONCURRENCY):
    if concurrency < 1:
        raise ValueError("concurrency должен быть >= 1")

    client = AsyncOpenAI(
        base_url=LOCAL_API_URL,
        api_key=API_KEY,
    )
    semaphore = asyncio.Semaphore(concurrency)

    prepared = []
    pending = []
    from_cache = 0

    for row in rows:
        prompt_text = build_prompt_text(row)
        h = text_hash(prompt_text)
        key = cache_key(row, prompt_text)
        cached = cache.get(key)

        if cached and cached.get("text_hash") == h and isinstance(cached.get("result"), dict):
            prepared.append((row, key, h, cached["result"], None))
            from_cache += 1
        else:
            pending_index = len(pending)
            prepared.append((row, key, h, None, pending_index))
            pending.append((row, key))

    print(
        f"Всего записей: {len(rows)}, "
        f"из кэша (текст не менялся): {from_cache}, "
        f"идёт в локальную модель: {len(pending)}"
    )
    print(f"Параллельность: {concurrency}")

    _progress_counter = [0]

    tasks = [
        asyncio.create_task(
            analyze_one_with_progress(client, row, semaphore, counter=_progress_counter, total=len(pending))
        )
        for row, _key in pending
    ]

    try:
        pending_results = await asyncio.gather(*tasks) if tasks else []

        results = []
        skipped_error_count = 0

        for row, key, h, cached_result, pending_index in prepared:
            if cached_result is not None:
                result = cached_result
                skipped = False
            else:
                result, skipped = pending_results[pending_index]
                if not skipped:
                    cache[key] = {
                        "text_hash": h,
                        "result": result,
                    }
                elif result.get("_skipped_error"):
                    skipped_error_count += 1

            results.append((row, result, skipped))

        if skipped_error_count:
            print(
                f"⚠️ Не удалось обработать из-за ошибок локальной модели/API: "
                f"{skipped_error_count}. Эти записи не кэшированы и будут "
                "повторены при следующем запуске."
            )

        return [(row, result) for row, result, _skipped in results]

    finally:
        await client.close()


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
            out_row["llm_skipped_error"] = bool(result.get("_skipped_error"))
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

    manual_review_count = sum(
        1 for _, r in results if requires_manual_review(r)
    )
    skipped_error_count = sum(
        1 for _, r in results if r.get("_skipped_error")
    )

    print(f"✅ {output_path} — {len(results)} записей")
    print(
        f"⚠️ Требует ручной проверки (серьёзные red_flags): "
        f"{manual_review_count}"
    )
    if skipped_error_count:
        print(
            f"⚠️ Не удалось обработать из-за ошибок модели/API: "
            f"{skipped_error_count}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Stage 2: извлечение признаков из объявлений через локальный Qwen3/llama-server."
    )
    parser.add_argument(
        "--input",
        default="krisha_astana_clean.csv",
        help="Входной CSV после Stage 1.",
    )
    parser.add_argument(
        "--output",
        default="krisha_astana_analyzed.csv",
        help="Выходной CSV с результатами Stage 2.",
    )
    parser.add_argument(
        "--cache",
        default="llm_analysis_cache.json",
        help="Файл кэша результатов LLM.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=LOCAL_CONCURRENCY,
        help=f"Количество одновременных запросов к llama-server (по умолчанию {LOCAL_CONCURRENCY}).",
    )
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency должен быть >= 1")

    try:
        asyncio.run(
            run(
                args.input,
                args.output,
                args.cache,
                args.concurrency,
            )
        )
    except FileNotFoundError as e:
        print(f"❌ Файл не найден: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        sys.exit(0)