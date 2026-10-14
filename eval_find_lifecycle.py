"""Строгий proxy-тест логики «НАХОДКА» по lifecycle Cleaner baseline.

Цели берутся из короткого временного окна. Benchmark строится только из
объявлений, которые уже были видны до начала окна. Цена и целей, и
эталона — initial_price, чтобы будущее снижение цены не попало в прогноз.

Исходы — не договоры аренды, а proxy:
* quick_missing: объявление исчезло за horizon дней и не вернулось;
* clean_quick_missing: то же, но ещё и без зафиксированного снижения цены.

Production не меняется.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import stage3_benchmark_v3 as stage3
import eval_repair_premium_v2 as premium_v2


ROOT = Path(__file__).resolve().parent
DEFAULT_BASELINE = ROOT / "baseline" / "krisha_almaty_baseline.csv"
DEFAULT_OUTPUT = ROOT / "cache" / "find_lifecycle"


def parse_dt(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def numeric(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def initial_price_row(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    initial = numeric(row.get("initial_price"))
    if initial is not None and initial > 0:
        result["price"] = initial
    return stage3.enrich(result)


def visible_at(row: dict[str, Any], moment: datetime) -> bool:
    first_seen = parse_dt(row.get("first_seen_at"))
    last_seen = parse_dt(row.get("last_seen_at"))
    if first_seen is None or first_seen >= moment:
        return False
    # last_seen is the safest common evidence for seeded rows that have no
    # missing_detected_at: they must actually have survived until the split.
    return last_seen is not None and last_seen >= moment


def lifecycle_outcome(
    row: dict[str, Any], horizon_days: int,
) -> dict[str, Any]:
    first_seen = parse_dt(row.get("first_seen_at"))
    missing = parse_dt(row.get("missing_detected_at"))
    drops = int(numeric(row.get("price_drop_count")) or 0)
    reactivations = int(numeric(row.get("reactivation_count")) or 0)
    days_to_missing = (
        (missing - first_seen).total_seconds() / 86400.0
        if first_seen is not None and missing is not None else None
    )
    quick = (
        days_to_missing is not None
        and 0 <= days_to_missing <= horizon_days
        and reactivations == 0
    )
    return {
        "days_to_missing": days_to_missing,
        "quick_missing": quick,
        "clean_quick_missing": quick and drops == 0,
        "price_drop": drops > 0,
        "price_drop_count": drops,
        "reactivation_count": reactivations,
    }


def prepare_model(pool: list[dict[str, Any]]) -> dict[str, Any]:
    citywide = stage3.citywide_median_by_rooms(pool)
    q25, q75 = stage3.price_segment_boundaries(pool)
    room_bounds = stage3.room_segment_boundaries(pool)
    score_index = stage3.building_scores_index(pool, q25, q75, room_bounds)
    repair_premium_signals = stage3.build_repair_premium_signals(
        pool, q25, q75, room_bounds
    )
    return {
        "citywide_median": citywide,
        "q25": q25,
        "q75": q75,
        "room_bounds": room_bounds,
        "score_index": score_index,
        "class_priors": stage3.prior_medians_by_rooms_class(pool, score_index),
        "area_slopes": stage3.area_slopes_by_rooms(pool),
        "spatial_index": stage3.SpatialIndex(pool),
        "building_index": stage3.BuildingIndex(pool),
        "ref_areas": stage3.median_area_by_rooms(pool),
        "repair_premium_signals": repair_premium_signals,
    }


def strict_repair_fair_pm2(
    target: dict[str, Any],
    unadjusted_pm2: float | None,
    pool: list[dict[str, Any]],
    premium_signals: list[dict[str, Any]],
    model: dict[str, Any],
) -> dict[str, Any]:
    """Строгая «ремонтная линия» из v2-эксперимента.

    Базовый класс берётся из слабо отремонтированных квартир того же
    дома и комнатности. Премия берётся как медиана минимум двух
    независимых зданий-доноров того же перехода, комнатности и типа ЖК.
    Жёсткого 16%-cap здесь нет.
    """
    if unadjusted_pm2 is None or unadjusted_pm2 <= 0:
        return {"fair_pm2": None, "donors": 0, "premium": None}
    try:
        target_score = float(target["interior_score"])
    except (KeyError, TypeError, ValueError):
        return {"fair_pm2": None, "donors": 0, "premium": None}
    target_band = premium_v2.score_band(target_score)
    transition = f"4_to_{target_band}"
    if transition not in premium_v2.PRIMARY_TRANSITIONS:
        return {"fair_pm2": None, "donors": 0, "premium": None}

    address = premium_v2.exact_address_key(target)
    weak_rows = [
        row for row in pool
        if premium_v2.exact_address_key(row) == address
        and row.get("_rooms") == target.get("_rooms")
        and premium_v2.valid_interior(row)
        and premium_v2.score_band(float(row["interior_score"])) == "4"
    ]
    if not weak_rows:
        return {"fair_pm2": None, "donors": 0, "premium": None}
    base_pm2 = statistics.median(float(row["_price_m2"]) for row in weak_rows)
    class_score = stage3._class_score_from_price(
        base_pm2,
        str(target["_rooms"]),
        model["room_bounds"],
        model["q25"],
        model["q75"],
    )
    if class_score is None or any(
        target.get(field) is None for field in ("_floor_total", "_lat", "_lon")
    ):
        return {"fair_pm2": None, "donors": 0, "premium": None}
    transfer_target = {
        "address": address,
        "rooms": str(target["_rooms"]),
        "transition": transition,
        "seller_pair": "unknown",
        "base_class_score": class_score,
        "building_class": premium_v2.building_class_label(class_score),
        "floor_total": float(target["_floor_total"]),
        "lat": float(target["_lat"]),
        "lon": float(target["_lon"]),
        "has_complex": bool(target.get("_complex_key")),
    }
    donors = premium_v2.donor_candidates(
        transfer_target, premium_signals, premium_v2.STRICT_FLOOR_GAP
    )
    values = premium_v2.independent_values(donors)
    if len(values) < 2:
        return {"fair_pm2": None, "donors": len(values), "premium": None}
    predicted_log = statistics.median(values)
    return {
        "fair_pm2": unadjusted_pm2 * math.exp(predicted_log),
        "donors": len(values),
        "premium": math.exp(predicted_log) - 1.0,
    }


def score_target(
    target: dict[str, Any], pool: list[dict[str, Any]], model: dict[str, Any],
) -> dict[str, Any]:
    return stage3.score_row(
        target,
        pool,
        model["citywide_median"],
        model["score_index"],
        model["q25"],
        model["q75"],
        soft_target=True,
        room_bounds=model["room_bounds"],
        class_priors=model["class_priors"],
        area_slopes=model["area_slopes"],
        spatial_index=model["spatial_index"],
        building_index=model["building_index"],
        ref_areas=model["ref_areas"],
        repair_premium_signals=model["repair_premium_signals"],
    )


def find_verdict(
    fair_pm2: float | None,
    target: dict[str, Any],
    confidence: float,
) -> tuple[float | None, bool]:
    actual_pm2 = target.get("_price_m2")
    if fair_pm2 is None or actual_pm2 is None or fair_pm2 <= 0:
        return None, False
    discount = (fair_pm2 - actual_pm2) / fair_pm2
    verdict, _ = stage3.verdict_from_diff(
        discount,
        confidence,
        target,
        stage3.data_warnings(target),
        robust_z=None,
    )
    return discount, verdict == "НАХОДКА"


def precision(rows: list[dict[str, Any]], field: str) -> float | None:
    if not rows:
        return None
    return sum(bool(row[field]) for row in rows) / len(rows)


def model_metrics(
    records: list[dict[str, Any]], variant: str,
) -> dict[str, Any]:
    eligible = [row for row in records if row[f"{variant}_discount"] is not None]
    finds = [row for row in eligible if row[f"{variant}_find"]]
    ranked = sorted(
        eligible, key=lambda row: row[f"{variant}_discount"], reverse=True
    )
    top10_n = max(1, math.ceil(len(ranked) * 0.10)) if ranked else 0
    top10 = ranked[:top10_n]
    result = {
        "variant": variant,
        "eligible": len(eligible),
        "finds": len(finds),
        "find_share": len(finds) / len(eligible) if eligible else None,
        "find_quick_precision": precision(finds, "quick_missing"),
        "find_clean_precision": precision(finds, "clean_quick_missing"),
        "find_price_drop_share": precision(finds, "price_drop"),
        "top10_n": len(top10),
        "top10_quick_precision": precision(top10, "quick_missing"),
        "top10_clean_precision": precision(top10, "clean_quick_missing"),
        "top10_price_drop_share": precision(top10, "price_drop"),
    }
    positives = sum(bool(row["clean_quick_missing"]) for row in records)
    for budget in (5, 10, 20):
        selected = ranked[:budget]
        true_positives = sum(bool(row["clean_quick_missing"]) for row in selected)
        result[f"top{budget}_precision"] = (
            true_positives / len(selected) if selected else None
        )
        result[f"top{budget}_recall"] = (
            true_positives / positives if positives else None
        )
    return result


def high_recall_queue_metrics(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Объединяет обычные production-находки с двумя ремонтными каналами."""
    production = [row for row in records if row["production_find"]]
    strict = [
        row for row in records
        if row["strict_repair_discount"] is not None
        and row["strict_repair_discount"] >= 0.05
    ]
    high_repair = sorted(
        (
            row for row in records
            if numeric(row.get("interior_score")) is not None
            and float(row["interior_score"]) >= 6.3
            and row["individual_discount"] is not None
            and row["individual_discount"] > 0
        ),
        key=lambda row: row["individual_discount"],
        reverse=True,
    )
    all_positives = sum(bool(row["clean_quick_missing"]) for row in records)
    high_positives = sum(
        bool(row["clean_quick_missing"])
        and numeric(row.get("interior_score")) is not None
        and float(row["interior_score"]) >= 6.3
        for row in records
    )
    result = []
    for budget in (5, 10, 20, 30, 40, 50):
        selected_by_id = {
            str(row["id"]): row
            for row in production + strict + high_repair[:budget]
        }
        selected = list(selected_by_id.values())
        true_positives = sum(bool(row["clean_quick_missing"]) for row in selected)
        high_true_positives = sum(
            bool(row["clean_quick_missing"])
            and numeric(row.get("interior_score")) is not None
            and float(row["interior_score"]) >= 6.3
            for row in selected
        )
        result.append({
            "high_repair_budget": budget,
            "production_alerts": len(production),
            "strict_repair_alerts": len(strict),
            "union_alerts": len(selected),
            "extra_alerts": len(selected) - len(production),
            "precision": true_positives / len(selected) if selected else None,
            "recall": true_positives / all_positives if all_positives else None,
            "high_repair_recall": (
                high_true_positives / high_positives if high_positives else None
            ),
        })
    return result


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--horizon-days", type=int, default=7)
    parser.add_argument("--window-hours", type=int, default=36)
    parser.add_argument("--end-offset-days", type=int, default=0)
    args = parser.parse_args()

    raw_rows = stage3.load_rows(args.baseline)
    observed = [parse_dt(row.get("scraped_at")) for row in raw_rows]
    snapshot = max(value for value in observed if value is not None)
    target_end = snapshot - timedelta(
        days=args.horizon_days + args.end_offset_days
    )
    target_start = target_end - timedelta(hours=args.window_hours)

    pool = [
        initial_price_row(row) for row in raw_rows
        if visible_at(row, target_start)
    ]
    stage3.mark_price_outliers(pool)
    pool = [row for row in pool if not row.get("_excluded_baseline")]
    targets_raw = [
        row for row in raw_rows
        if (first := parse_dt(row.get("first_seen_at"))) is not None
        and target_start <= first <= target_end
    ]
    targets = [initial_price_row(row) for row in targets_raw]

    model = prepare_model(pool)
    premium_pairs = premium_v2.build_raw_pairs(pool)
    premium_signals = premium_v2.aggregate_signals(
        premium_pairs,
        model["q25"],
        model["q75"],
        model["room_bounds"],
        split_seller=False,
    )
    records: list[dict[str, Any]] = []
    for raw, target in zip(targets_raw, targets):
        result = score_target(target, pool, model)
        if result.get("status") != "scored":
            continue
        confidence = float(result.get("benchmark_confidence") or 0.0)
        variants = {
            "no_repair": numeric(result.get("base_price_m2_unadjusted")),
            "production": numeric(result.get("base_price_m2")),
            "legacy": numeric(result.get("interior_legacy_price_m2")),
            "individual": numeric(result.get("interior_individual_price_m2")),
        }
        # Отдельные ремонтные ветки — частичные модели. Когда они
        # недоступны, отступаем к цене без ремонта, как делал бы
        # полный challenger, а не выбрасываем трудные цели.
        variants["legacy"] = variants["legacy"] or variants["no_repair"]
        variants["individual"] = variants["individual"] or variants["no_repair"]
        strict_repair = strict_repair_fair_pm2(
            target,
            variants["no_repair"],
            pool,
            premium_signals,
            model,
        )
        variants["strict_repair"] = strict_repair["fair_pm2"]
        variants["repair_union"] = max(
            value for value in (
                variants["production"], strict_repair["fair_pm2"]
            ) if value is not None
        )

        outcome = lifecycle_outcome(raw, args.horizon_days)
        record: dict[str, Any] = {
            "id": raw.get("id"),
            "url": raw.get("url"),
            "first_seen_at": raw.get("first_seen_at"),
            "initial_price": raw.get("initial_price") or raw.get("price"),
            "rooms": raw.get("rooms"),
            "square_m2": raw.get("square_m2"),
            "interior_score": raw.get("interior_score"),
            "benchmark_confidence": confidence,
            "production_verdict": result.get("verdict"),
            "production_value_score": result.get("value_score"),
            "production_price_quality_score": result.get("price_quality_score"),
            "production_quality_evidence_score": result.get("quality_evidence_score"),
            "production_data_confidence": result.get("data_confidence"),
            "production_seller_class": result.get("seller_class"),
            "production_repair_find_override": result.get("repair_find_override"),
            "strict_repair_donors": strict_repair["donors"],
            "strict_repair_premium": strict_repair["premium"],
            **outcome,
        }
        for name, fair_pm2 in variants.items():
            discount, is_find = find_verdict(fair_pm2, target, confidence)
            record[f"{name}_fair_pm2"] = fair_pm2
            record[f"{name}_discount"] = discount
            record[f"{name}_find"] = is_find
        # Production uses the special 5% threshold only when Stage 3 has
        # accepted strict structural repair evidence. Reproduce the actual
        # verdict instead of re-running the generic 17/21/27% ladder above.
        record["production_find"] = result.get("verdict") == "НАХОДКА"
        records.append(record)

    variants = (
        "no_repair", "production", "legacy", "individual",
        "strict_repair", "repair_union",
    )
    metrics = [model_metrics(records, variant) for variant in variants]
    queue_metrics = high_recall_queue_metrics(records)
    baseline_outcome = {
        "scored_targets": len(records),
        "quick_missing_rate": precision(records, "quick_missing"),
        "clean_quick_missing_rate": precision(records, "clean_quick_missing"),
        "price_drop_rate": precision(records, "price_drop"),
    }
    summary = {
        "snapshot": snapshot.isoformat(),
        "target_start": target_start.isoformat(),
        "target_end": target_end.isoformat(),
        "horizon_days": args.horizon_days,
        "end_offset_days": args.end_offset_days,
        "training_pool": len(pool),
        "raw_targets": len(targets),
        **baseline_outcome,
        "metrics": metrics,
        "high_recall_queues": queue_metrics,
        "warning": "missing — proxy, а не подтверждённая сделка",
    }

    args.output.mkdir(parents=True, exist_ok=True)
    write_csv(args.output / "predictions.csv", records)
    write_csv(args.output / "metrics.csv", metrics)
    write_csv(args.output / "high_recall_queues.csv", queue_metrics)
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        f"snapshot={snapshot.isoformat()}; targets={target_start.isoformat()} .. "
        f"{target_end.isoformat()}; pool={len(pool)}; scored={len(records)}"
    )
    if records:
        print(
            f"all: quick={baseline_outcome['quick_missing_rate']:.1%}; "
            f"clean_quick={baseline_outcome['clean_quick_missing_rate']:.1%}; "
            f"price_drop={baseline_outcome['price_drop_rate']:.1%}"
        )
    else:
        print("all: окно пустое — метрики не рассчитываются")
    for row in metrics:
        print(
            f"{row['variant']}: finds={row['finds']}; "
            f"find quick/clean/drop={row['find_quick_precision']!s}/"
            f"{row['find_clean_precision']!s}/{row['find_price_drop_share']!s}; "
            f"top10 quick/clean/drop={row['top10_quick_precision']!s}/"
            f"{row['top10_clean_precision']!s}/{row['top10_price_drop_share']!s}"
        )


if __name__ == "__main__":
    main()
