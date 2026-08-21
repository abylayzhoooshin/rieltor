"""
Stage 3 — ценовой бенчмарк и когорты (методология v3, Этапы 3-4).
Отдельный файл, читает выход Stage 2 (krisha_astana_analyzed.csv — то
есть исходная схема krisha_astana_detail.csv + is_installment_segment
из Stage 1 + finish_type/red_flags/premium_markers/extra_attributes/
requires_manual_review из Stage 2).

ЧТО ДЕЛАЕТ:
    Для каждого объявления строит когорту сравнимых квартир (лестница с
    фолбэком, адаптированная под реальные данные — см. ниже) и считает
    diff_pct — насколько цена/м² объявления отличается от медианы
    когорты, со скидкой на структурные факторы (крайние этажи).

РЕЖИМ ЭТАЛОНА (--baseline, НОВОЕ):
    Раньше когорта строилась self-referential — прямо из самого
    input-файла (usable_pool = те же rows, что и скорятся). Это было
    нормально, пока input и был единственным доступным пулом данных.
    Теперь есть build_baseline_table.py, который готовит большой,
    чистый, ЗАМОРОЖЕННЫЙ эталонный пул (krisha_astana_baseline.csv) —
    отдельно от того, что скорится в конкретном прогоне.

    Если передан --baseline: пул сравнения (usable_pool, segments,
    citywide_median) строится ИСКЛЮЧИТЕЛЬНО из эталона, а --input — это
    просто список объявлений, которым нужно посчитать diff_pct (сами
    они в пул сравнения не подмешиваются, даже если физически там уже
    присутствуют — см. фикс сравнения по id ниже). Если --baseline не
    передан — старое поведение (self-referential) сохранено как есть,
    для обратной совместимости и разовых прогонов без эталона.

    ВАЖНО (фикс identity → id): раньше объявление исключалось из пула
    сравнения проверкой identity (`r is not target`) — работало, только
    когда пул и скорируемые строки это один и тот же список объектов.
    Когда пул (эталон) и target (новый кандидат) — РАЗНЫЕ объекты по
    одному и тому же объявлению (например, эталон уже когда-то включил
    этот id, а сейчас его же снова скорят из fast/slow трека), identity
    ничего не поймает — квартира сравнится сама с собой. Сравнение
    переведено на равенство по id (см. score_row/same_bucket_pool).

    Функции load_rows/enrich/compute_price_segments/
    citywide_median_by_rooms/score_row специально оставлены
    module-level и не завязаны на CLI — оркестратор (orchestrator.py)
    импортирует их напрямую, чтобы скорить кандидатов fast/slow трека
    относительно эталона без похода в subprocess на каждую мелкую
    пачку кандидатов.

ЧЕГО НЕ ДЕЛАЕТ (по решению пользователя, зафиксировано явно):
    - НЕ считает финальный вердикт находка/справедливая/переоценена.
      Методология считает "избыточную_скидку" через remont_score
      (Этап 5, vision), которую мы сейчас пропускаем. Без него
      реальная_скидка (diff_pct) есть, а "ожидаемая_скидка за ремонт" —
      нет, и вычитать одно из другого пока не из чего. Вердикт
      добавится отдельным шагом (Stage 4?), когда появится vision.
    - НЕ включает угол (третий структурный коэффициент) — по
      методологии он выключен по умолчанию до отдельной эмпирической
      проверки значимости на локальных данных. Такой проверки не
      делали, значит выключен.
    - НЕ кластеризует по ML (DBSCAN и т.п.) — по решению пользователя,
      разброс считается прямой геометрией (haversine-радиус), без ML.

АДАПТАЦИЯ ЛЕСТНИЦЫ КОГОРТ ПОД РЕАЛЬНЫЕ ДАННЫЕ:
    Методология строит уровни 1-6 вокруг ЖК → района → города, но в
    реальных данных complex_id/complex_name заполнены только у ~4%
    объявлений, а district — 0%. Без ML и без geocoding (решение
    пользователя) единственные надёжные локационные сигналы —
    street (100% заполнен) и latitude/longitude (100%). Лестница:

      Уровень 1/2  — тот же ЖК (complex_id ИЛИ complex_name, что из
                     двух заполнено) + та же комнатность
                     (уровни 1 и 2 методологии по факту схлопываются в
                     один — года постройки в данных нет, разделять
                     нечем)
      Уровень 3    — та же street + та же комнатность + тот же
                     ценовой сегмент (эконом/комфорт/бизнес,
                     см. ниже)
      Уровень 4    — радиус RADIUS_L4_KM + та же комнатность
      Уровень 5    — радиус RADIUS_L5_KM + расширенный ценовой
                     диапазон (±PRICE_RANGE_L5_PCT от медианы по
                     городу для этой комнатности)
      Уровень 6    — весь город, та же комнатность, без локационного
                     фильтра (последний фолбэк, самое низкое доверие)

    На каждом уровне остаётся ограничение "та же корзина отделки" (см.
    ниже) — черновая с черновой, не с чистовой.

    Эскалация: идём по уровням 1→6, останавливаемся на первом уровне,
    где кандидатов >= MIN_USABLE_COHORT. N_min (вес доверия) — отдельный
    от MIN_USABLE_COHORT параметр: даже маленькая когорта (3-5 записей)
    используется, но с явно низким confidence_weight.

ЦЕНОВОЙ СЕГМЕНТ (эконом/комфорт/бизнес):
    Методология делит по медианам ЖК (3.2). Раз ЖК почти нет —
    адаптация: квантили price_m2 считаются по отдельным объявлениям
    (не по медианам ЖК) в рамках всего usable-пула города. Нижние 25%
    → эконом, средние 50% → комфорт, верхние 25% → бизнес. Используется
    только как один из фильтров Уровня 3, не как отдельный вывод.

ЧЁРНОВАЯ vs ГОТОВАЯ ОТДЕЛКА (Этап 3.1):
    finish_type == "черновая" не сравнивается с остальными вообще —
    отдельная корзина. None/"предчистовая"/"чистовая" — вторая
    корзина (пока не разделяем предчистовую от чистовой, разница
    слишком тонкая при N=216).

ИСКЛЮЧЕНО ИЗ ПОСТРОЕНИЯ КОГОРТ И ИЗ САМОГО СКОРИНГА:
    - requires_manual_review == True (Stage 2 red flags) — цена может
      быть аномальной (пожар/залив/т.п.), не должна тянуть медиану
    - is_installment_segment == True (Stage 1) — рассрочка от
      застройщика искажает price/m² относительно вторички (сейчас
      всегда False, но уважаем флаг на будущее)
    Эти записи попадают в output со статусом excluded_* и без diff_pct.

СТРУКТУРНЫЙ КОЭФФИЦИЕНТ — КРАЙНИЕ ЭТАЖИ:
    EXTREME_FLOOR_DISCOUNT — фиксированная эвристика (не регрессия: 216
    записей мало для честной локальной регрессии). Если у объявления
    floor == 1 или floor == floor_total — считаем, что "типичная" база
    когорты завышена относительно такой квартиры, и корректируем базу
    вниз на EXTREME_FLOOR_DISCOUNT перед сравнением. Когда наберётся
    достаточно данных для нормальной регрессии по методологии (Этап
    3.5) — заменить константу на посчитанный коэффициент.

Запуск:
    python stage3_benchmark.py \
        --input krisha_astana_analyzed.csv \
        --output krisha_astana_benchmark.csv
"""

