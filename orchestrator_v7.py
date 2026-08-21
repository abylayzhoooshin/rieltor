"""
Оркестратор — верхний слой над двумя ПОЛНОСТЬЮ независимыми воркерами
(slow track и fast track). Ничего не знает об их внутренней логике,
просто:
    1. Запускает каждый трек по своему расписанию (fast — часто,
       slow — редко) как ОТДЕЛЬНЫЙ ПРОЦЕСС (subprocess), а не импортом.
    2. После каждого прогона читает их выходные CSV
       (fast_new_listings.csv / price_drops.csv).
    3. Финально дедупит результат через общий реестр ever_sent_ids.json
       и решает, что реально уйдёт пользователю.

ПОЧЕМУ SUBPROCESS, А НЕ ИМПОРТ:
    fast_track.py физически лежит в другой папке (new_monitoring), чем
    v2_krisha_pars_fixed.py (slow_track), и делает
    `from v2_krisha_pars_fixed import ...` — это работает только если
    fast_track.py запускают ИЗ его собственной папки как обычный скрипт
    (Python подставляет папку скрипта в sys.path[0]) или если та папка
    отдельно добавлена в PYTHONPATH. Простой импорт оркестратором
    (`import fast_track`) из другой рабочей директории эту связку
    сломает. Поэтому оркестратор не импортирует ни один из треков —
    запускает их как `python script.py` с cwd = папка скрипта, ровно
    как если бы их руками запустили в терминале. Это заодно и честнее
    архитектурно: "полностью независимые воркеры" — значит и на уровне
    процессов, а не только файлов состояния.

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
    - не парсит сам, не трогает файлы состояния треков (fast_known_ids.json,
      price_state.json) — это их личные дела;
    - раздельные прокси для fast/slow — отдельная задача, сюда не входит.

Запуск:
    python orchestrator.py
    (Ctrl+C — остановить; оба трека доработают своё до следующего сна)

    Перед запуском (PowerShell):
        $env:TELEGRAM_BOT_TOKEN = "12345:AA...";  $env:TELEGRAM_CHAT_ID = "123456789"
        python orchestrator_v7.py
"""

import asyncio
import csv
import json
import os
import sys
from datetime import datetime, timezone

# ============================== ПУТИ (под структуру папок пользователя) ==============================

BASE_DIR = os.environ.get("KRISHA_BASE_DIR", os.path.dirname(os.path.abspath(__file__)))
SLOW_DIR = os.path.join(BASE_DIR, "slow_track")
FAST_DIR = os.path.join(BASE_DIR, "new_monitoring")

# Явно указываем основной slow parser. В проекте есть вторая копия
# (mycop/v2_krisha_pars_fixed.py), но она не должна автоматически
# подбираться оркестратором.
SLOW_PARSER_SCRIPT = os.path.join(
    BASE_DIR, "1_krisha_parser", "slow_track", "v2_krisha_pars_fixed.py"
)
SLOW_ETAP1_SCRIPT = os.path.join(SLOW_DIR, "etap1_clean_data_v2.py")
FAST_TRACK_SCRIPT = os.path.join(FAST_DIR, "fast_track.py")

# Выходные файлы треков — читаем их, но НЕ пишем (это их файлы).
SLOW_PRICE_DROPS_CSV = os.path.join(SLOW_DIR, "price_drops.csv")
FAST_NEW_LISTINGS_CSV = os.path.join(FAST_DIR, "fast_new_listings.csv")

# Актуальные stage-скрипты живут вместе в pipeline/, эталон — в baseline/,
# все временные/промежуточные файлы прогона оркестратора — в cache/
# (не трогаются вручную, безопасно чистить между прогонами).
PIPELINE_DIR = os.path.join(BASE_DIR, "pipeline")
BASELINE_DIR = os.path.join(BASE_DIR, "baseline")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

