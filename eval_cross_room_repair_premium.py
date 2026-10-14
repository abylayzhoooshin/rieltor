"""Офлайн-проверка переноса премии за ремонт между комнатностями одного дома.

Важно: переносится не цена и не price/m2 квартиры другой
комнатности, а только наблюдаемая log-премия за ремонт внутри того же
физического здания.

Тест строго временной: донорский сигнал должен стать известен до cutoff,
целевой — после cutoff. Production-код не меняется, OpenAI не вызывается.

Запуск:
    python -X utf8 eval_cross_room_repair_premium.py
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import eval_repair_premium_v2 as premium_v2
import stage3_benchmark_v3 as stage3


ROOT = Path(__file__).resolve().parent
DEFAULT_BASELINE = ROOT / "baseline" / "krisha_almaty_baseline.csv"
DEFAULT_OUTPUT = ROOT / "cache" / "repair_cross_room"


def quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    share = position - low
    return ordered[low] * (1.0 - share) + ordered[high] * share


def representative_pair(
    members: list[dict[str, Any]], median_log_premium: float,
) -> dict[str, Any]:
    return min(members, key=lambda row: abs(row["log_premium"] - median_log_premium))


def build_signals(raw_pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Сворачивает пары до одного голоса на дом/комнатность/переход."""
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for pair in raw_pairs:
        grouped[(pair["address"], pair["rooms"], pair["transition"])].append(pair)

    signals: list[dict[str, Any]] = []
    for (address, rooms, transition), members in grouped.items():
        log_premium = statistics.median(row["log_premium"] for row in members)
        low_score = statistics.median(row["low_score"] for row in members)
        high_score = statistics.median(row["high_score"] for row in members)
        score_gap = high_score - low_score
        if score_gap <= 0:
            continue
        representative = representative_pair(members, log_premium)
        signals.append({
            "address": address,
            "street": address[0],
            "house_num": address[1],
            "rooms": rooms,
            "transition": transition,
            "log_premium": log_premium,
            "low_score": low_score,
            "high_score": high_score,
            "score_gap": score_gap,
            "low_id": representative["low_id"],
            "high_id": representative["high_id"],
            "available_at": max(row["available_at"] for row in members),
            "raw_pair_count": len(members),
        })
    return signals


def cross_room_donors(
    target: dict[str, Any],
    train_signals: Iterable[dict[str, Any]],
    *,
    same_transition: bool,
) -> list[dict[str, Any]]:
    return [
        row for row in train_signals
        if row["address"] == target["address"]
        and row["rooms"] != target["rooms"]
        and (not same_transition or row["transition"] == target["transition"])
    ]


def relative_error(actual_log: float, predicted_log: float) -> float:
    actual = math.exp(actual_log)
    predicted = math.exp(predicted_log)
    return abs(predicted - actual) / actual


def donor_description(donors: list[dict[str, Any]]) -> str:
    parts = []
    for row in donors:
        premium = math.exp(row["log_premium"]) - 1.0
        parts.append(
            f"{row['rooms']}к:{row['low_id']}->{row['high_id']}"
            f"({row['low_score']:.1f}->{row['high_score']:.1f};{premium:+.1%})"
        )
    return " | ".join(parts)


def prediction_row(
    target: dict[str, Any],
    donors: list[dict[str, Any]],
    variant: str,
    predicted_log: float,
) -> dict[str, Any]:
    actual_premium = math.exp(target["log_premium"]) - 1.0
    predicted_premium = math.exp(predicted_log) - 1.0
    no_repair_error = relative_error(target["log_premium"], 0.0)
    error = relative_error(target["log_premium"], predicted_log)
    return {
        "street": target["street"],
        "house_num": target["house_num"],
        "target_rooms": target["rooms"],
        "transition": target["transition"],
        "target_low_id": target["low_id"],
        "target_high_id": target["high_id"],
        "target_low_score": target["low_score"],
        "target_high_score": target["high_score"],
        "target_score_gap": target["score_gap"],
        "actual_premium": actual_premium,
        "variant": variant,
        "donor_signals": len(donors),
        "donor_rooms": ",".join(sorted({str(row["rooms"]) for row in donors})),
        "donor_details": donor_description(donors),
        "predicted_premium": predicted_premium,
        "relative_error": error,
        "no_repair_error": no_repair_error,
        "improvement_vs_no_repair": no_repair_error - error,
    }


