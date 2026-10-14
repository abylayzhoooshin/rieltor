# Алматы: дубли квартир и горячие зоны аренды

Дата: 2026-10-13.

Этот документ одновременно является:

1. аудитом реализации покупки в Астане;
2. проектом адаптации для аренды в Алматы;
3. заданием для агента, который будет менять Collector Almaty;
4. заданием для агента, который после этого будет менять Cleaner Almaty.

## 1. Короткий вывод аудита

В астанинских проектах уже хорошо сделана **инфраструктура для поиска
дублей**: неизменяемый снимок исходных объявлений, полная история цены и
присутствия, стабильный `entity_id`, shortlist кандидатов, версионированные
фотодоказательства, ручные merge/split и выбор одной canonical-строки.

Но важно не переоценить степень готовности:

- metadata-поиск дублей реализован и работает только в `shadow`;
- техническое сравнение фото реализовано, но не откалибровано;
- автоматическое объединение намеренно выключено (`auto_merged=0`);
- 99 028 metadata-пар в измеренном снимке — кандидаты, а не подтверждённые
  дубли;
- «горячие места» описаны как будущая аналитика, но расчётного модуля,
  таблиц результатов и production endpoint в проекте сейчас нет.

Поэтому Алматы должен заимствовать архитектуру и уже проверенные примитивы,
но не включать auto-merge и не выдавать исчезновение объявления за
подтверждённую сдачу квартиры.

## 2. Какие проекты и файлы являются источниками

### Astana purchase Collector

Путь:

```text
C:\Users\Абылай\PycharmProjects\rieltorcollector -pokupka
```

Основные источники:

- `COLLECTOR_CANDIDATE_SNAPSHOT_HANDOFF.md` — утверждённый контракт;
- `build_candidates.py` — immutable pre-dedupe snapshot;
- `baseline_api.py` — version-pinned candidate endpoints;
- `master_db.py` — price observations и presence events;
- `paths.py` — хранение candidate versions под persistent `DATA_DIR`;
- `validate_candidate_migration.py` — проверка миграции;
- `test_candidate_snapshot.py` и `test_photo_refresh.py` — обязательные
  регрессии;
- `CANDIDATE_MIGRATION_RUNBOOK.md` — безопасный rollout/rollback.

### Astana purchase Cleaner

Путь:

```text
C:\Users\Абылай\PycharmProjects\ai pokupka astana
```

Основные источники:

- `PROJECT_CONTEXT.md` — актуальный статус и известные ограничения;
- `ENTITY_RESOLUTION_DESIGN.md` — модель entity и операции;
- `entity_matcher.py` — metadata shortlist;
- `entity_store.py` — entity, membership, lifecycle, canonical, merge/split;
- `collector_client.py` — строгая загрузка одной candidate version;
- `pipeline.py` — последовательность ingestion/publish;
- `photo_worker.py` — техническое сравнение фото;
- `photo_queue_report.py` и `PHOTO_QUEUE_ANALYSIS.md` — измерения очереди;
- `verdicts_api.py` — review/evidence/entity endpoints;
- `test_sales_cleaner.py` — регрессии.

Файл `DUPLICATES_AND_HOTSPOTS_TECHNOLOGY.md` полезен как объяснение идеи,
но его раздел о горячих зонах — **дизайн, а не существующая реализация**.

### Целевые проекты Алматы

Collector:

```text
C:\Users\Абылай\PycharmProjects\rieltorcollector Almaty
```

Cleaner:

```text
C:\Users\Абылай\PycharmProjects\ai almaty
```

Перед работой в каждом репозитории исполнитель обязан полностью прочитать его
`CLAUDE.md` и проверить грязный worktree. Нельзя перетирать пользовательские
изменения.

## 3. Как сейчас устроен поиск дублей в покупке

### 3.1 Единицы данных

```text
listing_id = отдельное объявление или эпизод публикации
entity_id  = стабильная идентичность физической квартиры
```

Исходные объявления не удаляются и не склеиваются физически. Несколько
`listing_id` становятся members одной entity. Это позволяет восстановить
историю цен, перевыставлений, одновременных объявлений хозяина и риелторов и
исправить ошибочный merge.

### 3.2 Что делает Collector

