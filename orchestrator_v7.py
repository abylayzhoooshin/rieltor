"""
Оркестратор — опрашивает внешний сервис rieltorcollector (отдельный
микросервис, который сам ходит на krisha.kz) за новыми/подешевевшими
объявлениями и прогоняет их через СУЩЕСТВУЮЩУЮ, НЕИЗМЕНЁННУЮ логику
оценки (pipeline/incoming_clean_v2.py -> pipeline/stage3_benchmark_v3.py).

Раньше здесь было два своих скрапера (fast track / slow track),
запускавшихся subprocess'ом по расписанию, и оркестратор читал их
выходные CSV. Сбор данных с krisha.kz теперь целиком ответственность
rieltorcollector; этот сервис только потребляет его API:
    GET /listings/changes?since=<event_id>&limit=<n>  — курсорная лента
        событий "new"/"price_drop" (без полных карточек, чтобы ответ
        оставался лёгким).
    GET /listings/{id}                                 — полная карточка
        объявления по id.
Обе точки задокументированы в for_ms3/ (baseline_api.py — код сервера,
README.md/CLAUDE.md — почему лента устроена именно так).

    1. Раз в COLLECTOR_POLL_INTERVAL_SEC опрашивает /listings/changes,
       докачивает полную карточку по каждому событию через /listings/{id}.
    2. Передаёт карточки (тонкий адаптер полей, без изменения самой
       оценки) в process_and_notify — тот же путь, что раньше получал
       строки из CSV воркеров.
    3. Финально дедупит результат через общий реестр ever_sent_ids.json
       и решает, что реально уйдёт пользователю.

Курсор ленты (last_event_id) хранится персистентно в collector_state.json
(атомарная запись tmp+os.replace, как ever_sent_ids.json) — переживает
рестарт процесса.

ДЕДУП — ГЛАВНАЯ ЗАДАЧА ЭТОГО СЛОЯ (закрывает камень №2 — bump-дубли):
    ever_sent_ids.json = {id: {"price": <цена на момент последней
    отправки>, "reason": ..., "sent_at": ...}}

    Правило: попавший из ЛЮБОГО трека кандидат (new или price_drop)
    реально уходит пользователю, только если:
      - этого id ещё не было в реестре (первая отправка), ИЛИ
      - его текущая цена СТРОГО МЕНЬШЕ цены на момент последней отправки.

    Это единое правило вместо двух (new/price_drop) само по себе решает:
      - bump без изменения цены (объявление просто снова попало в топ-5
        страниц, fast track снова видит его как "new") → цена не ниже
        последней отправленной → НЕ шлём повторно;
      - объявление продолжает дешеветь несколько раз подряд → каждое
        падение ниже последней отправленной цены → шлём каждый раз,
        это и есть смысл всего мониторинга;
      - slow track и fast track нашли одно и то же падение независимо
        друг от друга (могло быть) → второй раз это уже не даст цену
        ниже уже отправленной → дубль сам погасится.

ДОСТАВКА В TELEGRAM:
    Включена как best-effort слой ПОВЕРХ уже описанного outbox-first
    дедупа, а не вместо него. Порядок строгий:
        1. кандидат проходит dedupe_against_registry;
        2. СНАЧАЛА durable-запись в notifications_log.csv и
           ever_sent_ids.json (registry_lock уже отпущен после этого);
        3. ТОЛЬКО ПОТОМ попытка отправки в Telegram.
    Если шаг 3 не удался (сеть, неверный токен, flood control после всех
    ретраев) — объявление НЕ уйдёт повторно на следующем цикле, потому что
    с точки зрения registry оно уже "отправлено". notifications_log.csv
    остаётся источником правды и полной историей на случай ручной сверки/
    досылки — но сам Telegram-канал это не гарантирует, только best-effort.

    Токен бота и chat_id читаются из переменных окружения
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. Если хотя бы одна не задана,
    Telegram-отправка тихо выключается (лог и registry продолжают
    работать как раньше, ничего не падает).

ЧТО ЭТОТ СЛОЙ НЕ ДЕЛАЕТ (сознательно, пока не нужно):
    - не шлёт email — второй канал доставки не подключён;
    - сам не ходит на krisha.kz ни в каком виде — это целиком
      ответственность rieltorcollector;
    - не строит baseline сам — раз в 12 часов скачивает готовый чистый
      файл с rieltor-cleaner (GET /baseline/clean.csv, X-API-Key) и
      целиком заменяет им baseline/krisha_almaty_baseline.csv.

Запуск:
    python orchestrator_v7.py
    (Ctrl+C — остановить; текущая страница событий доработает до конца
    перед остановкой)

    Настройки и ключи берутся из файла .env рядом с этим скриптом
    (строки вида ИМЯ=значение). Переменные окружения, заданные вручную,
    имеют приоритет над .env. Нужны: RIELTOR_COLLECTOR_URL,
    RIELTOR_COLLECTOR_API_KEY, RIELTOR_CLEANER_API_KEY, OPENAI_API_KEY.
"""

import asyncio
import csv
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

from interior_api import start_interior_api
from interior_online_store import CleanerBaselineIndex, InteriorOnlineStore, TERMINAL_STATUSES
from interior_service import InteriorAuthenticationError, InteriorGateway, InteriorQueueWorker
from renovation_store import RenovationStore
from telegram_access import TelegramAccessStore, TelegramDetailsStore


# Windows may expose CP1251 stdout even in a modern PowerShell terminal.  The
# service logs contain Russian text and a few Unicode status symbols; one such
# symbol must never abort a collector cycle before its cursor is persisted.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _load_dotenv(path):
    """Читает ИМЯ=значение из .env в os.environ, не перетирая уже заданные
    переменные. Без внешних зависимостей. Пустые строки и строки с # игнорируются,
    кавычки вокруг значения снимаются. Подпроцессы (Stage 2) наследуют
    os.environ, так что ключ OpenAI до них доходит."""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f.read().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name, value = name.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if name and name not in os.environ:
                os.environ[name] = value


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ============================== ПУТИ (под структуру папок пользователя) ==============================

BASE_DIR = os.environ.get("KRISHA_BASE_DIR", os.path.dirname(os.path.abspath(__file__)))

# КОД и ДАННЫЕ разделены намеренно.
#
# BASE_DIR — где лежит сам код (pipeline/*.py). На Render это слепок
# репозитория: он пересоздаётся при каждом деплое, и всё, что туда
# записано, теряется.
#
# DATA_DIR — где лежит всё, что должно пережить рестарт и деплой:
# реестр отправленных (ever_sent_ids.json), курсор ленты
# (collector_state.json), журнал (notifications_log_v4.csv), скачанный
# baseline и cache/. На Render это точка монтирования постоянного диска
# (KRISHA_DATA_DIR=/var/data). Локально переменная не задана, и DATA_DIR
# совпадает с BASE_DIR — то есть прежнее поведение и прежние пути к
# файлам сохраняются один в один.
#
# Без этого разделения реестр отправленных обнулялся бы при каждом
# деплое, и бот заново рассылал бы все объявления, которые уже отправлял.
DATA_DIR = os.environ.get("KRISHA_DATA_DIR", BASE_DIR)

# Плоская раскладка: все stage-скрипты лежат рядом с оркестратором, в
# корне репозитория. Подкаталоги остались только у ДАННЫХ — эталон в
# baseline/, временные файлы прогона в cache/ (оба создаются сами и в
# git не попадают, поэтому загрузку файлов списком не усложняют).
PIPELINE_DIR = BASE_DIR
BASELINE_DIR = os.path.join(DATA_DIR, "baseline")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(BASELINE_DIR, exist_ok=True)

BASELINE_CSV = os.path.join(BASELINE_DIR, "krisha_almaty_baseline.csv")
STAGE3_OUTPUT_COLLECTOR_CSV = os.path.join(CACHE_DIR, "stage3_incoming_collector_latest.csv")
STAGE3_MODULE = os.path.join(PIPELINE_DIR, "stage3_benchmark_v3.py")
INCOMING_CLEAN_SCRIPT = os.path.join(PIPELINE_DIR, "incoming_clean_v2.py")
INCOMING_CACHE_COLLECTOR = os.path.join(CACHE_DIR, "incoming_llm_analysis_cache_collector.json")

# Файлы САМОГО оркестратора.
EVER_SENT_IDS_FILE = os.path.join(DATA_DIR, "ever_sent_ids.json")
# Персистентный курсор ленты событий rieltorcollector (last_event_id).
# Переживает рестарт процесса — без этого при каждом старте пришлось бы
# заново заказывать since=0, то есть перечитывать всю историю событий.
COLLECTOR_STATE_FILE = os.path.join(DATA_DIR, "collector_state.json")
# v3-схема несовместима с notifications_log_v2.csv (там есть
# base_price_m2_corrected и нет review_flags/robust_z), а
# append_notifications намеренно отказывается дописывать в файл с чужим
# заголовком. Заводим новый файл; v2 остаётся нетронутым как история.
NOTIFICATIONS_LOG_CSV = os.path.join(DATA_DIR, "notifications_log_v4.csv")
# Компактный неизменяемый журнал ВСЕХ прогнозов по событиям Collector, а
# не только квартир, отправленных в Telegram.
PREDICTION_AUDIT_DB = os.path.join(DATA_DIR, "prediction_audit.sqlite3")
PREDICTION_MODEL_VERSION = "stage3-v7-strict-repair-premium"
TELEGRAM_ACCESS_FILE = os.path.join(DATA_DIR, "telegram_access.json")
TELEGRAM_DETAILS_DB = os.path.join(DATA_DIR, "telegram_details.sqlite3")
_telegram_access_store = TelegramAccessStore(TELEGRAM_ACCESS_FILE)
_telegram_details_store = TelegramDetailsStore(TELEGRAM_DETAILS_DB)

# ============================== TELEGRAM ==============================

# Токен и chat_id — ТОЛЬКО из окружения, в коде их нет.
#
# Раньше здесь лежали зашитые дефолты, чтобы не задавать переменные в
# PowerShell перед каждым запуском. Для деплоя это не годится: файл
# уходит в git, а токен даёт полный доступ к боту — писать от его имени
# кому угодно. Локально те же значения кладутся в .env рядом со
# скриптом (он в .gitignore), на Render — в Environment сервиса.
#
# Если хотя бы одна переменная не задана, Telegram-доставка тихо
# выключается: логи и реестр продолжают работать, ничего не падает.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
# Несколько получателей — через запятую в ОДНОЙ переменной
# (TELEGRAM_CHAT_ID=489767497,123456789), а не отдельными переменными на
# каждого: список получателей меняется чаще, чем сам код, и не должен
# требовать правки render.yaml при каждом добавлении человека.
TELEGRAM_CHAT_IDS = [
    c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()
]
# Только этот Telegram user_id может менять настройки и выдавать доступ.
# Для старых деплоев первый положительный TELEGRAM_CHAT_ID служит безопасным
# fallback: в личном чате chat_id совпадает с user_id. Для группы fallback
# намеренно не применяется — отрицательный group chat_id не является user_id.
TELEGRAM_ADMIN_USER_ID = os.environ.get("TELEGRAM_ADMIN_USER_ID", "").strip()
if not TELEGRAM_ADMIN_USER_ID:
    TELEGRAM_ADMIN_USER_ID = next(
        (value for value in TELEGRAM_CHAT_IDS if value.isdigit()), "",
    )
TELEGRAM_ENABLED = bool(
    TELEGRAM_BOT_TOKEN and (TELEGRAM_CHAT_IDS or TELEGRAM_ADMIN_USER_ID)
)
TELEGRAM_API_ROOT = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

TELEGRAM_SEND_DELAY_SEC = 1.2   # запас от flood control (Telegram лимитирует ~1 сообщение/сек в чат)
TELEGRAM_MAX_RETRIES = 3
TELEGRAM_TIMEOUT_SEC = 15

# ============================== RIELTORCOLLECTOR ==============================

# URL и ключ внешнего сервиса, который сам ходит на krisha.kz и отдаёт
# события/карточки. Обязателен: без него у оркестратора нет источника
# данных вообще (см. validate_configuration).
RIELTOR_COLLECTOR_URL = os.environ.get("RIELTOR_COLLECTOR_URL", "").strip().rstrip("/")
# Заголовок X-API-Key. Пусто — запрос уйдёт без заголовка; если сервер
# требует ключ, это даст постоянные 401 (см. CollectorAuthError ниже),
# что и так будет видно в логе.
RIELTOR_COLLECTOR_API_KEY = os.environ.get("RIELTOR_COLLECTOR_API_KEY", "").strip()

