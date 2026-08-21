"""
Baseline table builder — эталонная (справочная) таблица для Stage 3.

ЗАЧЕМ:
    Stage 3 (stage3_benchmark.py) строит когорты и считает diff_pct
    объявления относительно похожих. Для этого ему нужен большой,
    ЧИСТЫЙ и ЗАМОРОЖЕННЫЙ пул объявлений — эталон, с которым потом
    сравниваются новые объявления (из fast/slow track или новых
    прогонов парсера). Этот скрипт готовит такой пул ОДИН РАЗ из
    сырого снепшота парсера (krisha_astana_detail.csv), не трогая сам
    сырой файл.

ЧТО ДЕЛАЕТ (по порядку — порядок важен, см. ниже):
    1. Stage 1 — переиспользует clean() из stage1_clean.py как есть:
       дедуп по id, отсев битых price/square/rooms, отсев
       archived/неживых (storage != "live"), проставляет
       is_installment_segment (пока всегда False — см. stage1_clean.py).
    2. Дедуп по photo_set_hash (НОВОЕ) — та же физическая квартира
       вполне может быть выставлена несколькими объявлениями (разные
       риелторы/агентства перевыставляют одну и ту же квартиру). Stage 1
       дедупит только по id, это другое. Если это не убрать ДО
       статистических фильтров — дубли исказят медиану/IQR когорты
       (тот же ЖК/улица посчитается "гуще", чем есть на самом деле).
       Из группы дублей (как и при дедупе по id в Stage 1) оставляется
       САМАЯ СВЕЖАЯ запись (по scraped_at/added_at), а не первая по
       порядку файла — см. freshest_first().
    2b. Дедуп по адресу+планировке (НОВОЕ) — страховка поверх п.2:
       photo_set_hash ловит только точное совпадение набора фото; если
       агентство перезалило другой набор — дубль проскочит мимо п.2.
       Ключ (улица, дом, этаж, комнатность, площадь≈) ловит такие
       случаи отдельно. См. докстринг dedupe_by_address_layout() —
       там же ограничение (риск склеить разные квартиры с одинаковой
       типовой планировкой на одном этаже).
    3. floor > floor_total (НОВОЕ) — целостность данных: если этаж
       больше этажности дома, это ошибка карточки/парсинга, а не
       квартира с "крайним этажом". Stage 3 использует floor/floor_total
       именно для скидки на крайний этаж — мусор в этом поле бьёт по
       structural-коэффициенту, а не только по цене.
    4. Price sanity (жёсткие границы + IQR по price_m2) — отсекает
       объявления с явно сломанной ценой за м² (опечатки типа "50 тг/м²"
       и т.п.), которых Stage 1 не ловит, потому что цена формально
       валидна (> 0). См. PRICE / SQUARE SANITY ниже.
    5. Square sanity (НОВОЕ, жёсткие границы + IQR по square_m2) — та же
       логика, что и для цены, но по площади: студия в 5 м² или
       "квартира" в 500 м² почти наверняка опечатка/ошибка парсинга,
       которую фильтр цены за м² может не поймать (соотношение
       цена/площадь там может выглядеть нормальным).
    6. Stage 2 — переиспользует analyze_all() из stage2_llm_analyze.py:
       LLM-разметка finish_type/red_flags/premium_markers/
       extra_attributes/requires_manual_review. Использует ТОТ ЖЕ файл
       кэша (llm_analysis_cache.json), что и обычный Stage 2 — если
       запись уже анализировалась когда-то (тем же текстом), повторно
       она не отправляется в API.
    7. Физический отсев по итогам Stage 2 (НОВОЕ, по решению
       пользователя) — записи с серьёзными red_flags (плесень, залив,
       пожар, судебные споры, аварийное состояние, несогласованная
       перепланировка → requires_manual_review) ИЛИ
       is_installment_segment уходят в dropped-лог, а НЕ остаются в
       эталоне с пометкой. Раньше их исключением на этапе когорт
       занимался Stage 3 (см. его докстринг), теперь эталон изначально
       компактнее — таких строк там просто нет.

    Порядок 2-3 (дедуп/целостность) ДО 4-5 (статистика) осознанный:
    статистические фильтры (IQR) считают квартили по группе — если в
    группе есть дубли или мусорные записи, это смещает сами границы,
    по которым потом всё остальное фильтруется.

ЧЕГО НЕ ДЕЛАЕТ:
    - не трогает krisha_astana_detail.csv (только читает)
    - не пересобирает себя молча: если output-файл уже существует —
      скрипт вообще ничего не делает (не читает input, не дёргает API,
      не трогает кэш) и просто сообщает об этом. Эталон замораживается
      осознанно один раз; чтобы пересобрать — удали
      krisha_astana_baseline.csv (и .dropped.csv рядом) руками и
      запусти снова. Никакого --force специально не добавлено —
      решение пользователя: пересборка эталона должна быть заметным,
      осознанным действием, а не флагом, который можно случайно
      передать в автоматизации.
    - НЕ фильтрует по координатам (кривой geocoding вне Астаны) и НЕ
      отсекает устаревшие по published_date — по решению пользователя,
      оставлено вне скоупа этого шага.
    - не решает финальный вердикт (находка/справедливая/переоценена)
      — это по-прежнему не входит в Stage 3, тем более не сюда

PRICE / SQUARE SANITY (общая логика для обоих полей):
    Двухуровневый фильтр, один и тот же код для price_m2 и square_m2
    (см. apply_hard_bounds/apply_iqr_filter — принимают функцию
    извлечения значения и подписи поля):

    1) Жёсткий пол/потолок — константы, НЕ подстраивающиеся под
       конкретный датасет. Для price_m2 откалиброваны по рыночной
       медиане аренды в Астане (~5500 тг/м²/мес на момент написания).
       Для square_m2 — по здравому смыслу жилых квартир (не студии в
       5 м² и не "квартиры" в полгектара). Всё вне границ — почти
       наверняка опечатка/битая карточка.
    2) IQR-выброс (метод Тьюки, k=IQR_MULTIPLIER) — ПОСЛЕ жёсткого
       фильтра, отдельно внутри каждой группы по комнатности (rooms),
       потому что и цена за м², и площадь у студий и у 4-комнатных
       объективно разные база. Группы меньше IQR_MIN_GROUP_SIZE не
       трогаются статистикой (ненадёжно на таком объёме) — только
       жёсткие границы.

    Все причины отсева пишутся в drop_reason baseline.dropped.csv с
    конкретными цифрами — чтобы можно было глазами проверить, что
    фильтр не режет лишнее.

СХЕМА ВЫХОДА:
    krisha_astana_baseline.csv — колонки как у krisha_astana_detail.csv
    + is_installment_segment (Stage 1) + finish_type/red_flags/
    premium_markers/extra_attributes/requires_manual_review (Stage 2).
    Это РОВНО та же схема, что у krisha_astana_analyzed.csv — то есть
    Stage 3 может принять этот файл на вход без каких-либо переделок.

    krisha_astana_baseline.dropped.csv — всё, что отсеялось на любом из
    шагов, с колонками drop_stage (stage1 / photo_dedup /
    floor_integrity / price_sanity / square_sanity / stage2_exclusion)
    и drop_reason.

    Колонки requires_manual_review/is_installment_segment в самом
    baseline.csv по-прежнему присутствуют (для совместимости схемы со
    Stage 3), но теперь там всегда False — если бы было True, строка
    уже ушла бы в dropped.csv на шаге 7.

Запуск:
    python build_baseline_table.py \
        --input krisha_astana_detail.csv \
        --output krisha_astana_baseline.csv \
        --cache llm_analysis_cache.json \
        --concurrency 4
"""

