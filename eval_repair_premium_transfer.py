"""Офлайн-проверка переноса премии за ремонт между похожими зданиями.

Скрипт не меняет production и не вызывает OpenAI. Он ищет внутри одного
физического дома пары квартир со слабым и хорошим ремонтом, измеряет
наблюдаемую разницу log(price/m2), а затем проверяет перенос этой разницы в
другой дом с учётом географии и ценового класса базовой квартиры.

Пример:
    python eval_repair_premium_transfer.py --inspect-id 1015853709
"""

from __future__ import annotations

import argparse
import math
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import stage3_benchmark_v3 as stage3


ROOT = Path(__file__).resolve().parent
DEFAULT_BASELINE = ROOT / "baseline" / "krisha_almaty_baseline.csv"

LOW_SCORE = (3.9, 4.9)
HIGH_SCORE = (5.9, 6.9)
MAX_AREA_RATIO = 1.20
MAX_DATE_GAP_DAYS = 180


def parse_date(row: dict[str, Any]) -> datetime | None:
    for field in ("scraped_at", "last_seen_at", "added_at", "created_at"):
        raw = str(row.get(field) or "").strip()
        if not raw:
            continue
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def valid_interior(row: dict[str, Any]) -> bool:
    try:
        score = float(row.get("interior_score"))
    except (TypeError, ValueError):
        return False
    return (
        1.0 <= score <= 10.0
        and row.get("interior_status") in {"ready", "ready_with_download_gaps"}
        and row.get("interior_coverage") in {"sufficient", "partial"}
    )


def exact_address_key(row: dict[str, Any]) -> tuple[str, str] | None:
    street = str(row.get("_street_norm") or "").strip()
    house = str(row.get("_house_num_norm") or "").strip()
    return (street, house) if street and house else None


def median(values: list[float]) -> float:
    return statistics.median(values)


