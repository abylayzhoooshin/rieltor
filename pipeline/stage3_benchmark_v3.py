"""
Stage 3 — объективная оценка цена/качество для новых объявлений.

Главный принцип:
    baseline = строгий, чистый и замороженный рынок;
    incoming = реальные новые объявления, которые НЕ проходят жёсткий
               baseline-фильтр.

Новые объявления не исключаются из-за:
    - риелтора;
    - отсутствия фото;
    - red_flags;
    - неполной LLM-разметки;
    - рассрочки.

Вместо этого эти признаки становятся предупреждениями и снижают
уверенность. Жёстко невозможные/сломанные числовые данные не позволяют
сделать осмысленный price/m² score, но строка всё равно остаётся в output.

Вердикт:
    - НАХОДКА — цена заметно ниже объективной базы при достаточной уверенности;
    - СПРАВЕДЛИВАЯ — находится около ожидаемого рынка;
    - ПЕРЕОЦЕНЕНА — заметно выше ожидаемого рынка;
    - НЕДОСТАТОЧНО ДАННЫХ — сравнение слишком слабое;
    - РУЧНАЯ ПРОВЕРКА — есть серьёзный red flag, который нельзя честно
      превращать в ценовую скидку автоматически.

Важно:
    Stage 3 не утверждает, что квартира "хорошая" только потому, что она
    дешёвая. Поэтому отдельно выводятся:
        value_score              — насколько цена привлекательна;
        quality_evidence_score   — насколько много подтверждений качества;
        data_confidence          — насколько надёжен сам benchmark;
        seller_class             — хозяин / риелтор / неизвестно.

Для baseline используется только пригодный эталонный пул.
Для incoming текущая квартира НИКОГДА не подмешивается в baseline.
"""

import argparse
import csv
import math
import statistics
import sys


# ============================== CONFIG ==============================

N_MIN = 8
MIN_USABLE_COHORT = 3

RADIUS_L4_KM = 1.0
RADIUS_L5_KM = 3.0
PRICE_RANGE_L5_PCT = 0.30

# Fallback only. If baseline has enough observations, the actual local
# extreme-floor effect is estimated from the baseline.
DEFAULT_EXTREME_FLOOR_FACTOR = 0.95
MIN_FLOOR_MODEL_N = 30
FLOOR_FACTOR_MIN = 0.90
FLOOR_FACTOR_MAX = 1.00

# Verdict thresholds are deliberately wider than the old v3 thresholds
# when confidence is weak.
FIND_THRESHOLD_HIGH_CONF = 0.10
FIND_THRESHOLD_MED_CONF = 0.13
FIND_THRESHOLD_LOW_CONF = 0.18

OVERPRICE_THRESHOLD_HIGH_CONF = -0.07
OVERPRICE_THRESHOLD_MED_CONF = -0.10
OVERPRICE_THRESHOLD_LOW_CONF = -0.15

OUTPUT_EXTRA_FIELDNAMES = [
    "status",
    "verdict",
    "verdict_reason",
    "finish_bucket",
    "price_segment",
    "cohort_level",
    "cohort_size",
    "confidence_weight",
    "benchmark_confidence",
    "is_extreme_floor",
    "floor_adjustment_factor",
    "base_price_m2",
    "base_price_m2_corrected",
    "diff_pct",
    "robust_z",
    "value_score",
    "quality_evidence_score",
    "price_quality_score",
    "price_quality_label",
    "data_confidence",
    "seller_class",
    "seller_confidence",
    "seller_reason",
    "data_warnings",
]


# ============================== HELPERS ==============================


def to_float(x):
    try:
        if x is None or str(x).strip() == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def to_bool(x):
    return str(x).strip().lower() in ("true", "1", "yes", "y")