# Как часто спрашивать rieltorcollector, есть ли новые события после курсора.
COLLECTOR_POLL_INTERVAL_SEC = int(os.environ.get("COLLECTOR_POLL_INTERVAL_SEC", str(60)))
# limit на страницу ленты событий. Сервер валидирует 1..500 (иначе 422) —
# держим свой дефолт внутри этого диапазона.
COLLECTOR_PAGE_LIMIT = int(os.environ.get("COLLECTOR_PAGE_LIMIT", str(200)))
COLLECTOR_TIMEOUT_SEC = 15
# Обрабатываем только эти события ленты; всё остальное пропускаем (курсор
# при этом двигается), чтобы будущий новый тип события не ушёл в оценку
# как обычное объявление.
COLLECTOR_REASONS = ("new", "price_drop")
# Событие старше этого возраста не идёт в Stage 2/Stage 3/Telegram.
# Фотооценка проверяет и старые события, чтобы после простоя не терять ID.
# 0 — не ограничивать Stage 2/Telegram.
COLLECTOR_MAX_EVENT_AGE_H = float(os.environ.get("COLLECTOR_MAX_EVENT_AGE_H", "12"))
# Первый запуск (нет collector_state.json): "end" — встать в конец ленты и
# не разбирать историю; "beginning" — начать с since=0.
COLLECTOR_START_FROM = os.environ.get("COLLECTOR_START_FROM", "beginning").strip().lower()
# Сколько раз подряд можно не разобрать одни и те же объявления в Stage 2,
# прежде чем их пропустить, а не держать всю ленту на одной странице.
LLM_FAIL_MAX_ATTEMPTS = 3

# ============================== BASELINE (rieltor-cleaner) ==============================

# Готовый чистый baseline отдаёт сервис rieltor-cleaner одним CSV-файлом
# (GET /baseline/clean.csv, заголовок X-API-Key). Раз в
# BASELINE_REFRESH_INTERVAL_SEC файл скачивается и ПОЛНОСТЬЮ заменяет
# локальный BASELINE_CSV. Ключ — только из окружения, в коде его нет.
# Пустой ключ — автообновление выключено, работаем с локальным файлом.
RIELTOR_CLEANER_URL = os.environ.get(
    "RIELTOR_CLEANER_URL", "https://rieltor-cleaner-almaty.onrender.com"
).strip().rstrip("/")
RIELTOR_CLEANER_API_KEY = os.environ.get("RIELTOR_CLEANER_API_KEY", "").strip()
BASELINE_REFRESH_ENABLED = bool(RIELTOR_CLEANER_URL and RIELTOR_CLEANER_API_KEY)

# rieltor-cleaner пересобирает файл раз в 12 часов.
BASELINE_REFRESH_INTERVAL_SEC = int(os.environ.get("BASELINE_REFRESH_INTERVAL_SEC", str(12 * 3600)))
# После неудачи (сеть, 401, 503 "ещё не собран") не ждём 12 часов.
BASELINE_RETRY_INTERVAL_SEC = int(os.environ.get("BASELINE_RETRY_INTERVAL_SEC", str(10 * 60)))
BASELINE_TIMEOUT_SEC = 120
# X-Built-At последнего успешно применённого файла — чтобы не заменять
# данные тем же самым файлом.
BASELINE_STATE_FILE = os.path.join(DATA_DIR, "baseline_state.json")
# Без этих колонок Stage 3 не может строить когорты. Результатов Stage 2
# (red_flags и т.п.) в baseline rieltor-cleaner нет — это 40 колонок
# карточки, и Stage 3 этого не требует: они нужны только входящим строкам.
BASELINE_REQUIRED_COLUMNS = ("id", "price", "rooms", "square_m2")

# ============================== ФОТООЦЕНКА ИНТЕРЬЕРА ==============================

# Пока переключатель выключен, новый слой вообще не влияет на прежний pipeline.
# После локальной проверки он включается одной переменной окружения.
INTERIOR_ENABLED = os.environ.get("INTERIOR_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on",
}
INTERIOR_DB = os.path.join(DATA_DIR, "interior_online.sqlite3")
INTERIOR_CACHE_DB = os.path.join(DATA_DIR, "interior_content_cache.sqlite3")
INTERIOR_EXCHANGE_API_KEY = os.environ.get("INTERIOR_EXCHANGE_API_KEY", "").strip()
INTERIOR_API_HOST = os.environ.get("INTERIOR_API_HOST", "0.0.0.0").strip()
INTERIOR_API_PORT = int(os.environ.get("PORT", os.environ.get("INTERIOR_API_PORT", "8080")))
INTERIOR_CONCURRENCY = int(os.environ.get("INTERIOR_CONCURRENCY", "2"))
INTERIOR_RETRY_DELAY_SEC = int(os.environ.get("INTERIOR_RETRY_DELAY_SEC", "30"))
INTERIOR_ERROR_RETRY_H = float(os.environ.get("INTERIOR_ERROR_RETRY_H", "6"))
INTERIOR_OPENAI_API_KEY = os.environ.get("RENOVATION_OPENAI_API_KEY", "").strip()
INTERIOR_DECISION_MAX_WAIT_SEC = int(
    os.environ.get("INTERIOR_DECISION_MAX_WAIT_SEC", "1200")
)

_interior_gateway = None
_interior_store = None

# Схема приведена к фактическому OUTPUT_EXTRA_FIELDNAMES версии v3.
# Изменения относительно v2-схемы:
#   - убрано base_price_m2_corrected: в v3 поправка на этаж больше не
#     применяется к цене (is_extreme_floor остался как диагностика), и
#     такого поля модуль не возвращает — колонка молча заполнялась
#     пустой строкой в каждой записи;
#   - добавлены base_price_m2 (то, с чем реально сравнивается цена),
#     robust_z, cohort_stop_level, confidence_weight;
#   - добавлены review_flags и cohort_notes: в v5 неполнота объявления
#     намеренно вынесена из вердикта в отдельную колонку, и без неё в
#     журнале теряется ровно то, ради чего вердикт переделывали.
NOTIFICATIONS_FIELDNAMES = [
    "id", "url", "title", "reason", "price", "old_price", "source", "sent_at",
    "photo_count", "seller_type", "owner_name", "is_identity_confirmed",
    "seller_class", "seller_confidence",
    "verdict", "verdict_reason",
    "diff_pct", "base_price_m2_unadjusted", "base_price_m2", "robust_z", "value_score",
    "interior_status", "interior_score", "interior_coverage",
    "interior_reference_score", "interior_reference_n",
    "interior_adjustment_confidence", "interior_adjustment_beta",
    "interior_price_adjustment_pct",
    "interior_adjustment_applied", "interior_adjustment_reason",
    "quality_evidence_score", "price_quality_score", "price_quality_label",
    "benchmark_confidence", "data_confidence",
    "cohort_level", "cohort_stop_level", "cohort_size", "confidence_weight",
    "data_warnings", "review_flags", "cohort_notes",
]

# ============================== ОБЩИЕ ХЕЛПЕРЫ ==============================


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_configuration():
    """Проверяем конфигурацию до запуска цикла опроса."""
    if not RIELTOR_COLLECTOR_URL:
        raise ValueError(
            "RIELTOR_COLLECTOR_URL не задан — без него оркестратору "
            "неоткуда брать объявления. Впишите RIELTOR_COLLECTOR_URL=... в файл "
            ".env рядом с orchestrator_v7.py (или задайте переменную окружения)."
        )
    if not (1 <= COLLECTOR_PAGE_LIMIT <= 500):
        raise ValueError(
            f"COLLECTOR_PAGE_LIMIT={COLLECTOR_PAGE_LIMIT} вне допустимого "
            "rieltorcollector диапазона 1..500 (сервер ответит 422)."
        )
    if COLLECTOR_POLL_INTERVAL_SEC <= 0:
        raise ValueError("COLLECTOR_POLL_INTERVAL_SEC должен быть > 0.")
    if COLLECTOR_START_FROM not in ("end", "beginning"):
        raise ValueError("COLLECTOR_START_FROM должен быть 'end' или 'beginning'.")
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise ValueError(
            "OPENAI_API_KEY не задан — Stage 2 (разбор описаний) без него не работает. "
            "Впишите OPENAI_API_KEY=... в файл .env рядом с orchestrator_v7.py."
        )
    if INTERIOR_ENABLED:
        if not INTERIOR_OPENAI_API_KEY:
            raise ValueError(
                "INTERIOR_ENABLED=1, но нет отдельного RENOVATION_OPENAI_API_KEY."
            )
        if not INTERIOR_EXCHANGE_API_KEY:
            raise ValueError(
                "INTERIOR_ENABLED=1, но INTERIOR_EXCHANGE_API_KEY не задан."
            )
        if INTERIOR_CONCURRENCY < 1:
            raise ValueError("INTERIOR_CONCURRENCY должен быть >= 1.")
        if INTERIOR_ERROR_RETRY_H <= 0:
            raise ValueError("INTERIOR_ERROR_RETRY_H должен быть > 0.")
        if INTERIOR_DECISION_MAX_WAIT_SEC < 0:
            raise ValueError("INTERIOR_DECISION_MAX_WAIT_SEC не может быть отрицательным.")

    if BASELINE_REFRESH_INTERVAL_SEC <= 0 or BASELINE_RETRY_INTERVAL_SEC <= 0:
        raise ValueError("BASELINE_REFRESH_INTERVAL_SEC и BASELINE_RETRY_INTERVAL_SEC должны быть > 0.")
    if not BASELINE_REFRESH_ENABLED and not os.path.isfile(BASELINE_CSV):
        raise FileNotFoundError(
            f"baseline: файл не найден: {BASELINE_CSV}, а автозагрузка выключена "
            "(задайте RIELTOR_CLEANER_API_KEY, чтобы скачать его с rieltor-cleaner)."
        )

    for label, path in [
        ("incoming cleaner", INCOMING_CLEAN_SCRIPT),
        ("stage3", STAGE3_MODULE),
    ]:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"{label}: файл не найден: {path}")

    print("=== Конфигурация ===")
    print(f"BASE_DIR       = {BASE_DIR} (код)")
    print(f"DATA_DIR       = {DATA_DIR} (состояние{', ПЕРСИСТЕНТНЫЙ' if DATA_DIR != BASE_DIR else ''})")
    print(f"BASELINE       = {BASELINE_CSV}")
    print(f"INCOMING       = {INCOMING_CLEAN_SCRIPT}")
    print(f"STAGE3         = {STAGE3_MODULE}")
    print(f"COLLECTOR_URL  = {RIELTOR_COLLECTOR_URL}")
    masked_key = (
        RIELTOR_COLLECTOR_API_KEY[:4] + "..." if len(RIELTOR_COLLECTOR_API_KEY) > 4
        else ("(пусто)" if not RIELTOR_COLLECTOR_API_KEY else "...")
    )
    print(f"COLLECTOR_KEY  = {masked_key}")
    print(f"COLLECTOR_POLL = каждые {COLLECTOR_POLL_INTERVAL_SEC}s, страница {COLLECTOR_PAGE_LIMIT}")
    print(
        f"COLLECTOR_FEED = события {COLLECTOR_REASONS}; Stage 2/Telegram не старше "
        f"{COLLECTOR_MAX_EVENT_AGE_H}ч; фотооценка обрабатывает всю ленту, "
        f"первый запуск: {COLLECTOR_START_FROM}"
    )
    print(f"STAGE2_MODEL   = {os.environ.get('OPENAI_MODEL', 'gpt-5-mini')} (OpenAI API, пачками)")
    print(
        f"INTERIOR       = {'включён' if INTERIOR_ENABLED else 'выключен'}"
        + (f" (workers={INTERIOR_CONCURRENCY}, port={INTERIOR_API_PORT}, "
             f"ожидание решения до {INTERIOR_DECISION_MAX_WAIT_SEC}s)" if INTERIOR_ENABLED else "")
    )
    if BASELINE_REFRESH_ENABLED:
        print(
            f"BASELINE_SRC   = {RIELTOR_CLEANER_URL}/baseline/clean.csv "
            f"(ключ {RIELTOR_CLEANER_API_KEY[:4]}..., раз в {BASELINE_REFRESH_INTERVAL_SEC}s)"
        )
    else:
        print("BASELINE_SRC   = выключено (нет RIELTOR_CLEANER_API_KEY), используется локальный файл")
    if TELEGRAM_ENABLED:
        masked = TELEGRAM_BOT_TOKEN[:6] + "..." if len(TELEGRAM_BOT_TOKEN) > 6 else "..."
        print(
            f"TELEGRAM       = включён (bot={masked}, "
            f"получателей: {len(telegram_recipients(include_paused=True))}, "
            f"admin: {'задан' if TELEGRAM_ADMIN_USER_ID else 'не задан'})"
        )
    else:
        print(
            "TELEGRAM       = выключен (задайте TELEGRAM_BOT_TOKEN и "
            "TELEGRAM_CHAT_ID в переменных окружения, чтобы включить)"
        )


