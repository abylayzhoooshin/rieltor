"""
Fast track — мониторинг НОВЫХ объявлений и РАННЕГО удешевления в окне
первых FAST_LIST_MAX_PAGES страниц. Независимый воркер.

Вызывается оркестратором каждые ~5 минут (частота задаётся снаружи, не
здесь). За один запуск:
    1. Сканирует первые FAST_LIST_MAX_PAGES страниц списка. Цена на
       странице списка УЖЕ ЕСТЬ в разметке (блок .a-card__price) — не
       нужен отдельный поход в карточку, чтобы её узнать.
    2. Сравнивает найденные id+цены с прошлым прогоном (fast_known_ids.json).
       - id, которых раньше не было → "new"
       - id, которые уже видели, но цена упала → "price_drop"
    3. Качает полную карточку (детали/фото/описание) ТОЛЬКО для этих
       двух категорий — не для всего окна, не для тех, у кого ничего
       не изменилось.
    4. Пишет их в FAST_NEW_LISTINGS_CSV (перезаписывается каждый прогон
       — снепшот ТОЛЬКО текущего прогона, оркестратор забирает файл
       сразу же). Колонка reason различает new/price_drop, для
       price_drop также пишется old_price.
    5. Заменяет fast_known_ids.json целиком на {id: цена} с текущего
       окна первых N страниц.

Первый запуск (когда fast_known_ids.json ещё не существует, либо явно
передан --warmup) — это WARMUP: состояние запоминается, но ничего не
шлётся в вывод. Иначе все id, которые физически существовали ДО старта
системы, улетели бы в вывод как "новые" одним пакетом.

ПОЛНОСТЬЮ независим от slow-track (v2_krisha_pars_fixed.py /
etap1_clean_data_v2.py): свои файлы состояния  вывода, никакой
координации, никаких общих локов. Использует напрямую только чистые
сетевые/утилитарные функции из v2_krisha_pars_fixed.py (fetch_url,
parse_detail_page, HEADERS, FETCH_URL, DETAIL_FIELDNAMES) — это
переиспользование БИБЛИОТЕЧНОГО кода, не рантайм-связь между
воркерами. Функция разбора страницы СПИСКА — своя, локальная (см.
parse_listing_page ниже): в отличие от slow track ей нужна ещё и цена,
которую общий парсер сейчас не извлекает, а трогать общий модуль ради
этого не нужно — это разные зоны ответственности.

ЧТО НЕ ДЕЛАЕТ:
    - не отслеживает объекты, которые выпали из окна первых N страниц
      (дальше эстафету по цене принимает slow track на своём часовом
      цикле — окно на 5 страниц при обычном трафике держит объявление
      в поле зрения по несколько часов, так что двойное покрытие есть)
    - не отслеживает подорожание — только падение цены
    - не отслеживает "пропавшие" объявления (removed)
    - никак не координируется со slow track

Запуск:
    python fast_track_parser.py \
        --known-ids fast_known_ids.json \
        --output fast_new_listings.csv \
        --max-pages 5
"""

import argparse
import asyncio
import csv
import json
import os
import random
import re
import sys

import aiohttp
from bs4 import BeautifulSoup

# slow_track — не пакет, а соседняя папка внутри 1_krisha_parser, поэтому
# импортируем через sys.path, а не через package-импорт. ВАЖНО: раньше
# здесь было `from mycop.v2_krisha_pars_fixed import ...` — mycop это
# черновая/легаси копия парсера, не связанная с поддерживаемой версией
# в slow_track/. Если предыдущий импорт вообще работал — это означало,
# что fast track тихо жил на другой, неподдерживаемой логике парсинга.
_SLOW_TRACK_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "slow_track"
)
if _SLOW_TRACK_DIR not in sys.path:
    sys.path.insert(0, _SLOW_TRACK_DIR)

from v2_krisha_pars_fixed import (
    DETAIL_FIELDNAMES,
    FETCH_URL,
    HEADERS,
    fetch_url,
    parse_detail_page,
)

