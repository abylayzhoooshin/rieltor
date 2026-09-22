# -*- coding: utf-8 -*-
"""
Замер качества оценки Stage 3: старая версия против новой, на одних и тех же
объявлениях, в трёх сценариях данных.

Цели ВЫВЕДЕНЫ из пула (честный hold-out): каждая оценивается по остальным
объявлениям, как оценивалось бы новое. Истина для цели — её собственная цена
за м². Ошибка |diff_pct|; меньше — лучше.

Запуск:
    python eval_stage3.py [--old путь] [--new путь] [--targets 2500] [--seeds 2]

Сценарии:
    чистый           пул как есть
    грязный          у 15% объявлений пула цена x1.6 или x0.6
    тонкий+грязный   пул урезан до 35% И у 15% цена испорчена

Метрики:
    p50 / p75 / p90     квантили ошибки
    >=35%               доля грубых ошибок
    НАХОД / ПЕРЕОЦ      число вердиктов
    смещ.               среднее diff_pct (0 — модель не смещена)
    размах 1к/2к/3к     систематический перекос по площади: медиана diff_pct
                        в 5 группах по площади внутри комнатности, максимум
                        минус минимум. Размер квартиры не должен влиять на
                        вердикт, так что чем ближе к нулю, тем лучше.
"""
import argparse
import collections
import importlib.util
import math
import os
import random
import statistics
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "baseline", "krisha_astana_baseline.csv")


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(q * len(v)))]


def contaminate(pool, share, seed):
    rnd = random.Random(seed)
    out = []
    for r in pool:
        if rnd.random() < share:
            c = dict(r)
            f = 1.6 if rnd.random() < 0.5 else 0.6
            c["price"] = float(c["price"]) * f
            c["_price_m2"] = c["_price_m2"] * f
            out.append(c)
        else:
            out.append(r)
    return out


def thin(pool, frac, seed):
    rnd = random.Random(seed)
    return [r for r in pool if rnd.random() < frac]


def build_ctx(m, pool):
    q25, q75 = m.price_segment_boundaries(pool)
    rb = m.room_segment_boundaries(pool)
    si = m.building_scores_index(pool, q25, q75, rb)
    ctx = {
        "pool": pool, "q25": q25, "q75": q75, "rb": rb, "si": si,
        "city": m.citywide_median_by_rooms(pool),
        "cp": m.prior_medians_by_rooms_class(pool, si),
        "sl": m.area_slopes_by_rooms(pool),
        "sp": m.SpatialIndex(pool), "bi": m.BuildingIndex(pool),
    }
    if hasattr(m, "median_area_by_rooms"):
        ctx["ref"] = m.median_area_by_rooms(pool)
    return ctx


def score_all(m, ctx, targets):
    out = {}
    kw = {}
    for t in targets:
        if "ref" in ctx:
            kw["ref_areas"] = ctx["ref"]
        r = m.score_row(
            t, ctx["pool"], ctx["city"], ctx["si"], ctx["q25"], ctx["q75"],
            soft_target=False, room_bounds=ctx["rb"], class_priors=ctx["cp"],
            area_slopes=ctx["sl"], spatial_index=ctx["sp"],
            building_index=ctx["bi"], **kw,
        )
        ok = r.get("status") == "scored" and r.get("diff_pct") is not None
        sq = m.to_float(t.get("square_m2"))
        out[t["id"]] = {
            "rooms": t["_rooms"], "sq": sq, "ok": ok,
            "diff": r.get("diff_pct") if ok else None,
            "verdict": r.get("verdict"),
            "conf": r.get("benchmark_confidence"),
        }
    return out