Collector не решает, какие объявления являются одной квартирой. Он хранит
source facts и атомарно публикует pre-dedupe candidate version из четырёх
согласованных наборов:

1. `listings`;
2. `listing_photos`;
3. `price_observations`;
4. `presence_events`.

Все страницы читаются с явным `version`. Старая retained version либо
читается целиком, либо отвечает 410; подмена её текущей версией запрещена.
Manifest содержит totals, schema version и source cursor. Cleaner сверяет
totals, `photo_count`, `photo_urls` и `photo_set_hash` и при несогласованности
не публикует частичный результат.

Фактическая реализация Collector Almaty после приёмки использует
`schema_version="candidate-v2"` и
`hash_algorithm="sha256-canonical-jsonl-v1"`. Все четыре dataset hash можно
независимо пересчитать по публичным HTTP-строкам. Для `listings` хешируется
уже восстановленная compact JSON-строка `photo_urls`, а не внутреннее NULL из
SQLite. Точный порядок полей и построение `content_sha256` зафиксированы в
`rieltorcollector Almaty\ALMATY_COLLECTOR_ENTITY_HANDOFF.md`.

Live presence history является transition-only:

- новый ID — `first_seen`;
- `active -> missing` — `missing_confirmed`;
- `missing -> active` — `reactivated`;
- `active -> active` обновляет только `listings.last_seen_at`.

Тип `seen` принимается ради совместимости со старыми или импортированными
данными, но обычный live Collector его не создаёт. Это предотвращает рост на
сотни тысяч строк в сутки и не мешает строить market episodes.

### 3.3 Что делает Cleaner

Каждый новый `listing_id` сначала получает отдельную singleton entity. Затем
`entity_matcher.py` строит не решение, а shortlist пар по блокам:

- одинаковый `photo_set_hash` непустых реальных фото;
- адрес + комнаты + округлённая площадь + этаж;
- ЖК + комнаты + округлённая площадь + этаж;
- координатная ячейка около 100 м + комнаты + площадь + этаж.

Блоки больше 100 строк не разворачиваются в полную клику. Пара хранится как
`listing_id_low < listing_id_high`.

Текущие hard conflicts:

- разные комнаты;
- площадь отличается больше чем на 10%;
- разный этаж;
- два заполненных, но разных точных адреса.

Координаты являются только положительным сигналом. Большое расстояние не
запрещает пару: геометка Krisha может быть ошибочной.

Приоритет очереди — не вероятность дубля:

| Условие | База priority |
|---|---:|
| Одинаковый реальный `photo_set_hash` | 100 |
| Не менее трёх metadata reasons | 80 |
| Две причины | 60 |
| Одна причина | 30 |

Пара `active-active` получает ещё 5, близкие координаты — ещё 2.

### 3.4 Фотографии

`photo_worker.py` ограничивает домены, байты и пиксели, применяет EXIF,
создаёт RGB-preview, считает content hash/fingerprint, удаляет повторные кадры
внутри объявления и выполняет one-to-one matching между объявлениями.

Известный важный урок: 266 старых «точных совпадений фото» оказались одной
служебной картинкой `photo-moderation-big.png`. Placeholder-фото теперь
игнорируются.

Текущий dHash+RGB score не является вероятностью дубля. До auto-merge нужны:

- классификатор ролей `kitchen/bathroom/bedroom/living_room/hallway/balcony/
  facade/plan/render/other/unknown`;
- сравнение только совместимых ролей;
- понижение веса типовых фасадов, планов и рендеров;
- частота одного fingerprint среди разных квартир;
- размеченная truth-выборка и измеренные precision/recall.

### 3.5 Canonical

После подтверждённого merge Cleaner публикует одну **цельную** строку member,
а не смешивает поля разных объявлений. Среди готовых активных members
выбирается минимальная валидная цена; при равенстве сохраняется прежний
canonical. В baseline дополнительно идут `entity_id`, число members,
диапазон активных цен, `relist_count`, lifecycle и даты наблюдения.

## 4. Оценка качества текущего решения

### Сильные стороны