import argparse
import asyncio
import csv
import json
import os
import statistics
import sys
from datetime import datetime, timezone

from stage1_clean import clean as stage1_clean_rows
from stage2_llm_analyze import (
    analyze_all,
    load_cache,
    save_cache,
    requires_manual_review,
    OUTPUT_EXTRA_FIELDNAMES,
)

# ============================== CONFIG ==============================

# Жёсткие границы price_m2 (тг/м²/мес) — калибровано по медиане аренды
# в Астане (~5500 тг/м² на момент написания). Не статистика, а защита
# от абсурда: опечатки, цена за период вместо м², мусорные карточки.
HARD_MIN_PRICE_M2 = 1000
HARD_MAX_PRICE_M2 = 30000

# Жёсткие границы square_m2 — здравый смысл жилых квартир, не
# статистика. Меньше 12 м² — не квартира (или ошибка парсинга); больше
# 300 м² — экзотика, которую не с чем сравнивать в обычной когорте.
HARD_MIN_SQUARE_M2 = 12
HARD_MAX_SQUARE_M2 = 300

# IQR (Тьюки) — применяется отдельно внутри каждой группы по комнатности,
# уже ПОСЛЕ жёсткого фильтра. Общий для price_m2 и square_m2.
#
# k=3, а не классические 1.5 — сознательно ослаблено. Группировка тут
# только по rooms (city-wide), без района/ЖК — это НЕ настоящая когорта
# для сравнения, а грубая страховка от опечаток на этапе сборки эталона.
# При k=1.5 честная рыночная вариация по районам (дорогая, но нормальная
# квартира в престижном районе среди дешёвых окраинных с тем же числом
# комнат) рискует вылететь как "выброс", хотя это не мусор, а сигнал,
# который нужен дальше. Настоящую точную отбраковку выбросов внутри
# узкой когорты (свой ЖК → рядом) делает Stage 3 — там IQR с k=1.5
# уместнее, потому что группа уже однородная. Здесь, city-wide, k=3
# ловит только действительно дикие значения (опечатки в цене/площади),
# а не естественный разброс.
IQR_MULTIPLIER = 3
IQR_MIN_GROUP_SIZE = 8  # меньше — статистика ненадёжна, IQR не считаем

