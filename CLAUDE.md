# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Note: nearly all source comments and docstrings are written in Russian (explaining *why*, in detail — this codebase leans heavily on long rationale comments instead of external docs). This file is in English; match up terms as needed.

## What this is

A personal pipeline that scores krisha.kz rental listings (**Almaty**) against a frozen "fair price" baseline and pushes good deals / suspicious listings to Telegram. Deployed on Render as a Web Service: the process still runs background loops and also exposes the interior-result exchange API (see `render.yaml`, `README.md`).

This is the Almaty instance. A sibling deployment covers Astana from the same codebase but a separate repo, separate collector/cleaner services, separate bot and separate disk — the two never share state. Anything city-specific here (the coordinate box in `stage3_benchmark_v3.py`, the Stage 2 prompt, the baseline filename) is hardcoded to Almaty, not parameterised.

This repo is **one of three services** and deliberately does very little on its own:

```
rieltorcollector ──(event feed: new / price_drop)──┐
                                                   ├──> THIS REPO ──> Telegram
rieltor-cleaner  ──(prebuilt baseline, every 12h)──┘
```

It does **not** scrape krisha.kz (that is `rieltorcollector`'s job) and does **not** build the baseline (that is `rieltor-cleaner`'s job). Both are external HTTP services reached with an `X-API-Key` header.

## Commands

There is a stdlib `unittest` suite and no build step, linter, or formatter.

```bash
pip install -r requirements.txt

# Run the orchestrator (needs .env — see .env.example):
python orchestrator_v7.py

# Run all unit tests (no real network/OpenAI calls):
python -m unittest discover -v

# Measure scoring quality, old Stage 3 vs new, on a hold-out sample:
python eval_stage3.py --targets 2500 --seeds 1
python eval_stage3.py --targets 2500 --seeds 1 --attribution   # break the effect down per fix

# Run the incoming cleaner standalone, for debugging one stage:
python incoming_clean_v2.py --input <raw.csv> --output <clean.csv> --cache <cache.json> --concurrency 4
```

`eval_stage3.py` compares `stage3_benchmark_v3.py` (new) against `stage3_benchmark_v3_old.py` (the previous version, kept for exactly this) — the latter is not dead code.

The only direct third-party runtime dependencies are `openai` and its pinned
`aiohttp` transport. Renovation photos are sent as
original bytes with `detail=low`; they are held only for the current request and
are not stored persistently.

## Architecture

### Data flow per cycle

`collector_job()` → durable decision queue → `interior_decision_loop()` →
`process_and_notify()` in `orchestrator_v7.py` is the actual sequencing; read it
before changing any one stage's contract with its neighbors.

```
GET /listings/changes?since=<event_id>&limit=<n>   # cursor feed, reasons new/price_drop
GET /listings/{id}                                  # full card per event
  → InteriorGateway             exact baseline/temp lookup or durable enqueue
  → enqueue_decision()          persist fresh card BEFORE advancing cursor
  → interior_decision_loop()    wait for terminal result or bounded timeout
  → clean_incoming_rows()      subprocess: incoming_clean_v2.py (SOFT cleaning)
  → score_incoming()           in-process: stage3_benchmark_v3.py vs the baseline
  → candidate_is_sendable()    verdict filter
  → dedupe_against_registry()  ever_sent_ids.json
  → append_notifications() + save_registry()   durable write BEFORE Telegram (outbox-first)
  → notify_telegram()          best-effort, outside the registry lock
```

The feed cursor is persisted in `collector_state.json`. Network/auth/card-fetch
failure or failure of either durable enqueue leaves the cursor unchanged. Once a
fresh decision row is committed, the cursor may advance: Stage 2/3 failures are
then retried from SQLite per card and survive restarts. The 12-hour event-age
window applies only to Stage 2/3/Telegram: the interior gateway sees the entire
available event feed. A clean deployment therefore uses
`COLLECTOR_START_FROM=beginning`.

### Code vs data: `BASE_DIR` and `DATA_DIR`

`BASE_DIR` is where the code lives (all `*.py` sit flat in the repo root). `DATA_DIR` (env `KRISHA_DATA_DIR`, defaults to `BASE_DIR`) is where everything mutable lives: `ever_sent_ids.json`, `collector_state.json`, `notifications_log_v4.csv`, `prediction_audit.sqlite3`, `telegram_access.json`, `telegram_details.sqlite3`, the two interior SQLite files, `baseline/`, and `cache/`.

On Render `DATA_DIR=/var/data` is a mounted persistent disk while the code directory is recreated on every deploy. **Any new state file must go under `DATA_DIR`**, or it will be silently wiped on the next deploy — and for the dedup registry specifically that means re-sending everything already sent.

