"""
Stage 1 — первичная чистка (методология v3, Этап 1). Полностью
детерминированная, без LLM и без сети — только то, что можно решить по
самим данным. Отдельный файл от анализа (Stage 2), по договорённости:
чистка и анализ — разные шаги пайплайна, каждый можно гонять и
переиспользовать независимо.

ЧТО ДЕЛАЕТ:
    - дедуп по id (страховка; сам детальный CSV и так обычно уникален
      по id, т.к. апсертится построчно — но если файл собирали руками
      из нескольких прогонов, тут это подчистится)
    - отсекает битые записи: нет валидной цены / площади / комнат
    - отсекает archived/неживые объявления (storage != "live") — это
      и есть "устаревшие объявления" из методологии: реальный сигнал,
      который парсер уже кладёт в поле storage, ничего выдумывать не
      пришлось
    - выделяет сегмент новостроек с рассрочкой в отдельный флаг
      (см. ЗАГЛУШКА ниже — сейчас всегда False, честного сигнала в
      данных нет)

ЧТО НЕ ДЕЛАЕТ (сознательно, см. прошлые решения по проекту):
    - дедуп по объекту (фото+адрес) — отложено, статус "не нужен пока"
    - фильтр коммерческой недвижимости — не нужен, источник и так
      craulит только /kvartiry/ (жилые квартиры), нечего отсеивать
    - ничего, что требует LLM или чтения текста описания — это Stage 2

ЗАГЛУШКА — сегмент "новостройка с рассрочкой":
    В методологии (Этап 1) новостройки с рассрочкой от застройщика или
    субсидированной ипотекой должны уходить в отдельный сегмент, чтобы
    их price/m² не смешивался со вторичкой при построении когорт
    (Этап 3). В текущей схеме данных (krisha_astana_detail.csv) НЕТ
    поля, которое напрямую говорит "это рассрочка от застройщика" —
    ни в структурированных полях, ни отдельным флагом. Пока что
    is_installment_segment всегда False (ничего не размечается), это
    сознательно оставлено как TODO, а не имитация правильного
    поведения через угадывание по тексту (текст — зона Stage 2, а не
    Stage 1, и полагаться на LLM в детерминированном чисто-табличном
    шаге не хочется). Когда появится реальный сигнал (например, парсер
    научится доставать это поле, или сформируется эвристика на данных)
    — здесь одна функция для правки.

ВЫХОД:
    - <output>.csv — прошедшие чистку записи, с добавленным полем
      is_installment_segment (сейчас всегда False)
    - <output>.dropped.csv — рядом, ЧТО было отсеяно и ПОЧЕМУ (колонка
      drop_reason) — чтобы ничего не терялось молча, можно проверить
      глазами, что чистка не отсекает лишнее

Запуск:
    python stage1_clean.py \
        --input krisha_astana_detail.csv \
        --output krisha_astana_clean.csv
"""

import argparse
import csv
import sys
from datetime import datetime, timezone

# ============================== ХЕЛПЕРЫ ==============================


def to_float(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def load_rows(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def is_installment_segment(row):
    """ЗАГЛУШКА — см. докстринг файла. Пока честного сигнала нет."""
    return False


def parse_freshness(row):
    """
    Возвращает сравнимую "свежесть" записи для дедупа по id: сначала
    пробуем scraped_at (самый точный сигнал — ISO datetime с
    таймзоной, момент реального скрапа), если пусто/не парсится —
    падаем на added_at (дата без времени). Если и этого нет — None,
    тогда дедуп просто оставит первую встреченную запись в группе
    (старое поведение, ничего не сломается).

    Таймзону нормализуем в UTC-naive (.replace(tzinfo=None) после
    приведения к UTC) — иначе сравнение tz-aware scraped_at с
    tz-naive added_at на разных записях одной группы падает с
    TypeError при сортировке.
    """
    for field in ("scraped_at", "added_at"):
        raw = (row.get(field) or "").strip()
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    return None


def freshness_sort_key(row):
    dt = parse_freshness(row)
    return (dt is not None, dt or datetime.min)


def find_drop_reason(row):
    """Возвращает причину отсева (str) или None, если запись годная."""
    price = to_float(row.get("price"))
    square = to_float(row.get("square_m2"))
    rooms = row.get("rooms")

    if price is None or price <= 0:
        return "no_valid_price"
    if square is None or square <= 0:
        return "no_valid_square"
    if not rooms:
        return "no_rooms"

    storage = (row.get("storage") or "").strip()
    if storage and storage != "live":
        return f"not_live (storage={storage})"

    return None


# ============================== ОСНОВНАЯ ЛОГИКА ==============================


def clean(rows):
    kept = []
    dropped = []

    # Группируем по id вместо стримингового "первая встреченная — та и
    # осталась": порядок строк в исходном CSV не гарантированно
    # хронологический (особенно если файл собирали руками из нескольких
    # прогонов — см. докстринг файла), поэтому "первая по файлу" и
    # "самая свежая" — не одно и то же. Внутри группы дублей сортируем
    # по freshness_sort_key и оставляем самую свежую (scraped_at, потом
    # added_at); если свежести нет ни у кого — sorted() стабилен, так
    # что первая в группе останется первой встреченной, как и раньше.
    groups = {}
    order = []
    for row in rows:
        rid = row.get("id")
        if rid not in groups:
            order.append(rid)
            groups[rid] = []
        groups[rid].append(row)

    for rid in order:
        group = groups[rid]
        if len(group) > 1:
            group = sorted(group, key=freshness_sort_key, reverse=True)
            for stale in group[1:]:
                dropped.append({**stale, "drop_reason": "duplicate_id"})
        freshest = group[0]

        reason = find_drop_reason(freshest)
        if reason:
            dropped.append({**freshest, "drop_reason": reason})
            continue

        row = dict(freshest)
        row["is_installment_segment"] = is_installment_segment(row)
        kept.append(row)

    return kept, dropped


def write_csv(path, rows, fieldnames):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ============================== MAIN ==============================


def run(input_path, output_path):
    rows = load_rows(input_path)
    print(f"Загружено записей: {len(rows)}")

    kept, dropped = clean(rows)

    input_fieldnames = list(rows[0].keys()) if rows else []
    write_csv(output_path, kept, input_fieldnames + ["is_installment_segment"])

    dropped_path = output_path.rsplit(".", 1)[0] + ".dropped.csv"
    write_csv(dropped_path, dropped, input_fieldnames + ["drop_reason"])

    installment_count = sum(1 for r in kept if r["is_installment_segment"])
    print(f"✅ Прошло чистку: {len(kept)}  (из них сегмент рассрочки: {installment_count} — заглушка, см. докстринг)")
    print(f"🗑️  Отсеяно: {len(dropped)} → подробности в {dropped_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="krisha_astana_detail.csv")
    parser.add_argument("--output", default="krisha_astana_clean.csv")
    args = parser.parse_args()
    try:
        run(args.input, args.output)
    except FileNotFoundError as e:
        print(f"❌ Файл не найден: {e}")
        sys.exit(1)