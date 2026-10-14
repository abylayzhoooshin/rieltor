# Задание: подключить Almaty Cleaner к online-фотооценкам Mainrieltor

Передай этот файл агенту, открытому в локальном репозитории
`almat-ai-cleaner`, вместе с `CLEANER_INTERIOR_EXCHANGE_CONTRACT.md` и папкой
`cleaner_online_examples/`.

---

Ты работаешь в локальном репозитории Almaty Cleaner. Сначала полностью прочитай
его `CLAUDE.md`, `README.md`, `pipeline.py`, `cleaner_db.py`,
`interior_baseline.py`, `service.py`, `render.yaml` и тесты. Не меняй оценщик
`renovation_scorer.py`: версия `simple-decimal-v4-original-gpt6-luna`, prompt,
модель и схема уже использованы для первоначального baseline и должны остаться
сопоставимыми.

## Фактическая архитектура — не переизобретать

- Collector продолжает отдавать Cleaner полный baseline обычным способом.
- Mainrieltor НЕ получает отдельный полный список для фотооценки. Он читает
  только существующую курсорную ленту Collector `new/price_drop`, загружает
  карточку `/listings/{id}` и сам ставит фотооценку в очередь.
- Mainrieltor перед новой оценкой ищет точный ключ сначала в последнем clean
  baseline Cleaner, затем в своей временной SQLite.
- Ключ: `listing_id + photo_set_hash + evaluator_version`.
- Смена только цены не вызывает новую фотооценку.
- Mainrieltor хранит новые результаты временно, Cleaner забирает их и хранит
  постоянно. Main удаляет только ACKнутые строки спустя 24 часа.
- Фотооценка пока не влияет на фильтрацию, цену или вердикт.

Не добавляй Collector endpoint, photo feed, полный candidate scan или новый
курсор в Cleaner/Mainrieltor. Это не часть системы.

## API Mainrieltor

Авторизация обоих endpoint:

```text
X-API-Key: <MAINRIELTOR_INTERIOR_API_KEY>
```

Получение:

```http
GET {MAINRIELTOR_URL}/interior/evaluations?after_id=0&limit=200
```

Ответ и все поля приведены в `cleaner_online_examples/get_response.json`.
Продолжай страницы с возвращённым `next_after_id`, пока `has_more=false`.
Каждый НОВЫЙ цикл синхронизации обязательно снова начинается с `after_id=0`.
Нельзя сохранять `after_id` между циклами: более старый `evaluation_id` может
завершиться позже более нового и иначе будет потерян.

Подтверждение пачками не более 500 ID:

```http
POST {MAINRIELTOR_URL}/interior/evaluations/ack
Content-Type: application/json

{"evaluation_ids":[41,42],"baseline_version":"<published version>"}
```

ACK идемпотентен. `missing>0` или `not_ready>0` считать ошибкой синхронизации и
логировать; повторная доставка безопасна.

## Реализация

### 1. HTTP-клиент

Добавь отдельный модуль, например `mainrieltor_client.py`:

- env `MAINRIELTOR_URL`, без завершающего `/`;
- env `MAINRIELTOR_INTERIOR_API_KEY`;
- env `MAINRIELTOR_INTERIOR_SYNC_ENABLED`, по умолчанию `0`;
- таймауты подключения/чтения и понятные исключения;
- GET всех страниц с `after_id=0` на каждом вызове;
- проверка, что `next_after_id` строго растёт, `evaluation_id` уникальны, число
  страниц ограничено, а JSON имеет ожидаемую схему;
- 401/403 — конфигурационная ошибка: текущий sync прекращается, ACK запрещён;
- timeout/5xx/невалидный JSON — текущий sync прекращается без ACK;
- секрет и полные payload не писать в лог.

Для этого клиента достаточно stdlib `urllib` или уже имеющегося `requests`.
Не добавляй асинхронность только ради двух последовательных HTTP-вызовов.

### 2. Постоянное компактное хранилище Cleaner

Текущий `clean_baseline_interior_map()` хранит историю только внутри последнего
опубликованного снимка. Этого недостаточно: строка может временно ждать новой
текстовой разметки или выпасть из снимка и затем вернуться. Добавь в
`cleaner.db` таблицу постоянных фотооценок.

Рекомендуемая схема:

```sql
CREATE TABLE IF NOT EXISTS interior_evaluations (
    listing_id TEXT NOT NULL,
    photo_hash_key TEXT NOT NULL,
    photo_set_hash TEXT,
    evaluator_version TEXT NOT NULL,
    status TEXT NOT NULL,
    score REAL,
    coverage TEXT,
    evaluation_key TEXT,
    content_set_key TEXT,
    evaluated_at TEXT,
    source_evaluation_id INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (listing_id, photo_hash_key, evaluator_version)
);
```

`photo_hash_key` — `photo_set_hash` либо пустая строка для `NULL`; это нужно,
потому что SQLite UNIQUE допускает несколько `NULL`. Не храни фото, URL,
prompt или полный ответ модели.

При миграции один раз засевай таблицу уже опубликованными `interior_*` полями
из `clean_baseline`, иначе первоначальный backfill исчезнет после первой
пересборки. Upsert разрешён только по точной тройке ключа. Повтор той же доставки
должен быть идемпотентен.

