# Чистый запуск Mainrieltor как Render Web Service

Репозиторий уже содержит готовый `render.yaml`. Создай в Render **Blueprint**
из этого GitHub-репозитория. Он создаст Web Service в Singapore, команду
`python orchestrator_v7.py`, healthcheck `/health` и persistent disk 1 GB в
`/var/data`.

## Обязательные секреты Mainrieltor

В Render Environment заполни:

```text
RIELTOR_COLLECTOR_URL=<base URL Collector без /listings/...>
RIELTOR_COLLECTOR_API_KEY=<ключ Collector>
RIELTOR_CLEANER_API_KEY=<CLEANER_API_KEY из Cleaner>
OPENAI_API_KEY=<отдельный ключ Stage 2>
RENOVATION_OPENAI_API_KEY=<отдельный ключ фотооценки>
INTERIOR_EXCHANGE_API_KEY=<новый длинный общий секрет Mainrieltor↔Cleaner>
TELEGRAM_BOT_TOKEN=<токен>
TELEGRAM_CHAT_ID=<chat id>
```

Blueprint уже задаёт:

```text
KRISHA_DATA_DIR=/var/data
RIELTOR_CLEANER_URL=https://rieltor-cleaner-almaty.onrender.com
INTERIOR_ENABLED=1
INTERIOR_CONCURRENCY=2
INTERIOR_ERROR_RETRY_H=6
INTERIOR_DECISION_MAX_WAIT_SEC=1200
COLLECTOR_START_FROM=beginning
COLLECTOR_POLL_INTERVAL_SEC=60
```

Не меняй `COLLECTOR_START_FROM` на `end` на первом запуске: чистый сервис должен
прочитать всю доступную ленту `new/price_drop`. События старше 12 часов проходят
только через фотошлюз и не рассылаются в Telegram.

## После запуска

1. Открой `https://<mainrieltor-primary-url>/health`. Должен быть HTTP 200 и
   JSON со статистикой очереди.
2. В логах сначала появится скачивание clean baseline Cleaner. До него лента
   намеренно не двигается.
3. Затем сервис читает Collector с `since=0`. Для каждой доступной карточки
   точная оценка берётся из baseline либо создаётся durable SQLite-задание.
   Свежие карточки получают приоритет; финальный Stage 3 ждёт фотооценку до
   20 минут, не блокируя курсор Collector.
4. Проверь, что `collector_state.json`, `interior_online.sqlite3` и
   `interior_content_cache.sqlite3` находятся на `/var/data` (это видно по
   строке `DATA_DIR` в startup-логе; Render Shell не обязателен).
5. После успешного запуска Mainrieltor задай в Environment Cleaner:

   ```text
   MAINRIELTOR_URL=https://<mainrieltor-primary-url>
   MAINRIELTOR_INTERIOR_API_KEY=<тот же INTERIOR_EXCHANGE_API_KEY>
   MAINRIELTOR_INTERIOR_SYNC_ENABLED=1
   CLEANER_API_ONLY=0
   ```

   Затем перезапусти Cleaner. Без последних двух строк код синхронизации есть,
   но Cleaner не забирает результаты и остаётся только в API-режиме.

## Важная гарантия и единственное исключение

Курсор Collector не продвигается, если карточку удалось скачать, но точный ключ
не удалось переиспользовать или durable-записать в очередь. После рестарта
страница повторяется. Если Collector отвечает `404` на конкретный ID, оценить
его физически невозможно; сервис громко логирует ID и продолжает ленту, чтобы
одна исчезнувшая карточка не остановила все последующие объявления.