def build_pair_buildings(
    pool: list[dict[str, Any]],
    q25: float,
    q75: float,
    room_bounds: dict[str, tuple[float, float]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[tuple[str, str], str], list[dict[str, Any]]] = defaultdict(list)
    for row in pool:
        key = exact_address_key(row)
        if key is None or not valid_interior(row):
            continue
        if row.get("_lat") is None or row.get("_lon") is None:
            continue
        if row.get("_floor_total") is None or row.get("_price_m2") is None:
            continue
        groups[(key, str(row.get("_rooms")))].append(row)

    buildings: list[dict[str, Any]] = []
    for (address, rooms), members in groups.items():
        pairs: list[dict[str, float]] = []
        for pos, left in enumerate(members):
            for right in members[pos + 1:]:
                if left.get("id") == right.get("id"):
                    continue
                left_hash = str(left.get("photo_set_hash") or "")
                right_hash = str(right.get("photo_set_hash") or "")
                if left_hash and left_hash == right_hash:
                    continue
                left_area = float(left.get("square_m2"))
                right_area = float(right.get("square_m2"))
                if max(left_area, right_area) / min(left_area, right_area) > MAX_AREA_RATIO:
                    continue
                left_date, right_date = parse_date(left), parse_date(right)
                if left_date and right_date:
                    if abs((left_date - right_date).days) > MAX_DATE_GAP_DAYS:
                        continue

                left_score = float(left.get("interior_score"))
                right_score = float(right.get("interior_score"))
                low, high = (left, right) if left_score <= right_score else (right, left)
                low_score = float(low.get("interior_score"))
                high_score = float(high.get("interior_score"))
                if not (LOW_SCORE[0] <= low_score <= LOW_SCORE[1]):
                    continue
                if not (HIGH_SCORE[0] <= high_score <= HIGH_SCORE[1]):
                    continue

                low_pm2 = float(low["_price_m2"])
                high_pm2 = float(high["_price_m2"])
                pairs.append({
                    "log_premium": math.log(high_pm2 / low_pm2),
                    "low_pm2": low_pm2,
                    "floor_total": (float(low["_floor_total"]) + float(high["_floor_total"])) / 2.0,
                    "lat": (float(low["_lat"]) + float(high["_lat"])) / 2.0,
                    "lon": (float(low["_lon"]) + float(high["_lon"])) / 2.0,
                    "has_complex": float(bool(low.get("_complex_key") or high.get("_complex_key"))),
                })

        if not pairs:
            continue
        low_pm2 = median([pair["low_pm2"] for pair in pairs])
        buildings.append({
            "address": address,
            "rooms": rooms,
            "log_premium": median([pair["log_premium"] for pair in pairs]),
            "low_pm2": low_pm2,
            # Это класс БАЗОВОЙ квартиры, а не circular-класс из цены цели.
            "base_class_score": stage3._class_score_from_price(
                low_pm2, rooms, room_bounds, q25, q75
            ),
            "floor_total": median([pair["floor_total"] for pair in pairs]),
            "lat": median([pair["lat"] for pair in pairs]),
            "lon": median([pair["lon"] for pair in pairs]),
            "has_complex": round(median([pair["has_complex"] for pair in pairs])) == 1,
            "pair_count": len(pairs),
        })
    return buildings


def donors_for(
    target: dict[str, Any],
    buildings: list[dict[str, Any]],
    radius_km: float,
    class_delta: float,
    floor_gap: float,
) -> list[dict[str, Any]]:
    result = []
    for candidate in buildings:
        if candidate["address"] == target["address"]:
            continue
        if candidate["rooms"] != target["rooms"]:
            continue
        if candidate["has_complex"] != target["has_complex"]:
            continue
        if target.get("base_class_score") is None or candidate.get("base_class_score") is None:
            continue
        if abs(candidate["base_class_score"] - target["base_class_score"]) > class_delta:
            continue
        if abs(candidate["floor_total"] - target["floor_total"]) > floor_gap:
            continue
        distance = stage3.haversine_km(
            target["lat"], target["lon"], candidate["lat"], candidate["lon"]
        )
        if distance > radius_km:
            continue
        item = dict(candidate)
        item["distance_km"] = distance
        result.append(item)
    return result


def relative_error(actual_log: float, predicted_log: float) -> float:
    actual = math.exp(actual_log)
    predicted = math.exp(predicted_log)
    return abs(predicted - actual) / actual


def validate_config(
    buildings: list[dict[str, Any]],
    radius_km: float,
    class_delta: float,
    floor_gap: float,
) -> dict[str, Any]:
    transferred, fixed = [], []
    donor_counts = []
    for target in buildings:
        donors = donors_for(target, buildings, radius_km, class_delta, floor_gap)
        if len(donors) < 2:
            continue
        predicted = median([row["log_premium"] for row in donors])
        transferred.append(relative_error(target["log_premium"], predicted))
        fixed.append(relative_error(target["log_premium"], 0.15))
        donor_counts.append(len(donors))
    return {
        "n": len(transferred),
        "transfer_mean": statistics.mean(transferred) if transferred else None,
        "transfer_median": median(transferred) if transferred else None,
        "fixed_mean": statistics.mean(fixed) if fixed else None,
        "fixed_median": median(fixed) if fixed else None,
        "mean_donors": statistics.mean(donor_counts) if donor_counts else None,
    }


def stage3_unadjusted_base(target: dict[str, Any], pool: list[dict[str, Any]]) -> float:
    citywide = stage3.citywide_median_by_rooms(pool)
    q25, q75 = stage3.price_segment_boundaries(pool)
    room_bounds = stage3.room_segment_boundaries(pool)
    score_index = stage3.building_scores_index(pool, q25, q75, room_bounds)
    result = stage3.score_row(
        target,
        pool,
        citywide,
        score_index,
        q25,
        q75,
        soft_target=True,
        room_bounds=room_bounds,
        class_priors=stage3.prior_medians_by_rooms_class(pool, score_index),
        area_slopes=stage3.area_slopes_by_rooms(pool),
        spatial_index=stage3.SpatialIndex(pool),
        building_index=stage3.BuildingIndex(pool),
        ref_areas=stage3.median_area_by_rooms(pool),
    )
    return float(result["base_price_m2_unadjusted"])


def inspect_target(
    target_id: str,
    rows: list[dict[str, Any]],
    pool: list[dict[str, Any]],
    buildings: list[dict[str, Any]],
    q25: float,
    q75: float,
    room_bounds: dict[str, tuple[float, float]],
) -> None:
    target = next((row for row in rows if str(row.get("id")) == target_id), None)
    if target is None:
        print(f"\nЦель {target_id}: не найдена")
        return
    address = exact_address_key(target)
    base_rows = [
        row for row in pool
        if exact_address_key(row) == address
        and str(row.get("id")) != target_id
        and row.get("_rooms") == target.get("_rooms")
        and valid_interior(row)
        and LOW_SCORE[0] <= float(row.get("interior_score")) <= LOW_SCORE[1]
    ]
    if not base_rows:
        print(f"\nЦель {target_id}: нет слабого базового аналога в доме")
        return
    base_pm2 = median([float(row["_price_m2"]) for row in base_rows])
    transfer_target = {
        "address": address,
        "rooms": str(target.get("_rooms")),
        "base_class_score": stage3._class_score_from_price(
            base_pm2, str(target.get("_rooms")), room_bounds, q25, q75
        ),
        "floor_total": float(target["_floor_total"]),
        "lat": float(target["_lat"]),
        "lon": float(target["_lon"]),
        "has_complex": bool(target.get("_complex_key")),
    }
    base_total = stage3_unadjusted_base(target, pool) * float(target["square_m2"])
    print(f"\nЦель {target_id}: base={base_total:,.0f} ₸; weak_pm2={base_pm2:,.0f}")
    configs = (
        ("strict_1km", 1.0, 0.25, 2.0),
        ("strict_5km", 5.0, 0.25, 2.0),
        ("uncertainty_5km", 5.0, 0.87, 2.0),
    )
    for name, radius, class_delta, floor_gap in configs:
        donors = donors_for(transfer_target, buildings, radius, class_delta, floor_gap)
        if len(donors) < 2:
            print(f"  {name}: недостаточно доноров ({len(donors)})")
            continue
        premium = median([row["log_premium"] for row in donors])
        print(
            f"  {name}: donors={len(donors)}, premium={math.exp(premium)-1:+.1%}, "
            f"estimate={base_total * math.exp(premium):,.0f} ₸"
        )
        for donor in sorted(donors, key=lambda row: row["distance_km"]):
            print(
                f"    {donor['address'][0]} {donor['address'][1]}: "
                f"{donor['distance_km']:.2f} км; "
                f"classΔ={abs(donor['base_class_score']-transfer_target['base_class_score']):.2f}; "
                f"premium={math.exp(donor['log_premium'])-1:+.1%}"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--inspect-id", default="1015853709")
    args = parser.parse_args()

    rows, pool = stage3.prepare_baseline(args.baseline)
    q25, q75 = stage3.price_segment_boundaries(pool)
    room_bounds = stage3.room_segment_boundaries(pool)
    buildings = build_pair_buildings(pool, q25, q75, room_bounds)
    print(
        f"Парных зданий: {len(buildings)}; "
        f"исходных пар: {sum(row['pair_count'] for row in buildings)}"
    )
    for name, radius, class_delta, floor_gap in (
        ("strict_1km", 1.0, 0.25, 2.0),
        ("strict_5km", 5.0, 0.25, 2.0),
        ("strict_5km_floor4", 5.0, 0.25, 4.0),
        ("uncertainty_5km", 5.0, 0.87, 2.0),
    ):
        metric = validate_config(buildings, radius, class_delta, floor_gap)
        print(
            f"{name}: n={metric['n']}; "
            f"transfer mean/median={metric['transfer_mean']:.2%}/{metric['transfer_median']:.2%}; "
            f"fixed16 mean/median={metric['fixed_mean']:.2%}/{metric['fixed_median']:.2%}; "
            f"mean donors={metric['mean_donors']:.1f}"
        )
    inspect_target(
        str(args.inspect_id), rows, pool, buildings, q25, q75, room_bounds
    )


if __name__ == "__main__":
    main()
