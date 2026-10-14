"""Офлайн-eval динамического веса аналогов с похожим ремонтом.

Скрипт ничего не меняет в production, не вызывает OpenAI и читает готовые
``interior_score`` из Cleaner baseline. По умолчанию используется временной
hold-out: старая часть рынка становится эталоном, новая — целями.

Пример быстрого запуска::

    python eval_repair_similarity.py --max-targets 1000 --inspect-id 1015853709

Полный прогон без ограничения::

    python eval_repair_similarity.py --max-targets 0 --bootstrap 1000

Важно: цена из baseline — asking price, а не обязательно цена договора.
Известную цену договора можно передать отдельно::

    python eval_repair_similarity.py --known-deal 1015853709=550000
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import os
import random
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
DEFAULT_BASELINE = ROOT / "baseline" / "krisha_almaty_baseline.csv"
DEFAULT_OUTPUT = ROOT / "cache" / "repair_similarity_eval"

# Около перехода 6.0 оценщик особенно неопределён: 5.8 и 6.2 не считаем
# взаимозаменяемыми, хотя численная разница всего 0.4. В устойчивом хорошем
# диапазоне 6.3..6.9 разрешаем более широкое окно.
REPAIR_BANDS = (
    ("weak", 1.0, 5.49, 0.50),
    ("transition_low", 5.5, 5.99, 0.25),
    ("transition_high", 6.0, 6.29, 0.25),
    ("good", 6.3, 6.99, 0.70),
    ("premium", 7.0, 10.0, 0.80),
)

# География остаётся prior, но уже не абсолютным запретом. L3 — до 500 м и
# тот же класс здания в Stage 3; более широким уровням доверяем меньше.
LEVEL_FACTOR = {"1-2": 1.20, "3": 1.00, "4": 0.60, "5": 0.30, "6": 0.12}


def load_stage3(path: Path):
    spec = importlib.util.spec_from_file_location("stage3_eval_similarity", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось загрузить Stage 3: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_number(value: Any) -> float | None:
    try:
        number = float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_date(row: dict[str, Any]) -> datetime | None:
    for field in ("scraped_at", "last_seen_at", "added_at", "created_at", "published_date"):
        raw = str(row.get(field) or "").strip()
        if not raw:
            continue
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def repair_band(score: float) -> tuple[str, float] | None:
    for name, low, high, tolerance in REPAIR_BANDS:
        if low <= score <= high:
            return name, tolerance
    return None


def repair_similarity(target_score: float, candidate_score: float) -> float:
    """0 = не сравнимы; (0, 1] = плавный вес внутри одной устойчивой зоны."""
    target_band = repair_band(target_score)
    candidate_band = repair_band(candidate_score)
    if target_band is None or candidate_band is None or target_band[0] != candidate_band[0]:
        return 0.0
    tolerance = target_band[1]
    delta = abs(target_score - candidate_score)
    if delta > tolerance:
        return 0.0
    # На самой границе вес остаётся 0.50, а не обрывается почти в ноль.
    sigma = tolerance / math.sqrt(2.0 * math.log(2.0))
    return math.exp(-0.5 * (delta / sigma) ** 2)


def quantile(values: list[float], probability: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    position = (len(ordered) - 1) * min(1.0, max(0.0, probability))
    left = int(math.floor(position))
    right = int(math.ceil(position))
    if left == right:
        return ordered[left]
    share = position - left
    return ordered[left] * (1.0 - share) + ordered[right] * share


def weighted_quantile(values: list[tuple[float, float]], probability: float) -> float | None:
    values = sorted((v, w) for v, w in values if w > 0 and math.isfinite(v))
    if not values:
        return None
    threshold = sum(w for _, w in values) * min(1.0, max(0.0, probability))
    cumulative = 0.0
    for value, weight in values:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return values[-1][0]


def geometric_blend(left: float, right: float, right_share: float) -> float:
    right_share = min(1.0, max(0.0, right_share))
    return math.exp((1.0 - right_share) * math.log(left) + right_share * math.log(right))


def adaptive_influence_cap(
    same_building_n: int,
    outside_n: int,
    min_same_building: int = 2,
    one_two_cap: float = 0.25,
    three_plus_cap: float = 0.75,
) -> float:
    """Максимальная доля matched-сигнала до умножения на confidence."""
    if same_building_n >= min_same_building or outside_n <= 0:
        return 0.0
    return one_two_cap if outside_n <= 2 else three_plus_cap


def usable_score(row: dict[str, Any], evaluator_version: str) -> tuple[float, float] | None:
    if str(row.get("interior_evaluator_version") or "").strip() != evaluator_version:
        return None
    status = str(row.get("interior_status") or "").strip()
    coverage = str(row.get("interior_coverage") or "").strip()
    if status not in {"ready", "ready_with_download_gaps"}:
        return None
    if coverage not in {"partial", "sufficient"}:
        return None
    score = parse_number(row.get("interior_score"))
    if score is None or not 1 <= score <= 10:
        return None
    confidence = 1.0 if coverage == "sufficient" else 0.65
    if status == "ready_with_download_gaps":
        confidence *= 0.85
    return score, confidence


def complex_value(row: dict[str, Any]) -> str:
    return str(row.get("complex_id") or row.get("complex_alias") or row.get("complex_name") or "").strip()


def relation_factor(target: dict[str, Any], candidate: dict[str, Any], level: str) -> tuple[str, float]:
    if level == "1-2":
        return "same_building", 1.0
    target_complex = complex_value(target)
    candidate_complex = complex_value(candidate)
    if target_complex and candidate_complex:
        # L3+ уже фильтруется Stage 3 по классу здания. Разные ЖК одного
        # класса допустимы, но имеют меньшую уверенность.
        return "different_complex_same_class", 0.60
    if bool(target_complex) != bool(candidate_complex):
        return "mixed_complex_and_non_complex", 0.35
    target_floors = parse_number(target.get("floor_total"))
    candidate_floors = parse_number(candidate.get("floor_total"))
    if target_floors is None or candidate_floors is None:
        return "non_complex_unknown_floors", 0.55
    gap = abs(target_floors - candidate_floors)
    if gap <= 2:
        return "non_complex_similar_floors", 1.0
    if gap <= 4:
        return "non_complex_floor_gap_3_4", 0.55
    return "non_complex_different_floors", 0.25


def duplicate_key(row: dict[str, Any]) -> tuple[Any, ...]:
    square = parse_number(row.get("square_m2"))
    price = parse_number(row.get("price"))
    return (
        str(row.get("street") or "").strip().casefold(),
        str(row.get("house_num") or "").strip().casefold(),
        row.get("rooms"),
        round(square, 1) if square is not None else None,
        round(price, -3) if price is not None else None,
    )


def build_context(module, pool: list[dict[str, Any]]) -> dict[str, Any]:
    citywide = module.citywide_median_by_rooms(pool)
    q25, q75 = module.price_segment_boundaries(pool)
    room_bounds = module.room_segment_boundaries(pool)
    score_index = module.building_scores_index(pool, q25, q75, room_bounds)
    class_priors = module.prior_medians_by_rooms_class(pool, score_index)
    area_slopes = module.area_slopes_by_rooms(pool)
    return {
        "pool": pool,
        "citywide": citywide,
        "q25": q25,
        "q75": q75,
        "room_bounds": room_bounds,
        "score_index": score_index,
        "class_priors": class_priors,
        "area_slopes": area_slopes,
        "ref_areas": module.median_area_by_rooms(pool),
        "spatial_index": module.SpatialIndex(pool),
        "building_index": module.BuildingIndex(pool),
    }


def score_target(module, target: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return module.score_row(
        target,
        context["pool"],
        context["citywide"],
        context["score_index"],
        context["q25"],
        context["q75"],
        soft_target=True,
        room_bounds=context["room_bounds"],
        class_priors=context["class_priors"],
        area_slopes=context["area_slopes"],
        spatial_index=context["spatial_index"],
        building_index=context["building_index"],
        ref_areas=context["ref_areas"],
    )


def matched_repair_estimate(
    target: dict[str, Any],
    result: dict[str, Any],
    pool_by_id: dict[str, dict[str, Any]],
    evaluator_version: str,
) -> dict[str, Any]:
    target_value = usable_score(target, evaluator_version)
    if target_value is None:
        return {"estimate": None, "reason": "target_without_reliable_score", "audit": []}
    target_score, target_confidence = target_value
    target_band = repair_band(target_score)
    candidates: list[dict[str, Any]] = []
    members = result.get("_cohort_members") or []
    duplicate_counts = Counter(
        duplicate_key(pool_by_id[str(m.get("id"))])
        for m in members
        if str(m.get("id")) in pool_by_id
    )

    for member in members:
        candidate = pool_by_id.get(str(member.get("id")))
        if candidate is None:
            continue
        candidate_value = usable_score(candidate, evaluator_version)
        if candidate_value is None:
            continue
        candidate_score, candidate_confidence = candidate_value
        similarity = repair_similarity(target_score, candidate_score)
        if similarity <= 0:
            continue
        level = str(member.get("level") or "6")
        relation, relation_weight = relation_factor(target, candidate, level)
        normalized_price_m2 = parse_number(member.get("price_m2"))
        if normalized_price_m2 is None or normalized_price_m2 <= 0:
            continue
        # Для уже схожего ремонта оставляем только маленькую непрерывную
        # поправку. Большой скачок между классами здесь не нужен: такие пары
        # отфильтрованы repair_similarity().
        repair_adjustment = max(-0.03, min(0.03, 0.02 * (target_score - candidate_score)))
        adjusted_price_m2 = normalized_price_m2 * math.exp(repair_adjustment)
        duplicate_divisor = duplicate_counts[duplicate_key(candidate)] or 1
        weight = (
            LEVEL_FACTOR.get(level, 0.10)
            * relation_weight
            * similarity
            * candidate_confidence
            / duplicate_divisor
        )
        candidates.append({
            "id": str(member.get("id")),
            "level": level,
            "relation": relation,
            "score": candidate_score,
            "score_delta": round(candidate_score - target_score, 3),
            "normalized_price_m2": normalized_price_m2,
            "adjusted_price_m2": adjusted_price_m2,
            "similarity": similarity,
            "weight_before_robust": weight,
            "duplicate_divisor": duplicate_divisor,
        })

    if not candidates:
        return {
            "estimate": None,
            "reason": "no_similar_repairs",
            "target_score": target_score,
            "band": target_band[0] if target_band else "unknown",
            "n": 0,
            "effective_n": 0.0,
            "same_building_n": 0,
            "outside_n": 0,
            "evidence_confidence": 0.0,
            "audit": [],
        }

    weighted_values = [(c["adjusted_price_m2"], c["weight_before_robust"]) for c in candidates]
    # Подозрительная цена не удаляется: winsorization лишь ограничивает её
    # вклад диапазоном p10..p90. При n<5 данных для такого решения мало.
    lo = weighted_quantile(weighted_values, 0.10) if len(candidates) >= 5 else None
    hi = weighted_quantile(weighted_values, 0.90) if len(candidates) >= 5 else None
    for candidate in candidates:
        value = candidate["adjusted_price_m2"]
        robust_value = max(lo, min(hi, value)) if lo is not None and hi is not None else value
        candidate["robust_price_m2"] = robust_value

    total_weight = sum(c["weight_before_robust"] for c in candidates)
    estimate = sum(
        c["robust_price_m2"] * c["weight_before_robust"] for c in candidates
    ) / total_weight
    for candidate in candidates:
        candidate["final_weight"] = candidate["weight_before_robust"] / total_weight
        candidate["weighted_contribution_m2"] = (
            candidate["robust_price_m2"] * candidate["final_weight"]
        )

    weights = [c["weight_before_robust"] for c in candidates]
    effective_n = total_weight**2 / sum(w * w for w in weights)
    same_building_n = sum(c["level"] == "1-2" for c in candidates)
    outside_n = len(candidates) - same_building_n
    evidence_confidence = target_confidence * min(1.0, effective_n / (effective_n + 1.0))
    p20 = weighted_quantile([(c["robust_price_m2"], c["final_weight"]) for c in candidates], 0.20)
    p80 = weighted_quantile([(c["robust_price_m2"], c["final_weight"]) for c in candidates], 0.80)
    observed_spread = max(
        abs((p20 or estimate) - estimate),
        abs((p80 or estimate) - estimate),
    ) / estimate
    # Квантили одного-двух аналогов создают ложную точность. Нижняя граница
    # неопределённости уменьшается только по мере роста effective_n.
    if effective_n < 1.5:
        sample_uncertainty = 0.30
        uncertainty_label = "very_high"
    elif effective_n < 2.5:
        sample_uncertainty = 0.22
        uncertainty_label = "high"
    elif effective_n < 4.0:
        sample_uncertainty = 0.16
        uncertainty_label = "medium"
    elif effective_n < 6.0:
        sample_uncertainty = 0.12
        uncertainty_label = "moderate"
    else:
        sample_uncertainty = 0.08
        uncertainty_label = "lower"
    uncertainty = max(observed_spread, sample_uncertainty)
    relation_weights = defaultdict(float)
    for candidate in candidates:
        relation_weights[candidate["relation"]] += candidate["final_weight"]

    return {
        "estimate": estimate,
        "reason": "ok",
        "target_score": target_score,
        "band": target_band[0] if target_band else "unknown",
        "n": len(candidates),
        "effective_n": effective_n,
        "same_building_n": same_building_n,
        "outside_n": outside_n,
        "evidence_confidence": evidence_confidence,
        "p20": p20,
        "p80": p80,
        "uncertainty_relative": uncertainty,
        "uncertainty_label": uncertainty_label,
        "uncertainty_low": estimate * (1.0 - uncertainty),
        "uncertainty_high": estimate * (1.0 + uncertainty),
        "relation_weights": dict(relation_weights),
        "audit": sorted(candidates, key=lambda c: c["final_weight"], reverse=True),
    }


def bucket_for(result: dict[str, Any], matched: dict[str, Any], target: dict[str, Any]) -> dict[str, str]:
    same_n = int(matched.get("same_building_n") or 0)
    outside_n = int(matched.get("outside_n") or 0)
    if same_n >= 2:
        match_supply = "same_building_2plus"
    elif outside_n >= 3:
        match_supply = "shortage_outside_3plus"
    elif outside_n:
        match_supply = "shortage_outside_1_2"
    elif same_n == 1:
        match_supply = "same_building_1_only"
    else:
        match_supply = "no_similar_match"
    cohort_size = int(result.get("cohort_size") or 0)
    confidence = parse_number(result.get("benchmark_confidence")) or 0.0
    return {
        "match_supply": match_supply,
        "repair_band": str(matched.get("band") or "unknown"),
        "complex": "complex" if complex_value(target) else "non_complex",
        "cohort_size": "0-3" if cohort_size <= 3 else "4-15" if cohort_size <= 15 else "16+",
        "benchmark_confidence": "low" if confidence < 0.49 else "medium" if confidence < 0.60 else "high",
    }


def bootstrap_mean_ci(values: list[float], samples: int, seed: int) -> tuple[float, float]:
    if not values or samples <= 0:
        return float("nan"), float("nan")
    rnd = random.Random(seed)
    means = [statistics.mean(rnd.choices(values, k=len(values))) for _ in range(samples)]
    return quantile(means, 0.025), quantile(means, 0.975)


def bootstrap_paired_delta_ci(
    current_errors: list[float],
    candidate_errors: list[float],
    samples: int,
    seed: int,
) -> tuple[float, float]:
    """CI для mean(current error - candidate error); плюс = улучшение."""
    deltas = [current - candidate for current, candidate in zip(current_errors, candidate_errors)]
    if not deltas or samples <= 0:
        return float("nan"), float("nan")
    rnd = random.Random(seed)
    means = [statistics.mean(rnd.choices(deltas, k=len(deltas))) for _ in range(samples)]
    return quantile(means, 0.025), quantile(means, 0.975)


def metric_row(
    name: str,
    records: list[dict[str, Any]],
    bootstrap: int,
    seed: int,
    group: str = "all",
) -> dict[str, Any]:
    pairs = [
        (r["actual_price_m2"], r.get(name), r.get("current_price_m2"))
        for r in records
        if r.get(name) is not None and r["actual_price_m2"] > 0
    ]
    errors = [abs(pred - actual) / actual for actual, pred, _ in pairs]
    signed = [(pred - actual) / actual for actual, pred, _ in pairs]
    current_errors = [abs(current - actual) / actual for actual, _, current in pairs]
    lo, hi = bootstrap_mean_ci(errors, bootstrap, seed)
    improvement = [current - candidate for current, candidate in zip(current_errors, errors)]
    improvement_lo, improvement_hi = bootstrap_paired_delta_ci(
        current_errors, errors, bootstrap, seed + 100_000,
    )
    return {
        "group": group,
        "variant": name,
        "n": len(pairs),
        "mean_error": statistics.mean(errors) if errors else None,
        "mean_error_ci_low": lo if errors else None,
        "mean_error_ci_high": hi if errors else None,
        "median_error": statistics.median(errors) if errors else None,
        "p90_error": quantile(errors, 0.90) if errors else None,
        "mean_signed_bias": statistics.mean(signed) if signed else None,
        "within_10pct": sum(e <= 0.10 for e in errors) / len(errors) if errors else None,
        "error_over_30pct": sum(e > 0.30 for e in errors) / len(errors) if errors else None,
        "mean_improvement_vs_current": statistics.mean(improvement) if improvement else None,
        "mean_improvement_ci_low": improvement_lo if improvement else None,
        "mean_improvement_ci_high": improvement_hi if improvement else None,
        "improved_vs_current": (
            sum(e < c for e, c in zip(errors, current_errors)) / len(errors) if errors else None
        ),
        "worsened_vs_current": (
            sum(e > c for e, c in zip(errors, current_errors)) / len(errors) if errors else None
        ),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_known_deals(values: Iterable[str]) -> dict[str, float]:
    result = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"--known-deal ожидает ID=PRICE, получено: {value}")
        listing_id, price = value.split("=", 1)
        parsed = parse_number(price)
        if not listing_id.strip() or parsed is None or parsed <= 0:
            raise ValueError(f"Некорректный --known-deal: {value}")
        result[listing_id.strip()] = parsed
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    parser.add_argument("--stage3", default=str(ROOT / "stage3_benchmark_v3.py"))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--mode", choices=("temporal", "loo"), default="temporal")
    parser.add_argument("--holdout-fraction", type=float, default=0.20)
    parser.add_argument("--max-targets", type=int, default=2500, help="0 = все цели")
    parser.add_argument("--seed", type=int, default=20261012)
    parser.add_argument("--bootstrap", type=int, default=300)
    parser.add_argument(
        "--influence-grid", default="0.25,0.50,0.75,1.00",
        help="Максимальная доля repair-matched оценки в challenger",
    )
    parser.add_argument("--min-same-building", type=int, default=2)
    parser.add_argument(
        "--adaptive-one-two-cap", type=float, default=0.25,
        help="Максимальная доля matched-оценки при 1–2 внешних аналогах",
    )
    parser.add_argument(
        "--adaptive-three-plus-cap", type=float, default=0.75,
        help="Максимальная доля matched-оценки при 3+ внешних аналогах",
    )
    parser.add_argument("--inspect-id", action="append", default=[])
    parser.add_argument("--known-deal", action="append", default=[])
    args = parser.parse_args()

    if not 0.05 <= args.holdout_fraction <= 0.50:
        parser.error("--holdout-fraction должен быть в диапазоне 0.05..0.50")
    influence_grid = sorted({float(v) for v in args.influence_grid.split(",")})
    if not influence_grid or any(v < 0 or v > 1 for v in influence_grid):
        parser.error("--influence-grid должен содержать числа 0..1")
    if not 0 <= args.adaptive_one_two_cap <= 1:
        parser.error("--adaptive-one-two-cap должен быть в диапазоне 0..1")
    if not 0 <= args.adaptive_three_plus_cap <= 1:
        parser.error("--adaptive-three-plus-cap должен быть в диапазоне 0..1")
    known_deals = parse_known_deals(args.known_deal)

    module = load_stage3(Path(args.stage3))
    _, usable = module.prepare_baseline(args.baseline)
    evaluator_version = __import__("interior_price_adjustment").EVALUATOR_VERSION
    dated = [(parse_date(row), row) for row in usable]
    dated = [(date, row) for date, row in dated if date is not None]
    if len(dated) < 100:
        raise RuntimeError("Слишком мало строк с датой для eval")
    dated.sort(key=lambda item: item[0])

    if args.mode == "temporal":
        split = int(len(dated) * (1.0 - args.holdout_fraction))
        cutoff = dated[split][0]
        pool = [row for date, row in dated if date < cutoff]
        targets = [row for date, row in dated if date >= cutoff]
    else:
        cutoff = None
        pool = list(usable)
        targets = list(usable)

    targets = [row for row in targets if usable_score(row, evaluator_version) is not None]
    rnd = random.Random(args.seed)
    if args.max_targets > 0 and len(targets) > args.max_targets:
        targets = rnd.sample(targets, args.max_targets)

    all_by_id = {str(row.get("id")): row for row in usable}
    evaluation_target_ids = {str(row.get("id")) for row in targets}
    target_ids = set(evaluation_target_ids)
    for listing_id in list(dict.fromkeys(args.inspect_id + list(known_deals))):
        row = all_by_id.get(str(listing_id))
        if row is not None and str(listing_id) not in target_ids:
            targets.append(row)
            target_ids.add(str(listing_id))

    print(
        f"mode={args.mode}; baseline usable={len(usable)}; pool={len(pool)}; "
        f"targets={len(targets)}; cutoff={cutoff.isoformat() if cutoff else 'none'}"
    )
    context = build_context(module, pool)
    pool_by_id = {str(row.get("id")): row for row in pool}
    inspection_ids = set(args.inspect_id) | set(known_deals)
    # Inspection answers a different question from temporal eval: "что модель
    # видит во всей скачанной базе сейчас?". Поэтому он получает полный pool,
    # но не попадает в aggregate-метрики временного hold-out.
    inspection_context = build_context(module, usable) if inspection_ids else None
    inspection_pool_by_id = {str(row.get("id")): row for row in usable}
    records: list[dict[str, Any]] = []
    audits: dict[str, Any] = {}

    for index, target in enumerate(targets, 1):
        target_id = str(target.get("id"))
        inspection_only = target_id in inspection_ids and target_id not in evaluation_target_ids
        active_context = inspection_context if inspection_only else context
        active_pool_by_id = inspection_pool_by_id if inspection_only else pool_by_id
        result = score_target(module, target, active_context)
        if result.get("status") != "scored":
            continue
        square = parse_number(target.get("square_m2"))
        asking = parse_number(target.get("price"))
        if square is None or square <= 0 or asking is None or asking <= 0:
            continue
        actual_total = known_deals.get(target_id, asking)
        actual_m2 = actual_total / square
        current_m2 = parse_number(result.get("base_price_m2"))
        if current_m2 is None or current_m2 <= 0:
            continue
        matched = matched_repair_estimate(target, result, active_pool_by_id, evaluator_version)
        buckets = bucket_for(result, matched, target)
        record = {
            "id": target_id,
            "evaluation_scope": "inspection" if inspection_only else "holdout",
            "date": parse_date(target).isoformat() if parse_date(target) else "",
            "rooms": target.get("rooms"),
            "square_m2": square,
            "asking_price": asking,
            "actual_price": actual_total,
            "actual_source": "known_deal" if target_id in known_deals else "asking_proxy",
            "actual_price_m2": actual_m2,
            "current_price_m2": current_m2,
            "current_total": current_m2 * square,
            "benchmark_confidence": result.get("benchmark_confidence"),
            "cohort_level": result.get("cohort_level"),
            "cohort_size": result.get("cohort_size"),
            "target_interior_score": matched.get("target_score"),
            "matched_price_m2": matched.get("estimate"),
            "matched_total": matched.get("estimate") * square if matched.get("estimate") else None,
            "matched_n": matched.get("n", 0),
            "matched_effective_n": matched.get("effective_n"),
            "same_building_matched_n": matched.get("same_building_n", 0),
            "outside_matched_n": matched.get("outside_n", 0),
            "matched_evidence_confidence": matched.get("evidence_confidence"),
            "matched_p20_total": matched.get("p20") * square if matched.get("p20") else None,
            "matched_p80_total": matched.get("p80") * square if matched.get("p80") else None,
            "matched_uncertainty": matched.get("uncertainty_relative"),
            "matched_uncertainty_label": matched.get("uncertainty_label"),
            "matched_uncertainty_low_total": (
                matched.get("uncertainty_low") * square if matched.get("uncertainty_low") else None
            ),
            "matched_uncertainty_high_total": (
                matched.get("uncertainty_high") * square if matched.get("uncertainty_high") else None
            ),
            **buckets,
        }
        estimate = matched.get("estimate")
        for influence in influence_grid:
            name = f"match_{int(round(influence * 100)):03d}_price_m2"
            if estimate is None:
                record[name] = current_m2
                record[name.replace("_price_m2", "_effective_share")] = 0.0
                continue
            # Если в самом доме уже достаточно похожих ремонтов, внешним
            # аналогам незачем получать максимальный вес. При дефиците доля
            # растёт до заданного cap, но всё равно умножается на измеренную
            # надёжность количества/качества совпадений.
            supply_factor = 0.50 if matched.get("same_building_n", 0) >= args.min_same_building else 1.0
            effective_share = influence * supply_factor * matched["evidence_confidence"]
            record[name] = geometric_blend(current_m2, estimate, effective_share)
            record[name.replace("_price_m2", "_effective_share")] = effective_share
        # Основная исследуемая политика. Максимальный вес получают внешние
        # аналоги только при доказанном дефиците похожего ремонта в доме.
        # Один-два внешних наблюдения слишком нестабильны для большого веса;
        # три и более уже дают заметный сигнал. В обоих случаях cap дополнительно
        # умножается на confidence из effective_n и качества фотооценки.
        same_n = int(matched.get("same_building_n") or 0)
        outside_n = int(matched.get("outside_n") or 0)
        adaptive_cap = adaptive_influence_cap(
            same_n,
            outside_n,
            min_same_building=args.min_same_building,
            one_two_cap=args.adaptive_one_two_cap,
            three_plus_cap=args.adaptive_three_plus_cap,
        ) if estimate is not None else 0.0
        adaptive_share = adaptive_cap * float(matched.get("evidence_confidence") or 0.0)
        record["adaptive_price_m2"] = (
            geometric_blend(current_m2, estimate, adaptive_share)
            if estimate is not None else current_m2
        )
        record["adaptive_effective_share"] = adaptive_share
        record["adaptive_cap"] = adaptive_cap
        records.append(record)
        if target_id in inspection_ids:
            audits[target_id] = {
                "target": {k: target.get(k) for k in (
                    "id", "price", "rooms", "square_m2", "floor_total", "street",
                    "house_num", "complex_id", "complex_name", "interior_score",
                )},
                "stage3": {k: result.get(k) for k in (
                    "base_price_m2_unadjusted", "base_price_m2", "benchmark_confidence",
                    "cohort_level", "cohort_size", "interior_adjustment_method",
                )},
                "matched": matched,
                "predictions": {
                    k: v for k, v in record.items()
                    if k.startswith("match_") or k.startswith("adaptive_")
                },
            }
        if index % 250 == 0:
            print(f"  scored {index}/{len(targets)}", flush=True)

    variant_names = ["current_price_m2", "adaptive_price_m2"] + [
        f"match_{int(round(value * 100)):03d}_price_m2" for value in influence_grid
    ]
    metric_records = [r for r in records if r["evaluation_scope"] == "holdout"]
    summaries = [
        metric_row(name, metric_records, args.bootstrap, args.seed + i, "all")
        for i, name in enumerate(variant_names)
    ]
    bucket_rows: list[dict[str, Any]] = []
    for dimension in ("match_supply", "repair_band", "complex", "cohort_size", "benchmark_confidence"):
        groups = defaultdict(list)
        for record in metric_records:
            groups[str(record.get(dimension))].append(record)
        for group_name, group_records in sorted(groups.items()):
            for i, name in enumerate(variant_names):
                bucket_rows.append(metric_row(
                    name,
                    group_records,
                    min(args.bootstrap, 150),
                    args.seed + i,
                    f"{dimension}={group_name}",
                ))

    chronological = sorted(metric_records, key=lambda row: row.get("date") or "")
    midpoint = len(chronological) // 2
    temporal_rows: list[dict[str, Any]] = []
    for split_name, split_records in (
        ("calibration_half", chronological[:midpoint]),
        ("validation_half", chronological[midpoint:]),
    ):
        for i, name in enumerate(variant_names):
            temporal_rows.append(metric_row(
                name,
                split_records,
                args.bootstrap,
                args.seed + i,
                split_name,
            ))

    output = Path(args.output_dir)
    write_csv(output / "predictions.csv", records)
    write_csv(output / "summary.csv", summaries)
    write_csv(output / "bucket_metrics.csv", bucket_rows)
    write_csv(output / "temporal_split_metrics.csv", temporal_rows)
    with (output / "inspection.json").open("w", encoding="utf-8") as handle:
        json.dump(audits, handle, ensure_ascii=False, indent=2, default=str)
    config = vars(args) | {
        "cutoff": cutoff.isoformat() if cutoff else None,
        "usable_rows": len(usable),
        "pool_rows": len(pool),
        "scored_targets": len(records),
        "evaluator_version": evaluator_version,
        "repair_bands": REPAIR_BANDS,
    }
    with (output / "config.json").open("w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2, default=str)

    print("\nИТОГ (ошибка относительно known deal, иначе asking proxy):")
    for row in sorted(summaries, key=lambda item: item["mean_error"] or float("inf")):
        print(
            f"  {row['variant']:24s} n={row['n']:5d} "
            f"mean={row['mean_error']:.3%} median={row['median_error']:.3%} "
            f"p90={row['p90_error']:.3%} bias={row['mean_signed_bias']:+.3%} "
            f">30%={row['error_over_30pct']:.2%}"
        )
    print(f"\nФайлы сохранены: {output}")
    if audits:
        for listing_id in audits:
            prediction = next((r for r in records if r["id"] == listing_id), None)
            if prediction:
                print(f"\nINSPECT id={listing_id}: actual={prediction['actual_price']:.0f}")
                matched_text = (
                    f"{prediction['matched_total']:.0f}"
                    if prediction.get("matched_total") is not None else "нет"
                )
                effective_n = parse_number(prediction.get("matched_effective_n")) or 0.0
                print(
                    f"  current={prediction['current_total']:.0f}; "
                    f"matched={matched_text} "
                    f"(n={prediction['matched_n']}, effective_n={effective_n:.2f})"
                )
                if prediction.get("matched_uncertainty_low_total") is not None:
                    print(
                        "  диагностический диапазон matched: "
                        f"{prediction['matched_uncertainty_low_total']:.0f}.."
                        f"{prediction['matched_uncertainty_high_total']:.0f} "
                        f"({prediction['matched_uncertainty_label']})"
                    )
                for name in variant_names[1:]:
                    print(f"  {name}: {prediction[name] * prediction['square_m2']:.0f}")


if __name__ == "__main__":
    main()