### The baseline is not built here

`baseline/krisha_almaty_baseline.csv` is downloaded whole from `rieltor-cleaner-almaty` (`GET /baseline/clean.csv`) every 12h and atomically replaces the local file. It is **not** in git (28 MB, regenerates constantly) and is **never** written to by the pipeline. `prepare_baseline()` = load + `enrich()` + sanity-filter outliers.

Until the file exists, `collector_job()` skips ticks with a log line rather than failing — that is the normal state for the first minute or two after a fresh deploy.

### Scoring (`stage3_benchmark_v3.py`)

Builds a cohort per listing from the baseline pool at decreasing precision: same complex (L1/2) → same street+house (L2b) → same street+price-segment (L3) → radius 1km/3km (L4/L5) → citywide (L6). Produces a verdict plus separate `value_score` (price attractiveness) and `quality_evidence_score` (how much is actually known about the unit) — a low price alone is never read as "good" without evidence.

`benchmark_confidence` = `level_factor × size_factor × dispersion_factor × homogeneity_factor` (see `confidence_from_cohorts()`). It gates the verdict thresholds: `FIND_THRESHOLD_{HIGH,MED,LOW}_CONF` are 0.17 / 0.21 / 0.27, selected by `CONFIDENCE_TIER_HIGH = 0.60` / `CONFIDENCE_TIER_MED = 0.49`. Anything that shrinks cohorts lowers confidence, which raises the find threshold, which quietly costs НАХОДКА verdicts — check that interaction when touching cohort selection.

### Two cleaning paths that must not be conflated

- `incoming_clean_v2.py` — **soft** cleaning for live candidates: never drops a row for market reasons (price, cohort, seller, red flags, missing photo). Bad data becomes an `incoming_data_warnings` column instead, because Stage 3 is designed to score thin listings with reduced confidence rather than silently excluding them. It imports `stage2_llm_analyze.analyze_all` as a library call, not as a subprocess. If every row comes back `llm_skipped_error` it exits with code 2 (`Stage2Unavailable`) rather than producing a biased score, and the orchestrator then aborts the cycle instead of sending anything.
- Hard cleaning (dropping rows outright) belongs to `rieltor-cleaner`, not here.

### State files (under `DATA_DIR`)

- `ever_sent_ids.json` — dedup registry (`{id: {price, reason, sent_at}}`). A candidate is re-sent only if its id is new or its price is *strictly lower* than at last send. `load_registry()` normalizes `price` to `float` on read — historically this was compared as a string and silently produced wrong decisions (`'90000.0' > '190000.0'` lexicographically); don't reintroduce a raw-string comparison.
- `collector_state.json` — the feed cursor (`last_event_id`).
- `baseline_state.json` — `X-Built-At` of the last applied baseline, so the same file isn't reapplied.
- `notifications_log_v4.csv` — append-only log of what was sent, written *before* the Telegram call. v4 adds the interior adjustment audit fields. Schema is versioned (`NOTIFICATIONS_FIELDNAMES`); `append_notifications()` refuses to append to a file with a different header rather than silently corrupting it — bump the filename instead of changing columns in place.
- `prediction_audit.sqlite3` — compact append-only forecast history for every scored Collector event, including rows that were not sent to Telegram. `collector_event_id` is the idempotency key; later price-drop events for the same listing are separate rows.
- `telegram_access.json` — tiny admin/access registry plus the `getUpdates` offset and global Telegram delivery modes. It deliberately stores no Telegram message history or listing payloads.
- `telegram_details.sqlite3` — bounded on-demand cache for the formatted cohort button: at most 500 texts, automatically expired after 1 hour; no photos or Telegram updates.
- `interior_online.sqlite3` — temporary durable queue/outbox keyed by `listing_id + photo_set_hash + evaluator_version`; only ACKed rows are garbage-collected after 24 hours.
- `interior_content_cache.sqlite3` — content-based evaluator cache. Image bytes themselves are never stored.

Everything under `cache/` is scratch for the current cycle and safe to delete between runs.

## Secrets

All secrets come from the environment, loaded from `.env` locally (see `.env.example`) and from the Render dashboard in production. There are no hardcoded credentials in the code and none should be added.

A live Telegram bot token was previously hardcoded in `orchestrator_v7.py` and remains in git history. If the repo is public, treat it as leaked and rotate it via @BotFather.

## `for_ms3_*`

Reference material for the external collector microservice (`for_ms3_CLAUDE.md`, `for_ms3_README.md`, an example `for_ms3_baseline_api.py`, example JSON payloads). It documents the `/listings/changes` and `/listings/{id}` endpoints this repo consumes. Treat it as an integration spec to read, not code to run or extend in place.