import argparse
import csv
import math
import statistics
import sys

# ============================== CONFIG ==============================

N_MIN = 6                     # знаменатель веса доверия
MIN_USABLE_COHORT = 3         # меньше этого — эскалируем на след. уровень

RADIUS_L4_KM = 1.0
RADIUS_L5_KM = 3.0
PRICE_RANGE_L5_PCT = 0.30     # ±30% от городской медианы для этой комнатности

EXTREME_FLOOR_DISCOUNT = 0.05  # 5% — эвристика-заглушка, см. докстринг

OUTPUT_EXTRA_FIELDNAMES = [
    "status", "finish_bucket", "price_segment",
    "cohort_level", "cohort_size", "confidence_weight",
    "is_extreme_floor", "base_price_m2", "base_price_m2_corrected", "diff_pct",
]

# ============================== ХЕЛПЕРЫ ==============================


def to_float(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def to_bool(x):
    return str(x).strip().lower() in ("true", "1", "yes")


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_rows(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def enrich(row):
    """Парсит числовые поля один раз, кладёт рядом — чтобы не
    перепарсивать на каждой итерации сравнения."""
    price = to_float(row.get("price"))
    square = to_float(row.get("square_m2"))
    price_m2 = to_float(row.get("price_m2")) or (price / square if price and square else None)
    floor = to_float(row.get("floor"))
    floor_total = to_float(row.get("floor_total"))

    row["_price_m2"] = price_m2
    row["_rooms"] = row.get("rooms")
    row["_lat"] = to_float(row.get("latitude"))
    row["_lon"] = to_float(row.get("longitude"))
    row["_complex_key"] = row.get("complex_id") or row.get("complex_name") or None
    row["_is_extreme_floor"] = bool(
        floor is not None and floor_total is not None and (floor == 1 or floor == floor_total)
    )
    row["_finish_bucket"] = "rough" if row.get("finish_type") == "черновая" else "finished"
    row["_excluded"] = to_bool(row.get("requires_manual_review")) or to_bool(row.get("is_installment_segment"))
    return row


def compute_price_segments(pool):
    """Квантили price_m2 по всему usable-пулу → {id: 'эконом'/'комфорт'/'бизнес'}."""
    values = sorted(r["_price_m2"] for r in pool if r["_price_m2"] is not None)
    if len(values) < 4:
        return {r["id"]: "комфорт" for r in pool}  # мало данных — всех в одну корзину, честно

    q25 = values[len(values) // 4]
    q75 = values[(len(values) * 3) // 4]

    segments = {}
    for r in pool:
        pm2 = r["_price_m2"]
        if pm2 is None:
            segments[r["id"]] = "комфорт"
        elif pm2 <= q25:
            segments[r["id"]] = "эконом"
        elif pm2 >= q75:
            segments[r["id"]] = "бизнес"
        else:
            segments[r["id"]] = "комфорт"
    return segments


def citywide_median_by_rooms(pool):
    by_rooms = {}
    for r in pool:
        by_rooms.setdefault(r["_rooms"], []).append(r["_price_m2"])
    return {
        rooms: statistics.median(v for v in vals if v is not None)
        for rooms, vals in by_rooms.items()
        if any(v is not None for v in vals)
    }


# ============================== ПОСТРОЕНИЕ КОГОРТЫ ==============================


def build_cohort(target, pool, segments, citywide_median):
    """pool уже отфильтрован под _finish_bucket == target бакет и
    исключает сам target. Возвращает (level, cohort_rows)."""
    same_rooms = [r for r in pool if r["_rooms"] == target["_rooms"]]

    # Уровень 1/2 — тот же ЖК
    if target["_complex_key"]:
        l12 = [r for r in same_rooms if r["_complex_key"] == target["_complex_key"]]
        if len(l12) >= MIN_USABLE_COHORT:
            return "1-2", l12

    # Уровень 3 — та же улица + тот же ценовой сегмент
    target_segment = segments.get(target["id"])
    l3 = [
        r for r in same_rooms
        if r.get("street") and r.get("street") == target.get("street")
        and segments.get(r["id"]) == target_segment
    ]
    if len(l3) >= MIN_USABLE_COHORT:
        return "3", l3

    # Уровень 4 — радиус RADIUS_L4_KM
    if target["_lat"] is not None and target["_lon"] is not None:
        l4 = [
            r for r in same_rooms
            if r["_lat"] is not None and r["_lon"] is not None
            and haversine_km(target["_lat"], target["_lon"], r["_lat"], r["_lon"]) <= RADIUS_L4_KM
        ]
        if len(l4) >= MIN_USABLE_COHORT:
            return "4", l4

        # Уровень 5 — радиус RADIUS_L5_KM + расширенный ценовой диапазон
        median_for_rooms = citywide_median.get(target["_rooms"])
        if median_for_rooms:
            lower = median_for_rooms * (1 - PRICE_RANGE_L5_PCT)
            upper = median_for_rooms * (1 + PRICE_RANGE_L5_PCT)
            l5 = [
                r for r in same_rooms
                if r["_lat"] is not None and r["_lon"] is not None
                and haversine_km(target["_lat"], target["_lon"], r["_lat"], r["_lon"]) <= RADIUS_L5_KM
                and r["_price_m2"] is not None and lower <= r["_price_m2"] <= upper
            ]
            if len(l5) >= MIN_USABLE_COHORT:
                return "5", l5

    # Уровень 6 — весь город, только комнатность (последний фолбэк)
    return "6", same_rooms


def score_row(target, pool, segments, citywide_median):
    if target["_excluded"]:
        reason = "excluded_manual_review" if to_bool(target.get("requires_manual_review")) else "excluded_installment"
        return {"status": reason}

    if target["_price_m2"] is None:
        return {"status": "no_price_m2"}

    # Сравнение по id, а не identity (r is not target) — раньше это
    # работало только пока пул и скорируемые строки были одним и тем
    # же списком объектов (self-referential режим). В режиме эталона
    # (--baseline) пул — отдельные объекты, и то же самое объявление
    # может физически присутствовать и там, и там (например, эталон
    # уже видел этот id раньше). identity бы такое не поймала и дала
    # квартире сравниться с самой собой.
    same_bucket_pool = [
        r for r in pool
        if r["_finish_bucket"] == target["_finish_bucket"] and r.get("id") != target.get("id")
    ]
    level, cohort = build_cohort(target, same_bucket_pool, segments, citywide_median)

    cohort_prices = [r["_price_m2"] for r in cohort if r["_price_m2"] is not None]
    if not cohort_prices:
        return {"status": "no_comparables"}

    base_price_m2 = statistics.median(cohort_prices)
    base_corrected = base_price_m2 * (1 - EXTREME_FLOOR_DISCOUNT) if target["_is_extreme_floor"] else base_price_m2

    diff_pct = (base_corrected - target["_price_m2"]) / base_corrected if base_corrected else None
    confidence_weight = min(1.0, len(cohort) / N_MIN)

    return {
        "status": "scored",
        "finish_bucket": target["_finish_bucket"],
        "price_segment": segments.get(target["id"]),
        "cohort_level": level,
        "cohort_size": len(cohort),
        "confidence_weight": round(confidence_weight, 2),
        "is_extreme_floor": target["_is_extreme_floor"],
        "base_price_m2": round(base_price_m2, 1),
        "base_price_m2_corrected": round(base_corrected, 1),
        "diff_pct": round(diff_pct, 4) if diff_pct is not None else None,
    }


# ============================== MAIN ==============================


def write_output(path, rows, results, base_fieldnames):
    fieldnames = base_fieldnames + OUTPUT_EXTRA_FIELDNAMES
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row, result in zip(rows, results):
            out_row = {k: v for k, v in row.items() if not k.startswith("_")}
            for field in OUTPUT_EXTRA_FIELDNAMES:
                out_row[field] = result.get(field, "")
            writer.writerow(out_row)


def run(input_path, output_path, baseline_path=None):
    rows = [enrich(r) for r in load_rows(input_path)]
    print(f"Загружено записей: {len(rows)}")

    if baseline_path:
        baseline_rows = [enrich(r) for r in load_rows(baseline_path)]
        usable_pool = [r for r in baseline_rows if not r["_excluded"]]
        print(
            f"Эталон загружен из {baseline_path}: {len(baseline_rows)} записей, "
            f"usable (не excluded) — {len(usable_pool)}"
        )
    else:
        print("⚠️  --baseline не задан — когорты строятся self-referential, из самого input (старое поведение).")
        usable_pool = [r for r in rows if not r["_excluded"]]

    segments = compute_price_segments(usable_pool)
    citywide_median = citywide_median_by_rooms(usable_pool)

    results = [score_row(r, usable_pool, segments, citywide_median) for r in rows]

    write_output(output_path, rows, results, list(rows[0].keys()) if rows else [])

    scored = sum(1 for r in results if r["status"] == "scored")
    by_level = {}
    for r in results:
        if r["status"] == "scored":
            by_level[r["cohort_level"]] = by_level.get(r["cohort_level"], 0) + 1

    print(f"✅ {output_path} — {scored} записей со скоринговым diff_pct")
    print(f"   по уровням когорт: {by_level}")
    excluded = sum(1 for r in results if r["status"].startswith("excluded"))
    no_comp = sum(1 for r in results if r["status"] == "no_comparables")
    print(f"   исключено (red flags/рассрочка): {excluded}, без сравнимых вообще: {no_comp}")
    print("ℹ️  Вердикт (находка/справедливая/переоценена) НЕ считается — нужен remont_score (vision), пока пропущен.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="krisha_astana_analyzed.csv")
    parser.add_argument("--output", default="krisha_astana_benchmark.csv")
    parser.add_argument(
        "--baseline", default=None,
        help="эталонная таблица (krisha_astana_baseline.csv из build_baseline_table.py). "
             "Если задана — когорты строятся из неё, а не self-referential из --input.",
    )
    args = parser.parse_args()
    try:
        run(args.input, args.output, args.baseline)
    except FileNotFoundError as e:
        print(f"❌ Файл не найден: {e}")
        sys.exit(1)