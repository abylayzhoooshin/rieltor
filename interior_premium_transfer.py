"""Строгое доказательство внешней премии за сильный ремонт.

Это не общая модель цены. Модуль отвечает на узкий вопрос: если в доме
цели есть слабая базовая квартира, какую премию за переход 4->6/7+
показывают другие сопоставимые здания?

Защита от шума структурная, а не через произвольный 16%-cap:

* один сигнал на дом/комнатность/переход;
* одинаковый тип ЖК/не-ЖК и комнатность;
* близкие этажность, география и непрерывный класс слабой базы;
* минимум два независимых здания-донора;
* только крупные переходы 4->6 и 4->7+, а не шумные 5.8->6.2.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from interior_price_adjustment import usable_interior_score


MAX_AREA_RATIO = 1.20
MAX_DATE_GAP_DAYS = 180
STRICT_RADIUS_KM = 5.0
STRICT_CLASS_DELTA = 0.25
STRICT_FLOOR_GAP = 2.0
MIN_DONOR_BUILDINGS = 2
ALLOWED_TRANSITIONS = {"4_to_6", "4_to_7plus"}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date(row: dict[str, Any]) -> datetime | None:
    for field in ("first_seen_at", "scraped_at", "last_seen_at", "added_at", "created_at"):
        raw = str(row.get(field) or "").strip()
        if not raw:
            continue
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _address(row: dict[str, Any]) -> tuple[str, str] | None:
    street = str(row.get("_street_norm") or "").strip()
    house = str(row.get("_house_num_norm") or "").strip()
    return (street, house) if street and house else None


def repair_band(score: float) -> str | None:
    if 4.0 <= score <= 4.99:
        return "4"
    if 5.0 <= score <= 5.99:
        return "5"
    if 6.0 <= score <= 6.99:
        return "6"
    if 7.0 <= score <= 10.0:
        return "7plus"
    return None


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    value = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    value = max(0.0, min(1.0, value))
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def build_premium_signals(
    pool: list[dict[str, Any]],
    class_score: Callable[[float, str, dict[str, tuple[float, float]], float, float], float | None],
    room_bounds: dict[str, tuple[float, float]],
    q25: float,
    q75: float,
) -> list[dict[str, Any]]:
    """Построить один устойчивый сигнал на здание/комнатность/переход."""
    grouped: dict[tuple[tuple[str, str], str], list[dict[str, Any]]] = defaultdict(list)
    for row in pool:
        address = _address(row)
        if address is None or usable_interior_score(row) is None:
            continue
        if any(row.get(field) is None for field in ("_price_m2", "_lat", "_lon", "_floor_total")):
            continue
        grouped[(address, str(row.get("_rooms")))].append(row)

    raw: dict[tuple[tuple[str, str], str, str], list[dict[str, Any]]] = defaultdict(list)
    for (address, rooms), members in grouped.items():
        for position, left in enumerate(members):
            for right in members[position + 1:]:
                if str(left.get("id")) == str(right.get("id")):
                    continue
                left_hash = str(left.get("photo_set_hash") or "")
                right_hash = str(right.get("photo_set_hash") or "")
                if left_hash and left_hash == right_hash:
                    continue
                left_area = _number(left.get("square_m2"))
                right_area = _number(right.get("square_m2"))
                if not left_area or not right_area:
                    continue
                if max(left_area, right_area) / min(left_area, right_area) > MAX_AREA_RATIO:
                    continue
                left_date, right_date = _date(left), _date(right)
                if left_date and right_date and abs((left_date - right_date).days) > MAX_DATE_GAP_DAYS:
                    continue
                left_score = usable_interior_score(left)[0]
                right_score = usable_interior_score(right)[0]
                left_band, right_band = repair_band(left_score), repair_band(right_score)
                transition = None
                if left_band == "4" and right_band in {"6", "7plus"}:
                    low, high = left, right
                    transition = f"4_to_{right_band}"
                elif right_band == "4" and left_band in {"6", "7plus"}:
                    low, high = right, left
                    transition = f"4_to_{left_band}"
                if transition not in ALLOWED_TRANSITIONS:
                    continue
                low_pm2 = float(low["_price_m2"])
                high_pm2 = float(high["_price_m2"])
                if low_pm2 <= 0 or high_pm2 <= 0:
                    continue
                raw[(address, rooms, transition)].append({
                    "log_premium": math.log(high_pm2 / low_pm2),
                    "low_pm2": low_pm2,
                    "floor_total": (float(low["_floor_total"]) + float(high["_floor_total"])) / 2,
                    "lat": (float(low["_lat"]) + float(high["_lat"])) / 2,
                    "lon": (float(low["_lon"]) + float(high["_lon"])) / 2,
                    "has_complex": bool(low.get("_complex_key") or high.get("_complex_key")),
                    "low_id": str(low.get("id") or ""),
                    "high_id": str(high.get("id") or ""),
                })

    signals = []
    for (address, rooms, transition), pairs in raw.items():
        low_pm2 = statistics.median(pair["low_pm2"] for pair in pairs)
        premium = statistics.median(pair["log_premium"] for pair in pairs)
        representative = min(pairs, key=lambda pair: abs(pair["log_premium"] - premium))
        signals.append({
            "address": address,
            "rooms": rooms,
            "transition": transition,
            "log_premium": premium,
            "base_class_score": class_score(low_pm2, rooms, room_bounds, q25, q75),
            "floor_total": statistics.median(pair["floor_total"] for pair in pairs),
            "lat": statistics.median(pair["lat"] for pair in pairs),
            "lon": statistics.median(pair["lon"] for pair in pairs),
            "has_complex": round(statistics.median(int(pair["has_complex"]) for pair in pairs)) == 1,
            "pair_count": len(pairs),
            "low_id": representative["low_id"],
            "high_id": representative["high_id"],
        })
    return signals


def strict_premium_evidence(
    target: dict[str, Any],
    pool: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    class_score: Callable[[float, str, dict[str, tuple[float, float]], float, float], float | None],
    room_bounds: dict[str, tuple[float, float]],
    q25: float,
    q75: float,
) -> dict[str, Any] | None:
    """Вернуть премию только при полном наборе строгих доказательств."""
    target_value = usable_interior_score(target)
    address = _address(target)
    target_area = _number(target.get("square_m2"))
    if target_value is None or target_value[1] < 1.0 or address is None or not target_area:
        return None
    target_band = repair_band(target_value[0])
    transition = f"4_to_{target_band}"
    if transition not in ALLOWED_TRANSITIONS:
        return None
    required = ("_rooms", "_floor_total", "_lat", "_lon")
    if any(target.get(field) is None for field in required):
        return None

    weak_rows = []
    target_hash = str(target.get("photo_set_hash") or "")
    for row in pool:
        if _address(row) != address or str(row.get("_rooms")) != str(target.get("_rooms")):
            continue
        usable = usable_interior_score(row)
        area = _number(row.get("square_m2"))
        price_m2 = _number(row.get("_price_m2"))
        if usable is None or repair_band(usable[0]) != "4" or not area or not price_m2:
            continue
        if max(area, target_area) / min(area, target_area) > MAX_AREA_RATIO:
            continue
        row_hash = str(row.get("photo_set_hash") or "")
        if target_hash and row_hash == target_hash:
            continue
        weak_rows.append(row)
    if not weak_rows:
        return None

    weak_pm2 = statistics.median(float(row["_price_m2"]) for row in weak_rows)
    target_class = class_score(
        weak_pm2, str(target["_rooms"]), room_bounds, q25, q75
    )
    if target_class is None:
        return None

    donors = []
    for signal in signals:
        if signal["address"] == address:
            continue
        if signal["rooms"] != str(target["_rooms"]):
            continue
        if signal["transition"] != transition:
            continue
        if signal["has_complex"] != bool(target.get("_complex_key")):
            continue
        donor_class = signal.get("base_class_score")
        if donor_class is None or abs(donor_class - target_class) > STRICT_CLASS_DELTA:
            continue
        if abs(float(signal["floor_total"]) - float(target["_floor_total"])) > STRICT_FLOOR_GAP:
            continue
        distance = _haversine_km(
            float(target["_lat"]), float(target["_lon"]),
            float(signal["lat"]), float(signal["lon"]),
        )
        if distance > STRICT_RADIUS_KM:
            continue
        donors.append({**signal, "distance_km": distance})
    if len(donors) < MIN_DONOR_BUILDINGS:
        return None

    logs = [float(donor["log_premium"]) for donor in donors]
    premium_log = statistics.median(logs)
    dispersion = statistics.median(abs(value - premium_log) for value in logs)
    confidence = (len(donors) / (len(donors) + 3.0)) * math.exp(-dispersion / 0.20)
    return {
        "transition": transition,
        "premium_log": premium_log,
        "premium_pct": (math.exp(premium_log) - 1.0) * 100.0,
        "donor_buildings": len(donors),
        "weak_base_n": len(weak_rows),
        "weak_base_pm2": weak_pm2,
        "confidence": confidence,
        "dispersion_log": dispersion,
        "donors": [
            {
                "address": donor["address"],
                "low_id": donor["low_id"],
                "high_id": donor["high_id"],
                "premium_pct": (math.exp(donor["log_premium"]) - 1.0) * 100.0,
                "distance_km": donor["distance_km"],
                "class_delta": abs(donor["base_class_score"] - target_class),
            }
            for donor in sorted(donors, key=lambda donor: donor["distance_km"])
        ],
    }