- правильное разделение source facts и identity resolution;
- неизменяемые версии исключают смешивание страниц разных снимков;
- полная трассируемость: listing не удаляется после merge;
- идемпотентные source events и операции;
- evidence полностью версионирован и устаревший ответ отклоняется;
- предусмотрены aliases, membership history, canonical history и recovery
  через split;
- дубли схлопываются до аналитики цены и срока экспозиции, а не после неё;
- один дом/одна квартира не получает лишний вес из-за нескольких объявлений.

### Ограничения и риски

- auto-merge ещё не доказан на размеченных данных;
- точный адрес/этаж/площадь дают много пар соседних квартир одного дома;
- одинаковые фото могут быть типовыми фотографиями ЖК;
- hard conflict по этажу или комнатам может скрыть дубль при ошибке автора;
- координатный `round(..., 3)` имеет границы ячеек: очень близкие точки по
  разные стороны границы не попадут в один блок;
- canonical с минимальной asking price полезен для поиска реальной доступной
  цены, но не доказывает цену сделки;
- late merge пересчитывает прошлую аналитику, поэтому hotspot snapshots должны
  иметь версии identity graph;
- в Collector покупки ранее отмечены два обязательных аудита: bootstrap
  `source_event_id` должен быть стабильным, а `observed_at` цены должен быть
  фактическим временем наблюдения, не временем миграции.

Итог: переносить как shadow-систему можно; включать автоматическое схлопывание
до разметки нельзя.

## 5. Что означает «горячая зона» для аренды Алматы

В аренде hotness — не дороговизна и не просто число объявлений. Это сигнал,
что сопоставимое предложение в локации быстро исчезает и редко возвращается.

Наблюдаем только proxy:

```text
active -> missing_confirmed -> не вернулось в течение контрольного периода
```

Это может означать сдачу, снятие владельцем, блокировку или перенос на новый
ID. Поэтому наружу следует называть метрику `rental_liquidity` или
`observed_absorption`, а не `confirmed_rental_demand`.

### 5.1 Сначала строится entity market episode

Расчёт идёт по `entity_id`, не по объявлениям. Один episode содержит:

- начало непрерывного присутствия;
- дату первого и подтверждённого missing;
- правую цензуру, если квартира ещё active;
- возврат/перевыставление members;
- стартовую, последнюю и минимальную цену;
- число и величину снижений;
- полноту history;
- характеристики квартиры и географические уровни.

Одновременные объявления двух агентов не создают два объекта и два
«исчезновения». Новый ID той же квартиры продолжает entity history.

### 5.2 Географические уровни

Считать параллельно:

1. точный дом;
2. ЖК;
3. малая H3-ячейка;
4. более крупная родительская H3-ячейка;
5. административный район;
6. весь город как prior.

H3 resolution и минимальный размер выборки выбираются backtest, а не
назначаются навсегда заранее. Координаты вне bbox Алматы помечаются invalid.
Строки без координат ещё могут участвовать в доме/ЖК/районе, если адрес
надёжно нормализован.

### 5.3 Сегментация

Нельзя сравнивать скорость студий с дорогими трёхкомнатными квартирами.
Минимальные strata:

- комнатность;
- диапазон площади;
- ЖК/не-ЖК и класс здания;
- относительная цена к справедливой оценке Stage 3;
- уровень ремонта или хотя бы крупная группа ремонта;
- календарная неделя/месяц.

`seller_type` сохраняется как разрез качества, но нельзя заранее считать
риелтора более надёжным. Текущие тесты Mainrieltor уже показали, что фильтр
«только риелторы» точность цены не улучшает.

### 5.4 Первая версия метрик

Для каждой zone × stratum × snapshot:

- `new_entities`;
- `active_entity_days`;
- `confirmed_missing_entities`;
- доля исчезнувших за 1, 3 и 7 дней;
- доля вернувшихся за 3 и 7 дней;
- доля со снижением цены до исчезновения;
- median observed exposure;
- Kaplan–Meier survival с active-наблюдениями как right-censored;
- relative hazard к городскому prior того же stratum;
- число entities с полной history;
- effective sample size и confidence.

Быстро исчезнувшее объявление со снижением цены и последующим возвратом —
слабый сигнал. Быстрое устойчивое исчезновение без снижения — более сильный.

### 5.5 Shrinkage вместо рейтинга малых выборок