def evaluate(
    signals: list[dict[str, Any]], holdout_fraction: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    dates = sorted(row["available_at"] for row in signals)
    cutoff_index = max(
        0, min(len(dates) - 1, int(len(dates) * (1.0 - holdout_fraction)))
    )
    cutoff = dates[cutoff_index]
    train = [row for row in signals if row["available_at"] < cutoff]
    targets = [row for row in signals if row["available_at"] >= cutoff]
    predictions: list[dict[str, Any]] = []

    for target in targets:
        same_transition = cross_room_donors(
            target, train, same_transition=True
        )
        if same_transition:
            raw_prediction = statistics.median(
                row["log_premium"] for row in same_transition
            )
            predictions.append(prediction_row(
                target, same_transition, "same_transition_premium", raw_prediction
            ))

            slopes = [
                row["log_premium"] / row["score_gap"]
                for row in same_transition if row["score_gap"] > 0
            ]
            scaled_prediction = statistics.median(slopes) * target["score_gap"]
            predictions.append(prediction_row(
                target, same_transition, "same_transition_score_scaled",
                scaled_prediction,
            ))

        any_transition = cross_room_donors(
            target, train, same_transition=False
        )
        if any_transition:
            slopes = [
                row["log_premium"] / row["score_gap"]
                for row in any_transition if row["score_gap"] > 0
            ]
            scaled_prediction = statistics.median(slopes) * target["score_gap"]
            predictions.append(prediction_row(
                target, any_transition, "any_transition_score_scaled",
                scaled_prediction,
            ))

    metadata = {
        "cutoff": cutoff.isoformat(),
        "signals": len(signals),
        "train_signals": len(train),
        "holdout_signals": len(targets),
    }
    return predictions, metadata


def metric_rows(
    predictions: list[dict[str, Any]], holdout_signals: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in predictions:
        grouped[row["variant"]].append(row)

    result = []
    for variant, members in sorted(grouped.items()):
        errors = [row["relative_error"] for row in members]
        baselines = [row["no_repair_error"] for row in members]
        improvements = [row["improvement_vs_no_repair"] for row in members]
        biases = [row["predicted_premium"] - row["actual_premium"] for row in members]
        result.append({
            "variant": variant,
            "n": len(members),
            "coverage_all_holdout": len(members) / holdout_signals,
            "mean_error": statistics.mean(errors),
            "median_error": statistics.median(errors),
            "p90_error": quantile(errors, 0.90),
            "paired_no_repair_mean": statistics.mean(baselines),
            "paired_no_repair_median": statistics.median(baselines),
            "mean_improvement_vs_no_repair": statistics.mean(improvements),
            "median_improvement_vs_no_repair": statistics.median(improvements),
            "mean_premium_bias": statistics.mean(biases),
            "errors_over_30pct": sum(error > 0.30 for error in errors),
        })
    return result


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--holdout-fraction", type=float, default=0.20)
    args = parser.parse_args()
    if not 0.05 <= args.holdout_fraction <= 0.50:
        parser.error("--holdout-fraction должен быть в диапазоне 0.05..0.50")

    _, pool = stage3.prepare_baseline(args.baseline)
    raw_pairs = premium_v2.build_raw_pairs(pool)
    signals = build_signals(raw_pairs)
    predictions, metadata = evaluate(signals, args.holdout_fraction)
    metrics = metric_rows(predictions, metadata["holdout_signals"])

    args.output.mkdir(parents=True, exist_ok=True)
    write_csv(args.output / "predictions.csv", predictions)
    write_csv(args.output / "metrics.csv", metrics)
    worst = sorted(
        predictions, key=lambda row: row["improvement_vs_no_repair"]
    )[:30]
    write_csv(args.output / "worst_errors.csv", worst)
    summary = {**metadata, "metrics": metrics}
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        f"Сигналов: {metadata['signals']}; train: {metadata['train_signals']}; "
        f"hold-out: {metadata['holdout_signals']}; cutoff: {metadata['cutoff']}"
    )
    for row in metrics:
        print(
            f"{row['variant']}: n={row['n']}, "
            f"coverage={row['coverage_all_holdout']:.1%}, "
            f"error mean/median/p90="
            f"{row['mean_error']:.2%}/{row['median_error']:.2%}/{row['p90_error']:.2%}, "
            f"paired no-repair={row['paired_no_repair_mean']:.2%}, "
            f"improvement={row['mean_improvement_vs_no_repair']:+.2%}"
        )


if __name__ == "__main__":
    main()
