"""Исследование нелинейной премии за ремонт без изменения production.

Строит независимые сигналы уровня «дом × комнатность × переход ремонта»,
разделяет собственников и агентов, оценивает устойчивость премий и выполняет
временной hold-out: доноры только из прошлого, цели только из будущего.

Результаты сохраняются в ``cache/repair_premium_v2``.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import stage3_benchmark_v3 as stage3
from eval_repair_premium_transfer import (
    exact_address_key,
    parse_date,
    stage3_unadjusted_base,
    valid_interior,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_BASELINE = ROOT / "baseline" / "krisha_almaty_baseline.csv"
DEFAULT_OUTPUT = ROOT / "cache" / "repair_premium_v2"

MAX_AREA_RATIO = 1.20
MAX_DATE_GAP_DAYS = 180
STRICT_RADIUS_KM = 5.0
STRICT_CLASS_DELTA = 0.25
STRICT_FLOOR_GAP = 2.0
SHRINKAGE_K = 3.0

SCORE_BANDS = (
    ("below4", 1.0, 3.99),
    ("4", 4.0, 4.99),
    ("5", 5.0, 5.99),
    ("6", 6.0, 6.99),
    ("7plus", 7.0, 10.0),
)
BAND_ORDER = {name: pos for pos, (name, _, _) in enumerate(SCORE_BANDS)}
PRIMARY_TRANSITIONS = {
    "4_to_5", "4_to_6", "4_to_7plus",
    "5_to_6", "5_to_7plus", "6_to_7plus",
}


def score_band(score: float) -> str | None:
    for name, low, high in SCORE_BANDS:
        if low <= score <= high:
            return name
    return None


def repair_transition(left_score: float, right_score: float) -> tuple[str, bool] | None:
    """Возвращает переход и признак, что left является его нижней стороной."""
    left_band = score_band(left_score)
    right_band = score_band(right_score)
    if left_band is None or right_band is None or left_band == right_band:
        return None
    if BAND_ORDER[left_band] < BAND_ORDER[right_band]:
        transition = f"{left_band}_to_{right_band}"
        return (transition, True) if transition in PRIMARY_TRANSITIONS else None
    transition = f"{right_band}_to_{left_band}"
    return (transition, False) if transition in PRIMARY_TRANSITIONS else None


def seller_pair_type(low: dict[str, Any], high: dict[str, Any]) -> str:
    low_class = str(low.get("_seller_class") or "unknown")
    high_class = str(high.get("_seller_class") or "unknown")
    if low_class == high_class == "owner":
        return "owner_owner"
    if low_class == high_class == "agent":
        return "agent_agent"
    if "unknown" in {low_class, high_class}:
        return "unknown"
    return "mixed"


def quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * min(1.0, max(0.0, probability))
    left = int(math.floor(position))
    right = int(math.ceil(position))
    if left == right:
        return ordered[left]
    share = position - left
    return ordered[left] * (1.0 - share) + ordered[right] * share


def median_datetime(values: list[datetime]) -> datetime:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def building_class_label(score: float | None) -> str:
    if score is None:
        return "unknown"
    # Не экспортируем русские внутренние label Stage 3 в машинные CSV.
    if score < stage3.CLASS_SCORE_ECONOM_MAX:
        return "econom"
    if score < stage3.CLASS_SCORE_COMFORT_MAX:
        return "comfort"
    return "business"


def bootstrap_median_ci(
    values: list[float], samples: int, seed: int,
) -> tuple[float | None, float | None]:
    if not values or samples <= 0:
        return None, None
    rnd = random.Random(seed)
    boot = [statistics.median(rnd.choices(values, k=len(values))) for _ in range(samples)]
    return quantile(boot, 0.025), quantile(boot, 0.975)


def build_raw_pairs(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[tuple[str, str], str], list[dict[str, Any]]] = defaultdict(list)
    for row in pool:
        address = exact_address_key(row)
        if address is None or not valid_interior(row):
            continue
        required = ("_price_m2", "_lat", "_lon", "_floor_total", "square_m2")
        if any(row.get(field) in (None, "") for field in required):
            continue
        groups[(address, str(row.get("_rooms")))].append(row)

    pairs: list[dict[str, Any]] = []
    for (address, rooms), members in groups.items():
        for pos, left in enumerate(members):
            for right in members[pos + 1:]:
                if str(left.get("id")) == str(right.get("id")):
                    continue
                left_hash = str(left.get("photo_set_hash") or "")
                right_hash = str(right.get("photo_set_hash") or "")
                if left_hash and left_hash == right_hash:
                    continue
                left_area = float(left["square_m2"])
                right_area = float(right["square_m2"])
                if max(left_area, right_area) / min(left_area, right_area) > MAX_AREA_RATIO:
                    continue
                left_date, right_date = parse_date(left), parse_date(right)
                if left_date is None or right_date is None:
                    continue
                if abs((left_date - right_date).days) > MAX_DATE_GAP_DAYS:
                    continue
                left_score = float(left["interior_score"])
                right_score = float(right["interior_score"])
                transition_info = repair_transition(left_score, right_score)
                if transition_info is None:
                    continue
                transition, left_is_low = transition_info
                low, high = (left, right) if left_is_low else (right, left)
                low_pm2 = float(low["_price_m2"])
                high_pm2 = float(high["_price_m2"])
                pairs.append({
                    "address": address,
                    "rooms": rooms,
                    "transition": transition,
                    "low_id": str(low.get("id")),
                    "high_id": str(high.get("id")),
                    "low_score": float(low["interior_score"]),
                    "high_score": float(high["interior_score"]),
                    "log_premium": math.log(high_pm2 / low_pm2),
                    "low_pm2": low_pm2,
                    "floor_total": (
                        float(low["_floor_total"]) + float(high["_floor_total"])
                    ) / 2.0,
                    "lat": (float(low["_lat"]) + float(high["_lat"])) / 2.0,
                    "lon": (float(low["_lon"]) + float(high["_lon"])) / 2.0,
                    "has_complex": bool(low.get("_complex_key") or high.get("_complex_key")),
                    "seller_pair": seller_pair_type(low, high),
                    # Сигнал становится известен лишь после появления обеих строк.
                    "available_at": max(left_date, right_date),
                })
    return pairs


def aggregate_signals(
    pairs: list[dict[str, Any]],
    q25: float,
    q75: float,
    room_bounds: dict[str, tuple[float, float]],
    *,
    split_seller: bool,
) -> list[dict[str, Any]]:
    """Один сигнал на дом/комнатность/переход, при необходимости — seller."""
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for pair in pairs:
        seller_group = pair["seller_pair"] if split_seller else "all"
        grouped[(pair["address"], pair["rooms"], pair["transition"], seller_group)].append(pair)

    signals: list[dict[str, Any]] = []
    for (address, rooms, transition, seller_pair), members in grouped.items():
        low_pm2 = statistics.median(row["low_pm2"] for row in members)
        class_score = stage3._class_score_from_price(
            low_pm2, rooms, room_bounds, q25, q75
        )
        signals.append({
            "address": address,
            "street": address[0],
            "house_num": address[1],
            "rooms": rooms,
            "transition": transition,
            "seller_pair": seller_pair,
            "log_premium": statistics.median(row["log_premium"] for row in members),
            "low_pm2": low_pm2,
            "base_class_score": class_score,
            "building_class": building_class_label(class_score),
            "floor_total": statistics.median(row["floor_total"] for row in members),
            "lat": statistics.median(row["lat"] for row in members),
            "lon": statistics.median(row["lon"] for row in members),
            "has_complex": round(statistics.median(
                int(row["has_complex"]) for row in members
            )) == 1,
            "available_at": max(row["available_at"] for row in members),
            "raw_pair_count": len(members),
        })
    return signals


def independent_values(signals: Iterable[dict[str, Any]]) -> list[float]:
    """Не даём одному дому удвоить вес через разные seller_pair."""
    by_building: dict[tuple[Any, ...], list[float]] = defaultdict(list)
    for row in signals:
        by_building[(row["address"], row["rooms"], row["transition"])].append(
            row["log_premium"]
        )
    return [statistics.median(values) for values in by_building.values()]


def transition_summaries(
    signals: list[dict[str, Any]], bootstrap: int, seed: int,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in signals:
        complex_group = "complex" if row["has_complex"] else "non_complex"
        groups[(row["transition"], row["building_class"], complex_group, "all")].append(row)
        groups[(
            row["transition"], row["building_class"], complex_group, row["seller_pair"]
        )].append(row)

    result = []
    for key, members in sorted(groups.items()):
        values = independent_values(members)
        if not values:
            continue
        lo, hi = bootstrap_median_ci(values, bootstrap, seed + len(result))
        median_log = statistics.median(values)
        result.append({
            "transition": key[0],
            "building_class": key[1],
            "building_type": key[2],
            "seller_pair": key[3],
            "independent_buildings": len(values),
            "median_premium": math.exp(median_log) - 1.0,
            "p10_premium": math.exp(quantile(values, 0.10)) - 1.0,
            "p25_premium": math.exp(quantile(values, 0.25)) - 1.0,
            "p75_premium": math.exp(quantile(values, 0.75)) - 1.0,
            "p90_premium": math.exp(quantile(values, 0.90)) - 1.0,
            "positive_share": sum(value > 0 for value in values) / len(values),
            "bootstrap_median_low": math.exp(lo) - 1.0 if lo is not None else None,
            "bootstrap_median_high": math.exp(hi) - 1.0 if hi is not None else None,
        })
    return result


def donor_candidates(
    target: dict[str, Any],
    donors: list[dict[str, Any]],
    floor_gap: float,
    same_seller: bool = False,
) -> list[dict[str, Any]]:
    result = []
    for donor in donors:
        if donor["address"] == target["address"]:
            continue
        if donor["transition"] != target["transition"]:
            continue
        if donor["rooms"] != target["rooms"]:
            continue
        if donor["has_complex"] != target["has_complex"]:
            continue
        if same_seller:
            if target["seller_pair"] not in {"owner_owner", "agent_agent"}:
                continue
            if donor["seller_pair"] != target["seller_pair"]:
                continue
        if target["base_class_score"] is None or donor["base_class_score"] is None:
            continue
        if abs(donor["base_class_score"] - target["base_class_score"]) > STRICT_CLASS_DELTA:
            continue
        if abs(donor["floor_total"] - target["floor_total"]) > floor_gap:
            continue
        distance = stage3.haversine_km(
            target["lat"], target["lon"], donor["lat"], donor["lon"]
        )
        if distance > STRICT_RADIUS_KM:
            continue
        item = dict(donor)
        item["distance_km"] = distance
        result.append(item)
    return result


def relative_error(actual_log: float, predicted_log: float) -> float:
    actual, predicted = math.exp(actual_log), math.exp(predicted_log)
    return abs(predicted - actual) / actual


def prior_for(
    target: dict[str, Any], donors: list[dict[str, Any]], same_seller: bool,
) -> float | None:
    candidates = [
        row for row in donors
        if row["transition"] == target["transition"]
        and row["building_class"] == target["building_class"]
        and row["has_complex"] == target["has_complex"]
        and (
            not same_seller
            or (
                target["seller_pair"] in {"owner_owner", "agent_agent"}
                and row["seller_pair"] == target["seller_pair"]
            )
        )
    ]
    values = independent_values(candidates)
    return statistics.median(values) if values else None


def temporal_evaluation(
    primary_signals: list[dict[str, Any]],
    seller_signals: list[dict[str, Any]],
    holdout_fraction: float,
) -> tuple[dict[str, Any], list[dict[str, Any]], datetime]:
    dates = sorted(row["available_at"] for row in primary_signals)
    cutoff_index = max(0, min(len(dates) - 1, int(len(dates) * (1.0 - holdout_fraction))))
    cutoff = dates[cutoff_index]
    donors = [row for row in primary_signals if row["available_at"] < cutoff]
    targets = [row for row in primary_signals if row["available_at"] >= cutoff]
    seller_donors = [row for row in seller_signals if row["available_at"] < cutoff]
    seller_targets = [
        row for row in seller_signals
        if row["available_at"] >= cutoff
        and row["seller_pair"] in {"owner_owner", "agent_agent"}
    ]
    prediction_rows: list[dict[str, Any]] = []

    variants = (
        ("strict_floor2", STRICT_FLOOR_GAP, False),
        ("strict_floor4", 4.0, False),
        ("shrinkage", 4.0, True),
    )
    for target in targets:
        base = {
            "street": target["street"],
            "house_num": target["house_num"],
            "rooms": target["rooms"],
            "transition": target["transition"],
            "building_class": target["building_class"],
            "building_type": "complex" if target["has_complex"] else "non_complex",
            "seller_pair": target["seller_pair"],
            "available_at": target["available_at"].isoformat(),
            "actual_premium": math.exp(target["log_premium"]) - 1.0,
        }
        prediction_rows.append({
            **base,
            "variant": "no_repair",
            "donors": 0,
            "predicted_premium": 0.0,
            "relative_error": relative_error(target["log_premium"], 0.0),
        })
        prediction_rows.append({
            **base,
            "variant": "fixed16",
            "donors": 0,
            "predicted_premium": math.exp(0.15) - 1.0,
            "relative_error": relative_error(target["log_premium"], 0.15),
        })
        for name, floor_gap, shrinkage in variants:
            local = donor_candidates(target, donors, floor_gap, False)
            local_values = independent_values(local)
            prior = prior_for(target, donors, False)
            predicted = None
            if shrinkage and prior is not None:
                if local_values:
                    z = len(local_values) / (len(local_values) + SHRINKAGE_K)
                    predicted = z * statistics.median(local_values) + (1.0 - z) * prior
                else:
                    predicted = prior
            elif len(local_values) >= 2:
                predicted = statistics.median(local_values)
            if predicted is None:
                continue
            prediction_rows.append({
                **base,
                "variant": name,
                "donors": len(local_values),
                "predicted_premium": math.exp(predicted) - 1.0,
                "relative_error": relative_error(target["log_premium"], predicted),
            })

    # Отдельный эксперимент: только owner-owner или agent-agent пары. Он не
    # смешивается с основной выборкой, иначе один дом снова получил бы
    # несколько голосов в общих метриках.
    for target in seller_targets:
        base = {
            "street": target["street"],
            "house_num": target["house_num"],
            "rooms": target["rooms"],
            "transition": target["transition"],
            "building_class": target["building_class"],
            "building_type": "complex" if target["has_complex"] else "non_complex",
            "seller_pair": target["seller_pair"],
            "available_at": target["available_at"].isoformat(),
            "actual_premium": math.exp(target["log_premium"]) - 1.0,
        }
        prediction_rows.append({
            **base,
            "variant": "no_repair_same_seller",
            "donors": 0,
            "predicted_premium": 0.0,
            "relative_error": relative_error(target["log_premium"], 0.0),
        })
        prediction_rows.append({
            **base,
            "variant": "fixed16_same_seller",
            "donors": 0,
            "predicted_premium": math.exp(0.15) - 1.0,
            "relative_error": relative_error(target["log_premium"], 0.15),
        })
        for name, shrinkage in (
            ("strict_same_seller", False),
            ("shrinkage_same_seller", True),
        ):
            local = donor_candidates(target, seller_donors, 4.0, True)
            local_values = independent_values(local)
            prior = prior_for(target, seller_donors, True)
            predicted = None
            if shrinkage and prior is not None:
                if local_values:
                    z = len(local_values) / (len(local_values) + SHRINKAGE_K)
                    predicted = z * statistics.median(local_values) + (1.0 - z) * prior
                else:
                    predicted = prior
            elif len(local_values) >= 2:
                predicted = statistics.median(local_values)
            if predicted is None:
                continue
            prediction_rows.append({
                **base,
                "variant": name,
                "donors": len(local_values),
                "predicted_premium": math.exp(predicted) - 1.0,
                "relative_error": relative_error(target["log_premium"], predicted),
            })

    metrics: dict[str, Any] = {
        "cutoff": cutoff.isoformat(),
        "train_signals": len(donors),
        "holdout_signals": len(targets),
        "seller_holdout_signals": len(seller_targets),
        "variants": {},
    }
    by_variant: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in prediction_rows:
        by_variant[row["variant"]].append(row)
    def prediction_key(row: dict[str, Any]) -> tuple[Any, ...]:
        return (
            row["street"], row["house_num"], row["rooms"], row["transition"],
            row["seller_pair"], row["available_at"],
        )
    baseline_maps = {
        name: {prediction_key(row): float(row["relative_error"]) for row in rows}
        for name, rows in by_variant.items()
        if name in {"no_repair", "no_repair_same_seller"}
    }
    for name, rows in sorted(by_variant.items()):
        errors = [row["relative_error"] for row in rows]
        denominator = len(seller_targets) if "same_seller" in name else len(targets)
        metric = {
            "n": len(rows),
            "coverage": len(rows) / denominator if denominator else 0.0,
            "mean_error": statistics.mean(errors),
            "median_error": statistics.median(errors),
            "p90_error": quantile(errors, 0.90),
            "error_over_30pct": sum(error > 0.30 for error in errors) / len(errors),
        }
        baseline_name = "no_repair_same_seller" if "same_seller" in name else "no_repair"
        paired = [
            baseline_maps[baseline_name][prediction_key(row)]
            for row in rows
            if prediction_key(row) in baseline_maps.get(baseline_name, {})
        ]
        if paired:
            metric.update({
                "paired_no_repair_mean": statistics.mean(paired),
                "paired_no_repair_median": statistics.median(paired),
                "paired_no_repair_p90": quantile(paired, 0.90),
                "mean_improvement_vs_no_repair": statistics.mean(paired) - metric["mean_error"],
            })
        metrics["variants"][name] = metric
    return metrics, prediction_rows, cutoff


def temporal_metric_rows(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Метрики по переходу/классу/продавцу, а не только общий итог."""
    groups: dict[tuple[str, str, str, str, str], list[float]] = defaultdict(list)
    for row in predictions:
        dimensions = (
            row["variant"], row["transition"], row["building_class"],
            row["building_type"], row["seller_pair"],
        )
        groups[dimensions].append(float(row["relative_error"]))
    result = []
    for key, errors in sorted(groups.items()):
        result.append({
            "variant": key[0],
            "transition": key[1],
            "building_class": key[2],
            "building_type": key[3],
            "seller_pair": key[4],
            "n": len(errors),
            "mean_error": statistics.mean(errors),
            "median_error": statistics.median(errors),
            "p90_error": quantile(errors, 0.90),
        })
    return result


