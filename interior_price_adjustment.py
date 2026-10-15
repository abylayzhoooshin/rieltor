"""Осторожная поправка справедливой аренды на состояние интерьера.

Оценка ремонта не является самостоятельным сигналом «находка». Она лишь
приводит рассчитанную Stage 3 цену аналогов к состоянию оцениваемой квартиры.
Если надёжной оценки цели или аналогов нет, функция возвращает исходную цену.
"""

from __future__ import annotations

import math
from typing import Any


EVALUATOR_VERSION = "simple-decimal-v4-original-gpt6-luna"

# Рынок оказался асимметричным: плохой ремонт даёт заметно больший дисконт,
# чем небольшое превосходство ремонта даёт премию. Коэффициенты выбраны на
# train-части и отдельно подтверждены на фиксированном hold-out; менять их без
# повторного eval_interior_adjustment.py нельзя.
INTERIOR_LOG_PRICE_PER_SCORE_BELOW_REFERENCE = 0.12
INTERIOR_LOG_PRICE_PER_SCORE_ABOVE_REFERENCE = 0.04
MAX_ABS_LOG_ADJUSTMENT = 0.15
MIN_REFERENCE_SCORES = 2

# v2: вместо одной поправки относительно «среднего ремонта» каждый аналог
# сначала приводится к классу ремонта цели. Премии — в log-price; они выбраны
# на старой части baseline и затем проверены на трёх следующих временных
# окнах. Это не линейная шкала: основной рыночный скачок проходит около 6.
# Непрерывная нелинейная кривая вместо четырёх классов ремонта. Каждый
# переход — smoothstep: на краях производная равна нулю, поэтому квартира не
# получает скачок справедливой цены из-за сотой доли балла около 5.5/6/7.
# Суммарные уровни (0.03/0.12/0.18 log-price) сохраняют наблюдавшийся масштаб,
# но теперь являются мягкими плато, а не дискретными корзинами.
REPAIR_PREMIUM_TRANSITIONS = (
    (5.25, 5.75, 0.03),
    (5.75, 6.25, 0.09),
    (6.75, 7.25, 0.06),
)
PAIRWISE_SCORE_SIMILARITY_SIGMA = 0.70
PAIRWISE_SIMILARITY_FLOOR = 0.15
PAIRWISE_CANDIDATE_SHARE = 0.75
PAIRWISE_MANY_COMPARABLES = 16
PAIRWISE_WINSOR_MIN_N = 8
PAIRWISE_WINSOR_FRACTION = 0.10


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def usable_interior_score(row: dict[str, Any]) -> tuple[float, float] | None:
    """Вернуть числовую оценку и её вес либо None для неизвестной оценки."""
    if str(row.get("interior_evaluator_version") or "").strip() != EVALUATOR_VERSION:
        return None
    status = str(row.get("interior_status") or "").strip()
    coverage = str(row.get("interior_coverage") or "").strip()
    if status not in {"ready", "ready_with_download_gaps"}:
        return None
    if coverage not in {"partial", "sufficient"}:
        return None
    score = _number(row.get("interior_score"))
    if score is None or not 1.0 <= score <= 10.0:
        return None
    confidence = 1.0 if coverage == "sufficient" else 0.65
    if status == "ready_with_download_gaps":
        confidence *= 0.85
    return score, confidence


def _weighted_median(values: list[tuple[float, float]]) -> float | None:
    values = sorted((value, weight) for value, weight in values if weight > 0)
    if not values:
        return None
    threshold = sum(weight for _, weight in values) / 2.0
    cumulative = 0.0
    for value, weight in values:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return values[-1][0]


def _weighted_quantile(values: list[tuple[float, float]], quantile: float) -> float | None:
    """Квантиль с учётом уже рассчитанного приоритета когорты."""
    values = sorted((value, weight) for value, weight in values if weight > 0)
    if not values:
        return None
    threshold = max(0.0, min(1.0, quantile)) * sum(weight for _, weight in values)
    cumulative = 0.0
    for value, weight in values:
        cumulative += weight
        if cumulative + 1e-12 >= threshold:
            return value
    return values[-1][0]


