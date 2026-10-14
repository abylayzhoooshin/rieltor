"""Проверка ценовой поправки ремонта на CSV, созданном Stage 3.

Пример:
  python eval_interior_adjustment.py --input cache/interior_adjusted_scored.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import statistics


def holdout(id_value: str) -> bool:
    digest = hashlib.sha256(id_value.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 5 == 0


def evaluate(path: str) -> dict[str, float | int]:
    raw_errors = []
    adjusted_errors = []
    applied = 0
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if not holdout(str(row.get("id") or "")):
                continue
            try:
                actual = float(row["price"]) / float(row["square_m2"])
                raw = float(row["base_price_m2_unadjusted"])
                adjusted = float(row["base_price_m2"])
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
            if actual <= 0 or raw <= 0 or adjusted <= 0:
                continue
            raw_errors.append(abs(actual - raw) / actual)
            adjusted_errors.append(abs(actual - adjusted) / actual)
            if str(row.get("interior_adjustment_applied")).lower() == "true":
                applied += 1
    if not raw_errors:
        raise ValueError("в hold-out нет пригодных строк")
    raw_mean = statistics.mean(raw_errors)
    adjusted_mean = statistics.mean(adjusted_errors)
    return {
        "rows": len(raw_errors),
        "applied": applied,
        "raw_mean_error": raw_mean,
        "adjusted_mean_error": adjusted_mean,
        "relative_improvement_pct": (raw_mean - adjusted_mean) / raw_mean * 100,
        "raw_median_error": statistics.median(raw_errors),
        "adjusted_median_error": statistics.median(adjusted_errors),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    metrics = evaluate(args.input)
    print(f"hold-out rows: {metrics['rows']}; adjustment applied: {metrics['applied']}")
    print(
        "mean relative error: "
        f"{metrics['raw_mean_error']:.4f} -> {metrics['adjusted_mean_error']:.4f} "
        f"({metrics['relative_improvement_pct']:+.2f}%)"
    )
    print(
        "median relative error: "
        f"{metrics['raw_median_error']:.4f} -> {metrics['adjusted_median_error']:.4f}"
    )