Сырые 2 из 2 быстрых исчезновения не должны делать дом самым горячим в
городе. Для долей используется сглаживание к родительскому уровню:

```text
smoothed_rate = (successes + prior_strength * parent_rate)
                / (eligible + prior_strength)
```

`prior_strength` выбирается на прошлых временных окнах. Для survival/hazard
применяется аналогичная иерархия: дом -> ЖК/малая ячейка -> крупная ячейка ->
район -> город. При недостатке данных API возвращает компоненты и низкую
уверенность, но не ярлык «горячая».

### 5.6 Защита от ложного hotness

Не обновлять snapshot либо понижать confidence при:

- неполном full scan;
- массовом одновременном missing по городу/агентству;
- `history_complete=false`;
- высокой доле reactivation;
- большом числе unresolved duplicate candidates;
- резком изменении покрытия Collector;
- слишком малом числе независимых entities;
- конфликте адреса и координат;
- системных изменениях правил Krisha.

## 6. Распределение ответственности в Алматы

| Компонент | Ответственность |
|---|---|
| Collector Almaty | Сбор неизменённых source facts, точная история цены и присутствия, фото и immutable candidate snapshot |
| Cleaner Almaty | Text verdict, entity resolution, canonical baseline, lifecycle episodes, shadow hotspot analytics |
| Mainrieltor Almaty | Оценка цены/ремонта, verdict «НАХОДКА», Telegram, prediction audit; не определяет identity самостоятельно |

Collector не должен присваивать `entity_id`. Mainrieltor не должен строить
собственный несовместимый граф дублей. Источник истины identity — Cleaner.

## 7. Задание агенту Collector Almaty

### 7.1 Что передать агенту

Передать:

1. путь целевого проекта
   `C:\Users\Абылай\PycharmProjects\rieltorcollector Almaty`;
2. путь референса
   `C:\Users\Абылай\PycharmProjects\rieltorcollector -pokupka`;
3. этот файл;
4. попросить сначала прочитать оба `CLAUDE.md` и
   `COLLECTOR_CANDIDATE_SNAPSHOT_HANDOFF.md` референса.

### 7.2 Что реализовать

- последовательные идемпотентные schema migrations;
- append-only `price_observations` с реальным `observed_at`;
- append-only transition-only `presence_events`: `first_seen`,
  `missing_confirmed`, `reactivated`; тип `seen` только принимается для
  совместимости, а повторный `active -> active` event не создаёт;
- стабильный `source_event_id`, одинаковый при повторной миграции/обработке;
- `history_complete=false` для bootstrap и true для новых надёжных событий;
- immutable `candidate_<version>.db` из одной SQLite read transaction;
- таблицы `listings`, `listing_photos`, `price_observations`,
  `presence_events`, `meta`;
- атомарный pointer и retention старых версий;
- endpoints meta/listings/photos/prices/presence с обязательным `version`;
- totals, hashes, integrity validation и fail-closed публикацию;
- публично проверяемые `candidate-v2` hashes по алгоритму
  `sha256-canonical-jsonl-v1`;
- candidate build по отдельному таймеру без запросов к Krisha;
- tests миграций, crash safety, version pinning, 410 для удалённой версии,
  photo consistency и concurrent writer snapshot.

Candidate должен включать все технически корректные active и missing строки
до бизнес-фильтра продавца, usable verdict и legacy dedupe.

### 7.3 Что нельзя копировать целиком

Нельзя заменять следующие алматинские части версиями покупки:

- `orchestrator.py` и `service.py` целиком;
- сетевой слой, RequestGate и расписание full/fast scan;
- city/category/price sanity settings;
- действующие `/listings/changes` и `/listings/{id}`;
- Almaty `build_baseline.py` целиком.

Алматинский Collector новее референса в организации конкурентных full/fast
scan. Нужно переносить схемы, builder и API-контракт точечным merge. Старый
`/baseline/table` и legacy dedupe остаются до end-to-end приёмки Cleaner.

### 7.4 Критерии готовности Collector