def parse_jsonish(value, default):
    """Small tolerant parser for CSV fields containing JSON-like lists/dicts."""
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    text = str(value).strip()
    if not text:
        return default

    import json
    try:
        return json.loads(text)
    except Exception:
        # Some historical CSVs contain Python-ish single-quoted lists.
        try:
            import ast
            return ast.literal_eval(text)
        except Exception:
            return default


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def load_rows(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def safe_rooms(row):
    raw = row.get("rooms")
    if raw is None:
        return None
    s = str(raw).strip().lower()
    # Preserve "студия" as a separate category if it exists.
    if s in ("studio", "студия"):
        return "studio"
    try:
        return str(int(float(s)))
    except Exception:
        return s or None


def infer_price_m2(row):
    pm2 = to_float(row.get("price_m2"))
    if pm2 is not None and pm2 > 0:
        return pm2

    price = to_float(row.get("price"))
    square = to_float(row.get("square_m2"))
    if price and price > 0 and square and square > 0:
        return price / square

    return None


def seller_class(row):
    """
    Сначала используем структурированный seller_type, затем owner_name.
    По текущей логике источника 'Хозяин' — наиболее сильный маркер владельца.
    Конкретное имя/компания без этого маркера считаем агентом/риелтором,
    но НЕ штрафуем цену за это.
    """
    owner = (row.get("owner_name") or "").strip()
    seller = (row.get("seller_type") or "").strip().lower()
    identity = to_bool(row.get("is_identity_confirmed"))

    if owner == "Хозяин":
        return "owner", 1.0, "owner_name=Хозяин"
    if any(x in seller for x in ("owner", "хозя", "собствен")):
        return "owner", 0.85 if not identity else 1.0, "seller_type указывает на владельца"
    if any(x in seller for x in ("agent", "риел", "агент", "company", "компан")):
        return "agent", 0.90, "seller_type указывает на агентство/риелтора"
    if owner:
        return "agent", 0.80, "указано конкретное имя вместо 'Хозяин'"
    return "unknown", 0.35, "маркер владельца/риелтора не определён"


def infer_finish_bucket(row):
    finish = (row.get("finish_type") or "").strip().lower()
    if finish == "черновая":
        return "rough"
    if finish in {"чистовая", "предчистовая"}:
        return "finished"
    return "unknown"


def enrich(row):
    row = dict(row)
    row["_price_m2"] = infer_price_m2(row)
    row["_rooms"] = safe_rooms(row)
    row["_lat"] = to_float(row.get("latitude"))
    row["_lon"] = to_float(row.get("longitude"))
    row["_complex_key"] = row.get("complex_id") or row.get("complex_name") or None

    floor = to_float(row.get("floor"))
    floor_total = to_float(row.get("floor_total"))
    row["_floor"] = floor
    row["_floor_total"] = floor_total
    row["_is_extreme_floor"] = bool(
        floor is not None
        and floor_total is not None
        and floor_total >= floor
        and (floor == 1 or floor == floor_total)
    )

    row["_finish_bucket"] = infer_finish_bucket(row)

    # Only baseline rows are allowed to be excluded from the reference pool.
    row["_excluded_baseline"] = (
        to_bool(row.get("requires_manual_review"))
        or to_bool(row.get("is_installment_segment"))
        or not row.get("id")
        or row["_price_m2"] is None
        or row["_rooms"] is None
    )

    sc, sc_conf, sc_reason = seller_class(row)
    row["_seller_class"] = sc
    row["_seller_confidence"] = sc_conf
    row["_seller_reason"] = sc_reason
    return row


def compute_price_segments(pool):
    values = sorted(
        r["_price_m2"] for r in pool
        if r["_price_m2"] is not None
    )
    if len(values) < 8:
        return {r.get("id"): "комфорт" for r in pool}

    q25 = statistics.quantiles(values, n=4, method="inclusive")[0]
    q75 = statistics.quantiles(values, n=4, method="inclusive")[2]

    result = {}
    for r in pool:
        pm2 = r["_price_m2"]
        if pm2 is None:
            result[r.get("id")] = "комфорт"
        elif pm2 <= q25:
            result[r.get("id")] = "эконом"
        elif pm2 >= q75:
            result[r.get("id")] = "бизнес"
        else:
            result[r.get("id")] = "комфорт"
    return result


def citywide_median_by_rooms(pool):
    by_rooms = {}
    for r in pool:
        if r["_price_m2"] is not None and r["_rooms"] is not None:
            by_rooms.setdefault(r["_rooms"], []).append(r["_price_m2"])
    return {
        rooms: statistics.median(values)
        for rooms, values in by_rooms.items()
        if values
    }


def same_target_id(r, target):
    return str(r.get("id")) == str(target.get("id")) and r.get("id") not in (None, "")


def price_segment_boundaries(pool):
    values = sorted(r["_price_m2"] for r in pool if r["_price_m2"] is not None)
    if len(values) < 8:
        return None, None
    qs = statistics.quantiles(values, n=4, method="inclusive")
    return qs[0], qs[2]


def target_price_segment(target, pool):
    pm2 = target.get("_price_m2")
    if pm2 is None:
        return "неизвестен"
    q25, q75 = price_segment_boundaries(pool)
    if q25 is None:
        return "комфорт"
    if pm2 <= q25:
        return "эконом"
    if pm2 >= q75:
        return "бизнес"
    return "комфорт"


def build_cohort(target, pool, segments, citywide_median):
    same_rooms = [
        r for r in pool
        if r["_rooms"] == target["_rooms"] and not same_target_id(r, target)
    ]

    if not same_rooms:
        return "none", []

    # Do not silently treat unknown finish as finished. For a target with
    # unknown finish we allow finished+unknown only as a lower-confidence
    # fallback, never rough+finished mixing.
    target_finish = target["_finish_bucket"]
    exact_finish = [r for r in same_rooms if r["_finish_bucket"] == target_finish]
    if target_finish == "unknown":
        comparable_rooms = [r for r in same_rooms if r["_finish_bucket"] in {"finished", "unknown"}]
    else:
        comparable_rooms = exact_finish

    if not comparable_rooms:
        return "none", []

    # L1/L2: same ЖК + rooms.
    if target["_complex_key"]:
        l12 = [
            r for r in comparable_rooms
            if r["_complex_key"] == target["_complex_key"]
        ]
        if len(l12) >= MIN_USABLE_COHORT:
            return "1-2", l12

    # L3: same street + rooms + price segment.
    target_segment = target_price_segment(target, pool)
    l3 = [
        r for r in comparable_rooms
        if r.get("street")
        and r.get("street") == target.get("street")
        and segments.get(r.get("id")) == target_segment
    ]
    if len(l3) >= MIN_USABLE_COHORT:
        return "3", l3

    # L4/L5: local radius.
    if target["_lat"] is not None and target["_lon"] is not None:
        l4 = [
            r for r in comparable_rooms
            if r["_lat"] is not None and r["_lon"] is not None
            and haversine_km(
                target["_lat"], target["_lon"],
                r["_lat"], r["_lon"]
            ) <= RADIUS_L4_KM
        ]
        if len(l4) >= MIN_USABLE_COHORT:
            return "4", l4

        median_for_rooms = citywide_median.get(target["_rooms"])
        if median_for_rooms:
            lo = median_for_rooms * (1 - PRICE_RANGE_L5_PCT)
            hi = median_for_rooms * (1 + PRICE_RANGE_L5_PCT)
            l5 = [
                r for r in comparable_rooms
                if r["_lat"] is not None and r["_lon"] is not None
                and haversine_km(
                    target["_lat"], target["_lon"],
                    r["_lat"], r["_lon"]
                ) <= RADIUS_L5_KM
                and r["_price_m2"] is not None
                and lo <= r["_price_m2"] <= hi
            ]
            if len(l5) >= MIN_USABLE_COHORT:
                return "5", l5

    # L6: same rooms across the city.
    return "6", comparable_rooms


def robust_stats(values):
    values = [v for v in values if v is not None and math.isfinite(v)]
    if not values:
        return None, None, None

    median = statistics.median(values)
    abs_dev = [abs(v - median) for v in values]
    mad = statistics.median(abs_dev)

    # IQR is useful as a second stability diagnostic.
    if len(values) >= 4:
        qs = statistics.quantiles(values, n=4, method="inclusive")
        iqr = qs[2] - qs[0]
    else:
        iqr = None

    return median, mad, iqr


def estimate_floor_factor(target, cohort, pool):
    """Estimate extreme-floor effect locally where possible.

    The methodology calls for a local structural effect. We therefore prefer
    the selected cohort; only if it is too small do we fall back to the wider
    same-rooms+finish baseline. If the cohort already contains only extreme
    floors, no extra correction is applied: otherwise we would double-count
    the floor effect.
    """
    if not target["_is_extreme_floor"]:
        return 1.0

    def estimate(candidates):
        extreme = [r["_price_m2"] for r in candidates if r["_is_extreme_floor"] and r["_price_m2"] is not None]
        normal = [r["_price_m2"] for r in candidates if not r["_is_extreme_floor"] and r["_price_m2"] is not None]
        if len(extreme) >= MIN_FLOOR_MODEL_N and len(normal) >= MIN_FLOOR_MODEL_N:
            e, n = statistics.median(extreme), statistics.median(normal)
            if n > 0:
                raw = e / n
                factor = 0.75 * raw + 0.25 * DEFAULT_EXTREME_FLOOR_FACTOR
                return max(FLOOR_FACTOR_MIN, min(FLOOR_FACTOR_MAX, factor))
        return None

    local = estimate(cohort)
    if local is not None:
        return local

    broader = [
        r for r in pool
        if r["_rooms"] == target["_rooms"]
        and r["_finish_bucket"] == target["_finish_bucket"]
    ]
    broad = estimate(broader)
    return broad if broad is not None else DEFAULT_EXTREME_FLOOR_FACTOR

def data_warnings(target):
    warnings = []

    if target["_price_m2"] is None:
        warnings.append("нет_price_m2")
    if target["_rooms"] is None:
        warnings.append("нет_rooms")
    if to_float(target.get("square_m2")) is None:
        warnings.append("нет_square_m2")
    if target["_lat"] is None or target["_lon"] is None:
        warnings.append("нет_координат")
    if not target.get("finish_type"):
        warnings.append("finish_type_неизвестен")

    try:
        photos = int(float(target.get("photo_count") or 0))
    except Exception:
        photos = 0
    if photos <= 0:
        warnings.append("нет_фото")
    elif photos < 5:
        warnings.append("мало_фото")

    if to_bool(target.get("requires_manual_review")):
        warnings.append("есть_red_flag")
    if to_bool(target.get("is_installment_segment")):
        warnings.append("рассрочка")
    if target["_seller_class"] == "agent":
        warnings.append("риелтор_или_агентство")
    elif target["_seller_class"] == "unknown":
        warnings.append("продавец_не_определён")

    return warnings


def quality_evidence_score(target):
    """
    Не пытается угадать "красивый ремонт" из воздуха.
    Это именно score наличия наблюдаемых доказательств качества,
    а не денежная поправка к цене.

    Сильные сигналы:
        finish_type,
        явно заявленные premium markers,
        меблировка,
        дополнительные удобства,
        фотографии.
    """
    score = 40.0

    finish = (target.get("finish_type") or "").strip().lower()
    if finish == "чистовая":
        score += 20
    elif finish == "предчистовая":
        score += 10
    elif finish == "черновая":
        score -= 15

    premium = parse_jsonish(target.get("premium_markers"), [])
    if isinstance(premium, list):
        score += min(15, 5 * len(premium))
    elif premium:
        score += 8

    furniture = (target.get("furniture") or "").strip().lower()
    if furniture and furniture not in ("нет", "—", "-"):
        score += 8
    if furniture in ("полностью", "полностью меблирована", "полностью меблирован"):
        score += 5

    extra = parse_jsonish(target.get("extra_attributes"), {})
    if isinstance(extra, dict):
        for key in ("parking", "security", "balcony", "view"):
            value = extra.get(key)
            if value not in (None, "", False, "не указано", "нет"):
                score += 2

    try:
        photos = int(float(target.get("photo_count") or 0))
    except Exception:
        photos = 0
    if photos >= 10:
        score += 10
    elif photos >= 5:
        score += 6
    elif photos >= 1:
        score += 2
    else:
        score -= 10

    # Red flags do not create an automatic "bad quality" price discount;
    # they create a manual-review requirement.
    if to_bool(target.get("requires_manual_review")):
        score -= 15

    return round(max(0.0, min(100.0, score)), 1)


def confidence_from_cohort(level, size):
    # Locality quality dominates. A large city-wide cohort is still weaker
    # than a small same-complex cohort.
    size_factor = min(1.0, math.log1p(max(size, 0)) / math.log1p(30))
    level_factor = {
        "1-2": 1.00,
        "3": 0.90,
        "4": 0.78,
        "5": 0.62,
        "6": 0.42,
        "none": 0.0,
    }.get(level, 0.25)

    return round(max(0.0, min(1.0, 0.35 * size_factor + 0.65 * level_factor)), 3)


def quality_confidence(target):
    warnings = data_warnings(target)
    # Missing core fields are much more damaging than being an agent.
    core = {"нет_price_m2", "нет_rooms"}
    missing_core = len(core.intersection(warnings))
    confidence_warnings = [w for w in warnings if w not in {"риелтор_или_агентство", "продавец_не_определён"}]
    score = 1.0 - 0.10 * len(confidence_warnings) - 0.25 * missing_core
    return max(0.0, min(1.0, score))


def price_quality_score(diff_pct, quality_score, benchmark_confidence, data_confidence):
    """
    Composite score for ranking, not a probability of a sale.

    65% = market-relative price advantage.
    25% = observable quality evidence.
    10% = confidence in the evidence/benchmark.

    Quality is intentionally not allowed to overwhelm market price: a model
    should not call an expensive apartment a bargain merely because its text
    says "designer renovation".
    """
    if diff_pct is None:
        return None
    value = max(0.0, min(100.0, 50.0 + 500.0 * diff_pct))
    confidence = 100.0 * (0.72 * benchmark_confidence + 0.28 * data_confidence)
    score = 0.65 * value + 0.25 * quality_score + 0.10 * confidence
    return round(max(0.0, min(100.0, score)), 1)


def price_quality_label(diff_pct, quality_score, warnings):
    if diff_pct is None:
        return "нет оценки"
    if diff_pct >= 0.10 and quality_score >= 55:
        label = "сильное цена/качество"
    elif diff_pct >= 0.05:
        label = "хорошее цена/качество"
    elif diff_pct <= -0.10:
        label = "слабое цена/качество"
    else:
        label = "обычное цена/качество"

    if "нет_фото" in warnings or "мало_фото" in warnings:
        label += "; качество не подтверждено фото"
    if "finish_type_неизвестен" in warnings:
        label += "; состояние не подтверждено текстом"
    return label


def verdict_from_diff(diff_pct, benchmark_confidence, target):
    if diff_pct is None:
        return "НЕДОСТАТОЧНО ДАННЫХ", "нет устойчивой рыночной базы"

    # Severe red flags are not priced automatically.
    if to_bool(target.get("requires_manual_review")):
        return "РУЧНАЯ ПРОВЕРКА", "есть red_flag: ценовую скидку нельзя честно оценить автоматически"

    if benchmark_confidence >= 0.75:
        find_t = FIND_THRESHOLD_HIGH_CONF
        over_t = OVERPRICE_THRESHOLD_HIGH_CONF
    elif benchmark_confidence >= 0.55:
        find_t = FIND_THRESHOLD_MED_CONF
        over_t = OVERPRICE_THRESHOLD_MED_CONF
    else:
        find_t = FIND_THRESHOLD_LOW_CONF
        over_t = OVERPRICE_THRESHOLD_LOW_CONF

    if diff_pct >= find_t:
        return "НАХОДКА", f"цена ниже скорректированной базы на {diff_pct:.1%}"
    if diff_pct <= over_t:
        return "ПЕРЕОЦЕНЕНА", f"цена выше скорректированной базы на {abs(diff_pct):.1%}"
    return "СПРАВЕДЛИВАЯ", f"цена близка к скорректированной базе ({diff_pct:+.1%})"


def score_row(target, pool, segments, citywide_median, soft_target=True):
    """
    score_row сохраняется module-level для совместимости с прежним
    orchestrator.py и внешними скриптами.
    """
    target = enrich(target)

    warnings = data_warnings(target)
    seller_cls, seller_conf, seller_reason = (
        target["_seller_class"],
        target["_seller_confidence"],
        target["_seller_reason"],
    )

    if target["_price_m2"] is None:
        return {
            "status": "insufficient_numeric_data",
            "verdict": "НЕДОСТАТОЧНО ДАННЫХ",
            "verdict_reason": "нет корректной цены и/или площади для price/m²",
            "seller_class": seller_cls,
            "seller_confidence": seller_conf,
            "seller_reason": seller_reason,
            "data_warnings": ";".join(warnings),
            "quality_evidence_score": quality_evidence_score(target),
            "data_confidence": round(quality_confidence(target), 3),
        }

    # Baseline exclusions are only relevant when scoring a baseline row in
    # offline mode. Incoming rows remain scoreable.
    if not soft_target and target["_excluded_baseline"]:
        reason = (
            "excluded_manual_review"
            if to_bool(target.get("requires_manual_review"))
            else "excluded_installment"
        )
        return {
            "status": reason,
            "verdict": "РУЧНАЯ ПРОВЕРКА",
            "verdict_reason": "строка не является пригодной для benchmark-пула",
            "seller_class": seller_cls,
            "seller_confidence": seller_conf,
            "seller_reason": seller_reason,
            "data_warnings": ";".join(warnings),
            "quality_evidence_score": quality_evidence_score(target),
            "data_confidence": 0.0,
        }

    # Finished and rough are never mixed.
    same_bucket_pool = [
        r for r in pool
        if r["_finish_bucket"] == target["_finish_bucket"]
        and not same_target_id(r, target)
        and not r.get("_excluded_baseline")
    ]

    level, cohort = build_cohort(
        target, same_bucket_pool, segments, citywide_median
    )

    if not cohort:
        return {
            "status": "no_comparables",
            "verdict": "НЕДОСТАТОЧНО ДАННЫХ",
            "verdict_reason": "нет сопоставимых объявлений в эталоне",
            "finish_bucket": target["_finish_bucket"],
            "cohort_level": level,
            "cohort_size": 0,
            "confidence_weight": 0.0,
            "benchmark_confidence": 0.0,
            "seller_class": seller_cls,
            "seller_confidence": seller_conf,
            "seller_reason": seller_reason,
            "data_warnings": ";".join(warnings),
            "quality_evidence_score": quality_evidence_score(target),
            "data_confidence": round(quality_confidence(target), 3),
        }

    prices = [r["_price_m2"] for r in cohort if r["_price_m2"] is not None]
    base_price_m2, mad, iqr = robust_stats(prices)
    if base_price_m2 is None:
        return {
            "status": "no_comparables",
            "verdict": "НЕДОСТАТОЧНО ДАННЫХ",
            "verdict_reason": "в когорте нет корректных price/m²",
            "finish_bucket": target["_finish_bucket"],
            "cohort_level": level,
            "cohort_size": len(cohort),
            "confidence_weight": 0.0,
            "benchmark_confidence": 0.0,
            "seller_class": seller_cls,
            "seller_confidence": seller_conf,
            "seller_reason": seller_reason,
            "data_warnings": ";".join(warnings),
            "quality_evidence_score": quality_evidence_score(target),
            "data_confidence": round(quality_confidence(target), 3),
        }

    floor_factor = estimate_floor_factor(target, cohort, pool)
    base_corrected = base_price_m2 * floor_factor
    diff_pct = (
        (base_corrected - target["_price_m2"]) / base_corrected
        if base_corrected
        else None
    )

    if mad and mad > 0:
        robust_z = 0.6745 * (target["_price_m2"] - base_price_m2) / mad
        robust_z = max(-10.0, min(10.0, robust_z))
    else:
        robust_z = None

    benchmark_conf = confidence_from_cohort(level, len(cohort))
    data_conf = quality_confidence(target)
    combined_conf = round(0.72 * benchmark_conf + 0.28 * data_conf, 3)

    verdict, verdict_reason = verdict_from_diff(
        diff_pct, combined_conf, target
    )

    # "value_score" is intentionally monotonic in diff_pct, but compressed
    # so a 40% apparent discount doesn't automatically become 100/100.
    if diff_pct is None:
        value_score = None
    else:
        value_score = 50 + 500 * diff_pct
        value_score = max(0.0, min(100.0, value_score))

    pq_score = price_quality_score(
        diff_pct,
        quality_evidence_score(target),
        benchmark_conf,
        data_conf,
    )
    pq_label = price_quality_label(
        diff_pct,
        quality_evidence_score(target),
        warnings,
    )

    return {
        "status": "scored",
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "finish_bucket": target["_finish_bucket"],
        "price_segment": target_price_segment(target, pool),
        "cohort_level": level,
        "cohort_size": len(cohort),
        "confidence_weight": round(min(1.0, len(cohort) / N_MIN), 2),
        "benchmark_confidence": benchmark_conf,
        "is_extreme_floor": target["_is_extreme_floor"],
        "floor_adjustment_factor": round(floor_factor, 4),
        "base_price_m2": round(base_price_m2, 1),
        "base_price_m2_corrected": round(base_corrected, 1),
        "diff_pct": round(diff_pct, 4) if diff_pct is not None else None,
        "robust_z": round(robust_z, 2) if robust_z is not None else None,
        "value_score": round(value_score, 1) if value_score is not None else None,
        "quality_evidence_score": quality_evidence_score(target),
        "price_quality_score": pq_score,
        "price_quality_label": pq_label,
        "data_confidence": data_conf,
        "seller_class": seller_cls,
        "seller_confidence": seller_conf,
        "seller_reason": seller_reason,
        "data_warnings": ";".join(warnings),
    }


def write_output(path, rows, results):
    base_fields = []
    for row in rows:
        for k in row.keys():
            if not k.startswith("_") and k not in base_fields:
                base_fields.append(k)

    fields = base_fields + [
        x for x in OUTPUT_EXTRA_FIELDNAMES if x not in base_fields
    ]

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row, result in zip(rows, results):
            out = {k: v for k, v in row.items() if not k.startswith("_")}
            out.update(result)
            writer.writerow(out)


def prepare_baseline(path):
    rows = [enrich(r) for r in load_rows(path)]
    usable = [r for r in rows if not r["_excluded_baseline"]]
    return rows, usable


def run(input_path, output_path, baseline_path=None, soft_target=True):
    rows = [enrich(r) for r in load_rows(input_path)]
    print(f"Загружено записей: {len(rows)}")

    if baseline_path:
        baseline_rows, usable_pool = prepare_baseline(baseline_path)
        print(
            f"Эталон: {baseline_path}; всего={len(baseline_rows)}, "
            f"usable={len(usable_pool)}"
        )
    else:
        # Backward-compatible offline mode.
        usable_pool = [
            r for r in rows
            if not r["_excluded_baseline"]
        ]
        print(
            "⚠️ --baseline не задан: используется self-referential pool. "
            "Для реального мониторинга это НЕ рекомендуется."
        )

    segments = compute_price_segments(usable_pool)
    citywide_median = citywide_median_by_rooms(usable_pool)

    results = [
        score_row(
            r, usable_pool, segments, citywide_median,
            soft_target=soft_target
        )
        for r in rows
    ]

    write_output(output_path, rows, results)

    scored = [r for r in results if r.get("status") == "scored"]
    verdicts = {}
    for r in scored:
        verdicts[r.get("verdict")] = verdicts.get(r.get("verdict"), 0) + 1

    print(f"✅ {output_path}: scored={len(scored)}")
    print(f"   verdicts={verdicts}")

    if baseline_path:
        print(
            "ℹ️ Incoming rows не чистятся по owner/photo/red_flags: "
            "эти признаки отражены как warnings/confidence."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="krisha_astana_analyzed.csv")
    parser.add_argument("--output", default="krisha_astana_benchmark.csv")
    parser.add_argument("--baseline", default=None)
    parser.add_argument(
        "--strict-target",
        action="store_true",
        help="использовать старое жёсткое поведение для target; "
             "для новых объявлений НЕ включать",
    )
    args = parser.parse_args()

    try:
        run(
            args.input,
            args.output,
            args.baseline,
            soft_target=not args.strict_target,
        )
    except FileNotFoundError as e:
        print(f"❌ Файл не найден: {e}")
        sys.exit(1)