BASELINE_CSV = os.path.join(BASELINE_DIR, "krisha_astana_baseline.csv")
SLOW_DETAIL_CSV = os.path.join(SLOW_DIR, "krisha_astana_detail.csv")
STAGE3_OUTPUT_FAST_CSV = os.path.join(CACHE_DIR, "stage3_incoming_fast_latest.csv")
STAGE3_OUTPUT_SLOW_CSV = os.path.join(CACHE_DIR, "stage3_incoming_slow_latest.csv")
STAGE3_MODULE = os.path.join(PIPELINE_DIR, "stage3_benchmark_v3.py")
INCOMING_CLEAN_SCRIPT = os.path.join(PIPELINE_DIR, "incoming_clean_v2.py")
INCOMING_CACHE_FAST = os.path.join(CACHE_DIR, "incoming_llm_analysis_cache_fast.json")
INCOMING_CACHE_SLOW = os.path.join(CACHE_DIR, "incoming_llm_analysis_cache_slow.json")

# Файлы САМОГО оркестратора — не трогаются треками.
EVER_SENT_IDS_FILE = os.path.join(BASE_DIR, "ever_sent_ids.json")
NOTIFICATIONS_LOG_CSV = os.path.join(BASE_DIR, "notifications_log_v2.csv")

# ============================== TELEGRAM ==============================

# Дефолты прямо в файле — чтобы не задавать переменные окружения в
# PowerShell перед каждым запуском. Впишите сюда свои значения.
# ⚠️ Файл с реальным токеном не стоит заливать в публичный git/делиться им —
# токен даёт полный доступ к боту (можно писать от его имени кому угодно).
DEFAULT_TELEGRAM_BOT_TOKEN = "8590142190:AAFfF3ZrtUrp57XADpUR-5G_shRUrnPfvMs"
DEFAULT_TELEGRAM_CHAT_ID = "489767497"  # впишите свой chat_id (см. getUpdates)

# Переменные окружения, если заданы, всё ещё имеют приоритет — это
# позволяет при желании переопределить дефолт без правки файла.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", DEFAULT_TELEGRAM_BOT_TOKEN).strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", DEFAULT_TELEGRAM_CHAT_ID).strip()
TELEGRAM_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

TELEGRAM_SEND_DELAY_SEC = 1.2   # запас от flood control (Telegram лимитирует ~1 сообщение/сек в чат)
TELEGRAM_MAX_RETRIES = 3
TELEGRAM_TIMEOUT_SEC = 15

# ============================== РАСПИСАНИЕ ==============================

FAST_INTERVAL_SEC = 5 * 60       # 5 минут — как и было решено (5 страниц окна)
SLOW_INTERVAL_SEC = 60 * 60      # 1 час — полный обход каталога, тяжелее

NOTIFICATIONS_FIELDNAMES = [
    "id", "url", "title", "reason", "price", "old_price", "source", "sent_at",
    "photo_count", "seller_type", "owner_name", "is_identity_confirmed",
    "seller_class", "seller_confidence",
    "verdict", "verdict_reason",
    "diff_pct", "base_price_m2_corrected", "value_score",
    "quality_evidence_score", "price_quality_score", "price_quality_label",
    "benchmark_confidence", "data_confidence",
    "cohort_level", "cohort_size", "data_warnings",
]

# ============================== ОБЩИЕ ХЕЛПЕРЫ ==============================


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_worker_script(preferred, filename):
    """
    Возвращает реальный путь worker-а.
    Сначала используем ожидаемый путь, затем ищем файл в BASE_DIR
    (не глубже 2 уровней). Это защищает от ошибки структуры папок.
    """
    preferred = os.path.abspath(preferred)
    if os.path.isfile(preferred):
        return preferred

    candidates = []
    for root, dirs, files in os.walk(BASE_DIR):
        depth = os.path.relpath(root, BASE_DIR).count(os.sep)
        if depth > 2:
            dirs[:] = []
            continue
        if filename in files:
            candidates.append(os.path.join(root, filename))

    if len(candidates) == 1:
        print(f"   ℹ️ {filename}: использую найденный путь {candidates[0]}")
        return os.path.abspath(candidates[0])

    if not candidates:
        raise FileNotFoundError(
            f"Не найден worker {filename}. Ожидался: {preferred}"
        )

    raise RuntimeError(
        f"Найдено несколько {filename}: {candidates}. "
        "Укажи однозначный путь в orchestrator."
    )