def stability_rows(
    signals: list[dict[str, Any]], cutoff: datetime,
) -> list[dict[str, Any]]:
    """Сравнивает медианную премию до и после temporal cutoff."""
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in signals:
        groups[(
            row["transition"], row["building_class"],
            "complex" if row["has_complex"] else "non_complex",
            row["seller_pair"],
        )].append(row)
    result = []
    for key, members in sorted(groups.items()):
        train = independent_values(row for row in members if row["available_at"] < cutoff)
        holdout = independent_values(row for row in members if row["available_at"] >= cutoff)
        if not train or not holdout:
            continue
        train_median = statistics.median(train)
        holdout_median = statistics.median(holdout)
        result.append({
            "transition": key[0],
            "building_class": key[1],
            "building_type": key[2],
            "seller_pair": key[3],
            "train_buildings": len(train),
            "holdout_buildings": len(holdout),
            "train_median_premium": math.exp(train_median) - 1.0,
            "holdout_median_premium": math.exp(holdout_median) - 1.0,
            "median_shift_log": holdout_median - train_median,
            "train_positive_share": sum(value > 0 for value in train) / len(train),
            "holdout_positive_share": sum(value > 0 for value in holdout) / len(holdout),
        })
    return result


def inspection(
    target_id: str,
    rows: list[dict[str, Any]],
    pool: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    q25: float,
    q75: float,
    room_bounds: dict[str, tuple[float, float]],
) -> dict[str, Any]:
    target = next((row for row in rows if str(row.get("id")) == target_id), None)
    if target is None:
        return {"id": target_id, "error": "target_not_found"}
    target_band = score_band(float(target["interior_score"]))
    address = exact_address_key(target)
    weak_rows = [
        row for row in pool
        if exact_address_key(row) == address
        and str(row.get("id")) != target_id
        and row.get("_rooms") == target.get("_rooms")
        and valid_interior(row)
        and score_band(float(row["interior_score"])) == "4"
    ]
    if not weak_rows or target_band is None:
        return {"id": target_id, "error": "no_base_or_target_band"}
    transition = f"4_to_{target_band}"
    base_pm2 = statistics.median(float(row["_price_m2"]) for row in weak_rows)
    class_score = stage3._class_score_from_price(
        base_pm2, str(target["_rooms"]), room_bounds, q25, q75
    )
    transfer_target = {
        "address": address,
        "rooms": str(target["_rooms"]),
        "transition": transition,
        "seller_pair": "unknown",
        "base_class_score": class_score,
        "building_class": building_class_label(class_score),
        "floor_total": float(target["_floor_total"]),
        "lat": float(target["_lat"]),
        "lon": float(target["_lon"]),
        "has_complex": bool(target.get("_complex_key")),
    }
    donors = donor_candidates(transfer_target, signals, STRICT_FLOOR_GAP)
    values = independent_values(donors)
    base_total = stage3_unadjusted_base(target, pool) * float(target["square_m2"])
    predicted = statistics.median(values) if len(values) >= 2 else None
    return {
        "id": target_id,
        "transition": transition,
        "base_total": base_total,
        "base_pm2": base_pm2,
        "base_class_score": class_score,
        "donor_buildings": len(values),
        "predicted_premium": math.exp(predicted) - 1.0 if predicted is not None else None,
        "predicted_total": base_total * math.exp(predicted) if predicted is not None else None,
        "donors": [
            {
                "street": row["street"],
                "house_num": row["house_num"],
                "distance_km": row["distance_km"],
                "class_delta": abs(row["base_class_score"] - class_score),
                "seller_pair": row["seller_pair"],
                "premium": math.exp(row["log_premium"]) - 1.0,
            }
            for row in sorted(donors, key=lambda item: item["distance_km"])
        ],
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--inspect-id", default="1015853709")
    parser.add_argument("--bootstrap", type=int, default=500)
    parser.add_argument("--holdout-fraction", type=float, default=0.20)
    parser.add_argument("--seed", type=int, default=20261013)
    args = parser.parse_args()

    rows, pool = stage3.prepare_baseline(args.baseline)
    q25, q75 = stage3.price_segment_boundaries(pool)
    room_bounds = stage3.room_segment_boundaries(pool)
    pairs = build_raw_pairs(pool)
    primary_signals = aggregate_signals(
        pairs, q25, q75, room_bounds, split_seller=False
    )
    seller_signals = aggregate_signals(
        pairs, q25, q75, room_bounds, split_seller=True
    )
    summaries = transition_summaries(seller_signals, args.bootstrap, args.seed)
    temporal, predictions, cutoff = temporal_evaluation(
        primary_signals, seller_signals, args.holdout_fraction
    )
    temporal_slices = temporal_metric_rows(predictions)
    stability = stability_rows(seller_signals, cutoff)
    target = inspection(
        str(args.inspect_id), rows, pool, primary_signals, q25, q75, room_bounds
    )

    args.output.mkdir(parents=True, exist_ok=True)
    write_csv(args.output / "transitions.csv", summaries)
    write_csv(args.output / "predictions.csv", predictions)
    write_csv(args.output / "temporal_metrics.csv", temporal_slices)
    write_csv(args.output / "stability.csv", stability)
    (args.output / f"inspection_{args.inspect_id}.json").write_text(
        json.dumps(json_ready(target), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {
        "baseline": str(args.baseline.resolve()),
        "raw_pairs": len(pairs),
        "signals": len(primary_signals),
        "seller_split_signals": len(seller_signals),
        "unique_buildings": len({row["address"] for row in primary_signals}),
        "signal_transitions": dict(Counter(row["transition"] for row in primary_signals)),
        "seller_pairs": dict(Counter(row["seller_pair"] for row in seller_signals)),
        "cutoff": cutoff.isoformat(),
        "temporal": temporal,
        "inspection": target,
        "config": {
            "score_bands": SCORE_BANDS,
            "strict_radius_km": STRICT_RADIUS_KM,
            "strict_class_delta": STRICT_CLASS_DELTA,
            "strict_floor_gap": STRICT_FLOOR_GAP,
            "holdout_fraction": args.holdout_fraction,
            "bootstrap": args.bootstrap,
        },
    }
    (args.output / "summary.json").write_text(
        json.dumps(json_ready(summary), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        f"Готово: raw_pairs={len(pairs)}, signals={len(primary_signals)}, "
        f"buildings={summary['unique_buildings']}, cutoff={cutoff.isoformat()}"
    )
    for name, metric in temporal["variants"].items():
        print(
            f"  {name}: n={metric['n']}, coverage={metric['coverage']:.1%}, "
            f"mean/median/p90={metric['mean_error']:.2%}/"
            f"{metric['median_error']:.2%}/{metric['p90_error']:.2%}"
        )
    if target.get("predicted_total") is not None:
        print(
            f"Цель {args.inspect_id}: base={target['base_total']:,.0f} ₸, "
            f"premium={target['predicted_premium']:+.1%}, "
            f"estimate={target['predicted_total']:,.0f} ₸, "
            f"donors={target['donor_buildings']}"
        )
    else:
        print(f"Цель {args.inspect_id}: {target.get('error') or 'недостаточно доноров'}")
    print(f"Результаты: {args.output.resolve()}")


if __name__ == "__main__":
    main()