- старую production DB можно мигрировать дважды с одинаковым результатом;
- snapshot самосогласован при параллельной записи scraper;
- изменение любого substantive набора меняет version;
- уже опубликованный файл не изменяется;
- Cleaner может дочитать закреплённую старую version после выхода новой;
- bootstrap события стабильны, а не получают новый ID при каждом запуске;
- текущий Mainrieltor event cursor продолжает работать без изменений;
- все старые и новые unit-тесты проходят в UTF-8;
- агент показывает schema diff, API-пример и dry-run на копии production DB.

### 7.5 Готовый текст задания Collector

```text
Изучи целевой Collector Almaty и референс Astana purchase по путям из
ALMATY_DUPLICATES_HOTSPOTS_HANDOFF.md. Не переноси проект целиком. Сохрани
актуальные Almaty scheduler, RequestGate, city/category настройки и event feed
Mainrieltor. Точечно добавь полную source history и immutable pre-dedupe
candidate snapshot по контракту purchase Collector. Исправь известные риски
стабильности bootstrap source_event_id и observed_at. Legacy baseline/dedupe
не отключай. Реализуй миграции, API, тесты, dry-run и rollback runbook. Не
трогай production и не используй реальные секреты.
```

## 8. Задание агенту Cleaner Almaty

Работу начинать только после того, как обновлённый Collector Almaty прошёл
приёмку и его фактический candidate API/schema зафиксирован.

### 8.1 Что передать агенту

Передать:

1. целевой проект `C:\Users\Абылай\PycharmProjects\ai almaty`;
2. референс `C:\Users\Абылай\PycharmProjects\ai pokupka astana`;
3. путь уже обновлённого `rieltorcollector Almaty`;
4. этот файл;
5. отчёт и API/schema diff агента Collector.

### 8.2 Что реализовать сначала

- version-pinned fetch всех candidate datasets без fallback на legacy;
- проверку manifest/totals/photos и сохранение последней хорошей версии при
  ошибке;
- entity schema, singleton assignment, membership/lifecycle/canonical history;
- metadata shortlist и review API в shadow;
- сохранение всех rental verdicts и текущей rental prompt/heuristics;
- canonical publication одной целой rental-строки;
- `entity_id` и audit-поля в clean baseline без удаления исходного `id`;
- отчёт по candidate queue и стратифицированную ручную выборку;
- photo worker только после metadata-аудита; auto-merge оставить выключенным.

Нельзя копировать sales `SYSTEM_PROMPT`, sales heuristics, seller rejection,
ценовые границы или поля покупки поверх арендного Cleaner. В entity ingestion
должны участвовать и `usable=false` listings: они могут быть members той же
физической квартиры, хотя canonical clean baseline публикует только актуально
допустимые строки.

### 8.3 Hotspot shadow-модуль

После entity ingestion добавить отдельные версионированные слои:

- `entity_market_episodes`;
- `zone_assignments`;
- `hotspot_snapshots`;
- `hotspot_metrics`.

Первый релиз только shadow/read-only:

- пересчёт детерминирован из source version + identity graph version + config;
- хранит raw counts, censored counts, возвраты, price drops и confidence;
- поддерживает дом/ЖК/H3/район;
- не публикует один непрозрачный score без компонентов;
- не влияет на чистый baseline, Stage 3 или Telegram;
- не называет missing подтверждённой сдачей;
- позволяет пересчитать прошлое после merge/split.

Endpoint первой версии, например:

```text
GET /analytics/hotspots/meta
GET /analytics/hotspots?version=...&level=...&limit=...&offset=...
GET /analytics/zones/{zone_id}?version=...
```

Точные H3 resolution, confirmation delay, minimum N и веса score сначала
идут конфигурацией challenger и выбираются temporal backtest.

### 8.4 Критерии готовности Cleaner

- повторный ingest одной source version идемпотентен;
- частичная/повреждённая version не заменяет clean snapshot;
- `listing_id` не теряется после merge;
- без ручного решения или откалиброванного порога membership не меняется;
- canonical всегда является реальной цельной source row;
- placeholder photo не создаёт candidate;
- координатная ошибка сама по себе не запрещает дубль;
- late merge пересчитывает entity lifecycle и hotspot history;
- hotspot eval использует temporal split и одинаковые цели для сравнения;
- отдельно показаны coverage, effective N, возвраты и массовые missing;
- существующий `/baseline/clean.csv` остаётся совместим с Mainrieltor;
- все старые rental regression cases и новые entity-тесты проходят.