# Допуск по площади (м²) для дедупа по адресу+планировке — см.
# dedupe_by_address_layout(). Разные объявления одной и той же квартиры
# иногда указывают чуть разную площадь (округление/опечатка на десятые).
SQUARE_DEDUP_TOLERANCE_M2 = 1.0

DROPPED_EXTRA_FIELDNAMES = ["drop_stage", "drop_reason"]

# ============================== ХЕЛПЕРЫ ==============================


def to_float(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


CANDIDATE_DELIMITERS = [",", ";", "\t"]


def detect_delimiter(path):
    """
    csv.DictReader по умолчанию ожидает запятую. Если файл пересохранили
    в Excel на локали, где системный разделитель списков — ';' (обычное
    дело для RU/KZ Windows), колонки после чтения окажутся битыми: 'id'
    и все остальные поля пропадут как отдельные ключи, а вся строка
    схлопнется в одно поле. Проверяем заголовок на нескольких
    кандидатах-разделителях и берём тот, при котором находятся 'id' и
    'price' — это надёжнее универсального Sniffer'а, т.к. full_description
    может содержать что угодно и сбивать эвристику.
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        first_line = f.readline()
    for d in CANDIDATE_DELIMITERS:
        header = [h.strip() for h in first_line.strip().split(d)]
        if "id" in header and "price" in header:
            return d
    return ","  # фолбэк — ниже всё равно всплывёт понятная ошибка


def fix_duplicate_id_header(header):
    """
    ОБХОДНОЙ ПУТЬ (см. переписку) — баг в парсере: первая колонка по
    смыслу это id объявления, но названа "complex_id", а настоящий
    complex_id идёт дальше по списку тоже как "complex_id" — имя
    дублируется, реальной колонки "id" в файле нет вообще.
    csv.DictReader на дублирующихся именах отдаёт только ПОСЛЕДНЕЕ
    значение под этим ключом — то есть молча теряет первую колонку.

    Это костыль на стороне чтения, а не исправление первопричины —
    правильное место фикса всё ещё сам парсер, который пишет
    krisha_astana_detail.csv (переименовать первую колонку в 'id' при
    записи). Когда там поправят — эта функция станет no-op (условие
    ниже просто не совпадёт), можно смело оставить в коде.
    """
    if "id" in header:
        return header, False
    if header.count("complex_id") < 2:
        return header, False

    fixed = list(header)
    first_idx = fixed.index("complex_id")
    fixed[first_idx] = "id"
    return fixed, True


def load_rows(path):
    delimiter = detect_delimiter(path)
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        raw_header = next(csv.reader(f, delimiter=delimiter))
        header, patched = fix_duplicate_id_header(raw_header)
        if patched:
            print(
                "⚠️  Заголовок содержит 'complex_id' дважды и не содержит 'id' — "
                "похоже на баг в парсере (см. докстринг fix_duplicate_id_header). "
                "Переименовал первую колонку в 'id' на лету. Почини это в источнике "
                "krisha_astana_detail.csv, это временный костыль."
            )
        rows = list(csv.DictReader(f, fieldnames=header, delimiter=delimiter))

    if rows and "id" not in rows[0]:
        preview = list(rows[0].keys())[:3]
        raise ValueError(
            f"Не нашёл колонку 'id' в {path} (пробовал разделитель {delimiter!r}). "
            f"Первые ключи после чтения: {preview}. Похоже, файл был "
            "пересохранён с другим разделителем колонок или битой "
            "кодировкой (например, Excel мог сохранить CSV с ';' вместо "
            "','). Открой файл в текстовом редакторе (не Excel) и "
            "проверь, чем реально разделены колонки в первой строке."
        )
    return rows


def price_m2_value(row):
    price_m2 = to_float(row.get("price_m2"))
    if price_m2 is not None:
        return price_m2
    price = to_float(row.get("price"))
    square = to_float(row.get("square_m2"))
    if price and square:
        return price / square
    return None


def square_m2_value(row):
    return to_float(row.get("square_m2"))


def rooms_group_key(row):
    return row.get("rooms")
def to_bool(value):
    if isinstance(value, bool):
        return value

    if value is None:
        return False

    return str(value).strip().lower() in {
        "1", "true", "yes", "y", "да"
    }

# ============================== СВЕЖЕСТЬ (для дедупа) ==============================


def parse_freshness(row):
    """
    Та же логика, что и в stage1_clean.py (см. её докстринг там) —
    отдельная копия здесь, чтобы дедуп по фото и по адресу тоже выбирал
    самую свежую запись группы, а не первую по порядку файла.
    scraped_at (точный ISO-datetime) → фолбэк на added_at (дата) →
    None, если нет ни того, ни другого (тогда сортировка ничего не
    меняет и берётся первая встреченная, как раньше).
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


def freshest_first(group):
    """Сортирует группу дублей по убыванию свежести. Стабильна: если ни
    у кого нет scraped_at/added_at, порядок группы не меняется — первая
    встреченная в файле остаётся первой (старое поведение как фолбэк)."""
    return sorted(
        group,
        key=lambda r: (parse_freshness(r) is not None, parse_freshness(r) or datetime.min),
        reverse=True,
    )


# ============================== ДЕДУП ПО ФОТО ==============================


DUPLICATE_PRICE_TOLERANCE_PCT = 0.03  # >3% разницы в цене — уже не "тот же листинг"


def _looks_like_different_listing(a, b):
    """
    Возвращает True, если a и b, несмотря на совпавший сигнал (фото/адрес),
    похожи на ДВА РАЗНЫХ легитимных объявления одной квартиры (например,
    риелтор перевыставил чужими фото, а реальный хозяин выложил своё по
    более честной цене), а не на технический дубль одного и того же
    объявления/перезалива.

    Эвристика: цена отличается заметно (> DUPLICATE_PRICE_TOLERANCE_PCT)
    И продавец различается (owner_name или seller_type не совпадают).
    Оба условия сразу — иначе слишком легко словить ложное срабатывание
    (агентство просто скорректировало цену того же объявления).
    """
    try:
        price_a = float(a.get("price") or 0)
        price_b = float(b.get("price") or 0)
    except (TypeError, ValueError):
        return False
    if price_a <= 0 or price_b <= 0:
        return False

    price_diff = abs(price_a - price_b) / max(price_a, price_b)
    if price_diff <= DUPLICATE_PRICE_TOLERANCE_PCT:
        return False

    seller_a = (
        (a.get("owner_name") or "").strip().lower(),
        (a.get("seller_type") or "").strip().lower(),
    )
    seller_b = (
        (b.get("owner_name") or "").strip().lower(),
        (b.get("seller_type") or "").strip().lower(),
    )
    return seller_a != seller_b


def dedupe_by_photo_hash(rows):
    """
    Одна и та же квартира может быть выставлена несколькими
    объявлениями (перевыставление разными риелторами/агентствами).
    photo_set_hash — сигнал "это тот же набор фото", то есть
    физически та же квартира. Пустой хэш ничего не значит (не с чем
    сравнивать) — такие записи не трогаем.

    Из группы с одинаковым непустым хэшем оставляем САМУЮ СВЕЖУЮ (по
    scraped_at/added_at, см. freshest_first) — КРОМЕ случаев, когда
    запись похожа не на технический дубль, а на отдельное легитимное
    объявление той же квартиры от другого продавца по другой цене
    (см. _looks_like_different_listing) — такие записи оставляем ОБЕ и
    помечаем warning'ом "possible_duplicate_different_seller", а не
    удаляем молча.
    """
    groups = {}
    order = []
    no_hash = []
    for row in rows:
        h = (row.get("photo_set_hash") or "").strip()
        if not h:
            no_hash.append(row)
            continue
        if h not in groups:
            order.append(h)
            groups[h] = []
        groups[h].append(row)

    kept, dropped = list(no_hash), []
    for h in order:
        group = freshest_first(groups[h])
        survivors = [group[0]]
        for candidate in group[1:]:
            if any(_looks_like_different_listing(candidate, s) for s in survivors):
                flagged = dict(candidate)
                flagged["baseline_warning"] = "possible_duplicate_different_seller"
                survivors.append(flagged)
            else:
                dropped.append((candidate, "duplicate_photo_set_hash"))
        kept.extend(survivors)
    return kept, dropped


# ============================== ДЕДУП ПО АДРЕСУ+ПЛАНИРОВКЕ ==============================


def dedupe_by_address_layout(rows):
    """
    Вторая, более грубая линия дедупа — страховка на случай, когда
    photo_set_hash не срабатывает (агентство перезалило другой набор
    фото, поменяло порядок, добавило/убрало одну фотографию — хэш
    набора меняется, хотя квартира та же физически).

    Ключ: (улица, дом, этаж, комнатность) + площадь В ПРЕДЕЛАХ ДОПУСКА
    SQUARE_DEDUP_TOLERANCE_M2. Совпадение по всем сразу — сильный
    сигнал "это одна и та же квартира", не просто "похожая". Площадь
    сравниваем с допуском, а не строгим округлением до целого — иначе
    пары вроде 55.4 и 55.6 (одна и та же квартира, просто разное
    округление у разных риелторов) уедут в разные "корзины" из-за
    границы округления и дедуп их не поймает. Внутри группы
    (улица+дом+этаж+комнаты) площади сортируются и склеиваются в один
    кластер, если сосед по сортировке отличается не больше чем на
    допуск (транзитивная склейка цепочкой, не только попарно).

    ОГРАНИЧЕНИЕ (осознанное, не пытаемся его прятать): если в доме
    типовая застройка и на одном этаже реально существуют РАЗНЫЕ
    квартиры с одинаковой планировкой (например, зеркальные подъезды),
    этот фильтр их склеит как дубль. Это редкий случай — нужно точное
    совпадение улицы, дома, этажа, комнатности И площади с точностью
    до метра сразу — но не нулевой. Поэтому, как и везде в пайплайне,
    ничего не удаляется молча: причина попадает в dropped.csv с
    указанием конкретного ключа, чтобы можно было глазами проверить и
    при необходимости ослабить фильтр (например, дополнительно
    требовать совпадения complex_id).

    Не трогает записи с пустой улицей ИЛИ пустым номером дома — не с
    чем сравнивать, оставляем как есть.

    Запускать ПОСЛЕ dedupe_by_photo_hash: тот дедуп надёжнее (по сути
    точное совпадение), этот — более грубая эвристика поверх того, что
    уже прошло первую линию.
    """
    def base_key(row):
        street = (row.get("street") or "").strip().lower()
        house = (row.get("house_num") or "").strip().lower()
        floor = (row.get("floor") or "").strip()
        rooms = (row.get("rooms") or "").strip()
        return (street, house, floor, rooms)

    groups = {}
    order = []
    no_key = []
    for row in rows:
        street = (row.get("street") or "").strip()
        house = (row.get("house_num") or "").strip()
        if not street or not house:
            no_key.append(row)
            continue
        k = base_key(row)
        if k not in groups:
            order.append(k)
            groups[k] = []
        groups[k].append(row)

    kept, dropped = list(no_key), []
    for k in order:
        group = groups[k]

        # без валидной площади сравнивать не с чем — не трогаем
        with_square = [(r, to_float(r.get("square_m2"))) for r in group]
        no_square = [r for r, sq in with_square if sq is None]
        kept.extend(no_square)

        sized = sorted(((r, sq) for r, sq in with_square if sq is not None), key=lambda t: t[1])

        # цепочная кластеризация по допуску: разбиваем отсортированный
        # список там, где разрыв между соседями больше допуска
        clusters = []
        current = []
        prev_sq = None
        for r, sq in sized:
            if current and (sq - prev_sq) > SQUARE_DEDUP_TOLERANCE_M2:
                clusters.append(current)
                current = []
            current.append(r)
            prev_sq = sq
        if current:
            clusters.append(current)

        street, house, floor, rooms = k
        for cluster in clusters:
            if len(cluster) == 1:
                kept.append(cluster[0])
                continue
            cluster_sorted = freshest_first(cluster)
            survivors = [cluster_sorted[0]]
            reason = (
                f"duplicate_address_layout (street={street!r}, house={house!r}, "
                f"floor={floor!r}, rooms={rooms!r}, square within "
                f"{SQUARE_DEDUP_TOLERANCE_M2}m2)"
            )
            for candidate in cluster_sorted[1:]:
                if any(_looks_like_different_listing(candidate, s) for s in survivors):
                    flagged = dict(candidate)
                    flagged["baseline_warning"] = "possible_duplicate_different_seller"
                    survivors.append(flagged)
                else:
                    dropped.append((candidate, reason))
            kept.extend(survivors)

    return kept, dropped


# ============================== ЦЕЛОСТНОСТЬ: ЭТАЖ ==============================


def floor_integrity_filter(rows):
    """floor > floor_total — ошибка карточки/парсинга, а не квартира на
    крайнем этаже. Если хоть одно из полей не заполнено — не судим,
    оставляем (не наша забота тут восстанавливать данные)."""
    kept, dropped = [], []
    for row in rows:
        floor = to_float(row.get("floor"))
        floor_total = to_float(row.get("floor_total"))
        if floor is not None and floor_total is not None and floor > floor_total:
            dropped.append((
                row,
                f"floor_gt_floor_total (floor={floor:.0f} > floor_total={floor_total:.0f})",
            ))
            continue
        kept.append(row)
    return kept, dropped


# ============================== ЖЁСТКИЙ ФИЛЬТР + IQR (ОБЩАЯ ЛОГИКА) ==============================


def apply_hard_bounds(rows, value_fn, min_val, max_val, field_label):
    kept, dropped = [], []
    for row in rows:
        v = value_fn(row)
        if v is None:
            dropped.append((row, f"no_valid_{field_label}_for_sanity_check"))
            continue
        if v < min_val:
            dropped.append((row, f"{field_label}_too_low_hard ({v:.0f} < {min_val})"))
            continue
        if v > max_val:
            dropped.append((row, f"{field_label}_too_high_hard ({v:.0f} > {max_val})"))
            continue
        kept.append(row)
    return kept, dropped


def apply_iqr_filter(rows, value_fn, field_label, group_key_fn=rooms_group_key):
    """
    IQR-выброс отдельно по группам (по умолчанию — rooms). Группы
    меньше IQR_MIN_GROUP_SIZE пропускаются нетронутыми (недостаточно
    данных для честной статистики).
    """
    by_group = {}
    for row in rows:
        by_group.setdefault(group_key_fn(row), []).append(row)

    kept, dropped = [], []
    for group_key, group_rows in by_group.items():
        values = sorted(v for v in (value_fn(r) for r in group_rows) if v is not None)
        if len(values) < IQR_MIN_GROUP_SIZE:
            kept.extend(group_rows)  # мало данных — не трогаем
            continue

        q1 = statistics.quantiles(values, n=4)[0]
        q3 = statistics.quantiles(values, n=4)[2]
        iqr = q3 - q1
        lower = q1 - IQR_MULTIPLIER * iqr
        upper = q3 + IQR_MULTIPLIER * iqr

        for row in group_rows:
            v = value_fn(row)
            if v is not None and (v < lower or v > upper):
                dropped.append((
                    row,
                    f"{field_label}_outlier_iqr (group={group_key}, {v:.0f} вне [{lower:.0f}, {upper:.0f}])",
                ))
            else:
                kept.append(row)

    return kept, dropped


def sanity_filter(rows, value_fn, hard_min, hard_max, field_label):
    print(f"   {field_label}: жёсткие границы [{hard_min}, {hard_max}]")
    after_hard, dropped_hard = apply_hard_bounds(rows, value_fn, hard_min, hard_max, field_label)
    print(f"   {field_label} жёсткий фильтр: прошло {len(after_hard)}, отсеяно {len(dropped_hard)}")

    after_iqr, dropped_iqr = apply_iqr_filter(after_hard, value_fn, field_label)
    print(f"   {field_label} IQR (группы rooms, min размер {IQR_MIN_GROUP_SIZE}): "
          f"прошло {len(after_iqr)}, отсеяно {len(dropped_iqr)}")

    dropped = dropped_hard + dropped_iqr
    return after_iqr, dropped


# ============================== ВЫВОД ==============================


def write_baseline(path, rows_with_results, base_fieldnames):
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


def write_dropped(path, dropped_triples, base_fieldnames):
    fieldnames = base_fieldnames + DROPPED_EXTRA_FIELDNAMES
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row, stage, reason in dropped_triples:
            out_row = {k: v for k, v in row.items() if k in base_fieldnames}
            out_row["drop_stage"] = stage
            out_row["drop_reason"] = reason
            writer.writerow(out_row)


# ============================== MAIN ==============================


async def run(input_path, output_path, dropped_path, cache_path, concurrency):
    if os.path.exists(output_path):
        print(f"⏭️  {output_path} уже существует — эталон не пересобирается.")
        print("    Чтобы пересобрать: удали этот файл (и .dropped.csv рядом) вручную и запусти снова.")
        return

    rows = load_rows(input_path)
    print(f"Загружено сырых записей из {input_path}: {len(rows)}")
    all_dropped = []

    # --- Stage 1 (переиспользуем как есть) ---
    stage1_kept, stage1_dropped_raw = stage1_clean_rows(rows)
    print(f"Stage 1: прошло {len(stage1_kept)}, отсеяно {len(stage1_dropped_raw)}")
    all_dropped += [(r, "stage1", r.get("drop_reason")) for r in stage1_dropped_raw]

    # --- Дедуп по фото (новое) ---
    photo_kept, photo_dropped = dedupe_by_photo_hash(stage1_kept)
    print(f"Дедуп по photo_set_hash: прошло {len(photo_kept)}, отсеяно {len(photo_dropped)}")
    all_dropped += [(r, "photo_dedup", reason) for r, reason in photo_dropped]

    # --- Дедуп по адресу+планировке (новое, страховка поверх фото-дедупа) ---
    addr_kept, addr_dropped = dedupe_by_address_layout(photo_kept)
    print(f"Дедуп по адресу+планировке: прошло {len(addr_kept)}, отсеяно {len(addr_dropped)}")
    all_dropped += [(r, "address_layout_dedup", reason) for r, reason in addr_dropped]

    # --- Целостность этажа (новое) ---
    floor_kept, floor_dropped = floor_integrity_filter(addr_kept)
    print(f"floor > floor_total: прошло {len(floor_kept)}, отсеяно {len(floor_dropped)}")
    all_dropped += [(r, "floor_integrity", reason) for r, reason in floor_dropped]

    # --- Price sanity ---
    price_kept, price_dropped = sanity_filter(
        floor_kept, price_m2_value, HARD_MIN_PRICE_M2, HARD_MAX_PRICE_M2, "price_m2"
    )
    all_dropped += [(r, "price_sanity", reason) for r, reason in price_dropped]

    # --- Square sanity (новое) ---
    square_kept, square_dropped = sanity_filter(
        price_kept, square_m2_value, HARD_MIN_SQUARE_M2, HARD_MAX_SQUARE_M2, "square_m2"
    )
    all_dropped += [(r, "square_sanity", reason) for r, reason in square_dropped]

    sane_rows = square_kept

    # --- Stage 2 (переиспользуем как есть, с общим кэшем) ---
    if not sane_rows:
        print("Нет записей после чистки — Stage 2 пропущен.")
        results = []
    else:
        cache = load_cache(cache_path)
        results = await analyze_all(sane_rows, cache, concurrency)
        save_cache(cache_path, cache)

    # --- Физический отсев по red_flags / рассрочке (по решению пользователя) ---
    # Раньше эти записи оставались в эталоне с флагом requires_manual_review /
    # is_installment_segment, и их исключением занимался уже Stage 3 на
    # этапе построения когорт. Теперь эталон компактнее — такие записи
    # физически уходят в dropped-лог прямо здесь, в baseline.csv их не
    # будет вообще (Stage 3 по-прежнему проверяет эти флаги в своей логике
    # исключения — это ничему не мешает, просто там больше нечего исключать,
    # т.к. подобных строк в эталоне уже не будет).
    final_kept = []
    for row, result in results:
        manual_review = requires_manual_review(result)
        is_installment = to_bool(row.get("is_installment_segment"))
        if manual_review or is_installment:
            reasons = []
            if manual_review:
                reasons.append(f"requires_manual_review (red_flags={result.get('red_flags') or []})")
            if is_installment:
                reasons.append("is_installment_segment")
            all_dropped.append((row, "stage2_exclusion", "; ".join(reasons)))
        else:
            final_kept.append((row, result))

    base_fieldnames = list(rows[0].keys()) if rows else []

    if "is_installment_segment" not in base_fieldnames:
        base_fieldnames.append("is_installment_segment")
    if "baseline_warning" not in base_fieldnames:
        # добавляется dedupe_by_photo_hash/dedupe_by_address_layout, когда
        # запись оставлена как "похоже на другого продавца", а не удалена
        base_fieldnames.append("baseline_warning")

    write_baseline(output_path, final_kept, base_fieldnames)
    write_dropped(dropped_path, all_dropped, base_fieldnames)

    print(f"✅ {output_path} — {len(final_kept)} записей в эталоне")
    print(f"🗑️  {dropped_path} — {len(all_dropped)} отсеянных записей, с причинами по каждому шагу "
          f"(включая red_flags/рассрочку — теперь тоже физический отсев, а не просто флаг)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="krisha_astana_detail.csv")
    parser.add_argument("--output", default="krisha_astana_baseline.csv")
    parser.add_argument("--cache", default="llm_analysis_cache.json")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    dropped_path = args.output.rsplit(".", 1)[0] + ".dropped.csv"
    try:
        asyncio.run(run(args.input, args.output, dropped_path, args.cache, args.concurrency))
    except FileNotFoundError as e:
        print(f"❌ Файл не найден: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        sys.exit(0)