def validate_configuration():
    """Проверяем конфигурацию до запуска обоих циклов."""
    global SLOW_PARSER_SCRIPT, SLOW_ETAP1_SCRIPT, FAST_TRACK_SCRIPT
    global FAST_DIR, SLOW_DIR
    global FAST_NEW_LISTINGS_CSV, SLOW_PRICE_DROPS_CSV, SLOW_DETAIL_CSV

    # Для slow parser путь намеренно НЕ auto-discover:
    # в проекте есть две копии одного имени, и выбор по имени опасен.
    SLOW_PARSER_SCRIPT = os.path.abspath(SLOW_PARSER_SCRIPT)
    if not os.path.isfile(SLOW_PARSER_SCRIPT):
        raise FileNotFoundError(
            f"Основной slow parser не найден: {SLOW_PARSER_SCRIPT}"
        )
    SLOW_ETAP1_SCRIPT = resolve_worker_script(
        SLOW_ETAP1_SCRIPT, "etap1_clean_data_v2.py"
    )
    FAST_TRACK_SCRIPT = resolve_worker_script(
        FAST_TRACK_SCRIPT, "fast_track.py"
    )

    # ВАЖНО: реальное расположение воркеров могло отличаться от
    # BASE_DIR/new_monitoring и BASE_DIR/slow_track (см. resolve_worker_script
    # выше — он ищет файл по всему дереву BASE_DIR, если ожидаемого пути
    # нет). FAST_DIR/SLOW_DIR и все CSV-пути, которые от них зависят,
    # должны пересчитываться ПОСЛЕ auto-discovery, а не браться из
    # исходного (возможно неверного) предположения о структуре папок.
    # Без этого оркестратор молча читает пустой список из несуществующего
    # файла (read_csv_rows на missing path отдаёт []) и каждый цикл
    # тихо считает 0 кандидатов, даже если воркер реально что-то нашёл.
    FAST_DIR = os.path.dirname(FAST_TRACK_SCRIPT)
    SLOW_DIR = os.path.dirname(SLOW_ETAP1_SCRIPT)
    FAST_NEW_LISTINGS_CSV = os.path.join(FAST_DIR, "fast_new_listings.csv")
    SLOW_PRICE_DROPS_CSV = os.path.join(SLOW_DIR, "price_drops.csv")
    SLOW_DETAIL_CSV = os.path.join(SLOW_DIR, "krisha_astana_detail.csv")

    for label, path in [
        ("fast worker", FAST_TRACK_SCRIPT),
        ("slow parser", SLOW_PARSER_SCRIPT),
        ("slow etap1", SLOW_ETAP1_SCRIPT),
        ("baseline", BASELINE_CSV),
        ("incoming cleaner", INCOMING_CLEAN_SCRIPT),
        ("stage3", STAGE3_MODULE),
    ]:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"{label}: файл не найден: {path}")

    print("=== Конфигурация ===")
    print(f"BASE_DIR       = {BASE_DIR}")
    print(f"FAST worker    = {FAST_TRACK_SCRIPT}")
    print(f"SLOW parser    = {SLOW_PARSER_SCRIPT}")
    print(f"SLOW etap1     = {SLOW_ETAP1_SCRIPT}")
    print(f"BASELINE       = {BASELINE_CSV}")
    print(f"INCOMING       = {INCOMING_CLEAN_SCRIPT}")
    print(f"STAGE3         = {STAGE3_MODULE}")
    print(f"FAST_NEW_LISTINGS = {FAST_NEW_LISTINGS_CSV}")
    print(f"SLOW_PRICE_DROPS  = {SLOW_PRICE_DROPS_CSV}")
    print(f"SLOW_DETAIL_CSV   = {SLOW_DETAIL_CSV}")
    if TELEGRAM_ENABLED:
        masked = TELEGRAM_BOT_TOKEN[:6] + "..." if len(TELEGRAM_BOT_TOKEN) > 6 else "..."
        print(f"TELEGRAM       = включён (bot={masked}, chat_id={TELEGRAM_CHAT_ID})")
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
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_registry(path, registry):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
        f.flush()
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def to_notification_row(row):
    """Один и тот же компактный формат используется и для CSV-лога, и для
    текста Telegram-сообщения — чтобы не разъезжались схемы."""
    return {field: row.get(field, "") for field in NOTIFICATIONS_FIELDNAMES}


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
                f"Используется новый файл notifications_log_v2.csv; не дописываем в старый CSV."
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


