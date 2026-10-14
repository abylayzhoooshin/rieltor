# Контракт online-фотооценок Mainrieltor → Almaty Cleaner

## Граница ответственности

- Mainrieltor получает карточки только из обычной курсорной ленты Collector
  `GET /listings/changes` → `GET /listings/{id}`.
- Отдельного полного списка/endpoint кандидатов на фотооценку нет и не нужно.
- Mainrieltor временно хранит новые результаты до подтверждения Cleaner.
- Cleaner постоянно хранит результаты и публикует их в clean baseline.
- Фотооценка пока не влияет на очистку, цену, Stage 3 или Telegram.

## Ключ актуальности

```text
listing_id + photo_set_hash + evaluator_version
```

`photo_set_hash` приходит от Collector и считается так:

```python
def photo_set_hash(photo_urls):
    if not photo_urls:
        return None
    return hashlib.sha256("|".join(sorted(photo_urls)).encode("utf-8")).hexdigest()
```

Это hash полных URL, не содержимого. Порядок не влияет, дубли не удаляются,
пустой набор даёт `NULL`. Mainrieltor проверяет переданный hash точным
пересчётом, но не приравнивает его к `evaluation_key` или `content_set_key`.

Изменение только цены не требует оценки. Новый ID, другой photo hash или другая
версия оценщика требуют отдельного результата.

## Авторизация

Оба endpoint требуют:

```text
X-API-Key: <INTERIOR_EXCHANGE_API_KEY>
```

В Cleaner тот же секрет называется `MAINRIELTOR_INTERIOR_API_KEY`.

## Получение результатов

```http
GET /interior/evaluations?after_id=0&limit=200
```

`limit` от 1 до 500. Ответ:

```json
{
  "results": [],
  "count": 0,
  "has_more": false,
  "next_after_id": 0
}
```

Публичные поля результата:

```text
evaluation_id
listing_id
photo_set_hash
evaluator_version
status
score
coverage
evaluation_key
content_set_key
evaluated_at
```

Endpoint выдаёт только terminal-статусы: `ready`,
`ready_with_download_gaps`, `no_photos`, `photos_unavailable`.

`after_id` используется только для страниц одного обхода. Каждый новый цикл
Cleaner начинается с `after_id=0`; сохранять watermark между циклами нельзя.
Причина: evaluation 10 может закончиться после evaluation 20. Постоянный
watermark 20 навсегда пропустил бы 10.

## Подтверждение

```http
POST /interior/evaluations/ack
Content-Type: application/json

{
  "evaluation_ids": [41, 42],
  "baseline_version": "<успешно опубликованная версия>"
}
```

От 1 до 500 уникальных ID. Ответ:

```json
{
  "requested": 2,
  "acknowledged": 2,
  "already_acknowledged": 0,
  "missing": 0,
  "not_ready": 0
}
```

ACK идемпотентен. `missing` и `not_ready` Cleaner не игнорирует: это ошибка
синхронизации. Mainrieltor удаляет ACKнутую временную строку не ранее чем через
24 часа. Unacked по возрасту не удаляются.

## Допустимые значения

| status | coverage | score |
|---|---|---|
| `ready` | `sufficient`, `partial`, `insufficient` | число 1.0–10.0, но при `insufficient` только `NULL` |
| `ready_with_download_gaps` | те же значения | те же правила |
| `no_photos` | `NULL` | `NULL` |
| `photos_unavailable` | `NULL` | `NULL` |

Неизвестная оценка не заменяется нулём/средним и не удаляет квартиру.

## Поля clean baseline

```text
photo_set_hash
interior_status
interior_score
interior_coverage
interior_evaluator_version
interior_evaluation_key
interior_content_set_key
interior_evaluated_at
```

Перенос между пересборками только по точной тройке ключа. Cleaner хранит
компактную постоянную таблицу этих оценок в своей `cleaner.db`, потому что один
лишь текущий snapshot не переживает временное ожидание текстовой разметки.

## Безопасный цикл Cleaner

1. Успешно получить полный Collector baseline обычным существующим клиентом.
2. Забрать все страницы Mainrieltor, начиная с `after_id=0`.
3. Провалидировать и idempotent-upsert в постоянную таблицу Cleaner.
4. Собрать новый clean baseline и присоединить только точные ключи.
5. Атомарно опубликовать/commit.
6. После успешной публикации ACKнуть:
   - применённые результаты;
   - результаты для исчезнувших ID;
   - устаревшие photo hash;
   - окончательно отфильтрованные строки.
7. Не ACKать строку, которая ещё ждёт текстовой очистки, и ничего не ACKать при
   ошибке/пропуске публикации.

Если ACK упал после commit, Main отдаст результат снова. Повторный upsert и ACK
обязаны быть безопасны: доставка at-least-once, а не exactly-once.