# ============================== CONFIG ==============================

# Сколько страниц списка сканируем каждый прогон. При обычном притоке
# новых объявлений (десятки в час) окно на 5 страниц держит объявление
# в поле зрения несколько часов — этого достаточно, чтобы slow track
# (часовой цикл) гарантированно успел захватить его хотя бы раз-два,
# прежде чем оно органически выпадет из этого окна.
FAST_LIST_MAX_PAGES = 5

# Свои паузы — не связаны со slow-track, свой профиль нагрузки на сайт.
FAST_LIST_DELAY_MIN = 1.0
FAST_LIST_DELAY_MAX = 2.0
FAST_DETAIL_DELAY_MIN = 1.0
FAST_DETAIL_DELAY_MAX = 2.0

# Сколько карточек (new + price_drop) качаем одновременно. Ожидаемый
# объём за 5 минут небольшой, держим последовательным по умолчанию —
# та же логика, что и у slow track (риск бана по IP).
FAST_DETAIL_CONCURRENCY = 1

FAST_KNOWN_IDS_FILE = "fast_known_ids.json"
FAST_NEW_LISTINGS_CSV = "fast_new_listings.csv"

OUTPUT_FIELDNAMES = list(DETAIL_FIELDNAMES) + ["reason", "old_price"]


# ============================== СТАДИЯ 1: СПИСОК (id + цена) ==============================


def parse_listing_page(html, page_num):
    """
    Локальный парсер списка — в отличие от парсера slow track, забирает
    ещё и цену прямо со страницы списка (она уже есть в разметке карточки,
    блок .a-card__price), чтобы ловить падение цены без похода в карточку.
    """
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.a-card[data-id]")
    rows = {}
    for card in cards:
        advert_id = card.get("data-id")
        if not advert_id:
            continue
        price_el = card.select_one(".a-card__price")
        price = None
        if price_el:
            digits = re.sub(r"[^\d]", "", price_el.get_text())
            price = int(digits) if digits else None
        rows[advert_id] = {
            "id": advert_id,
            "url": f"https://krisha.kz/a/show/{advert_id}",
            "price": price,
            "page_number": page_num,
        }
    return rows


async def scan_window(session, max_pages):
    """Сканирует первые max_pages страниц, возвращает {id: {price, url, ...}}."""
    print(f"=== Fast track: список (первые {max_pages} стр.) ===")
    pages = {}
    for page_num in range(1, max_pages + 1):
        html = await fetch_url(session, FETCH_URL, params={"page": page_num})
        if html is None:
            print(f"   ⏭️  Страница {page_num} пропущена из-за ошибок сети")
            continue
        page_rows = parse_listing_page(html, page_num)
        pages.update(page_rows)
        print(f"   📄 Страница {page_num}/{max_pages}: найдено {len(page_rows)} ID")
        await asyncio.sleep(random.uniform(FAST_LIST_DELAY_MIN, FAST_LIST_DELAY_MAX))
    return pages


# ============================== СТАДИЯ 2: ДЕТАЛИ (new + price_drop) ==============================


async def fetch_details(session, ids, concurrency):
    """
    Качает полную карточку для переданных id (объединённый список new +
    price_drop). Простая очередь без circuit breaker/resume — объём за
    5-минутный цикл небольшой; если сайт начал банить, это раньше и
    заметнее проявится на slow track с его большим объёмом запросов.
    """
    results = {}
    queue = asyncio.Queue()
    for advert_id in ids:
        queue.put_nowait(advert_id)

    async def worker():
        while True:
            try:
                advert_id = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            url = f"https://krisha.kz/a/show/{advert_id}"
            html = await fetch_url(session, url)
            if html is None:
                print(f"   ⏭️  ID {advert_id} пропущен из-за ошибок сети")
            else:
                results[advert_id] = parse_detail_page(html, advert_id)
            await asyncio.sleep(random.uniform(FAST_DETAIL_DELAY_MIN, FAST_DETAIL_DELAY_MAX))

    workers = [
        asyncio.create_task(worker())
        for _ in range(min(concurrency, max(1, len(ids))))
    ]
    if workers:
        await asyncio.gather(*workers)
    return results