def format_diff_pct(value):
    try:
        return f"{float(value) * 100:+.1f}%"
    except (TypeError, ValueError):
        return "—"


def format_telegram_message(notif_row):
    """notif_row — уже компактная строка вида to_notification_row(...)."""
    price = notif_row.get("price") or "—"
    old_price = notif_row.get("old_price")
    price_line = f"{price} ₸"
    if old_price not in (None, "", "0", 0):
        price_line += f" (было {old_price} ₸)"

    verdict = notif_row.get("verdict") or "—"
    verdict_icon = "🔥" if verdict == "НАХОДКА" else "🔎"

    lines = [
        f"{verdict_icon} <b>{escape_html(notif_row.get('title') or 'Без названия')}</b>",
        f"💰 {escape_html(price_line)}",
        f"📉 Отклонение от базы: {format_diff_pct(notif_row.get('diff_pct'))}",
        f"🏆 Вердикт: {escape_html(verdict)}",
        f"ℹ️ {escape_html(notif_row.get('verdict_reason') or '')}",
        f"👤 Продавец: {escape_html(notif_row.get('seller_class') or '—')}"
        f" (уверенность {escape_html(notif_row.get('seller_confidence') or '—')})",
        f"📊 Когорта: уровень {escape_html(notif_row.get('cohort_level') or '—')}"
        f", n={escape_html(notif_row.get('cohort_size') or '—')}"
        f", уверенность бенчмарка {escape_html(notif_row.get('benchmark_confidence') or '—')}",
    ]
    if notif_row.get("data_warnings"):
        lines.append(f"⚠️ {escape_html(notif_row['data_warnings'])}")
    url = notif_row.get("url")
    if url:
        lines.append(f'🔗 <a href="{escape_html(url)}">Открыть на Krisha</a>')

    return "\n".join(lines)


def _telegram_post(payload):
    """Синхронный HTTP POST без внешних зависимостей — запускается через
    run_in_executor, чтобы не блокировать event loop."""
    import urllib.request
    import urllib.parse
    import urllib.error

    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(TELEGRAM_API_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TELEGRAM_TIMEOUT_SEC) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


async def send_telegram_message(text):
    if not TELEGRAM_ENABLED:
        return False

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }

    loop = asyncio.get_running_loop()
    for attempt in range(1, TELEGRAM_MAX_RETRIES + 1):
        try:
            status, body = await loop.run_in_executor(None, _telegram_post, payload)
            if status == 200:
                return True
            print(f"   ⚠️ Telegram API вернул {status} (попытка {attempt}/{TELEGRAM_MAX_RETRIES}): {body[:200]}")
        except Exception as exc:
            print(f"   ⚠️ Telegram send сбой (попытка {attempt}/{TELEGRAM_MAX_RETRIES}): {type(exc).__name__}: {exc}")
        if attempt < TELEGRAM_MAX_RETRIES:
            await asyncio.sleep(2 * attempt)
    return False


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
        text = format_telegram_message(notif_row)
        ok = await send_telegram_message(text)
        if ok:
            print(f"   📨 Telegram: отправлено id={notif_row.get('id')} ({notif_row.get('verdict')})")
        else:
            print(
                f"   ⚠️ Telegram: не удалось отправить id={notif_row.get('id')} "
                "после всех попыток — запись всё равно осталась в notifications_log.csv"
            )
        await asyncio.sleep(TELEGRAM_SEND_DELAY_SEC)


def normalize_candidate(row, source, reason):
    """Сохраняем всю строку: Stage 3 нужны rooms/area/location/finish."""
    out = dict(row)
    out["source"] = source
    out["reason"] = reason or out.get("reason") or ("price_drop" if source == "slow_track" else "new")
    if source == "slow_track":
        out["price"] = out.get("price") or out.get("new_price")
        out["old_price"] = out.get("old_price") or out.get("previous_price")
    out["price"] = to_float(out.get("price"))
    out["old_price"] = to_float(out.get("old_price"))
    return out


def merge_with_detail_snapshot(rows):
    """Обогащает sparse worker output последним detail-snapshot по id."""
    detail_rows = read_csv_rows(SLOW_DETAIL_CSV)
    if not detail_rows:
        return rows

    index = {str(r.get("id")): r for r in detail_rows if r.get("id")}
    merged = []
    for row in rows:
        base = dict(index.get(str(row.get("id")), {}))
        base.update({k: v for k, v in row.items() if v not in (None, "")})
        merged.append(base)
    return merged