def _smoothstep(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def _repair_log_premium(score: float) -> float:
    """Плавная нелинейная рыночная премия за состояние ремонта."""
    premium = 0.0
    for start, end, increment in REPAIR_PREMIUM_TRANSITIONS:
        premium += increment * _smoothstep((score - start) / (end - start))
    return premium


def _geometric_blend(left: float, right: float, right_share: float) -> float:
    """Смешать цены мультипликативно, как и остальные log-price эффекты."""
    return math.exp(
        (1.0 - right_share) * math.log(left)
        + right_share * math.log(right)
    )


def _individual_interior_estimate(
    target_score: float,
    cohort_infos: list[dict[str, Any]],
) -> dict[str, Any]:
    """Привести КАЖДЫЙ аналог к ремонту цели и устойчиво агрегировать.

    Вес когорты (а значит приоритет того же дома перед радиусом) остаётся
    неизменным. Сходство ремонта перераспределяет вес только ВНУТРИ одной
    когорты и потому не позволяет далёкому дому перебить точный L1/2.
    """
    pairs: list[tuple[float, float]] = []
    ids: set[str] = set()
    target_premium = _repair_log_premium(target_score)

    for cohort in cohort_infos:
        cohort_weight = max(0.0, float(cohort.get("weight") or 0.0))
        if cohort_weight <= 0:
            continue
        candidates: list[tuple[float, float, str]] = []
        for row in cohort.get("rows") or ():
            usable = usable_interior_score(row)
            price_m2 = _number(row.get("_price_m2"))
            if usable is None or price_m2 is None or price_m2 <= 0:
                continue
            score, score_confidence = usable
            comparable_premium = _repair_log_premium(score)
            log_adjustment = max(
                -MAX_ABS_LOG_ADJUSTMENT,
                min(MAX_ABS_LOG_ADJUSTMENT, target_premium - comparable_premium),
            )
            adjusted = price_m2 * math.exp(log_adjustment)
            similarity = PAIRWISE_SIMILARITY_FLOOR + (
                1.0 - PAIRWISE_SIMILARITY_FLOOR
            ) * math.exp(
                -0.5 * ((target_score - score) / PAIRWISE_SCORE_SIMILARITY_SIGMA) ** 2
            )
            local_weight = score_confidence * similarity
            row_id = str(row.get("id") or id(row))
            candidates.append((adjusted, local_weight, row_id))

        local_total = sum(weight for _, weight, _ in candidates)
        if local_total <= 0:
            continue
        # Нормализация внутри уровня принципиальна: сумма снова равна весу
        # когорты, каким её рассчитал Stage 3 до знания о ремонте.
        for adjusted, local_weight, row_id in candidates:
            pairs.append((adjusted, cohort_weight * local_weight / local_total))
            ids.add(row_id)

    if len(ids) < MIN_REFERENCE_SCORES or not pairs:
        return {"price_m2": None, "count": len(ids)}

    # Обрезанное (winsorized) среднее выиграло у обычного среднего и медианы
    # на временном hold-out. При малом n ничего не режем.
    if len(pairs) >= PAIRWISE_WINSOR_MIN_N:
        # Квантили обязательно взвешенные. Иначе десять дальних квартир с
        # суммарным весом 0.1 могли бы обрезать единственный аналог того же дома
        # с весом 0.9 и фактически отменить географическую иерархию Stage 3.
        lo = _weighted_quantile(pairs, PAIRWISE_WINSOR_FRACTION)
        hi = _weighted_quantile(pairs, 1.0 - PAIRWISE_WINSOR_FRACTION)
        if lo is not None and hi is not None:
            pairs = [(max(lo, min(hi, value)), weight) for value, weight in pairs]

    total_weight = sum(weight for _, weight in pairs)
    price_m2 = (
        sum(value * weight for value, weight in pairs) / total_weight
        if total_weight > 0 else None
    )
    return {"price_m2": price_m2, "count": len(ids)}


def interior_reference(cohort_infos: list[dict[str, Any]]) -> dict[str, Any]:
    """Оценить типичный ремонт тех же когорт, из которых получена цена."""
    total_cohort_weight = sum(max(0.0, float(c.get("weight") or 0.0)) for c in cohort_infos)
    references: list[tuple[float, float]] = []
    ids: set[str] = set()
    available_weight = 0.0

    for cohort in cohort_infos:
        cohort_weight = max(0.0, float(cohort.get("weight") or 0.0))
        scores: list[tuple[float, float]] = []
        cohort_rows = list(cohort.get("rows") or ())
        for row in cohort_rows:
            usable = usable_interior_score(row)
            if usable is None:
                continue
            scores.append(usable)
            ids.add(str(row.get("id") or id(row)))
        cohort_median = _weighted_median(scores)
        if cohort_median is not None and cohort_weight > 0:
            references.append((cohort_median, cohort_weight))
            # A single scored apartment must not make a large cohort look
            # fully covered. Partial/gapped results contribute their reduced
            # confidence to the coverage fraction as well.
            score_coverage = (
                sum(confidence for _, confidence in scores) / len(cohort_rows)
                if cohort_rows else 0.0
            )
            available_weight += cohort_weight * score_coverage

    score = _weighted_median(references)
    count = len(ids)
    weight_coverage = (
        min(1.0, available_weight / total_cohort_weight)
        if total_cohort_weight > 0 else 0.0
    )
    # Две оценки уже позволяют осторожную поправку; уверенность плавно
    # приближается к единице и не скачет на жёсткой границе размера когорты.
    sample_confidence = count / (count + 3.0) if count else 0.0
    confidence = weight_coverage * sample_confidence
    return {
        "score": score,
        "count": count,
        "weight_coverage": weight_coverage,
        "confidence": confidence,
    }


def adjust_price_for_interior(
    base_price_m2: float | None,
    target: dict[str, Any],
    cohort_infos: list[dict[str, Any]],
    external_premium_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Вернуть исходную и скорректированную цену плюс полный audit trail."""
    reference = interior_reference(cohort_infos)
    target_value = usable_interior_score(target)
    result = {
        "base_price_m2_unadjusted": base_price_m2,
        "base_price_m2_adjusted": base_price_m2,
        "interior_target_score": target_value[0] if target_value else None,
        "interior_reference_score": reference["score"],
        "interior_reference_n": reference["count"],
        "interior_reference_coverage": reference["weight_coverage"],
        "interior_adjustment_confidence": 0.0,
        "interior_adjustment_beta": None,
        "interior_score_delta": None,
        "interior_price_adjustment_pct": 0.0,
        "interior_adjustment_applied": False,
        "interior_adjustment_reason": "",
        "interior_adjustment_method": "not_applied",
        "interior_legacy_price_m2": None,
        "interior_individual_price_m2": None,
        "interior_individual_n": 0,
        "interior_external_premium_pct": None,
        "interior_external_premium_donors": 0,
        "interior_external_weak_n": 0,
        "interior_external_confidence": 0.0,
        "interior_external_price_m2": None,
        "interior_external_transition": "",
    }
    if not base_price_m2 or base_price_m2 <= 0:
        result["interior_adjustment_reason"] = "нет исходной справедливой цены"
        return result
    if target_value is None:
        result["interior_adjustment_reason"] = "нет надёжной оценки ремонта квартиры"
        return result

    def apply_strict_external() -> bool:
        """Применить доказанную междомовую премию без общего 16%-cap.

        Исключение безопасно не величиной cap, а условиями построения evidence:
        полный interior цели, слабый аналог в её доме и минимум два независимых
        сопоставимых здания, в каждом из которых наблюдался тот же переход.
        """
        evidence = external_premium_evidence or {}
        premium_log = _number(evidence.get("premium_log"))
        donors = int(evidence.get("donor_buildings") or 0)
        weak_n = int(evidence.get("weak_base_n") or 0)
        if (
            target_value[1] < 1.0
            or premium_log is None
            or premium_log <= 0
            or donors < 2
            or weak_n < 1
        ):
            return False
        external_price = base_price_m2 * math.exp(premium_log)
        if external_price <= float(result["base_price_m2_adjusted"] or 0):
            return False
        result.update({
            "base_price_m2_adjusted": external_price,
            "interior_adjustment_confidence": max(
                float(result["interior_adjustment_confidence"] or 0.0),
                float(evidence.get("confidence") or 0.0),
            ),
            "interior_price_adjustment_pct": (external_price / base_price_m2 - 1.0) * 100.0,
            "interior_adjustment_applied": True,
            "interior_adjustment_reason": (
                "перенесена наблюдаемая премия того же перехода ремонта "
                "из сопоставимых зданий"
            ),
            "interior_adjustment_method": "strict_external_premium",
            "interior_external_premium_pct": evidence.get("premium_pct"),
            "interior_external_premium_donors": donors,
            "interior_external_weak_n": weak_n,
            "interior_external_confidence": float(evidence.get("confidence") or 0.0),
            "interior_external_price_m2": external_price,
            "interior_external_transition": str(evidence.get("transition") or ""),
        })
        return True

    if reference["score"] is None or reference["count"] < MIN_REFERENCE_SCORES:
        result["interior_adjustment_reason"] = "недостаточно оценённых аналогов"
        apply_strict_external()
        return result

    target_score, target_confidence = target_value
    confidence = target_confidence * reference["confidence"]
    delta = target_score - reference["score"]
    beta = (
        INTERIOR_LOG_PRICE_PER_SCORE_BELOW_REFERENCE
        if delta < 0
        else INTERIOR_LOG_PRICE_PER_SCORE_ABOVE_REFERENCE
    )
    log_adjustment = beta * delta * confidence
    log_adjustment = max(-MAX_ABS_LOG_ADJUSTMENT, min(MAX_ABS_LOG_ADJUSTMENT, log_adjustment))
    legacy_price_m2 = base_price_m2 * math.exp(log_adjustment)
    adjusted_price_m2 = legacy_price_m2
    method = "aggregate_reference"

    # Индивидуальная модель проверялась только для полных и безошибочно
    # загруженных фотографий цели. При partial/download_gaps оставляем прежнюю
    # осторожную формулу, где неполнота уже уменьшает confidence.
    individual = {"price_m2": None, "count": 0}
    if target_confidence == 1.0:
        individual = _individual_interior_estimate(target_score, cohort_infos)
        individual_price_m2 = individual["price_m2"]
        individual_n = int(individual["count"])
        if individual_price_m2 is not None:
            candidate = _geometric_blend(
                legacy_price_m2,
                individual_price_m2,
                PAIRWISE_CANDIDATE_SHARE,
            )
            if individual_n <= 3:
                adjusted_price_m2 = candidate
                method = "individual_thin_sample"
            elif individual_n >= PAIRWISE_MANY_COMPARABLES:
                adjusted_price_m2 = _geometric_blend(
                    legacy_price_m2,
                    candidate,
                    0.5,
                )
                method = "individual_many_blend"
            else:
                # В средней выборке уже достаточно наблюдений, чтобы не
                # выбрасывать рассчитанную попарную оценку. Раньше для 4–15
                # аналогов здесь безусловно оставалась агрегатная формула:
                # даже несколько квартир того же класса с почти идентичным
                # ремонтом влияли только на audit-поле, но не на итоговую
                # справедливую цену. Используем ту же осторожную смесь, что
                # и для тонкой выборки: 25% агрегатной и 75% индивидуальной
                # оценки. Иерархия географических когорт внутри individual
                # уже сохранена, а общий cap ниже по-прежнему ограничивает
                # максимальный сдвиг.
                adjusted_price_m2 = candidate
                method = "individual_mid_sample"

    # Общий предохранитель остаётся прежним: знание о ремонте никогда не может
    # сдвинуть исходный benchmark более чем на exp(±0.15), даже если среднее
    # индивидуально приведённых аналогов само по себе оказалось далеко от него.
    final_log_adjustment = math.log(adjusted_price_m2 / base_price_m2)
    final_log_adjustment = max(
        -MAX_ABS_LOG_ADJUSTMENT,
        min(MAX_ABS_LOG_ADJUSTMENT, final_log_adjustment),
    )
    adjusted_price_m2 = base_price_m2 * math.exp(final_log_adjustment)
    factor = adjusted_price_m2 / base_price_m2

    result.update({
        "base_price_m2_adjusted": adjusted_price_m2,
        "interior_adjustment_confidence": confidence,
        "interior_adjustment_beta": beta,
        "interior_score_delta": delta,
        "interior_price_adjustment_pct": (factor - 1.0) * 100.0,
        "interior_adjustment_applied": True,
        "interior_adjustment_reason": "цена приведена к ремонту относительно тех же аналогов",
        "interior_adjustment_method": method,
        "interior_legacy_price_m2": legacy_price_m2,
        "interior_individual_price_m2": individual["price_m2"],
        "interior_individual_n": individual["count"],
    })
    apply_strict_external()
    return result