# ============================== СОСТОЯНИЕ / ВЫВОД ==============================


def load_known(path):
    """
    Безопасное чтение. Битый файл не крашит воркер навсегда — сохраняем
    его как .corrupted и стартуем с пустого состояния (тогда следующий
    прогон отработает как warmup — это лучше, чем бесконечный крэш-луп).
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        backup_path = path + ".corrupted"
        try:
            os.replace(path, backup_path)
        except OSError:
            pass
        print(
            f"⚠️  {path} повреждён ({e}), сохранён как {backup_path}, "
            f"стартуем с пустого состояния (следующий прогон = warmup)."
        )
        return {}


def save_known(path, known):
    """Атомарная запись: tmp-файл + fsync + os.replace."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(known, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def write_output(path, rows):
    # Перезаписывается каждый прогон — снепшот ТОЛЬКО этого прогона, не
    # журнал за всё время. Оркестратор забирает файл сразу после запуска.
    # Пишется всегда (даже пустым, только с заголовком) — чтобы у
    # оркестратора не было гонки за "файл ещё не существует".
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


# ============================== MAIN ==============================


async def run(known_ids_path, output_path, max_pages, concurrency, warmup=False, session=None):
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(headers=HEADERS)
    try:
        pages = await scan_window(session, max_pages)
        known = load_known(known_ids_path)

        is_first_run = warmup or not known

        if is_first_run:
            print("↪️  Первый запуск (warmup) — запоминаю текущее состояние, ничего не отправляю.")

        new_ids = [i for i in pages if i not in known]
        price_drop_ids = [
            i for i, row in pages.items()
            if i in known
            and row["price"] is not None
            and known.get(i) is not None
            and row["price"] < known[i]
        ]

        print(
            f"Всего id в окне (стр. 1-{max_pages}): {len(pages)}, "
            f"новых: {len(new_ids)}, подешевевших: {len(price_drop_ids)}"
        )

        output_rows = []
        if not is_first_run:
            ids_to_fetch = list(dict.fromkeys(new_ids + price_drop_ids))
            details = await fetch_details(session, ids_to_fetch, concurrency)

            for i in new_ids:
                row = details.get(i)
                if row:
                    row = dict(row)
                    row["reason"] = "new"
                    row["old_price"] = ""
                    output_rows.append(row)
            for i in price_drop_ids:
                row = details.get(i)
                if row:
                    row = dict(row)
                    row["reason"] = "price_drop"
                    row["old_price"] = known[i]
                    output_rows.append(row)

        write_output(output_path, output_rows)

        # Известные id+цены заменяются ЦЕЛИКОМ тем, что видно в окне ПРЯМО
        # СЕЙЧАС (не растёт бесконечно). Если объявление естественным
        # образом выпадет из окна более новыми постами — оно просто
        # перестанет быть в этом файле; если появится снова, будет
        # обработано заново как "new" (это не проблема — fast track не
        # претендует на полную историю, это задача slow track).
        save_known(known_ids_path, {i: row["price"] for i, row in pages.items()})

        print(f"✅ {output_path} — {len(output_rows)} записей за этот прогон (new+price_drop)")
        print(f"✅ {known_ids_path} — заменён ({len(pages)} id с первых {max_pages} стр.)")
    finally:
        if own_session:
            await session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--known-ids", default=FAST_KNOWN_IDS_FILE)
    parser.add_argument("--output", default=FAST_NEW_LISTINGS_CSV)
    parser.add_argument("--max-pages", type=int, default=FAST_LIST_MAX_PAGES)
    parser.add_argument("--concurrency", type=int, default=FAST_DETAIL_CONCURRENCY)
    parser.add_argument("--warmup", action="store_true", help="Первый запуск: только запомнить состояние, ничего не слать")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.known_ids, args.output, args.max_pages, args.concurrency, args.warmup))
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        sys.exit(0)