def normalize_fast_row(row):
    return normalize_candidate(row, "fast_track", row.get("reason") or "new")


def normalize_slow_row(row):
    return normalize_candidate(row, "slow_track", "price_drop")


def passes_basic_sanity(row):
    """Только техническая проверка; owner/photo/red_flags здесь НЕ режем."""
    rid = row.get("id")
    price = to_float(row.get("price") or row.get("new_price"))
    return bool(rid) and price is not None and price > 0


def load_stage3():
    import importlib.util

    if not os.path.exists(BASELINE_CSV):
        raise FileNotFoundError(
            f"Не найден frozen baseline: {BASELINE_CSV}. "
            "Сначала соберите krisha_astana_baseline.csv."
        )

    spec = importlib.util.spec_from_file_location("stage3_benchmark_v2", STAGE3_MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    baseline_rows = module.load_rows(BASELINE_CSV)
    if not baseline_rows:
        raise ValueError(f"Baseline пустой: {BASELINE_CSV}")
    required = {"id", "rooms", "square_m2"}
    missing = sorted(required - set(baseline_rows[0].keys()))
    if missing:
        raise ValueError(f"Baseline не совместим со Stage 3, нет колонок: {missing}")
    baseline = [module.enrich(r) for r in baseline_rows]
    usable = [r for r in baseline if not r["_excluded_baseline"]]
    if len(usable) < 10:
        raise ValueError(f"В baseline только {len(usable)} пригодных строк — скоринг небезопасен")
    segments = module.compute_price_segments(usable)
    citywide = module.citywide_median_by_rooms(usable)

    return module, usable, segments, citywide


def score_incoming(rows):
    module, usable, segments, citywide = load_stage3()
    results = []

    for row in rows:
        target = module.enrich(row)
        result = module.score_row(
            target,
            usable,
            segments,
            citywide,
            soft_target=True,
        )
        merged = dict(row)
        merged.update(result)
        results.append(merged)

    return results


def candidate_is_sendable(row):
    if not passes_basic_sanity(row):
        return False
    if row.get("verdict") == "НАХОДКА":
        return True
    # Manual-review objects are useful only when they are also materially
    # cheaper than the benchmark; otherwise we would notify on every red flag.
    if row.get("verdict") == "РУЧНАЯ ПРОВЕРКА":
        try:
            return float(row.get("diff_pct")) >= 0.10
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
        price = row.get("price")
        if rid is None or price is None:
            continue  # без валидной цены сравнивать не с чем — не наша забота тут чистить

        prev = registry.get(rid)
        if prev is None or price < prev.get("price", float("inf")):
            row["sent_at"] = now
            to_send.append(row)
            registry[rid] = {"price": price, "reason": row["reason"], "sent_at": now}
        # иначе: цена не ниже последней отправленной (bump/повтор) — пропускаем молча

    return to_send


# ============================== ЦИКЛЫ ==============================

registry_lock = asyncio.Lock()


async def clean_incoming_rows(rows, source):
    """
    Входящие объявления проходят отдельный SOFT-cleaner.
    Оригинальные stage1_clean.py/stage2_llm_analyze.py CLI и build_baseline_table
    здесь НЕ запускаются.
    """
    if not rows:
        return []

    import tempfile

    raw_path = os.path.join(CACHE_DIR, f"_incoming_{source}_raw.csv")
    clean_path = os.path.join(CACHE_DIR, f"_incoming_{source}_clean.csv")
    cache_path = INCOMING_CACHE_FAST if source == "fast" else INCOMING_CACHE_SLOW

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
        print("   ⚠️ incoming_clean не отработал; используем исходные строки.")
        return rows

    return read_csv_rows(clean_path)


async def process_and_notify(candidates_by_source, source):
    """
    worker -> dedicated soft incoming cleaner -> Stage 3 against the
    already-existing frozen baseline -> registry dedup.

    ВАЖНО:
    - baseline НИКОГДА не создаётся/перестраивается этим оркестратором;
    - original stage1_clean.py/stage2_llm_analyze.py НЕ запускаются;
    - incoming не проходит baseline filtering.
    """
    rows = merge_with_detail_snapshot(candidates_by_source)
    if not rows:
        return []

    rows = await clean_incoming_rows(rows, source)
    if not rows:
        return []

    # Не отбрасываем строки с плохой ценой/площадью здесь.
    # Stage 3 сохранит их с недостаточной уверенностью; отправка
    # всё равно невозможна без валидной цены.
    scored = score_incoming(rows)

    try:
        fields = []
        for row in scored:
            for key in row:
                if key not in fields:
                    fields.append(key)
        output_path = STAGE3_OUTPUT_FAST_CSV if source == "fast" else STAGE3_OUTPUT_SLOW_CSV
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
    # Намеренно вне registry_lock: сетевой запрос не должен блокировать
    # второй трек.
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


async def fast_job():
    print(f"\n=== [fast] цикл {utcnow_iso()} ===")
    code = await run_subprocess(FAST_TRACK_SCRIPT, [], cwd=os.path.dirname(FAST_TRACK_SCRIPT))
    if code != 0:
        print("   ⛔ [fast] worker завершился с ошибкой; старый CSV НЕ обрабатываем.")
        return
    if not os.path.exists(FAST_NEW_LISTINGS_CSV):
        print(
            f"   ⚠️ [fast] worker отработал успешно, но {FAST_NEW_LISTINGS_CSV} "
            "не найден — вероятна рассинхронизация путей, проверь FAST_DIR."
        )
    rows = [normalize_fast_row(r) for r in read_csv_rows(FAST_NEW_LISTINGS_CSV)]
    sent = await process_and_notify(rows, "fast")
    print(f"   📬 [fast] кандидатов из прогона: {len(rows)}, реально отправлено: {len(sent)}")


async def slow_job():
    print(f"\n=== [slow] цикл {utcnow_iso()} ===")
    code = await run_subprocess(SLOW_PARSER_SCRIPT, ["all"], cwd=os.path.dirname(SLOW_PARSER_SCRIPT))
    if code != 0:
        print("   ⛔ [slow] parser завершился с ошибкой; старый price_drops НЕ обрабатываем.")
        return
    # Этот скрипт оставляем, пока не проверен его контракт: он может быть
    # именно тем, что строит price_drops.csv. Но это НЕ baseline Stage 1/2.
    code = await run_subprocess(SLOW_ETAP1_SCRIPT, [], cwd=os.path.dirname(SLOW_ETAP1_SCRIPT))
    if code != 0:
        print("   ⛔ [slow] подготовка price_drops завершилась с ошибкой; старый CSV НЕ обрабатываем.")
        return
    if not os.path.exists(SLOW_PRICE_DROPS_CSV):
        print(
            f"   ⚠️ [slow] etap1 отработал успешно, но {SLOW_PRICE_DROPS_CSV} "
            "не найден — вероятна рассинхронизация путей, проверь SLOW_DIR."
        )
    rows = [normalize_slow_row(r) for r in read_csv_rows(SLOW_PRICE_DROPS_CSV)]
    sent = await process_and_notify(rows, "slow")
    print(f"   📬 [slow] кандидатов из прогона: {len(rows)}, реально отправлено: {len(sent)}")


async def safe_periodic_loop(name, interval_sec, job):
    while True:
        try:
            await periodic_loop(name, interval_sec, job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"   💥 [{name}] неожиданный сбой цикла: {type(exc).__name__}: {exc}")
            print(f"   🔄 [{name}] продолжим со следующего запуска через {interval_sec}s.")
            await asyncio.sleep(interval_sec)


async def fast_loop():
    await safe_periodic_loop("fast", FAST_INTERVAL_SEC, fast_job)


async def slow_loop():
    await safe_periodic_loop("slow", SLOW_INTERVAL_SEC, slow_job)


async def main():
    validate_configuration()
    print(
        "Оркестратор запущен. baseline = frozen read-only reference; "
        "baseline builder и original Stage 1/2 не запускаются. "
        "fast = фиксированный интервал 5 мин от старта цикла; slow = 1 час. "
        "При ошибке worker старый CSV не обрабатывается. "
        f"Telegram-доставка: {'включена' if TELEGRAM_ENABLED else 'выключена'}."
    )
    await asyncio.gather(fast_loop(), slow_loop())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        sys.exit(0)