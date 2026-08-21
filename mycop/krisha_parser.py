"""
Парсер объявлений аренды квартир с krisha.kz (список, без захода в карточки).

Как это работает:
- Идём по страницам списка /arenda/kvartiry/astana/?page=N
- С каждой страницы вытаскиваем все доступные поля прямо из HTML карточек
  (цена, комнаты, площадь, этаж, адрес, район, ЖК, меблировка, тип продавца,
  дата публикации, фото, метки объявления и т.д.)
- Пишем построчно в CSV (не копим всё в памяти)
- Сохраняем прогресс (номер последней обработанной страницы) в progress.json,
  чтобы при обрыве/бане можно было продолжить с того же места, а не заново
- Между запросами — случайная пауза 2-4 сек, "по-человечески"

Запуск:
    python krisha_parser.py

Настройки — в блоке CONFIG ниже.
"""

import asyncio
import csv
import json
import random
import re
import os
import sys
from datetime import datetime

import aiohttp
from bs4 import BeautifulSoup

# ============================== CONFIG ==============================

BASE_URL = "https://krisha.kz/"
FETCH_URL = BASE_URL + "arenda/kvartiry/astana/"

OUTPUT_CSV = "krisha_astana_rent.csv"
PROGRESS_FILE = "progress.json"

# Пауза между запросами страниц (сек) — рандомизация против бана
DELAY_MIN = 2.0
DELAY_MAX = 4.0

# Сколько раз повторять запрос страницы при ошибке/бане перед тем, как пропустить
MAX_RETRIES = 3
# Пауза после неудачной попытки (сек), растёт с каждым повтором (backoff)
RETRY_BASE_DELAY = 5.0

# Ограничение на количество страниц для теста. None = без ограничения (все страницы)
MAX_PAGES = None  # например, поставь 20 для быстрого теста

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

FIELDNAMES = [
    "id",
    "url",
    "title",
    "rooms",
    "square_m2",
    "floor",
    "floor_total",
    "price_tenge",
    "district",
    "address",
    "full_subtitle",
    "complex_name",
    "furniture",
    "description_preview",
    "seller_type",
    "identity_confirmed",
    "city",
    "published_date",
    "photo_url",
    "is_top",
    "is_hot",
    "is_urgent",
    "page_number",
    "scraped_at",
]

# ============================== HELPERS ==============================


def load_progress():
    """Читает номер последней успешно обработанной страницы."""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("last_completed_page", 0)
        except Exception:
            return 0
    return 0


def save_progress(page_num):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_completed_page": page_num}, f)