### 8.5 Готовый текст задания Cleaner

```text
Изучи rental Cleaner Almaty, entity implementation Astana purchase и уже
обновлённый Collector Almaty по путям из
ALMATY_DUPLICATES_HOTSPOTS_HANDOFF.md. Перенеси entity resolution как
адаптацию, не заменяя rental prompt, heuristics и pipeline sales-версиями.
Сначала сделай version-pinned ingestion, singleton entities, shadow metadata
candidates, canonical baseline и ручной audit. Auto-merge оставь выключенным.
Затем реализуй версионированную shadow-аналитику rental liquidity по entity
market episodes на уровнях дом/ЖК/H3/район, с censored observations,
reactivation/price-drop penalties, shrinkage и temporal backtest. Ничего не
выкатывай в production без отдельной приёмки.
```

## 9. Порядок внедрения

1. Backup и read-only аудит production-копии Collector Almaty.
2. Добавочные миграции и source history в Collector.
3. Immutable candidate snapshot и API, legacy baseline остаётся.
4. Cleaner читает новый контракт в shadow и сверяет totals.
5. Создаются singleton entities и metadata candidates, merge выключен.
6. Ручная truth-разметка стратифицированной выборки.
7. Photo roles/prevalence, затем калибровка precision/recall.
8. Entity clean baseline включается только при достаточной точности и с
   быстрым откатом на legacy snapshot.
9. Строятся market episodes и shadow hotspot snapshots.
10. Несколько будущих временных окон проверяют стабильность hotness.
11. Только потом Mainrieltor начинает использовать `entity_id` для Telegram
    dedupe и/или liquidity как дополнительный признак.

Не следует одновременно включать auto-merge, менять clean baseline и вводить
hotspot score: тогда невозможно понять источник регрессии.

## 10. Что потребуется изменить в Mainrieltor позднее

Текущий `ever_sent_ids.json` дедуплицирует только `listing_id`. После приёмки
Cleaner понадобится отдельный совместимый этап:

- получать `entity_id` для live listing через Cleaner API или опубликованную
  membership map;
- хранить и `listing_id`, и `entity_id`;
- не отправлять одну квартиру повторно под новым ID, кроме реального снижения
  минимальной цены entity;
- не ломать текущую идемпотентность `collector_event_id` в
  `prediction_audit.sqlite3`;
- версионировать модель и identity graph в audit;
- сначала логировать `rental_liquidity` в shadow, не менять verdict
  «НАХОДКА» без отдельного eval.

Это отдельная задача. Collector не должен ждать её для начала накопления
правильной истории.

## 11. Что ещё нового было введено в проектах покупки

Помимо самих дублей и идеи горячих зон:

- immutable pre-dedupe candidate snapshots;
- полная price/presence history и признак её полноты;
- стабильные `entity_id`, membership history и aliases;
- lifecycle/relist/reactivation на уровне физической квартиры;
- canonical history и публикация одной цельной строки;
- versioned metadata candidate revisions;
- versioned photo evidence с защитой от stale results;
- placeholder-photo filtering;
- RGB fingerprints, intra-listing dedupe и one-to-one photo matching;
- ручные merge/split primitives, operation log и lineage;
- review API и воспроизводимый queue report;
- fail-closed ingestion без fallback на несовместимый legacy snapshot;
- проект раздельного развёртывания Collector/Cleaner с persistent SQLite,
  backup/restore и rollback.

Ещё не реализованы или не доказаны:

- photo role classifier;
- prevalence типовых photo assets;
- production truth set и откалиброванный auto-merge;
- быстрый online entity event flow;
- рабочий consumer горячих зон;
- подтверждение, что `missing_confirmed` означает продажу/сдачу.

## 12. Главный принцип приёмки

Нужно всегда различать:

```text
metadata candidate
  != похожие фотографии
  != подтверждённая одна квартира
  != подтверждённая сдача
  != доказанная горячая зона
```

Цель первой итерации Алматы — начать собирать корректную историю и получить
измеряемый shadow-контур. Точность и пороги принимаются после данных, а не
предполагаются из архитектуры.
