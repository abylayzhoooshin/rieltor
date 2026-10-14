"""Воспроизводимые случайная и граничная выборки фотооценки."""

import argparse
import asyncio
import csv
import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from renovation_store import RenovationStore
from renovation_worker import (
    parse_photo_urls,
    process_listing,
)
from run_renovation_standalone import BASE_DIR, ResponsesHTTPClient


def local_key():
    key = (
        os.environ.get("RENOVATION_OPENAI_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    if not key and (BASE_DIR / ".env").exists():
        values = {}
        for line in (BASE_DIR / ".env").read_text(encoding="utf-8-sig").splitlines():
            name, sep, value = line.strip().partition("=")
            if sep:
                values[name.strip()] = value.strip().strip("\"'")
        key = values.get("RENOVATION_OPENAI_API_KEY", "") or values.get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("RENOVATION_OPENAI_API_KEY/OPENAI_API_KEY отсутствует")
    return key


async def run(args):
    key = local_key()
    data_dir = Path(os.environ.get("KRISHA_DATA_DIR", BASE_DIR))
    baseline = data_dir / "baseline" / "krisha_almaty_baseline.csv"
    rows = {}
    malformed = 0
    with baseline.open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            if not row.get("id"):
                continue
            try:
                row["parsed_photos"] = parse_photo_urls(row.get("photo_urls"))
            except ValueError:
                malformed += 1
                continue
            rows[str(row["id"])] = row
    population = sorted(rows.values(), key=lambda row: str(row["id"]))
    rng = random.Random(args.seed)
    if args.mode == "random":
        sampled = [("random", row) for row in rng.sample(population, 10)]
        used = {row["id"] for _, row in sampled}
        sampled += [("many_photos", row) for row in sorted(
            (row for row in population if row["id"] not in used),
            key=lambda row: (-len(row["parsed_photos"]), str(row["id"])),
        )[:2]]
    elif args.mode == "edge":
        sampled = []
        used = set()

        def add_exact(count, amount, label):
            candidates = [row for row in population
                          if row["id"] not in used
                          and row.get("status") == "active"
                          and len(row["parsed_photos"]) == count]
            chosen = rng.sample(candidates, min(amount, len(candidates)))
            for row in chosen:
                used.add(row["id"])
                sampled.append((label, row))

        add_exact(0, 1, "no_photos")
        add_exact(1, 2, "one_photo")
        add_exact(2, 2, "two_photos")
        add_exact(3, 2, "three_photos")
        add_exact(4, 1, "four_photos")
        add_exact(8, 1, "thin_control")
        add_exact(15, 1, "normal_control")
        for listing_id, label in (("1001415963", "duplicate_heavy"),
                                  ("1015797426", "common_areas_first")):
            row = rows.get(listing_id)
            if row and row["id"] not in used:
                used.add(row["id"])
                sampled.append((label, row))
    elif args.mode == "common-areas":
        # Пользователь заметил, что первые 30 кадров этой карточки показывают
        # ЖК и холл. Синтетический ID не перезаписывает binding полной карточки.
        source = rows["1015797426"]
        common_only = dict(source)
        common_only["id"] = "1015797426-common-areas-only"
        common_only["parsed_photos"] = source["parsed_photos"][:30]
        sampled = [("common_areas_only", common_only)]
    else:
        sampled = [
            ("duplicate_heavy", rows["1001415963"]),
            ("common_areas_first", rows["1015797426"]),
        ]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = data_dir / "interior" / f"sample_{stamp}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {"seed": args.seed, "mode": args.mode, "baseline": str(baseline),
              "population": len(population), "malformed_rows": malformed,
              "started_at": stamp, "results": []}
    for group, row in sampled:
        report["results"].append({"id": row["id"], "group": group,
                                  "photo_urls_count": len(row["parsed_photos"]),
                                  "url": row.get("url") or f"https://krisha.kz/a/show/{row['id']}",
                                  "status": "pending"})

    def save():
        temporary = report_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(report_path)

    save()
    print(f"Report: {report_path}", flush=True)
    print("Selected: " + json.dumps(report["results"], ensure_ascii=False), flush=True)
    client = ResponsesHTTPClient(key)
    started = time.perf_counter()
    with RenovationStore(data_dir / "interior" / "renovation.sqlite3") as store:
        for result, (_, row) in zip(report["results"], sampled):
            tick = time.perf_counter()
            before = client.calls
            try:
                value = await process_listing(store=store, client=client,
                    listing_id=row["id"], price=float(row["price"]),
                    photo_urls=row["parsed_photos"], event_reason="sample_test")
                payload = dict(value.pop("payload", {}))
                payload.pop("photo_manifest", None)
                result.update(value)
                result["payload"] = payload
            except Exception as exc:
                result["status"] = "error"
                result["error"] = f"{type(exc).__name__}: {str(exc)}".replace(key, "[REDACTED]")[:1000]
            result["seconds"] = round(time.perf_counter() - tick, 2)
            result["http_calls"] = client.calls - before
            report["elapsed_seconds"] = round(time.perf_counter() - started, 2)
            report["http_calls"] = client.calls
            save()
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if result.get("status") == "error" and any(
                code in result.get("error", "") for code in ("OpenAI HTTP 401", "OpenAI HTTP 403", "OpenAI HTTP 429")
            ):
                print("Stopping after API authentication/quota/rate-limit error.", flush=True)
                break
    print(f"Finished: {report_path}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20261002)
    parser.add_argument(
        "--mode",
        choices=("random", "edge", "common-areas", "large"),
        default="random"
    )
    asyncio.run(run(parser.parse_args()))