def ensure_csv_header():
    """Создаёт CSV с заголовком, если файла ещё нет."""
    if not os.path.exists(OUTPUT_CSV):
        with open(OUTPUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()


def append_rows_to_csv(rows):
    if not rows:
        return
    with open(OUTPUT_CSV, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        for row in rows:
            writer.writerow(row)


def parse_title_details(title_text):
    """
    Из заголовка вида "3-комнатная квартира · 106 м² · 4/8 этаж"
    достаём комнаты, площадь, этаж, этажность.
    Заголовок может быть неполным (например, без этажа).
    """
    rooms = None
    square = None
    floor = None
    floor_total = None

    if not title_text:
        return rooms, square, floor, floor_total

    m_rooms = re.search(r"(\d+)-комнатная", title_text)
    if m_rooms:
        rooms = m_rooms.group(1)

    m_square = re.search(r"([\d.,]+)\s*м²", title_text)
    if m_square:
        square = m_square.group(1).replace(",", ".")

    m_floor = re.search(r"(\d+)/(\d+)\s*этаж", title_text)
    if m_floor:
        floor = m_floor.group(1)
        floor_total = m_floor.group(2)
    else:
        m_floor_single = re.search(r"(\d+)\s*этаж", title_text)
        if m_floor_single:
            floor = m_floor_single.group(1)

    return rooms, square, floor, floor_total


def parse_price(card):
    price_el = card.select_one(".a-card__price")
    if not price_el:
        return None
    # текст вида "300 000 ₸" (с неразрывными пробелами) -> берём только цифры
    text = price_el.get_text(" ", strip=True)
    digits = re.sub(r"[^\d]", "", text)
    return digits or None


def parse_complex_and_furniture(preview_text):
    """
    В превью описания часто встречается шаблон:
    "жил. комплекс X, меблирована полностью, <остальной текст>"
    Достаём ЖК и меблировку, если они есть.
    """
    complex_name = None
    furniture = None

    if not preview_text:
        return complex_name, furniture

    m_complex = re.search(r"жил\.\s*комплекс\s+([^,]+),", preview_text)
    if m_complex:
        complex_name = m_complex.group(1).strip()

    m_furniture = re.search(r"меблирована\s+([^,]+),", preview_text)
    if m_furniture:
        furniture = m_furniture.group(1).strip()

    return complex_name, furniture


def parse_seller_type(card):
    footer = card.select_one(".a-card__footer")
    if not footer:
        return None
    classes = footer.get("class", [])
    if "user-owner" in classes:
        return "owner"
    if "user-specialist" in classes:
        return "specialist"
    if "user-company" in classes:
        return "company"
    return None


def parse_card(card, page_num):
    """Извлекает все доступные поля из одной карточки объявления."""
    advert_id = card.get("data-id")

    title_el = card.select_one(".a-card__title")
    title_text = title_el.get_text(strip=True) if title_el else None
    rooms, square, floor, floor_total = parse_title_details(title_text)

    url = None
    if advert_id:
        url = f"https://krisha.kz/a/show/{advert_id}"

    price = parse_price(card)

    subtitle_el = card.select_one(".a-card__subtitle")
    subtitle_text = subtitle_el.get_text(strip=True) if subtitle_el else None

    district = None
    address = subtitle_text
    if subtitle_text and "р-н" in subtitle_text:
        # обычно формат "Название р-н, Улица дом"
        parts = subtitle_text.split(",", 1)
        district = parts[0].strip()
        if len(parts) > 1:
            address = parts[1].strip()

    preview_el = card.select_one(".a-card__text-preview")
    preview_text = preview_el.get_text(strip=True) if preview_el else None
    complex_name, furniture = parse_complex_and_furniture(preview_text)

    seller_type = parse_seller_type(card)
    identity_confirmed = bool(card.select_one(".identification-confirmed-badge"))

    stats_items = card.select(".a-card__stats-item")
    city = stats_items[0].get_text(strip=True) if len(stats_items) > 0 else None
    published_date = stats_items[1].get_text(strip=True) if len(stats_items) > 1 else None

    picture_el = card.select_one("picture.a-image__picture")
    photo_url = picture_el.get("data-full-src") if picture_el else None

    is_top = bool(card.select_one(".paid-icon__img.tfi-round-top-fill"))
    is_hot = bool(card.select_one(".paid-icon__img.tfi-round-fire-fill"))
    is_urgent = bool(card.select_one(".a-card__label")) or bool(
        card.select_one(".paid-icon__img.tfi-round-price-tag-fill")
    )

    return {
        "id": advert_id,
        "url": url,
        "title": title_text,
        "rooms": rooms,
        "square_m2": square,
        "floor": floor,
        "floor_total": floor_total,
        "price_tenge": price,
        "district": district,
        "address": address,
        "full_subtitle": subtitle_text,
        "complex_name": complex_name,
        "furniture": furniture,
        "description_preview": preview_text,
        "seller_type": seller_type,
        "identity_confirmed": identity_confirmed,
        "city": city,
        "published_date": published_date,
        "photo_url": photo_url,
        "is_top": is_top,
        "is_hot": is_hot,
        "is_urgent": is_urgent,
        "page_number": page_num,
        "scraped_at": datetime.utcnow().isoformat(timespec="seconds"),
    }


def parse_listing_page(html, page_num):
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.a-card[data-id]")
    return [parse_card(card, page_num) for card in cards]


def get_total_pages_from_html(html):
    """Достаём pagesCount из window.digitalData (см. исходный скрипт пользователя)."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        if script.string and "window.digitalData" in script.string:
            m = re.search(r"window\.digitalData\s*=\s*(\{.*?\});", script.string, re.S)
            if m:
                try:
                    data = json.loads(m.group(1))
                    return int(data.get("listing", {}).get("pagesCount", 1))
                except Exception:
                    pass
    return 1


# ============================== MAIN LOGIC ==============================


async def fetch_page(session, page_num):
    """Загружает одну страницу списка с ретраями. Возвращает HTML или None."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(
                FETCH_URL, params={"page": page_num}, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                if resp.status == 200:
                    return await resp.text()
                else:
                    print(
                        f"   ⚠️  Страница {page_num}: статус {resp.status} "
                        f"(попытка {attempt}/{MAX_RETRIES})"
                    )
        except Exception as e:
            print(
                f"   ⚠️  Страница {page_num}: ошибка запроса {e!r} "
                f"(попытка {attempt}/{MAX_RETRIES})"
            )

        if attempt < MAX_RETRIES:
            backoff = RETRY_BASE_DELAY * attempt
            print(f"   ⏳ Пауза {backoff:.0f} сек перед повтором...")
            await asyncio.sleep(backoff)

    print(f"   ❌ Страница {page_num}: не удалось загрузить после {MAX_RETRIES} попыток, пропускаю")
    return None


async def run():
    ensure_csv_header()
    start_page = load_progress() + 1

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        print("Запрашиваю первую страницу, чтобы узнать общее число страниц...")
        first_html = await fetch_page(session, 1)
        if first_html is None:
            print("Не удалось получить даже первую страницу. Прерываю работу.")
            return

        total_pages = get_total_pages_from_html(first_html)
        if MAX_PAGES:
            total_pages = min(total_pages, MAX_PAGES)

        print(f"✅ Всего страниц: {total_pages}")

        if start_page > 1:
            print(f"↪️  Продолжаю с страницы {start_page} (по данным {PROGRESS_FILE})")
        else:
            # первую страницу уже скачали выше — сразу распарсим её, чтобы не тратить запрос повторно
            rows = parse_listing_page(first_html, 1)
            append_rows_to_csv(rows)
            print(f"   📄 Страница 1: сохранено {len(rows)} объявлений")
            save_progress(1)
            start_page = 2

        total_saved = 0

        for page_num in range(start_page, total_pages + 1):
            html = await fetch_page(session, page_num)

            if html is not None:
                rows = parse_listing_page(html, page_num)
                append_rows_to_csv(rows)
                total_saved += len(rows)
                print(f"   📄 Страница {page_num}/{total_pages}: сохранено {len(rows)} объявлений")
                save_progress(page_num)
            else:
                # страница не загрузилась даже после ретраев — не двигаем прогресс,
                # чтобы при повторном запуске скрипт попробовал её снова
                print(f"   ⏭️  Страница {page_num} пропущена из-за ошибок сети")

            sleep_time = random.uniform(DELAY_MIN, DELAY_MAX)
            print(f"   ⏳ Пауза {sleep_time:.2f} сек...\n")
            await asyncio.sleep(sleep_time)

        print(f"\n🎉 Готово. Всего сохранено за этот запуск: {total_saved} объявлений")
        print(f"Результат: {os.path.abspath(OUTPUT_CSV)}")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nПрервано пользователем. Прогресс сохранён, можно продолжить позже.")
        sys.exit(0)