def report(title, results, buckets, targets_dict=None):
    names = list(results)
    common = [k for k in results[names[0]] if all(results[n][k]["ok"] for n in names)]
    print(f"\n{'=' * 104}\n{title}\n{'=' * 104}")
    print(f"целей {len(results[names[0]])}, оценены ВСЕМИ вариантами: {len(common)}\n")
    print(f"{'вариант':34s}{'покр.':>7s}{'p50':>8s}{'p75':>8s}{'p90':>8s}{'>=35%':>7s}"
          f"{'НАХОД':>7s}{'ПЕРЕОЦ':>8s}{'смещ.':>8s}{'р.1к':>7s}{'р.2к':>7s}{'р.3к':>7s}")
    for n in names:
        R = results[n]
        cov = sum(1 for k in R if R[k]["ok"]) / len(R)
        e = [abs(R[k]["diff"]) for k in common]
        rg = []
        for rm in ("1", "2", "3"):
            ks = sorted([k for k in common if R[k]["rooms"] == rm and R[k]["sq"]],
                        key=lambda k: R[k]["sq"])
            if len(ks) < 100:
                rg.append("—")
                continue
            q = len(ks) // 5
            md = [statistics.median(R[k]["diff"] for k in (ks[i*q:(i+1)*q] if i < 4 else ks[4*q:]))
                  for i in range(5)]
            rg.append(f"{max(md) - min(md):.3f}")
        print(f"{n:34s}{100*cov:>6.1f}%{pct(e,.5):>8.4f}{pct(e,.75):>8.4f}{pct(e,.9):>8.4f}"
              f"{100*sum(1 for x in e if x >= .35)/len(e):>6.1f}%"
              f"{sum(1 for k in common if R[k]['verdict']=='НАХОДКА'):>7d}"
              f"{sum(1 for k in common if R[k]['verdict']=='ПЕРЕОЦЕНЕНА'):>8d}"
              f"{statistics.mean(R[k]['diff'] for k in common):>+8.3f}"
              f"{rg[0]:>7s}{rg[1]:>7s}{rg[2]:>7s}")

    order = ("0", "1-2", "3-5", "6+")
    cnt = {b: [k for k in common if buckets[k] == b] for b in order}
    print(f"\nмедианная ошибка по числу объявлений той же комнатности в доме цели:")
    print(f"{'вариант':34s}" + "".join(f"{b:>10s}" for b in order))
    print(f"{'  (число целей)':34s}" + "".join(f"{len(cnt[b]):>10d}" for b in order))
    for n in names:
        R = results[n]
        row = f"{n:34s}"
        for b in order:
            ks = cnt[b]
            row += (f"{pct([abs(R[k]['diff']) for k in ks], .5):>10.4f}"
                    if len(ks) >= 20 else f"{'—':>10s}")
        print(row)

    # Примеры изменившихся вердиктов
    if len(names) >= 2 and targets_dict:
        old_results = results[names[0]]
        new_results = results[names[1]]
        changes = []
        for k in common:
            if old_results[k]["verdict"] != new_results[k]["verdict"]:
                changes.append((k, old_results[k], new_results[k]))

        if changes:
            print(f"\n--- примеры изменившихся вердиктов ({len(changes)} всего) ---")
            # группировать: НАХОДКА->*, *->НАХОДКА, остальное
            finding_lost = [c for c in changes if c[1]["verdict"] == "НАХОДКА"]
            finding_gained = [c for c in changes if c[2]["verdict"] == "НАХОДКА"]
            other = [c for c in changes if c[1]["verdict"] != "НАХОДКА" and c[2]["verdict"] != "НАХОДКА"]

            for label, group in (("НАХОДКА -> (потеряна)", finding_lost),
                                 ("-> НАХОДКА (новая)", finding_gained),
                                 ("другие изменения", other)):
                if group:
                    print(f"\n{label}: {len(group)}")
                    for k, old, new in group[:8]:
                        t = targets_dict.get(k, {})
                        rooms = t.get("_rooms", "?")
                        sq = t.get("square_m2", "?")
                        price = t.get("price", "?")
                        print(f"  {str(k):12s}  {rooms}к {sq}м² {price}₸  "
                              f"{old['verdict']:15s} -> {new['verdict']:15s}  "
                              f"diff {old['diff']:+.3f} -> {new['diff']:+.3f}  "
                              f"conf {old['conf']:.2f} -> {new['conf']:.2f}")

    return common


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default=os.path.join(ROOT, "stage3_benchmark_v3_old.py"))
    ap.add_argument("--new", default=os.path.join(ROOT, "stage3_benchmark_v3.py"))
    ap.add_argument("--targets", type=int, default=2500)
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--attribution", action="store_true",
                    help="разложить эффект новой версии по правкам")
    args = ap.parse_args()

    old = load_module(args.old, "s3_old")
    new = load_module(args.new, "s3_new")
    _, usable_old = old.prepare_baseline(BASE)
    _, usable_new = new.prepare_baseline(BASE)
    by_id_new = {r["id"]: r for r in usable_new}

    lower_median = lambda v: sorted(v)[(len(v) - 1) // 2]
    real_median = new.cohort_median
    real_window = new.AREA_WINDOW_RATIO

    def variants():
        yield "СТАРАЯ версия", old, None
        yield "НОВАЯ версия", new, {}
        if args.attribution:
            yield "новая, без окна по площади", new, {"AREA_WINDOW_RATIO": None}
            yield "новая, окно ±15%", new, {"AREA_WINDOW_RATIO": 1.15}
            yield "новая, окно ±25%", new, {"AREA_WINDOW_RATIO": 1.25}
            yield "новая, старая медиана (нижняя)", new, {"cohort_median": lower_median}

    for seed_i in range(args.seeds):
        seed = 20260920 + seed_i
        rnd = random.Random(seed)
        ids = [r["id"] for r in rnd.sample(usable_old, args.targets)]
        idset = set(ids)
        scen = []
        base_old = [r for r in usable_old if r["id"] not in idset]
        base_new = [r for r in usable_new if r["id"] not in idset]
        scen.append(("ЧИСТЫЙ ПУЛ", base_old, base_new))
        d_old, d_new = contaminate(base_old, .15, seed + 1), contaminate(base_new, .15, seed + 1)
        if seed_i == 0:
            scen.append(("ГРЯЗНЫЙ ПУЛ (15% цен x1.6 / x0.6)", d_old, d_new))
            t_old = contaminate(thin(base_old, .35, seed + 2), .15, seed + 3)
            t_new = contaminate(thin(base_new, .35, seed + 2), .15, seed + 3)
            scen.append(("ТОНКИЙ+ГРЯЗНЫЙ (35% пула, 15% цен испорчено)", t_old, t_new))

        for title, pool_old, pool_new in scen:
            t0 = time.time()
            c_old, c_new = build_ctx(old, pool_old), build_ctx(new, pool_new)
            tg_old = [r for r in usable_old if r["id"] in idset]
            tg_new = [by_id_new[i] for i in ids if i in by_id_new]
            targets_by_id = {t["id"]: t for t in tg_new}
            buckets = {}
            for t in tg_new:
                mem = c_new["bi"].members(t, c_new["sp"])
                n = sum(1 for x in mem if x["_rooms"] == t["_rooms"])
                buckets[t["id"]] = "0" if n == 0 else ("1-2" if n <= 2 else ("3-5" if n <= 5 else "6+"))
            res = {}
            for name, mod, patch in variants():
                if mod is old:
                    res[name] = score_all(old, c_old, tg_old)
                else:
                    saved = {}
                    for k, v in (patch or {}).items():
                        saved[k] = getattr(new, k)
                        setattr(new, k, v)
                    try:
                        res[name] = score_all(new, c_new, tg_new)
                    finally:
                        for k, v in saved.items():
                            setattr(new, k, v)
                print(f"  [{title}] seed {seed}: {name} {time.time()-t0:.0f}c", flush=True)
            report(f"{title}   (seed {seed})", res, buckets, targets_by_id)


if __name__ == "__main__":
    main()
