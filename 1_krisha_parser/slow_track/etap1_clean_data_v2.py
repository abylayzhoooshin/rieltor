"""
Slow track — мониторинг удешевления (бывший "Этап 1", роль сильно урезана).

ЧТО ДЕЛАЕТ:
    Сравнивает сегодняшний сырой снепшот от парсера (все объявления, что
    видно на сайте прямо сейчас, включая цену) с предыдущим сохранённым
    результатом (price_state.json). Находит объявления, которые ПОДЕШЕВЕЛИ
    с прошлого раза, и отдаёт ТОЛЬКО их — в price_drops.csv, откуда их
    забирает оркестратор.

ЧТО СОЗНАТЕЛЬНО НЕ ДЕЛАЕТ (отложено на потом):
    - никакой чистки/валидации битых записей (пустая цена, вне границ и т.п.)
    - никакого дедупа по id/фото
    - никакой сегментации (рассрочка и т.п.)
    - не отслеживает подорожание — цель найти варианты выгоднее, а не
      вообще любое изменение цены
    - не отслеживает "пропавшие" объявления (removed/missing_streak) —
      отдельная задача на будущее
    - не шлёт "новые" объявления никуда — это зона ответственности
      fast-трека, полностью отдельного воркера со своим состоянием

ВАЖНО: это ПОЛНОСТЬЮ независимый воркер от fast-трека. Ничего не шарят —
ни файлы, ни state, ни блокировки. Свой отдельный price_state.json.

СОСТОЯНИЕ (price_state.json):
    {id: {"price": ..., "url": ..., "title": ..., "last_seen_at": ...}}

    Целиком ЗАМЕНЯЕТСЯ новым снепшотом каждый прогон (не аппендится, не
    растёт бесконечно). Новые объявления просто попадают в него без
    события на выходе. Пропавшие объявления просто перестают в нём быть —
    без специальной обработки (removed не отслеживаем).

ВЫХОД:
    price_drops.csv — ТОЛЬКО объявления, подешевевшие в этом прогоне.
    Перезаписывается каждый раз (не аппендится) — оркестратор должен
    забрать файл сразу после прогона, копить историю тут не нужно.

Запуск:
    python etap1_clean_data_v2.py \
        --input krisha_astana_detail.csv \
        --state price_state.json \
        --output price_drops.csv
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

from v2_krisha_pars_fixed import DETAIL_FIELDNAMES

# ============================== ОБЩИЕ ХЕЛПЕРЫ ==============================


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_float(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def load_rows(input_path):
    with open(input_path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_state(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ============================== ОСНОВНАЯ ЛОГИКА ==============================

DROPS_FIELDNAMES = DETAIL_FIELDNAMES + ["old_price", "new_price", "price_drop", "detected_at"]
# Полный набор колонок krisha_astana_detail.csv (id, url, title, price, все
# остальные поля карточки) + 4 колонки самого события падения цены.
# Раньше тут был урезанный вручную список — переключились на полный набор,
# чтобы не забывать дописывать поля каждый раз, когда для скоринга/фильтра
# понадобится что-то ещё: они и так уже есть во входной строке бесплатно.


def find_price_drops(rows, prev_state):
    """
    rows — сегодняшний сырой снепшот (все объявления от парсера).
    prev_state — предыдущий результат {id: {...}}.

    Возвращает (price_drops, new_state):
      - price_drops: только те id, у которых была известная цена раньше
        И новая цена строго меньше старой. Новые id (которых не было в
        prev_state) в price_drops НЕ попадают.
      - new_state: полный upsert-снепшот на сегодня — им целиком
        заменяется prev_state (включая новые id, без event'а).
    """
    price_drops = []
    new_state = {}
    now = utcnow_iso()

    for row in rows:
        rid = row.get("id")
        new_price = to_float(row.get("price"))
        if rid is None or new_price is None:
            continue  # без валидной цены сравнивать нечего — чистка отдельно, позже

        prev = prev_state.get(rid)
        if prev is not None:
            old_price = to_float(prev.get("price"))
            if old_price is not None and new_price < old_price:
                drop_row = dict(row)  # вся строка как есть — id, url, title,
                # price, все остальные detail-поля (photo_count, seller_type,
                # owner_name, rooms, district и т.д.)
                drop_row["old_price"] = old_price
                drop_row["new_price"] = new_price
                drop_row["price_drop"] = old_price - new_price
                drop_row["detected_at"] = now
                price_drops.append(drop_row)

        new_state[rid] = {
            "price": new_price,
            "url": row.get("url"),
            "title": row.get("title"),
            "last_seen_at": now,
        }

    return price_drops, new_state


def write_drops(path, drops):
    # Перезаписывается каждый прогон — это снепшот ТОЛЬКО текущего прогона,
    # не журнал за всё время. Оркестратор забирает его сразу после запуска.
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DROPS_FIELDNAMES)
        writer.writeheader()
        writer.writerows(drops)


# ============================== MAIN ==============================


def run(input_path, state_path, output_path):
    rows = load_rows(input_path)
    print(f"Загружено записей: {len(rows)}")

    prev_state = load_state(state_path)
    price_drops, new_state = find_price_drops(rows, prev_state)

    write_drops(output_path, price_drops)
    save_state(state_path, new_state)

    print(f"Подешевело: {len(price_drops)} (было известно ранее: {len(prev_state)}, всего сегодня: {len(new_state)})")
    print(f"✅ {output_path} — только подешевевшие за этот прогон")
    print(f"✅ {state_path} — полностью заменён новым снепшотом ({len(new_state)} записей)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="krisha_astana_detail.csv")
    parser.add_argument("--state", default="price_state.json")
    parser.add_argument("--output", default="price_drops.csv")
    args = parser.parse_args()
    try:
        run(args.input, args.state, args.output)
    except FileNotFoundError as e:
        print(f"❌ Файл не найден: {e}")
        sys.exit(1)