### 3. Строгая валидация результата

Принимать только статусы:

```text
ready
ready_with_download_gaps
no_photos
photos_unavailable
```

Инварианты:

- `listing_id` и `evaluator_version` непустые;
- `photo_set_hash` — `NULL` или 64 lowercase hex;
- `ready*` требует coverage `sufficient|partial|insufficient`;
- `partial|sufficient` требует score от 1.0 до 10.0;
- `insufficient` требует `score=NULL`;
- `no_photos|photos_unavailable` требует `score=NULL`, `coverage=NULL`;
- неизвестные поля можно игнорировать, но нарушение инварианта прекращает
  текущую синхронизацию без ACK.

Не заменяй `NULL` нулём, средним баллом или заглушкой.

### 4. Встрой в существующий полный цикл

Работай с уже полученным полным `rows` Collector в `pipeline.run_cycle()`.
Не делай отдельный запрос в Collector.

Безопасный порядок:

1. Успешно получить полный baseline Collector как сейчас.
2. Забрать все unacked страницы Mainrieltor.
3. Провалидировать и idempotent-upsert результаты в `interior_evaluations`.
4. Выполнить существующую текстовую обработку.
5. При сборке clean baseline присоединять оценку из постоянной таблицы только
   по точной тройке `id + текущий photo_set_hash + EVALUATOR_VERSION`.
6. Атомарно заменить clean baseline и commit, как сейчас.
7. Только после успешной публикации отправить ACK подходящих результатов.

Сетевой сбой Mainrieltor не должен ломать старый clean baseline: цикл может
продолжить обычную текстовую работу без новых online-оценок, но ACK не делает.

`run_ingest_tick()` не имеет нового полного снимка Collector. Для минимальной и
понятной реализации online-sync выполняй только в полном `run_cycle()`, раз в
12 часов. Не подтверждай результат на основании устаревшего `_last_full_fetch`.

### 5. Что ACKать после успешной публикации

Для каждой доставки из текущего GET:

- текущая строка Collector существует, её ключ совпадает и она вошла в
  опубликованный clean baseline с этой оценкой → ACK;
- строка существует, но `photo_set_hash` уже другой → ACK как безопасно
  устаревшую, не применять;
- ID отсутствует в успешно загруженном полном снимке Collector → ACK как
  исчезнувшую;
- актуальная строка окончательно отбракована `usable=0` → ACK как
  `filtered_out`, не применять в чужую строку;
- строка ещё ждёт текстовой разметки либо публикация пропущена
  (`empty_result`, `relabel_in_progress`) → НЕ ACKать. Она придёт повторно в
  следующем цикле;
- если сохранение/commit/публикация завершились исключением → ничего не ACKать.

Отдельного disposition endpoint нет: эти причины запиши в структурированный
лог/счётчики Cleaner, а Main получает обычный идемпотентный ACK.

### 6. Транзакции и сбои ACK

Сохранение постоянной оценки и нового baseline должно завершиться до ACK.
HTTP ACK нельзя выполнять внутри открытой SQLite write-транзакции. Если ACK
упал после commit, Main отдаст строки повторно; следующий upsert и ACK должны
быть безопасными. Это нормальная at-least-once доставка.

## Render/env

Добавь в `render.yaml` Cleaner:

```yaml
- key: MAINRIELTOR_URL
  sync: false
- key: MAINRIELTOR_INTERIOR_API_KEY
  sync: false
- key: MAINRIELTOR_INTERIOR_SYNC_ENABLED
  value: "1"
```

Не хардкодь URL или ключ. Удали из документации неверное утверждение об
«отдельном списке кандидатов Collector». `RENOVATION_OPENAI_API_KEY` Cleaner
для online-sync не нужен: модель вызывает Mainrieltor.

## Обязательные тесты без сети

Не ослабляй существующие тесты. Добавь как минимум:

1. пагинация более 500 результатов и новый цикл снова с `after_id=0`;
2. зацикленный/нерастущий cursor, дубли ID, 401, timeout, 5xx и плохой JSON не
   приводят к ACK;
3. повторная доставка идемпотентна;
4. смена только цены сохраняет оценку;
5. другой `photo_set_hash` или версия не переиспользуют её;
6. `NULL/insufficient/no_photos/photos_unavailable` не превращаются в балл;
7. оценка переживает временное исчезновение строки из clean baseline и
   применяется при возврате точного ключа;
8. первоначальные оценки из текущего clean baseline мигрируют в таблицу;
9. waiting и пропущенная публикация не ACKаются;
10. published, filtered_out, disappeared и superseded ACKаются только после
    успешного полного цикла;
11. сбой replace/commit даёт 0 ACK;
12. сбой ACK после commit не портит baseline; повторный цикл завершает ACK;
13. секрет не попадает в лог и БД.

Запусти полный набор тестов Cleaner. Никаких реальных HTTP/OpenAI вызовов в
unit-тестах. Не делай commit, push или deploy. В конце сообщи пользователю:
изменённые файлы, миграцию БД, env, результаты тестов и точный порядок локального
smoke-test с тестовым Mainrieltor URL.