def to_float(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def read_csv_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


async def run_subprocess(script_path, args, cwd=None):
    """
    Безопасный subprocess wrapper.
    cwd всегда берём из реального расположения script_path, если явно
    не передан. Ошибка CreateProcess/WinError не валит весь orchestrator.
    """
    script_path = os.path.abspath(script_path)
    actual_cwd = os.path.abspath(cwd or os.path.dirname(script_path))

    if not os.path.isfile(script_path):
        print(f"   ⛔ worker-файл не найден: {script_path}")
        return -1

    if not os.path.isdir(actual_cwd):
        print(f"   ⛔ cwd не является каталогом: {actual_cwd}")
        return -1

    print(f"   ▶️  {script_path} {' '.join(args)}")
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, script_path, *args,
            cwd=actual_cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        print(
            f"   ⛔ Не удалось запустить {os.path.basename(script_path)}: "
            f"{exc} | script={script_path} | cwd={actual_cwd}"
        )
        return -1
    out, _ = await proc.communicate()
    text = out.decode(errors="replace")
    if text.strip():
        # Пробрасываем вывод треков как есть — это их собственные принты,
        # оркестратор их не парсит и не интерпретирует.
        print(text)
    if proc.returncode != 0:
        print(f"   ⚠️  {os.path.basename(script_path)} завершился с кодом {proc.returncode}")
    return proc.returncode


# ============================== РЕЕСТР ever_sent_ids.json ==============================


def load_registry(path):
    """Читает реестр и НОРМАЛИЗУЕТ цену к float.

    Исторически цена попадала сюда строкой: строки после incoming_clean
    перечитываются из CSV, где всё — str, и dedupe_against_registry клал
    их в реестр как есть. Из-за этого сравнение "текущая цена ниже
    отправленной" выполнялось лексикографически: '90000.0' > '190000.0',
    то есть падение цены на 53% НЕ считалось падением, а рост со 190 000
    до 1 000 000 считался ('1000000.0' < '190000.0'). Миграция делается
    на чтении, так что старый файл чинится сам при первом же запуске и
    отдельный скрипт не нужен.
    """
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    registry = {}
    for rid, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        entry = dict(entry)
        entry["price"] = to_float(entry.get("price"))
        registry[str(rid)] = entry
    return registry


def save_registry(path, registry):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _plain_price(value):
    """250000.0 -> 250000 (для журнала и Telegram); нечисловое не трогаем."""
    number = to_float(value)
    if number is not None and number.is_integer():
        return int(number)
    return value


def to_notification_row(row):
    """Один и тот же компактный формат используется и для CSV-лога, и для
    текста Telegram-сообщения — чтобы не разъезжались схемы."""
    out = {field: row.get(field, "") for field in NOTIFICATIONS_FIELDNAMES}
    out["price"] = _plain_price(out["price"])
    out["old_price"] = _plain_price(out["old_price"])
    return out


def append_notifications(path, rows):
    """
    ЕДИНСТВЕННЫЙ растущий файл в системе. Остальные — снепшоты одного
    прогона (перезаписываются), а этот — журнал того, что реально ушло
    пользователю за всё время. Дозаписывается, не перезаписывается.
    """
    if not rows:
        return
    file_exists = os.path.exists(path)
    if file_exists:
        with open(path, "r", encoding="utf-8-sig", newline="") as existing:
            header = next(csv.reader(existing), [])
        if header != NOTIFICATIONS_FIELDNAMES:
            raise RuntimeError(
                f"{path} имеет старую/несовместимую схему. "
                f"Текущая схема — Stage 3 + interior (notifications_log_v4.csv); "
                f"не дописываем в CSV с другим набором колонок."
            )
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=NOTIFICATIONS_FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        # Stage 3 keeps the full listing row, but the notification log has a
        # deliberate compact schema. Drop non-log fields explicitly.
        writer.writerows([to_notification_row(row) for row in rows])


# ============================== TELEGRAM ДОСТАВКА ==============================


def escape_html(text):
    """Минимальный набор экранирования для Telegram parse_mode=HTML."""
    return (
        str(text if text is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def format_deviation(value):
    """Отклонение от базы: направление только цветом и стрелкой.

    Сырой diff_pct показывать нельзя. Он равен (база - цена)/база, то
    есть сверху ограничен единицей, а снизу не ограничен ничем: квартира
    втрое дороже базы даёт -2.27, и на экране это выглядело как
    "📉 Отклонение от базы: -227.0%" — читается как поломка, хотя
    арифметика верна.

    Обе стороны нормированы на базу, поэтому величина интерпретируется
    одинаково в оба направления: |diff_pct| = насколько цена отличается
    от базы в долях базы. Печатаем модуль и словами, куда именно.
    """
    v = to_float(value)
    if v is None:
        return "—", "📊"
    if v > 0:
        return f"{v * 100:.1f}%", "🟢⬇️"
    if v < 0:
        return f"{abs(v) * 100:.1f}%", "🔴⬆️"
    return "0.0%", "⚪️"


def format_diff_pct(value):
    """Сырой diff_pct со знаком — для журнала и отладки, не для Telegram."""
    try:
        return f"{float(value) * 100:+.1f}%"
    except (TypeError, ValueError):
        return "—"


def review_direction(notif_row):
    """Для ТРЕБУЕТ ПРОВЕРКИ: в какую сторону аномалия.

    diff_pct = (base - price)/base, то есть положительный = дешевле базы.
    Отдельного поля с направлением Stage 3 не отдаёт, знак diff_pct —
    единственный доступный признак.
    """
    diff = to_float(notif_row.get("diff_pct"))
    if diff is None:
        return None
    return "скидка" if diff > 0 else "переоценка"


# How many comparables to list in the cohort message. Telegram caps a
# message at 4096 characters and each line here runs ~90; 20 lines plus
# the header leaves comfortable headroom. Cohorts are routinely larger
# than this (median 35, p90 91 on the current baseline), so truncation
# is the normal case, not the exception — hence the explicit "и ещё N"
# line rather than a silent cut.
COHORT_MESSAGE_MAX_ROWS = 20

# Which verdicts get the on-demand cohort button. НАХОДКА because that is the
# claim worth auditing before spending time on a viewing; ТРЕБУЕТ
# ПРОВЕРКИ because its whole point is that the cohort itself is
# suspect, so the comparables are exactly what a human needs to see.
COHORT_MESSAGE_VERDICTS = {"НАХОДКА", "ТРЕБУЕТ ПРОВЕРКИ"}

_COHORT_LEVEL_LABEL = {
    "1-2": "тот же дом/ЖК",
    "3": "радиус 0.5 км",
    "4": "радиус 1 км",
    "5": "радиус 3 км",
    "6": "весь город",
}


def format_cohort_message(row):
    """On-demand message: the comparables the verdict was computed from.

    Sent separately rather than appended to the main card so the primary
    notification stays short and scannable on a phone, and so a failure
    to build this list can never block the verdict itself.
    """
    members = row.get("_cohort_members") or []
    if not members:
        return None

    # Считаем из текущих price/square, а не из row["price_m2"]: это
    # поле — сырой проход из скрапа и может отставать от цены на момент
    # отправки (уведомление уходит повторно при СТРОГОМ понижении цены,
    # см. dedupe_against_registry — но price_m2 из старого скана на тот
    # момент ещё не пересчитан). При падении цены с 300000 на 200000 при
    # 40 м² это показывало 7500 ₸/м² вместо верных 5000.
    price = to_float(row.get("price"))
    square = to_float(row.get("square_m2"))
    target_pm2 = (price / square) if price and square else None
    if target_pm2 is None:
        target_pm2 = to_float(row.get("price_m2")) or None

    # Ровный срез по ВСЕМУ диапазону, а не первые N.
    #
    # Список отсортирован по возрастанию цены/м², и обычная обрезка
    # `members[:N]` показывала бы только самые дешёвые. На реальной
    # находке это читалось прямо наоборот, чем есть: объект по 3286
    # ₸/м² на фоне показанных 3153-5312 выглядит дорогим, хотя медиана
    # всей когорты 5840 и он дешевле базы на 43%. Берём равномерно
    # распределённые позиции, чтобы был виден и низ, и середина, и верх.
    if len(members) > COHORT_MESSAGE_MAX_ROWS:
        step = (len(members) - 1) / (COHORT_MESSAGE_MAX_ROWS - 1)
        idx = sorted({round(i * step) for i in range(COHORT_MESSAGE_MAX_ROWS)})
        shown = [members[i] for i in idx]
    else:
        shown = list(members)
    omitted = len(members) - len(shown)

    header = [f"📊 <b>База сравнения</b> — {len(members)} объявлений по всем уровням"]
    # Состав ВЕДУЩЕГО уровня отдельно. В карточке рядом стоят
    # "уровень 1-2" и "n=65", и это читается как "65 объявлений в том же
    # доме", хотя 65 — сумма по всем уровням, а в самом L1/2 может быть
    # две строки. Уровень выигрывает весом (LEVEL_WEIGHT L1/2 = 1.0
    # против L5 = 0.006), а не количеством.
    dom = row.get("cohort_level")
    if dom:
        dom_members = [m for m in members if m["level"] == dom]
        label = _COHORT_LEVEL_LABEL.get(dom, dom)
        header.append(
            f"Решает уровень L{dom} ({escape_html(label)}): "
            f"<b>{len(dom_members)} объявл.</b>"
        )
    if target_pm2:
        header.append(f"Цена объекта: <b>{int(target_pm2):,} ₸/м²</b>".replace(",", " "))
    base_pm2 = to_float(row.get("base_price_m2"))
    raw_base_pm2 = to_float(row.get("base_price_m2_unadjusted"))
    adjustment_applied = _is_true(row.get("interior_adjustment_applied"))
    if adjustment_applied and raw_base_pm2:
        header.append(
            f"База аналогов до ремонта: <b>{int(raw_base_pm2):,} ₸/м²</b>".replace(",", " ")
        )
    if base_pm2:
        label = "Справедливая с учётом ремонта" if adjustment_applied else "База (взвешенная по когортам)"
        header.append(f"{label}: <b>{int(base_pm2):,} ₸/м²</b>".replace(",", " "))
    # Медиана показанного среза сама по себе ни о чём не говорит, а вот
    # медиана всей когорты — тот ориентир, относительно которого читатель
    # проверяет вердикт.
    all_pm2 = sorted(x for x in (to_float(m.get("price_m2")) for m in members) if x)
    if all_pm2:
        med = all_pm2[len(all_pm2) // 2]
        header.append(
            f"Разброс когорты: {int(all_pm2[0]):,} … <b>медиана {int(med):,}</b> … {int(all_pm2[-1]):,} ₸/м²".replace(",", " ")
        )
    lines = header + [""]
    current_level = None
    for m in shown:
        if m["level"] != current_level:
            current_level = m["level"]
            label = _COHORT_LEVEL_LABEL.get(current_level, current_level)
            lines.append(f"<b>L{current_level} — {escape_html(label)}</b>")

        pm2 = to_float(m.get("price_m2"))
        pm2_txt = f"{int(pm2):,}".replace(",", " ") if pm2 else "—"
        price = to_float(m.get("price"))
        price_txt = f"{int(price):,}".replace(",", " ") if price else "—"
        sq = m.get("square_m2") or "—"
        rooms = m.get("rooms") or "—"
        title = f"{rooms}к {sq}м² — {price_txt} ₸ ({pm2_txt} ₸/м²)"
        url = m.get("url")
        if url:
            lines.append(f'• <a href="{escape_html(str(url))}">{escape_html(title)}</a>')
        else:
            lines.append(f"• {escape_html(title)}")

    if omitted:
        lines.append(f"\n…и ещё {omitted} — показан ровный срез по всему диапазону цен")
    return "\n".join(x for x in lines if x != "" or True)


# Начальное/legacy-значение. После первого запуска режим хранится в
# telegram_access.json и меняется администратором командой /mode.
# Отправлять ли ВСЕ квартиры, а не только те, что прошли
# candidate_is_sendable. Диагностический режим: нужен, чтобы оценивать
# работу самой модели — что она решила по каждому кандидату и на чём, —
# а не только получать готовые находки.
#
# По объёму проходит свободно. Реальный поток по baseline: 325 новых
# объявлений в сутки, то есть примерно одно за пятиминутный цикл, в
# пике десяток. Десять квартир по два сообщения с задержкой 1.2с
# (Telegram душит около 1 сообщения в секунду в чат) — это 24с при
# интервале 300с. Осторожность нужна только после сброса
# known_ids.json: первый прогон после этого выгребает всё окно списка
# разом (наблюдалось 60 кандидатов) и рассылка займёт пару минут.
SEND_ALL_VERDICTS = True


def telegram_send_all_verdicts():
    return _telegram_access_store.settings()["send_mode"] == "all"


def explain_confidence(row):
    """Почему benchmark_confidence именно такая.

    Формула (confidence_from_cohorts) перемножает три сомножителя, и
    низкое значение почти всегда объясняется одним из них. Разбираем по
    опубликованным колонкам, а не пересчитываем — так объяснение не
    разъедется с реальным числом, если формулу поменяют.
    """
    reasons = []
    conf = to_float(row.get("benchmark_confidence"))
    level = row.get("cohort_level")

    # 1. Размер. size_factor = eff_n / (eff_n + 3.0): при eff_n=1 это
    # 0.25, при eff_n=3 — 0.5. На тонкой когорте потолок уверенности
    # низкий независимо от всего остального.
    prefix = {"1-2": "l12", "3": "l3", "4": "l4", "5": "l5", "6": "l6"}.get(level)
    dom_n = to_float(row.get(f"{prefix}_n")) if prefix else None
    if dom_n is not None and dom_n <= 2:
        reasons.append(f"ведущий уровень L{level} стоит всего на {int(dom_n)} объявл.")

    # 2. Уровень. LEVEL_CONFIDENCE: L1/2=1.00, L3=0.90, L4=0.78,
    # L5=0.62, L6=0.42 — чем шире география, тем слабее свидетельство.
    if level in ("4", "5", "6"):
        label = {"4": "радиус 1 км", "5": "радиус 3 км", "6": "весь город"}[level]
        reasons.append(f"сравнение идёт по широкой географии ({label}), а не по своему дому")

    # 3. Разброс. Внутрикогортный разброс — по замерам в самом модуле
    # лучший предиктор ошибки: по квинтилям 0.149 -> 0.092.
    disp = to_float(row.get("cohort_dispersion"))
    if disp is not None and disp >= 0.30:
        reasons.append(f"цены внутри когорты разбросаны широко (разброс {disp:.2f})")
    disag = to_float(row.get("cohort_disagreement"))
    if disag is not None and disag >= 0.20:
        reasons.append(f"уровни когорт противоречат друг другу (расхождение {disag:.2f})")

    if not reasons:
        return None
    tier = "низкая" if (conf or 0) < 0.49 else ("средняя" if conf < 0.60 else "высокая")
    return f"Уверенность {tier} ({conf:.3f}), потому что: " + "; ".join(reasons)


def format_telegram_message(notif_row, full_row=None):
    """notif_row — уже компактная строка вида to_notification_row(...).

    full_row нужен для разбора уверенности: он читает l*_n,
    cohort_dispersion и cohort_disagreement, которых в схеме журнала нет.
    """
    price = notif_row.get("price") or "—"
    old_price = notif_row.get("old_price")
    price_line = f"{price} ₸"
    if old_price not in (None, "", "0", 0):
        price_line += f" (было {old_price} ₸)"

    verdict = notif_row.get("verdict") or "—"
    verdict_icon = {
        "НАХОДКА": "🔥",
        "ТРЕБУЕТ ПРОВЕРКИ": "⚠️",
        "РУЧНАЯ ПРОВЕРКА": "🔎",
    }.get(verdict, "🔎")

    # Направление уже однозначно показывает цветная стрелка отклонения.
    # Не дублируем его словами в вердикте.
    verdict_label = verdict

    deviation_text, deviation_icon = format_deviation(notif_row.get("diff_pct"))

    lines = [
        f"{verdict_icon} <b>{escape_html(notif_row.get('title') or 'Без названия')}</b>",
        f"💰 {escape_html(price_line)}",
        f"{deviation_icon} Отклонение: {escape_html(deviation_text)}",
        f"🏆 Вердикт: {escape_html(verdict_label)}",
        f"👤 Продавец: {escape_html(notif_row.get('seller_class') or '—')}",
    ]

    confidence = to_float(notif_row.get("benchmark_confidence"))
    if confidence is None:
        confidence_text = "—"
    else:
        tier = "высокая" if confidence >= 0.60 else (
            "средняя" if confidence >= 0.49 else "низкая"
        )
        confidence_text = f"{confidence:.3f} ({tier})"
    lines.append(f"🎯 Уверенность: {escape_html(confidence_text)}")

    interior_score = to_float(notif_row.get("interior_score"))
    interior_reference = to_float(notif_row.get("interior_reference_score"))
    interior_adjustment = to_float(notif_row.get("interior_price_adjustment_pct"))
    if interior_score is not None and interior_reference is not None:
        sign = "+" if (interior_adjustment or 0) > 0 else ""
        lines.append(
            f"🛠 Ремонт: <b>{interior_score:.1f}/10</b>; "
            f"у аналогов {interior_reference:.1f}; "
            f"поправка справедливой цены {sign}{(interior_adjustment or 0):.1f}%"
        )
    else:
        status = notif_row.get("interior_status") or "нет оценки"
        coverage = notif_row.get("interior_coverage")
        detail = f"{status}/{coverage}" if coverage else status
        lines.append(f"🛠 Ремонт: {escape_html(detail)} — цену не корректировал")

    return "\n".join(lines)


def _telegram_api_post(method, payload, *, timeout=TELEGRAM_TIMEOUT_SEC):
    """Синхронный вызов Bot API без сторонних зависимостей."""
    import urllib.request
    import urllib.parse
    import urllib.error

    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{TELEGRAM_API_ROOT}/{method}", data=data, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def _telegram_post(payload):
    """Обратная совместимость тестов и прежнего sendMessage-кода."""
    return _telegram_api_post("sendMessage", payload)


async def _telegram_call(method, payload, *, timeout=TELEGRAM_TIMEOUT_SEC):
    loop = asyncio.get_running_loop()
    status, body = await loop.run_in_executor(
        None,
        lambda: _telegram_api_post(method, payload, timeout=timeout),
    )
    try:
        parsed = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        parsed = {"ok": False, "description": str(body)[:500]}
    return status, parsed


async def _send_to_one_chat(
    chat_id, text, *, reply_markup=None, disable_web_page_preview=False,
):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": (
            "true" if disable_web_page_preview else "false"
        ),
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    loop = asyncio.get_running_loop()
    for attempt in range(1, TELEGRAM_MAX_RETRIES + 1):
        try:
            status, body = await loop.run_in_executor(None, _telegram_post, payload)
            if status == 200:
                return True
            print(f"   ⚠️ Telegram API вернул {status} для chat_id={chat_id} "
                  f"(попытка {attempt}/{TELEGRAM_MAX_RETRIES}): {body[:200]}")
        except Exception as exc:
            print(f"   ⚠️ Telegram send сбой для chat_id={chat_id} "
                  f"(попытка {attempt}/{TELEGRAM_MAX_RETRIES}): {type(exc).__name__}: {exc}")
        if attempt < TELEGRAM_MAX_RETRIES:
            await asyncio.sleep(2 * attempt)
    return False


def telegram_recipients(*, include_paused=False):
    """Legacy env + выданные администратором доступы, без дублей."""
    settings = _telegram_access_store.settings()
    if settings["paused"] and not include_paused:
        return []
    values = list(TELEGRAM_CHAT_IDS)
    values.extend(_telegram_access_store.recipients())
    if TELEGRAM_ADMIN_USER_ID:
        values.append(TELEGRAM_ADMIN_USER_ID)
    revoked = _telegram_access_store.revoked()
    return list(dict.fromkeys(
        str(value) for value in values
        if str(value)
        and (
            str(value) == TELEGRAM_ADMIN_USER_ID
            or str(value) not in revoked
        )
    ))


async def send_telegram_message(
    text, *, chat_ids=None, reply_markup=None, disable_web_page_preview=False,
):
    """Рассылает сообщение всем активным получателям независимо.

    Возвращает True, если доставлено хотя бы одному — тем же смыслом
    пользовался единственный получатель раньше, и от него зависит,
    уйдёт ли следом второе сообщение с базой сравнения (см. notify_telegram).
    """
    if not TELEGRAM_ENABLED:
        return False

    recipients = telegram_recipients() if chat_ids is None else [
        str(value) for value in chat_ids
    ]
    if not recipients:
        return False
    results = await asyncio.gather(*(
        _send_to_one_chat(
            chat_id, text, reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
        for chat_id in recipients
    ))
    return any(results)


def _telegram_admin_help():
    return (
        "🛠 <b>Управление ботом</b>\n"
        "/status — состояние и режимы\n"
        "/users — доступы и ожидающие запросы\n"
        "/grant ID — выдать доступ\n"
        "/revoke ID — отозвать доступ\n"
        "/mode all|useful — все вердикты или только полезные\n"
        "/cohorts all|important|off — кому показывать кнопку аналогов\n"
        "/pause и /resume — остановить/возобновить рассылку"
    )


def _telegram_settings_text():
    snapshot = _telegram_access_store.snapshot()
    settings = snapshot["settings"]
    return (
        "⚙️ <b>Telegram</b>\n"
        f"Рассылка: {'пауза' if settings['paused'] else 'активна'}\n"
        f"Вердикты: {escape_html(settings['send_mode'])}\n"
        f"Кнопка аналогов: {escape_html(settings['cohort_mode'])}\n"
        f"Пользователей: {len(snapshot['authorized'])}\n"
        f"Запросов доступа: {len(snapshot['pending'])}"
    )


def _telegram_user_label(entry):
    username = entry.get("username")
    name = entry.get("display_name")
    label = f"@{username}" if username else (name or "без имени")
    return f"{escape_html(label)} — <code>{escape_html(entry.get('chat_id'))}</code>"


async def _telegram_notify_access_request(entry):
    keyboard = {
        "inline_keyboard": [[
            {
                "text": "✅ Разрешить",
                "callback_data": f"access:grant:{entry['chat_id']}",
            },
            {
                "text": "❌ Отклонить",
                "callback_data": f"access:deny:{entry['chat_id']}",
            },
        ]],
    }
    await _send_to_one_chat(
        TELEGRAM_ADMIN_USER_ID,
        "🔐 <b>Запрос доступа</b>\n" + _telegram_user_label(entry),
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def _telegram_handle_admin_command(chat_id, command, argument):
    if command in {"/start", "/help"}:
        await _send_to_one_chat(chat_id, _telegram_admin_help())
        return
    if command in {"/status", "/settings"}:
        await _send_to_one_chat(chat_id, _telegram_settings_text())
        return
    if command == "/users":
        snapshot = _telegram_access_store.snapshot()
        lines = ["👥 <b>Разрешённые пользователи</b>"]
        authorized = list(snapshot["authorized"].values())
        lines.extend(_telegram_user_label(item) for item in authorized[:50])
        if not authorized:
            lines.append("— пока никого")
        lines.append("\n⏳ <b>Ожидают решения</b>")
        pending = list(snapshot["pending"].values())
        lines.extend(_telegram_user_label(item) for item in pending[:50])
        if not pending:
            lines.append("— нет запросов")
        await _send_to_one_chat(chat_id, "\n".join(lines))
        return
    if command in {"/grant", "/revoke"}:
        try:
            target = argument.split()[0]
            if command == "/grant":
                _telegram_access_store.grant(
                    target, granted_by=TELEGRAM_ADMIN_USER_ID,
                )
                answer = f"✅ Доступ выдан: <code>{escape_html(target)}</code>"
                await _send_to_one_chat(
                    target,
                    "✅ Доступ к уведомлениям разрешён администратором.",
                )
            else:
                changed = _telegram_access_store.revoke(target)
                answer = (
                    "✅ Доступ отозван" if changed else "ℹ️ ID не найден"
                ) + f": <code>{escape_html(target)}</code>"
            await _send_to_one_chat(chat_id, answer)
        except (IndexError, ValueError) as exc:
            await _send_to_one_chat(chat_id, f"⚠️ {escape_html(exc)}")
        return
    if command == "/mode":
        value = argument.strip().lower()
        try:
            _telegram_access_store.set_setting("send_mode", value)
            await _send_to_one_chat(chat_id, _telegram_settings_text())
        except ValueError:
            await _send_to_one_chat(chat_id, "Использование: /mode all или /mode useful")
        return
    if command == "/cohorts":
        value = argument.strip().lower()
        try:
            _telegram_access_store.set_setting("cohort_mode", value)
            await _send_to_one_chat(chat_id, _telegram_settings_text())
        except ValueError:
            await _send_to_one_chat(
                chat_id, "Использование: /cohorts all, important или off",
            )
        return
    if command in {"/pause", "/resume"}:
        _telegram_access_store.set_setting("paused", command == "/pause")
        await _send_to_one_chat(chat_id, _telegram_settings_text())
        return
    await _send_to_one_chat(chat_id, _telegram_admin_help())


async def _telegram_handle_message(message):
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = str(chat.get("id") or "")
    user_id = str(sender.get("id") or "")
    text = str(message.get("text") or "").strip()
    if not chat_id or not user_id or not text.startswith("/"):
        return
    head, _, argument = text.partition(" ")
    command = head.split("@", 1)[0].lower()
    if user_id == TELEGRAM_ADMIN_USER_ID:
        await _telegram_handle_admin_command(chat_id, command, argument)
        return
    if chat.get("type") != "private":
        await _send_to_one_chat(
            chat_id, "Для запроса доступа напишите боту в личные сообщения.",
        )
        return
    if _telegram_access_store.is_authorized(chat_id):
        await _send_to_one_chat(
            chat_id,
            "✅ Доступ активен. Вы будете получать новые уведомления. "
            "Настройки доступны только администратору.",
        )
        return
    display_name = " ".join(
        value for value in (sender.get("first_name"), sender.get("last_name"))
        if value
    )
    status, entry = _telegram_access_store.request_access(
        chat_id=chat_id,
        user_id=user_id,
        username=sender.get("username") or "",
        display_name=display_name,
    )
    if status == "requested":
        await _telegram_notify_access_request(entry)
        text = "⏳ Запрос отправлен администратору."
    else:
        text = "⏳ Запрос уже ожидает решения администратора."
    await _send_to_one_chat(chat_id, text)


async def _telegram_handle_callback(callback):
    sender_id = str((callback.get("from") or {}).get("id") or "")
    callback_id = callback.get("id")
    data = str(callback.get("data") or "")

    if data.startswith("cohort:"):
        chat_id = str(
            (((callback.get("message") or {}).get("chat") or {}).get("id"))
            or ""
        )
        allowed = (
            sender_id == TELEGRAM_ADMIN_USER_ID
            or chat_id in telegram_recipients(include_paused=True)
        )
        if not allowed:
            if callback_id:
                await _telegram_call(
                    "answerCallbackQuery",
                    {"callback_query_id": callback_id, "text": "Нет доступа"},
                )
            return
        listing_id = data.split(":", 1)[1]
        detail = _telegram_details_store.get(listing_id)
        if callback_id:
            await _telegram_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_id,
                    "text": "Открываю базу сравнения" if detail else "Детали уже устарели",
                },
            )
        if detail and chat_id:
            await _send_to_one_chat(
                chat_id, detail, disable_web_page_preview=True,
            )
        return

    if sender_id != TELEGRAM_ADMIN_USER_ID:
        if callback_id:
            await _telegram_call(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": "Нет доступа"},
            )
        return
    try:
        prefix, action, target = data.split(":", 2)
        if prefix != "access" or action not in {"grant", "deny"}:
            raise ValueError("неизвестное действие")
        if action == "grant":
            _telegram_access_store.grant(
                target, granted_by=TELEGRAM_ADMIN_USER_ID,
            )
            await _send_to_one_chat(
                target, "✅ Администратор разрешил доступ к уведомлениям.",
            )
            answer = "Доступ выдан"
        else:
            _telegram_access_store.deny(target)
            await _send_to_one_chat(target, "❌ Запрос доступа отклонён.")
            answer = "Запрос отклонён"
        await _send_to_one_chat(
            TELEGRAM_ADMIN_USER_ID,
            f"{escape_html(answer)}: <code>{escape_html(target)}</code>",
        )
    except (TypeError, ValueError) as exc:
        answer = str(exc)
    if callback_id:
        await _telegram_call(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": answer[:180]},
        )


async def telegram_control_loop():
    """Long polling команд; сырые Telegram updates на диск не сохраняются."""
    print("   [telegram-control] админское управление и доступы включены")
    while True:
        payload = {
            "offset": _telegram_access_store.update_offset,
            "timeout": 25,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        try:
            status, body = await _telegram_call(
                "getUpdates", payload, timeout=35,
            )
            if status != 200 or not body.get("ok"):
                raise RuntimeError(body.get("description") or f"HTTP {status}")
            for update in body.get("result") or []:
                if update.get("message"):
                    await _telegram_handle_message(update["message"])
                elif update.get("callback_query"):
                    await _telegram_handle_callback(update["callback_query"])
                _telegram_access_store.advance_offset(
                    int(update.get("update_id") or 0) + 1,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                "   ⚠️ [telegram-control] "
                f"{type(exc).__name__}: {exc}; повтор через 5с"
            )
            await asyncio.sleep(5)


async def notify_telegram(rows):
    """
    Best-effort отправка уже сохранённых (durable-залогированных) кандидатов.
    Вызывается ПОСЛЕ append_notifications/save_registry и ВНЕ registry_lock —
    сетевой I/O не должен держать лок другого трека.
    """
    if not TELEGRAM_ENABLED or not rows:
        return

    for row in rows:
        notif_row = to_notification_row(row)
        text = format_telegram_message(notif_row, full_row=row)
        url = notif_row.get("url")
        buttons = []
        if url:
            buttons.append({"text": "🏠 Открыть на Krisha", "url": str(url)})

        # Детали не летят вторым сообщением автоматически. Храним только
        # готовый текст (максимум 500 записей / 1 час) и выдаём по кнопке.
        cohort_mode = _telegram_access_store.settings()["cohort_mode"]
        wants_details = (
            cohort_mode == "all"
            or (
                cohort_mode == "important"
                and notif_row.get("verdict") in COHORT_MESSAGE_VERDICTS
            )
        )
        if wants_details:
            try:
                cohort_text = format_cohort_message(row)
            except Exception as exc:
                cohort_text = None
                print(
                    f"   ⚠️ Не удалось собрать базу сравнения для "
                    f"id={notif_row.get('id')}: {exc}"
                )
            if cohort_text:
                _telegram_details_store.put(notif_row.get("id"), cohort_text)
                buttons.append({
                    "text": "📊 База сравнения",
                    "callback_data": f"cohort:{notif_row.get('id')}",
                })
        keyboard = {"inline_keyboard": [buttons]} if buttons else None
        ok = await send_telegram_message(text, reply_markup=keyboard)
        if ok:
            print(f"   📨 Telegram: отправлено id={notif_row.get('id')} ({notif_row.get('verdict')})")
        else:
            print(
                f"   ⚠️ Telegram: не удалось отправить id={notif_row.get('id')} "
                "после всех попыток — запись всё равно осталась в notifications_log.csv"
            )

        await asyncio.sleep(TELEGRAM_SEND_DELAY_SEC)


def normalize_candidate(row, source, reason):
    """Сохраняем всю строку: Stage 3 нужны rooms/area/location."""
    out = dict(row)
    out["source"] = source
    out["reason"] = reason or out.get("reason") or ("price_drop" if source == "slow_track" else "new")
    if source == "slow_track":
        out["price"] = out.get("price") or out.get("new_price")
        out["old_price"] = out.get("old_price") or out.get("previous_price")
    out["price"] = to_float(out.get("price"))
    out["old_price"] = to_float(out.get("old_price"))
    return out


def adapt_collector_listing(card, event):
    """Тонкий адаптер: карточка rieltorcollector -> строка для normalize_candidate.

    Поля карточки (/listings/{id}) уже совпадают по именам с тем, что
    ожидает Stage 3 (id, price, rooms, square_m2, district, street,
    seller_type, ...) — переименовывать нечего. Из карточки нет полей,
    которые несёт СОБЫТИЕ (/listings/changes): reason, old_price (цена
    ДО изменения), event_id и observed_at. Последние два нужны компактному
    журналу прогнозов. Остальные поля карточки (status, first_seen_at и т.п.) для
    Stage 3 не нужны, но и не мешают — normalize_candidate/score_row
    читают только свои ключи, лишние молча игнорируются при записи в CSV.
    """
    row = dict(card)
    row["reason"] = event.get("reason")
    row["old_price"] = event.get("old_price")
    row["collector_event_id"] = event.get("event_id")
    row["collector_observed_at"] = event.get("observed_at")
    return row


def normalize_collector_row(row):
    return normalize_candidate(row, "collector", row.get("reason"))


def passes_basic_sanity(row):
    """Только техническая проверка; owner/photo/red_flags здесь НЕ режем."""
    rid = row.get("id")
    price = to_float(row.get("price") or row.get("new_price"))
    return bool(rid) and price is not None and price > 0


# Контекст скоринга (индексы, приоры, наклоны по площади) зависит только от
# файла baseline, а он меняется раз в 12 часов. Считать его заново на каждый
# пакет незачем, тем более что оценка наклона по площади теперь включает
# бутстрап. Кэшируем контекст рабочего baseline по (путь, mtime, размер):
# после замены файла ключ меняется, и контекст пересчитывается сам.
# Проверочные загрузки (validate_downloaded_baseline) идут по другому пути и
# в кэш не попадают.
_STAGE3_CTX_CACHE = {"key": None, "ctx": None}


def _baseline_cache_key(path):
    st = os.stat(path)
    return (os.path.abspath(path), st.st_mtime_ns, st.st_size)


def load_stage3(baseline_path=None):
    use_cache = baseline_path is None or (
        os.path.abspath(baseline_path) == os.path.abspath(BASELINE_CSV)
    )
    baseline_path = baseline_path or BASELINE_CSV
    if not os.path.exists(baseline_path):
        raise FileNotFoundError(
            f"Не найден baseline: {baseline_path}. "
            "Он скачивается с rieltor-cleaner (см. refresh_baseline)."
        )

    if use_cache:
        key = _baseline_cache_key(baseline_path)
        if _STAGE3_CTX_CACHE["key"] == key:
            return _STAGE3_CTX_CACHE["ctx"]
    ctx = _build_stage3_context(baseline_path)
    if use_cache:
        _STAGE3_CTX_CACHE["key"] = key
        _STAGE3_CTX_CACHE["ctx"] = ctx
    return ctx


def _build_stage3_context(baseline_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("stage3_benchmark_v3", STAGE3_MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # prepare_baseline() = load_rows + enrich + mark_price_outliers.
    # Вызываем именно её, а не голый enrich(): mark_price_outliers —
    # единственное место, где применяются санитарные границы цены
    # (PRICE_M2_ABS_MIN/MAX, SQUARE_ABS_MIN/MAX, робастный log-MAD тест).
    # Без неё одна опечатка в цене сдвигает медианы города, квартили
    # классов зданий и приор, то есть бенчмарк ВСЕХ объявлений, а не
    # только своего собственного.
    baseline_rows, usable = module.prepare_baseline(baseline_path)
    if not baseline_rows:
        raise ValueError(f"Baseline пустой: {baseline_path}")
    required = {"id", "rooms", "square_m2"}
    missing = sorted(required - set(baseline_rows[0].keys()))
    if missing:
        raise ValueError(f"Baseline не совместим со Stage 3, нет колонок: {missing}")
    if len(usable) < 10:
        raise ValueError(f"В baseline только {len(usable)} пригодных строк — скоринг небезопасен")

    # Всё, что score_row ожидает получить уже посчитанным. Набор и порядок
    # повторяют run() внутри stage3_benchmark_v3.py — это единственный
    # достоверный источник того, из чего собирается контекст скоринга.
    # `segments` в v3 не читается ничем: параметр сохранён ради обратной
    # совместимости сигнатуры, и run() сам передаёт туда None.
    citywide = module.citywide_median_by_rooms(usable)
    q25, q75 = module.price_segment_boundaries(usable)
    room_bounds = module.room_segment_boundaries(usable)
    score_index = module.building_scores_index(usable, q25, q75, room_bounds)
    class_priors = module.prior_medians_by_rooms_class(usable, score_index)
    slope_notes = []
    area_slopes = module.area_slopes_by_rooms(usable, notes=slope_notes)
    ref_areas = module.median_area_by_rooms(usable)
    spatial_index = module.SpatialIndex(usable)
    building_index = module.BuildingIndex(usable)
    repair_premium_signals = module.build_repair_premium_signals(
        usable, q25, q75, room_bounds
    )
    print("📐 [stage3] наклон по площади (внутри домов): " + "; ".join(sorted(slope_notes)))

    return {
        "module": module,
        "usable": usable,
        "citywide": citywide,
        "q25": q25,
        "q75": q75,
        "room_bounds": room_bounds,
        "score_index": score_index,
        "class_priors": class_priors,
        "area_slopes": area_slopes,
        "ref_areas": ref_areas,
        "spatial_index": spatial_index,
        "building_index": building_index,
        "repair_premium_signals": repair_premium_signals,
    }


def score_incoming(rows):
    ctx = load_stage3()
    module = ctx["module"]
    results = []

    for row in rows:
        # score_row сам вызывает enrich() первой строкой, поэтому передаём
        # сырую строку: так `row` остаётся без служебных `_`-полей и
        # merged не тащит их в CSV.
        result = module.score_row(
            row,
            ctx["usable"],
            ctx["citywide"],
            ctx["score_index"],
            ctx["q25"],
            ctx["q75"],
            soft_target=True,
            room_bounds=ctx["room_bounds"],
            class_priors=ctx["class_priors"],
            area_slopes=ctx["area_slopes"],
            spatial_index=ctx["spatial_index"],
            building_index=ctx["building_index"],
            ref_areas=ctx["ref_areas"],
            repair_premium_signals=ctx["repair_premium_signals"],
        )
        merged = dict(row)
        merged.update(result)
        results.append(merged)

    return results


def append_prediction_audit(rows, path=None):
    """Durable-запись прогноза на момент события Collector.

    event_id обеспечивает идемпотентность повторной страницы, а новое
    price_drop того же listing_id сохраняется отдельной строкой.
    """
    path = path or PREDICTION_AUDIT_DB
    predicted_at = utcnow_iso()
    records = []
    for row in rows:
        if str(row.get("source") or "") != "collector":
            continue
        try:
            event_id = int(row.get("collector_event_id"))
        except (TypeError, ValueError):
            continue

        asking_price = to_float(row.get("price"))
        area = to_float(row.get("square_m2"))
        fair_price_m2 = to_float(row.get("base_price_m2"))
        predicted_price = (
            round(fair_price_m2 * area, 2)
            if fair_price_m2 is not None and area is not None and area > 0
            else None
        )
        records.append((
            event_id,
            str(row.get("id") or ""),
            str(row.get("reason") or ""),
            str(row.get("collector_observed_at") or ""),
            predicted_at,
            asking_price,
            predicted_price,
            to_float(row.get("benchmark_confidence")),
            str(row.get("verdict") or ""),
            PREDICTION_MODEL_VERSION,
        ))

    if not records:
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with sqlite3.connect(path, timeout=30) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS predictions (
                collector_event_id INTEGER PRIMARY KEY,
                listing_id TEXT NOT NULL,
                reason TEXT,
                event_at TEXT,
                predicted_at TEXT NOT NULL,
                asking_price REAL,
                predicted_price REAL,
                benchmark_confidence REAL,
                verdict TEXT,
                model_version TEXT NOT NULL
            )
            """
        )
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO predictions (
                collector_event_id, listing_id, reason, event_at,
                predicted_at, asking_price, predicted_price,
                benchmark_confidence, verdict, model_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            records,
        )
        inserted = conn.total_changes - before
    return inserted


MANUAL_REVIEW_MIN_DIFF_PCT = 0.05

# ТРЕБУЕТ ПРОВЕРКИ в Stage 3 v3 симметричен: срабатывает и на аномально
# большую скидку, и на аномально большое отклонение ВВЕРХ. Наверх мы его
# не пускаем.
#
# Измерено на baseline: из 224 таких вердиктов 122 — переоценённые
# квартиры, то есть 13% всего потока уведомлений уходило на объявления
# заведомо дороже базы. Пользы в них нет вдвойне: текст самого вердикта
# гласит «вероятнее ошибка когорты, чем реальная переоценка» — то есть
# модель прямо сообщает, что не верит числу, потому что вердикт
# выдаётся только при benchmark_confidence ниже SUSPICIOUS_DIFF_CONF_FLOOR.
# Уведомлять о дорогой квартире на основании базы, которой не доверяем,
# смысла не имеет.
#
# Переоценённые никуда не пропадают: они по-прежнему скорятся, пишутся в
# cache/stage3_incoming_*.csv и попадают в notifications_log — не
# отправляется только push.
SEND_OVERPRICED_REVIEW = False


def candidate_is_sendable(row):
    if not passes_basic_sanity(row):
        return False
    # Диагностический режим: пропускаем всё, что вообще поддаётся оценке.
    # passes_basic_sanity выше остаётся — строка без валидной цены не
    # несёт никакой информации даже для разбора.
    if telegram_send_all_verdicts():
        return True
    if row.get("verdict") == "НАХОДКА":
        return True
    # ТРЕБУЕТ ПРОВЕРКИ в Stage 3 v3 СИММЕТРИЧЕН: он срабатывает и на
    # аномально большую скидку, и на аномально большое отклонение ВВЕРХ
    # (verdict_from_diff, ветка diff_pct <= -SUSPICIOUS_DIFF_THRESHOLD).
    # Наверх не пускаем — см. SEND_OVERPRICED_REVIEW выше.
    if row.get("verdict") == "ТРЕБУЕТ ПРОВЕРКИ":
        if SEND_OVERPRICED_REVIEW:
            return True
        # Направление определяется знаком diff_pct: положительный —
        # дешевле базы. Отдельного поля с направлением Stage 3 не даёт.
        # При неразобранном diff_pct не шлём: без знака невозможно
        # отличить скидку от переоценки.
        diff = to_float(row.get("diff_pct"))
        return diff is not None and diff > 0
    # Manual-review objects (red flag present) are useful only when they are
    # also materially cheaper than the benchmark; otherwise we would notify
    # on every red flag. Threshold lowered from 0.10 to 0.05: at 0.10 a
    # moderate discount (6-9%) with an unrelated/ambiguous red flag was
    # silently dropped, even though 6-9% off is exactly the range a human
    # reviewer would want to weigh against the flag themselves.
    if row.get("verdict") == "РУЧНАЯ ПРОВЕРКА":
        try:
            return float(row.get("diff_pct")) >= MANUAL_REVIEW_MIN_DIFF_PCT
        except (TypeError, ValueError):
            return False
    return False


def dedupe_against_registry(candidates, registry):
    """
    Единое правило для new И price_drop, из любого трека:
    отправляем, только если id ещё не отправляли ВООБЩЕ, либо текущая
    цена строго меньше цены на момент последней отправки. Реестр
    обновляется тут же (in-place), сохранить на диск — на вызывающей
    стороне (после обработки ОБОИХ треков за цикл, если понадобится
    объединить — но по факту треки работают в разных циклах, так что
    сохраняем сразу после каждого).
    """
    to_send = []
    now = utcnow_iso()
    for row in candidates:
        rid = row.get("id")
        # ОБЯЗАТЕЛЬНО через to_float: после clean_incoming_rows строки
        # перечитываются из CSV, поэтому price здесь — str, и без
        # приведения сравнение уходит в лексикографическое (см.
        # load_registry). Тип также зависел от того, отработал ли
        # incoming_clean: на fallback-пути price оставался float, и
        # сравнение float < str падало с TypeError.
        price = to_float(row.get("price"))
        if not rid or price is None:
            continue  # без валидной цены сравнивать не с чем — не наша забота тут чистить
        rid = str(rid)
        row["price"] = price  # нормализуем и для лога, и для Telegram

        prev = registry.get(rid)
        prev_price = to_float(prev.get("price")) if prev else None
        if prev is None or prev_price is None or price < prev_price:
            row["sent_at"] = now
            to_send.append(row)
            registry[rid] = {"price": price, "reason": row["reason"], "sent_at": now}
        # иначе: цена не ниже последней отправленной (bump/повтор) — пропускаем молча

    return to_send


# ============================== ЦИКЛЫ ==============================

registry_lock = asyncio.Lock()


class IncomingCleanFailed(RuntimeError):
    """Очистка входящих провалилась — цикл не должен идти дальше."""


async def clean_incoming_rows(rows, source):
    """
    Входящие объявления проходят отдельный SOFT-cleaner
    (incoming_clean_v2.py). stage1_clean.py и build_baseline_table.py
    здесь не участвуют вообще. stage2_llm_analyze.py не запускается как
    CLI, но incoming_clean_v2 импортирует из него analyze_all — то есть
    LLM-разбор по этому пути выполняется.
    """
    if not rows:
        return []

    import tempfile

    raw_path = os.path.join(CACHE_DIR, f"_incoming_{source}_raw.csv")
    clean_path = os.path.join(CACHE_DIR, f"_incoming_{source}_clean.csv")
    cache_path = INCOMING_CACHE_COLLECTOR

    # Сохраняем весь worker output без market filtering.
    fieldnames = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)

    with open(raw_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    code = await run_subprocess(
        INCOMING_CLEAN_SCRIPT,
        [
            "--input", raw_path,
            "--output", clean_path,
            "--cache", cache_path,
            "--concurrency", "4",
        ],
        cwd=BASE_DIR,
    )
    if code != 0 or not os.path.exists(clean_path):
        # Раньше здесь возвращались исходные строки и цикл шёл дальше.
        # Это неверно по двум причинам. Во-первых, incoming_clean теперь
        # выходит с кодом 2, когда Stage 2 не отработал ни по одной
        # строке: premium_markers пуст у всех, red_flags пуст у всех, и
        # скоринг получается систематически смещённым, но внешне
        # валидным. Во-вторых, у исходных строк price — float, а у
        # прочитанных из CSV — str, так что fallback ещё и подсовывал
        # дальше по цепочке другой тип, чем happy path.
        raise IncomingCleanFailed(
            f"incoming_clean завершился с кодом {code} для трека '{source}'. "
            f"Цикл прерван: скорить и рассылать по неочищенным данным нельзя."
        )

    return read_csv_rows(clean_path)


class LlmRowsFailed(RuntimeError):
    """Часть объявлений страницы не прошла Stage 2. Остальные уже
    обработаны; страницу нужно повторить, курсор не двигать."""

    def __init__(self, ids):
        super().__init__(f"Stage 2 не разобрал {len(ids)} объявл.: {', '.join(ids[:5])}")
        self.ids = ids


async def process_and_notify(candidates_by_source, source, drop_llm_failures=False):
    """
    worker -> dedicated soft incoming cleaner -> Stage 3 against the
    already-existing frozen baseline -> registry dedup.

    ВАЖНО:
    - baseline НИКОГДА не создаётся/перестраивается этим оркестратором;
    - stage1_clean.py CLI не запускается; stage2_llm_analyze.py не
      запускается как CLI, но его analyze_all вызывается из
      incoming_clean_v2;
    - incoming не проходит baseline filtering.
    """
    rows = candidates_by_source
    if not rows:
        return []

    rows = await clean_incoming_rows(rows, source)
    if not rows:
        return []

    # Объявления, по которым Stage 2 не сработал (llm_skipped_error), не
    # скорим и не отправляем: без red_flags квартира с плесенью или после
    # пожара ушла бы как НАХОДКА и навсегда попала бы в реестр. Остальные
    # идут дальше как обычно.
    failed_ids = [str(r.get("id")) for r in rows if _is_true(r.get("llm_skipped_error"))]
    if failed_ids:
        rows = [r for r in rows if not _is_true(r.get("llm_skipped_error"))]
        print(f"   ⚠️ [{source}] Stage 2 не разобрал {len(failed_ids)} объявл.: {', '.join(failed_ids[:5])}")

    to_send = await _score_and_notify(rows, source) if rows else []

    if failed_ids:
        if drop_llm_failures:
            print(f"   🗑️  [{source}] пропускаю после повторных неудач: {', '.join(failed_ids)}")
        else:
            raise LlmRowsFailed(failed_ids)
    return to_send


def _is_true(value):
    return str(value).strip().lower() in ("true", "1", "yes")


async def _score_and_notify(rows, source):
    """Скоринг против baseline, фильтр вердиктов, дедуп по реестру, журнал
    и Telegram — для строк, уже прошедших очистку и Stage 2."""
    # Не отбрасываем строки с плохой ценой/площадью здесь.
    # Stage 3 сохранит их с недостаточной уверенностью; отправка
    # всё равно невозможна без валидной цены.
    scored = score_incoming(rows)

    # До Telegram-фильтра: в историю попадают все рассчитанные квартиры.
    # Ошибка записи прерывает обработку, а повтор безопасен благодаря PK.
    audit_inserted = append_prediction_audit(scored)
    if audit_inserted:
        print(f"   🧾 [{source}] сохранено прогнозов для будущего eval: {audit_inserted}")

    try:
        fields = []
        for row in scored:
            for key in row:
                # `_`-prefixed keys are internal payloads (e.g.
                # _cohort_members, a list of dicts for the notification
                # layer). Writing them would dump a Python repr into a
                # CSV cell.
                if key.startswith("_"):
                    continue
                if key not in fields:
                    fields.append(key)
        output_path = STAGE3_OUTPUT_COLLECTOR_CSV
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(scored)
    except Exception as exc:
        print(f"   ⚠️ Не удалось записать Stage 3 output ({source}): {exc}")

    eligible = [r for r in scored if candidate_is_sendable(r)]

    async with registry_lock:
        registry = load_registry(EVER_SENT_IDS_FILE)
        to_send = dedupe_against_registry(eligible, registry)
        if to_send:
            # Outbox-first: don't mark an item as sent until the durable
            # notification log accepted it. This avoids the failure mode
            # "registry says sent, but log write crashed".
            append_notifications(NOTIFICATIONS_LOG_CSV, to_send)
            save_registry(EVER_SENT_IDS_FILE, registry)

    # Telegram — best-effort слой ПОВЕРХ уже сохранённого durable-лога.
    # Намеренно вне registry_lock: сетевой запрос не должен держать лок
    # дольше необходимого.
    if to_send:
        await notify_telegram(to_send)

    return to_send


async def periodic_loop(name, interval_sec, job):
    """Start immediately, then on a fixed start-time cadence.

    We do not use `sleep(interval)` after the job because that makes the real
    period = job_duration + interval. If a run is late, missed slots are
    skipped rather than launching overlapping subprocesses.
    """
    loop = asyncio.get_running_loop()
    next_run = loop.time()
    while True:
        now = loop.time()
        if now < next_run:
            await asyncio.sleep(next_run - now)
        started = loop.time()
        await job()
        next_run += interval_sec
        # Skip missed schedule slots after a long run.
        now = loop.time()
        if next_run <= now:
            missed = int((now - next_run) // interval_sec) + 1
            next_run += missed * interval_sec
        duration = loop.time() - started
        print(f"   ⏱️ [{name}] длительность цикла: {duration:.1f}s; следующий запуск через {max(0, next_run-loop.time()):.1f}s")


class CollectorNetworkError(RuntimeError):
    """Сеть/DNS/таймаут при обращении к rieltorcollector — не HTTP-код,
    а сбой самого запроса."""


class CollectorAuthError(RuntimeError):
    """401 от rieltorcollector — неверный или отсутствующий X-API-Key."""


class CollectorServerError(RuntimeError):
    """5xx или иной неожиданный HTTP-код от rieltorcollector."""


def load_collector_cursor(path):
    """last_event_id с прошлого запуска. Без файла/при порче — with 0,
    т.е. с начала ленты; дубли на первом проходе после потери курсора
    погасит ever_sent_ids.json (dedupe_against_registry), не мы."""
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return int(data.get("last_event_id", 0))
    except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
        print(f"   ⚠️ Не удалось прочитать курсор {path} ({exc}), начинаю с since=0")
        return 0


def save_collector_cursor(path, cursor):
    """Атомарная запись (tmp + fsync + os.replace), как save_registry —
    краш процесса посреди записи не должен портить курсор."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last_event_id": cursor}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _collector_get(path, params=None):
    """Синхронный GET к rieltorcollector (запускается через run_in_executor,
    как _telegram_post, чтобы не блокировать event loop)."""
    import urllib.request
    import urllib.parse
    import urllib.error

    url = RIELTOR_COLLECTOR_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    if RIELTOR_COLLECTOR_API_KEY:
        req.add_header("X-API-Key", RIELTOR_COLLECTOR_API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=COLLECTOR_TIMEOUT_SEC) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise CollectorNetworkError(str(exc.reason)) from exc


async def _collector_get_json(path, params=None, allow_404=False):
    """JSON GET + разбор кодов ошибок. Бросает CollectorAuthError/
    CollectorServerError/CollectorNetworkError — вызывающая сторона
    (collector_job) решает, что делать: по ТЗ это "залогировать, не
    двигать курсор, повторить на следующем плановом опросе".
    allow_404=True — для /listings/{id}, где 404 разбирается отдельно
    (возвращаем None, а не бросаем исключение).
    """
    loop = asyncio.get_running_loop()
    try:
        status, body = await loop.run_in_executor(None, _collector_get, path, params)
    except CollectorNetworkError:
        raise
    except Exception as exc:
        raise CollectorNetworkError(str(exc)) from exc

    if status == 401:
        raise CollectorAuthError(body[:300])
    if status == 404 and allow_404:
        return None
    if status >= 500 or status != 200:
        raise CollectorServerError(f"HTTP {status}: {body[:300]}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise CollectorServerError(f"невалидный JSON в ответе {path}: {exc}") from exc


async def fetch_changes(since, limit):
    return await _collector_get_json("/listings/changes", {"since": since, "limit": limit})


async def fetch_listing(advert_id):
    """Полная карточка объявления. None — легитимный 404 разрешён
    вызывающей стороне отличить от бага (см. collector_job)."""
    return await _collector_get_json(f"/listings/{advert_id}", allow_404=True)


class BaselineDownloadError(RuntimeError):
    """Не удалось получить/принять baseline: локальные данные остаются как были."""


def load_baseline_state():
    if not os.path.exists(BASELINE_STATE_FILE):
        return {}
    try:
        with open(BASELINE_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_baseline_state(built_at):
    tmp = BASELINE_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"built_at": built_at, "applied_at": utcnow_iso()}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, BASELINE_STATE_FILE)


def _download_baseline(tmp_path, prev_built_at):
    """Синхронно качает /baseline/clean.csv в tmp_path (run_in_executor).

    Возвращает ("unchanged"|"downloaded", built_at). X-Built-At сверяется
    по заголовкам ДО чтения тела: если файл не менялся, ~весь CSV не качаем.
    Обрыв определяем по Content-Length — http.client при преждевременном
    закрытии соединения в read(n) молча отдаёт короткий хвост.
    """
    import http.client
    import urllib.error
    import urllib.request

    req = urllib.request.Request(RIELTOR_CLEANER_URL + "/baseline/clean.csv", method="GET")
    req.add_header("X-API-Key", RIELTOR_CLEANER_API_KEY)
    try:
        resp = urllib.request.urlopen(req, timeout=BASELINE_TIMEOUT_SEC)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise BaselineDownloadError("401: неверный RIELTOR_CLEANER_API_KEY") from exc
        if exc.code == 503:
            raise BaselineDownloadError("503: baseline ещё не собран на стороне rieltor-cleaner") from exc
        raise BaselineDownloadError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        raise BaselineDownloadError(f"сеть: {exc}") from exc

    with resp:
        built_at = resp.headers.get("X-Built-At")
        if built_at and built_at == prev_built_at:
            return "unchanged", built_at
        expected = resp.headers.get("Content-Length")
        written = 0
        try:
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                f.flush()
                os.fsync(f.fileno())
        except (http.client.HTTPException, OSError) as exc:
            raise BaselineDownloadError(f"обрыв скачивания после {written} байт: {exc}") from exc

    if written == 0:
        raise BaselineDownloadError("получен пустой файл")
    if expected is not None and expected.isdigit() and int(expected) != written:
        raise BaselineDownloadError(f"файл усечён: получено {written} из {expected} байт")
    return "downloaded", built_at


def validate_downloaded_baseline(path):
    """Проверяем скачанный файл ТЕМ ЖЕ кодом, которым он потом будет
    читаться (load_stage3 -> prepare_baseline), до замены рабочего."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        header = next(csv.reader(f), [])
    missing = [c for c in BASELINE_REQUIRED_COLUMNS if c not in header]
    if missing:
        raise BaselineDownloadError(f"в файле нет обязательных колонок: {missing}")
    try:
        ctx = load_stage3(path)
    except Exception as exc:
        raise BaselineDownloadError(f"Stage 3 не принимает файл: {type(exc).__name__}: {exc}") from exc
    return len(ctx["usable"])


async def refresh_baseline():
    """Один заход: скачать -> проверить -> атомарно заменить BASELINE_CSV.

    Полная замена, не дополнение: объявления, которых нет в новом файле,
    исчезают. На любой ошибке (401/503/сеть/обрыв/битый файл) рабочий
    baseline не трогается. Возвращает True, если состояние актуально
    (заменили или файл не менялся), False — если нужно повторить раньше.
    """
    os.makedirs(BASELINE_DIR, exist_ok=True)
    prev_built_at = load_baseline_state().get("built_at") if os.path.isfile(BASELINE_CSV) else None
    tmp_path = BASELINE_CSV + ".download"
    loop = asyncio.get_running_loop()
    try:
        outcome, built_at = await loop.run_in_executor(None, _download_baseline, tmp_path, prev_built_at)
        if outcome == "unchanged":
            print(f"   ℹ️ [baseline] X-Built-At={built_at} не изменился — оставляю текущий файл.")
            return True
        usable = validate_downloaded_baseline(tmp_path)
        # Замена — в потоке event loop: score_incoming синхронный, поэтому
        # между чтением файла скорингом и этим replace вклиниться нельзя.
        os.replace(tmp_path, BASELINE_CSV)
        save_baseline_state(built_at)
        print(f"   ✅ [baseline] заменён: X-Built-At={built_at}, пригодных строк {usable}.")
        return True
    except BaselineDownloadError as exc:
        print(f"   ⚠️ [baseline] {exc}. Оставляю прежний baseline, повтор позже.")
        return False
    except Exception as exc:
        print(f"   💥 [baseline] неожиданный сбой: {type(exc).__name__}: {exc}. Оставляю прежний baseline.")
        return False
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


async def baseline_loop():
    while True:
        print(f"\n=== [baseline] обновление {utcnow_iso()} ===")
        try:
            ok = await refresh_baseline()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"   💥 [baseline] сбой цикла: {type(exc).__name__}: {exc}")
            ok = False
        await asyncio.sleep(BASELINE_REFRESH_INTERVAL_SEC if ok else BASELINE_RETRY_INTERVAL_SEC)


_llm_failures = {}  # курсор начала страницы -> сколько раз подряд Stage 2 её не осилил


def _event_too_old(event):
    """True, если событие старше окна Stage 2/Telegram (по observed_at).

    Это окно не относится к фотооценке: photo gateway должен увидеть каждую
    доступную карточку из накопившейся ленты, даже если Telegram она уже не нужна.
    Нет/не разбирается observed_at — считаем событие свежим."""
    if COLLECTOR_MAX_EVENT_AGE_H <= 0:
        return False
    raw = str(event.get("observed_at") or "").strip()
    if not raw:
        return False
    try:
        observed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - observed).total_seconds() / 3600
    return age_h > COLLECTOR_MAX_EVENT_AGE_H


async def _find_feed_end():
    """event_id последнего события ленты, без запросов карточек."""
    cursor = 0
    while True:
        changes = await fetch_changes(cursor, 500)
        cursor = changes.get("next_since", cursor)
        if changes.get("count", 0) < 500:
            return cursor


def _attach_interior_result(row, interior):
    """Copy only the stable photo-evaluation contract into a scoring row."""
    status = interior.get("interior_status_now", interior.get("status"))
    score = interior.get("interior_score_now", interior.get("score"))
    coverage = interior.get("interior_coverage_now", interior.get("coverage"))
    version = interior.get(
        "interior_evaluator_version_now", interior.get("evaluator_version")
    )
    row["interior_status"] = status or ""
    row["interior_score"] = "" if score is None else score
    row["interior_coverage"] = coverage or ""
    row["interior_evaluator_version"] = version or ""
    return row


async def interior_decision_loop(queue_store):
    """Score fresh cards only after photo evaluation, with a bounded wait.

    The Collector cursor advances after enqueue_decision(), so a slow photo
    request cannot block or lose the event feed. The decision row itself is
    durable and is retried after restarts.
    """
    while True:
        jobs = queue_store.claim_decisions(
            max_wait_seconds=INTERIOR_DECISION_MAX_WAIT_SEC,
            limit=20,
        )
        if not jobs:
            await asyncio.sleep(2)
            continue
        rows = []
        by_listing = {}
        for job in jobs:
            row = json.loads(job["row_json"])
            if job.get("interior_status_now"):
                _attach_interior_result(row, job)
            rows.append(row)
            by_listing.setdefault(str(job["listing_id"]), []).append(job)
        job_ids = [int(job["decision_id"]) for job in jobs]
        try:
            await process_and_notify(
                rows, "collector", drop_llm_failures=False,
            )
        except LlmRowsFailed as exc:
            failed = set(exc.ids)
            retry_ids = [
                int(job["decision_id"]) for listing in failed
                for job in by_listing.get(listing, ())
                if int(job.get("attempt_count") or 0) < LLM_FAIL_MAX_ATTEMPTS - 1
            ]
            dropped_ids = [
                int(job["decision_id"]) for listing in failed
                for job in by_listing.get(listing, ())
                if int(job.get("attempt_count") or 0) >= LLM_FAIL_MAX_ATTEMPTS - 1
            ]
            unresolved = set(retry_ids) | set(dropped_ids)
            done_ids = [value for value in job_ids if value not in unresolved]
            done_ids.extend(dropped_ids)
            queue_store.complete_decisions(done_ids)
            queue_store.retry_decisions(
                retry_ids, error=str(exc), delay_seconds=COLLECTOR_POLL_INTERVAL_SEC,
            )
            print(
                f"   ⚠️ [decision] {exc}; повтор={len(retry_ids)}, "
                f"пропущено после {LLM_FAIL_MAX_ATTEMPTS} попыток={len(dropped_ids)}."
            )
        except Exception as exc:
            queue_store.retry_decisions(
                job_ids,
                error=f"{type(exc).__name__}: {exc}",
                delay_seconds=COLLECTOR_POLL_INTERVAL_SEC,
            )
            print(f"   ⚠️ [decision] пакет не обработан: {type(exc).__name__}: {exc}")
        else:
            queue_store.complete_decisions(job_ids)
            print(f"   ✅ [decision] обработано карточек: {len(job_ids)}")


async def collector_job():
    """Один плановый тик опроса rieltorcollector.

    Пагинация — внутренний while, а не отдельная задача: пока страница
    ленты заполнена целиком (count == COLLECTOR_PAGE_LIMIT), значит
    событий больше, чем влезло, и следующую страницу нужно забрать сразу
    же, не дожидаясь следующего planового тика (см. ТЗ). Курсор
    При включённой фотооценке свежая карточка сначала durable сохраняется
    в SQLite-очереди решения, и только после этого двигается курсор; Stage 2/3
    выполнит отдельный decision worker. При выключенной фотооценке действует
    прежний синхронный путь. Ошибка до durable-записи оставляет курсор на месте.
    """
    print(f"\n=== [collector] цикл {utcnow_iso()} ===")
    if not os.path.isfile(BASELINE_CSV):
        # Первый запуск на пустом диске: baseline ещё скачивается. Скорить
        # нечем, а курсор двигать нельзя — события подождут.
        print(f"   ⏳ [collector] baseline ещё не загружен ({BASELINE_CSV}) — тик пропущен.")
        return
    first_run = not os.path.exists(COLLECTOR_STATE_FILE)
    cursor = load_collector_cursor(COLLECTOR_STATE_FILE)

    if first_run and COLLECTOR_START_FROM == "end":
        # Без сохранённого курсора since=0 заставил бы разобрать ВСЮ историю
        # ленты: по запросу на карточку и вызову LLM на каждое событие.
        # Встаём в конец ленты — это дешёвые запросы: только события, без карточек.
        try:
            cursor = await _find_feed_end()
        except CollectorAuthError as exc:
            print(f"   ⛔ [collector] неверный X-API-Key: {exc}")
            return
        except (CollectorNetworkError, CollectorServerError) as exc:
            print(f"   ⚠️ [collector] не удалось определить конец ленты: {exc}. Повтор на следующем цикле.")
            return
        save_collector_cursor(COLLECTOR_STATE_FILE, cursor)
        print(f"   🏁 [collector] первый запуск: курсор поставлен в конец ленты (event_id={cursor}).")

    while True:
        try:
            changes = await fetch_changes(cursor, COLLECTOR_PAGE_LIMIT)
        except CollectorAuthError as exc:
            print(f"   ⛔ [collector] неверный X-API-Key: {exc}")
            return
        except (CollectorNetworkError, CollectorServerError) as exc:
            print(
                f"   ⚠️ [collector] {type(exc).__name__} на /listings/changes: {exc}. "
                "Курсор не продвигаю, повтор на следующем цикле."
            )
            return

        events = changes.get("events") or []
        count = changes.get("count", len(events))
        next_since = changes.get("next_since", cursor)

        if not events:
            print(f"   ℹ️ [collector] новых событий нет (since={cursor}).")
            return

        wanted, skipped_reason, old_for_notifications = [], 0, 0
        for event in events:
            if event.get("reason") not in COLLECTOR_REASONS:
                skipped_reason += 1
            else:
                wanted.append(event)
                if _event_too_old(event):
                    old_for_notifications += 1
        if skipped_reason or old_for_notifications:
            print(
                f"   [collector] не new/price_drop — {skipped_reason}; "
                f"старых для Stage 2/Telegram (> {COLLECTOR_MAX_EVENT_AGE_H}ч) — "
                f"{old_for_notifications}. Фотооценка всё равно проверит их."
            )

        rows = []
        decisions_enqueued = 0
        aborted = False
        for event in wanted:
            advert_id = event.get("id")
            try:
                card = await fetch_listing(advert_id)
            except CollectorAuthError as exc:
                print(f"   ⛔ [collector] неверный X-API-Key: {exc}")
                return
            except (CollectorNetworkError, CollectorServerError) as exc:
                # Обычная сетевая ошибка/5xx — не привязана к конкретному
                # id. Повторяем ВСЮ страницу целиком на следующем плановом
                # опросе, курсор не двигаем (см. договорённость по ТЗ).
                print(
                    f"   ⚠️ [collector] {type(exc).__name__} на /listings/{advert_id}: {exc}. "
                    "Курсор не продвигаю, повтор на следующем цикле."
                )
                aborted = True
                break
            if card is None:
                # rieltorcollector коммитит событие и карточку в одной
                # транзакции — 404 для id из /listings/changes НЕ должен
                # происходить в принципе. Это баг на их стороне, а не
                # "карточка ещё не готова"; пропускаем событие (иначе вся
                # лента встанет намертво на нём) и громко сообщаем.
                print(
                    f"   🐛 [collector] БАГ rieltorcollector: 404 для id={advert_id} "
                    f"из event_id={event.get('event_id')} (reason={event.get('reason')})"
                )
                continue
            too_old = _event_too_old(event)
            interior = None
            if _interior_gateway is not None:
                interior = _interior_gateway.consider_card(
                    card, event.get("reason"), priority=0 if too_old else 100,
                )
                print(
                    f"   [interior] id={advert_id}: {interior['action']} "
                    f"(status={interior.get('status')})"
                )
            # Возраст события ограничивает только дорогие Stage 2/Stage 3/Telegram.
            # Photo gateway выше уже durable-но переиспользовал или поставил
            # задание для каждой доступной карточки, включая старую ленту.
            if not too_old:
                normalized = normalize_collector_row(adapt_collector_listing(card, event))
                if _interior_store is not None:
                    _attach_interior_result(normalized, interior or {})
                    decision_delivery_key = (interior or {}).get("delivery_key")
                    if (interior or {}).get("status") in TERMINAL_STATUSES:
                        # The complete compact result is already copied into
                        # row_json. It must remain immediately scoreable even
                        # if Cleaner ACK/GC removes the temporary delivery.
                        decision_delivery_key = None
                    _interior_store.enqueue_decision(
                        event_id=int(event["event_id"]),
                        listing_id=advert_id,
                        delivery_key_value=decision_delivery_key,
                        row=normalized,
                    )
                    decisions_enqueued += 1
                else:
                    rows.append(normalized)

        if aborted:
            return

        if rows:
            # Если часть объявлений не прошла Stage 2, страница повторяется
            # (курсор не двигаем; уже отправленные отсеет реестр, успешные
            # разборы лежат в кэше). После LLM_FAIL_MAX_ATTEMPTS неудач подряд
            # проблемные объявления пропускаются.
            prior = _llm_failures.get(cursor, 0)
            try:
                sent = await process_and_notify(
                    rows, "collector",
                    drop_llm_failures=prior >= LLM_FAIL_MAX_ATTEMPTS - 1,
                )
            except LlmRowsFailed as exc:
                _llm_failures[cursor] = prior + 1
                print(
                    f"   ⚠️ [collector] {exc}. Попытка {prior + 1}/{LLM_FAIL_MAX_ATTEMPTS}; "
                    "курсор не продвигаю, повтор на следующем цикле."
                )
                return
            _llm_failures.pop(cursor, None)
            print(f"   📬 [collector] карточек в странице: {len(rows)}, реально отправлено: {len(sent)}")
        elif decisions_enqueued:
            print(
                f"   📥 [collector] свежих карточек durable поставлено на решение: "
                f"{decisions_enqueued}."
            )
        else:
            print(f"   ℹ️ [collector] на странице из {len(events)} событий нечего оценивать.")

        cursor = next_since
        save_collector_cursor(COLLECTOR_STATE_FILE, cursor)

        if count < COLLECTOR_PAGE_LIMIT:
            break
        print(f"   ↻ [collector] страница заполнена целиком (count={count}) — сразу забираю следующую.")


async def safe_periodic_loop(name, interval_sec, job):
    while True:
        try:
            await periodic_loop(name, interval_sec, job)
        except asyncio.CancelledError:
            raise
        except IncomingCleanFailed as exc:
            # Это не «сбой», а намеренный обрыв: данные для скоринга
            # непригодны. Печатаем отдельно, чтобы в логе было видно
            # причину, а не безликое «неожиданный сбой».
            print(f"   ⛔ [{name}] цикл прерван: {exc}")
            print(f"   🔄 [{name}] повтор через {interval_sec}s.")
            await asyncio.sleep(interval_sec)
        except Exception as exc:
            print(f"   💥 [{name}] неожиданный сбой цикла: {type(exc).__name__}: {exc}")
            print(f"   🔄 [{name}] продолжим со следующего запуска через {interval_sec}s.")
            await asyncio.sleep(interval_sec)


async def collector_loop():
    await safe_periodic_loop("collector", COLLECTOR_POLL_INTERVAL_SEC, collector_job)


async def interior_worker_loop(worker_number, queue_store, client, auth_failed):
    cache = RenovationStore(INTERIOR_CACHE_DB)
    worker = InteriorQueueWorker(
        queue_store=queue_store,
        renovation_store=cache,
        client=client,
        retry_delay_seconds=INTERIOR_RETRY_DELAY_SEC,
    )
    try:
        while not auth_failed.is_set():
            try:
                outcome = await worker.run_once()
            except InteriorAuthenticationError as exc:
                auth_failed.set()
                print(f"   [interior-{worker_number}] authorization error; queue paused: {exc}")
                return
            if outcome is None:
                await asyncio.sleep(2)
                continue
            print(
                f"   [interior-{worker_number}] evaluation_id={outcome['evaluation_id']}: "
                f"{outcome['status']}"
            )
    finally:
        cache.close()


async def interior_gc_loop(queue_store):
    while True:
        requeued = queue_store.requeue_errors(older_than_hours=INTERIOR_ERROR_RETRY_H)
        if requeued:
            print(f"   [interior-retry] requeued technical errors: {requeued}")
        details = queue_store.garbage_collect_details(grace_hours=24)
        if details["deleted"]:
            with RenovationStore(INTERIOR_CACHE_DB) as cache:
                pruned = cache.prune_orphans(
                    listing_ids=details["orphaned_listing_ids"],
                    evaluation_keys=details["orphaned_evaluation_keys"],
                )
            print(
                f"   [interior-gc] removed acknowledged rows: {details['deleted']}; "
                f"orphan cache results: {pruned['results']}"
            )
        await asyncio.sleep(3600)


async def main():
    global _interior_gateway, _interior_store
    validate_configuration()
    print(
        "Оркестратор запущен. baseline скачивается с rieltor-cleaner и "
        "целиком заменяет локальный файл; baseline builder и original "
        "Stage 1/2 здесь не запускаются. "
        f"Опрос rieltorcollector каждые {COLLECTOR_POLL_INTERVAL_SEC}s. "
        "При сбое до durable-записи карточки курсор не продвигается. "
        "После записи фотооценка и Stage 2/3 продолжаются из SQLite после рестарта. "
        f"Telegram-доставка: {'включена' if TELEGRAM_ENABLED else 'выключена'}."
    )
    loops = [collector_loop()]
    if TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_USER_ID:
        loops.append(telegram_control_loop())
    if BASELINE_REFRESH_ENABLED:
        loops.append(baseline_loop())
    if INTERIOR_ENABLED:
        from openai import AsyncOpenAI

        queue_store = InteriorOnlineStore(INTERIOR_DB)
        resumed = queue_store.resume_incomplete()
        resumed_decisions = queue_store.resume_decisions()
        _interior_store = queue_store
        _interior_gateway = InteriorGateway(
            store=queue_store,
            baseline=CleanerBaselineIndex(BASELINE_CSV),
        )
        server, _thread = start_interior_api(
            queue_store,
            host=INTERIOR_API_HOST,
            port=INTERIOR_API_PORT,
            api_key=INTERIOR_EXCHANGE_API_KEY,
        )
        print(
            f"   [interior] API started on {INTERIOR_API_HOST}:{INTERIOR_API_PORT}; "
            f"resumed evaluations: {resumed}; decisions: {resumed_decisions}"
        )
        client = AsyncOpenAI(
            api_key=INTERIOR_OPENAI_API_KEY,
            max_retries=0,
            timeout=180.0,
        )
        auth_failed = asyncio.Event()
        loops.extend(
            interior_worker_loop(number, queue_store, client, auth_failed)
            for number in range(1, INTERIOR_CONCURRENCY + 1)
        )
        loops.append(interior_gc_loop(queue_store))
        loops.append(interior_decision_loop(queue_store))
    await asyncio.gather(*loops)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        sys.exit(0)
