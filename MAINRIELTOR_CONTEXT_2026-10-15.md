# Mainrieltor Almaty — полный handoff на 2026-10-15

Этот файл предназначен для передачи работы в новый чат. Он продолжает
`MAINRIELTOR_CONTEXT_2026-10-12.md`, но содержит достаточно информации, чтобы
понять текущее состояние без чтения всей переписки.

## 1. Главная цель продукта

Это система поиска выгодных объявлений аренды квартир в Алматы.

Главная продуктовая цель — **не пропускать реальные находки**, а не добиться
минимальной средней ошибки любой ценой. Поэтому допустимо прислать пользователю
несколько лишних кандидатов с честно указанной низкой уверенностью. При этом для
дедупликации действует обратная асимметрия: ложное объединение двух разных
квартир опаснее, чем временно оставить один дубль. Неуверенные пары должны
оставаться раздельными.

Архитектура состоит из трёх независимых сервисов:

```text
Collector Almaty -> события new/price_drop и candidate-v2
Cleaner Almaty   -> production baseline и отдельный entity shadow
Mainrieltor      -> Stage 2/3, оценка цены, Telegram
```

Mainrieltor не парсит Krisha и не строит baseline. Collector не оценивает
ремонт. Cleaner не должен самостоятельно переключать production на entity
baseline без отдельного проверенного решения.

## 2. Репозитории, рабочие папки и Render

### Mainrieltor

- локальная папка:
  `C:\Users\Абылай\PycharmProjects\rieltor Almaty`;
- GitHub:
  `https://github.com/abylayzhoooshin/Alamty-rieltor-main-new`;
- Render:
  `https://rieltor-orchestrator-almaty-5ukw.onrender.com`;
- основной исторический контекст:
  `MAINRIELTOR_CONTEXT_2026-10-12.md`.

Рабочее дерево Mainrieltor сильно изменено и содержит много untracked-файлов.
Не делать reset/checkout и не копировать поверх него старые файлы. Особенно
нельзя заменять актуальный `orchestrator_v7.py` старой корневой версией:
должны сохраниться `telegram_access.py`, админские команды, callback-кнопки и
логика нескольких Telegram-получателей.

### Cleaner Almaty

- локальная рабочая копия, в которой находятся последние эксперименты:
  `C:\Users\Абылай\PycharmProjects\codex-publish-20261014\cleaner`;
- GitHub:
  `https://github.com/abylayzhoooshin/almat-ai-cleaner`;
- текущий GitHub/локальный HEAD на момент handoff:
  `eaea96bd6ffd5660f3929ed19abaa864b5501e38`;
- последние два опубликованных commit:
  `4e85230 Add safe shadow vision dedupe evaluation` и
  `eaea96b Expose deployed entity versions in health`;
- Render:
  `https://rieltor-cleaner-almaty.onrender.com`;
- подробная холодная ревизия:
  `SYSTEM_COLD_REVIEW_2026-10-15.md` в рабочей папке Cleaner.

Последние изменения AI-дедупликации закоммичены, запушены в `main` и успешно
задеплоены Render. Это подтверждено не только GitHub, но и публичным `/health`,
который возвращает полный SHA commit и версии алгоритмов.

### Collector Almaty

- локальная папка:
  `C:\Users\Абылай\PycharmProjects\rieltorcollector Almaty`;
- GitHub:
  `https://github.com/abylayzhoooshin/almaty-rieltor-collector`.

Папка Collector, проверенная при составлении этого handoff, не содержит `.git`,
поэтому её локальный commit нельзя считать источником истины. Источник для
deploy — GitHub.

## 3. Что уже сделано в Mainrieltor по ремонту

Контрольный объект пользователя:

- Krisha ID `1015853709`;
- Курмангазы, 31;
- 2 комнаты, 50 м², пятиэтажный дом, не ЖК;
- `interior_score=6.4`;
- фактически сдан за `550 000 ₸` менее чем за неделю.

Старая логика давала около `+5.73%` за ремонт и оценку около `388 590 ₸`.
Причина — агрегирование слабых аналогов и общий cap `exp(±0.15)`, примерно
`±16.2%`.

После исследований добавлена узкая high-recall ветка строгого переноса премии:

- обычная поправка по-прежнему имеет защитный cap;
- без cap разрешены только подтверждённые переходы `4->6` и `4->7plus`;
- нужен слабый аналог того же дома и комнатности;
- нужны минимум два независимых здания-донора;
- проверяются площадь, этажность, непрерывный ценовой класс, география и тип
  ЖК;
- один дом даёт один robust-сигнал;
- одинаковые фото и слишком удалённые по времени пары исключаются;
- при принятом строгом evidence используется специальный high-recall порог
  находки 5%; обычные пороги 17/21/27% не менялись;
- версия прогноза: `stage3-v7-strict-repair-premium`.

Для `1015853709` локально получено:

```text
база без ремонта:       336 450 ₸
переносимая премия:     +50.59%
доноры:                 2 независимых здания
справедливая цена:      506 670 ₸
при asking 450 000 ₸:   скидка 11.19%, НАХОДКА
при asking 550 000 ₸:   -8.55%, СПРАВЕДЛИВАЯ
```

Это направление лучше соответствует реальному примеру, но два донора не дают
высокой статистической уверенности. Обнаружена важная проблема: собственная
уверенность внешней премии ремонта не входит в итоговый
`benchmark_confidence`. В результате итоговая цена может сильно вырасти, а
интерфейс продолжит показывать уверенность только ценовой когорты. Для цели
«не пропустить находку» такой объект можно присылать, но Telegram должен
отдельно показывать уверенность ремонтной части.

Полный повторный прогон всех квартир для новой оценки ремонта решили пока не
делать. Текущая оценка ремонта остаётся слегка шумной, но пригодной. Дорогое
массовое переоценивание следует отложить до нового prompt/output и строгой
оценки пользы.

## 4. Что сделано в Collector

Collector получил долговременную историю и candidate-v2:

- идемпотентные SQLite migrations;
- `price_observations` с фактическим временем list/detail-наблюдения;
- transition-only presence events:
  `first_seen`, `missing_confirmed`, `reactivated` и migration-only
  `bootstrap_state`;
- обычное active-наблюдение обновляет `last_seen_at`, но не раздувает таблицу
  событием `seen`;
- deterministic SHA-256 `source_event_id`;
- `history_complete=false` для bootstrap/legacy и `true` для новых событий;
- immutable pre-dedupe candidate snapshot;
- version-pinned endpoints listings/photos/prices/presence;
- manifest `candidate-v2`,
  `hash_algorithm=sha256-canonical-jsonl-v1`;
- atomically published pointer, fail-closed и HTTP 410 для удалённой версии;
- legacy Mainrieltor feed `/listings/changes` и `/listings/{id}` сохранён.

Production migration была проверена на базе из 17 358 listing:

```text
integrity_check: ok
listings before/after: 17 358 / 17 358
active/missing: 8 033 / 9 325
price_history: 26 653
initial presence coverage: 17 358
двойной migration pass: идемпотентен
```

Также исправлен `seller_type`. После migration v3 в production Collector:

```text
owner:        12 088
professional: 5 271
unknown:           0
```

Последний показанный вручную candidate build публиковал около 17 227 строк.
Collector и Cleaner должны продолжать принимать оба направления смены
продавца `owner <-> professional` как возможный relist, а не как запрет дубля.

## 5. Что уже работает в Cleaner на Render

Cleaner имеет dual-source архитектуру:

- legacy `/baseline/meta` + `/baseline/table` — единственный источник
  production `/baseline/clean`;
- candidate-v2 используется только для entity store, очереди кандидатов и
  `/baseline/entity-shadow`;
- candidate failure не останавливает production cycle;
- legacy failure не разрешает candidate -> production fallback;
- production и shadow имеют независимые версии и cache;
- auto-merge отсутствует, везде `auto_merged=0`.

На Render применён metadata generator:

```text
almaty-rental-metadata-v4
```

Один из предшествовавших dry-run v4 (исторический снимок):

```text
source listings:        17 234
candidate pairs:        55 429
legacy_match:            1 295
rescue_review:          11 332
metadata_conflict:      42 154
awaiting_review:           648
auto_merged:                 0
```

После финального deploy штатный Render cycle завершился успешно и опубликовал
listing-based production baseline:

```text
published:          15 613
rejected:              953
waiting:                56
shadow_published:   16 223
production_clean_touched by entity dedupe: false
auto merge: 0
```

Production-настройка `MIN_BATCH_SIZE` восстановлена и фактически подтверждена
через `/health`: значение `5`. Последний цикл отправил один text Batch на 56
объявлений; `pending_ai=0`. Старое значение `999999` больше не актуально.

Сервис на Render перезапускался несколько раз во время ручных shell-команд.
Это объяснялось ограничением памяти 512 MB: тяжёлый refresh одновременно с
рабочим cycle давал пики памяти. Последний штатный cycle после этого завершился
успешно. Массовые эксперименты нельзя запускать внутри production Render web
service.

## 6. Логика metadata-пар v4

Старая логика Almaty не была выброшена. Она сохранена как
`legacy_duplicate_decision` и дополнена recall-контурами.

Основные принципы:

- одинаковая квартира может изменить ID, цену, описание, продавца и порядок
  фотографий;
- Krisha генерирует новые URL, поэтому равенство URL/photo set не является
  обязательным;
- смена `owner -> professional` и `professional -> owner` допустима;
- одинаковый этаж — сильный признак, но near-miss с разницей 1–2 этажа идёт в
  `rescue_review`, а не автоматически удаляется;
- близкая площадь допускает ошибки измерения;
- адреса нормализуются для Russian/Kazakh/Latin вариантов и разных форматов
  номера дома;
- одинаковый текст/его shingles — дополнительный признак;
- совпадение ЖК само по себе недостаточно;
- interior/renovation score может только подтвердить согласованность и не
  создаёт дубль самостоятельно;
- большие географические/адресные блоки только формируют кандидатов, а не
  являются решением;
- `metadata_conflict` не отправляется модели массово;
- production baseline пока остаётся listing-based.

Очередь `rescue_review` специально сохраняет возможные дубли, которые старая
строгая логика могла пропустить: drift площади, этаж ±1–2 или missing,
комнатность ±1, смена типа продавца, мультиязычный адрес и отсутствие старого
price/text confirmation. Эти пары всегда требуют сильного визуального
подтверждения.

## 7. AI-дедупликация v4 — код задеплоен, массовый прогон ещё не выполнен

Работа находится в:

```text
C:\Users\Абылай\PycharmProjects\codex-publish-20261014\cleaner
```

Изменённые tracked-файлы:

- `entity_matcher.py`;
- `entity_store.py`;
- `photo_worker.py`;
- `test_entity_resolution.py`;
- `test_photo_worker.py`.

Новые untracked-файлы:

- `eval_entity_pair_ai.py`;
- `run_entity_dedupe_shadow.py`;
- `prepare_pair_holdout.py`;
- `rerun_pair_prompt_eval.py`;
- `build_pair_review_html.py`;
- `build_holdout_review_html.py`;
- `build_error_explanation_html.py`;
- тесты этих модулей;
- `SYSTEM_COLD_REVIEW_2026-10-15.md`.

Ничего из этого ещё не нужно считать production-кодом.

### Исправления логики

1. Запись photo evidence больше не заменяет статус пары на
   `photo_evidence_ready`. Сохраняется происхождение:
   `legacy_match`, `rescue_review`, `awaiting_review` и т.д.

2. Prompt обновлён до:

```text
almaty-rental-pair-review-v4
```

Policy:

```text
almaty-rental-pair-policy-v2
```

3. Prompt требует учитывать фиксированную геометрию квартиры, кухню и санузел,
   отличать переставленную мебель/фильтр изображения от другой квартиры и не
   объявлять `mixed_photo_set` по одному слабому несоответствию.

4. Выбираемые кадры распределяются по всему упорядоченному набору, а не берутся
   только первые шесть.

5. Модель намеренно не получает цену, продавца, описание и renovation score,
   чтобы решение об идентичности не подстраивалось под коммерческие признаки.

6. Для veto `same` нужны минимум два независимых устойчивых противоречия.
   Несколько расхождений в одной комнате считаются одним противоречием.

7. Два exact reused photo могут подтвердить legacy-пару даже из одной комнаты.
   Exact reuse в разных зонах может снять raw `uncertain` только для
   legacy/awaiting, но rescue остаётся строже.

8. `mixed_photo_set + same_apartment` не разрешает автоматический merge.

9. Группы строятся только как полные клики подтверждённых пар. Транзитивность
   A=B, B=C не считается доказательством A=C.

10. Runner разделяет provisional и independently confirmed projection и не
    пишет на Render, не меняет production, не публикует shadow и не merge-ит.

### Ручная разметка пользователя

Ручные файлы:

- `C:\Users\Абылай\Downloads\entity_pair_manual_review.json`;
- `C:\Users\Абылай\Downloads\entity_pair_model_error_explanations.json`;
- `C:\Users\Абылай\Downloads\entity_pair_manual_review (1).json`.

Ключевая обратная связь:

- типовые кухни/санузлы застройщика могут быть почти одинаковыми у разных
  квартир;
- один разный диван ничего не доказывает — мебель могли заменить;
- цветовые фильтры сильно меняют плитку/диван;
- нужны устойчивые геометрические различия и комбинация независимых комнат;
- пара №27 (`1004355741` / `1015815622`) — дубль с ошибкой в выкладке фото;
- пара №47 (`1014849792` / `1016215971`) — две разные квартиры: разные кухни и
  санузлы; модель должна отвечать `different`.

После v4/policy-v2 обе контрольные пары классифицируются правильно.

## 8. Фактические результаты AI-проверки

Основной отчёт hold-out:

```text
C:\Users\Абылай\PycharmProjects\rieltor Almaty\cache\entity_pair_holdout_v4\holdout_100_ai_v4_policy2.json
```

Измерение:

```text
100 API calls
423 812 input tokens
59 479 output tokens
ручных evaluable пар: 59
coverage: 89.83%
accepted accuracy: 100%
same precision: 100%
same recall: 87.88%
false same: 0
```

Матрица на ручной части:

```text
manual different: 24 -> different, 2 -> uncertain
manual same:      29 -> same,      4 -> uncertain
```

Это хороший результат, но 59 размеченных пар недостаточно для безопасного
автоматического объединения всей базы.

План готовой очереди:

```text
C:\Users\Абылай\PycharmProjects\rieltor Almaty\cache\entity_dedupe_shadow_v4_ready_plan.json
```

```text
current shadow rows: 16 229
legacy_match ready:      856
rescue_review ready:  11 047
awaiting_review ready:   652
всего ready:          12 555
```

Текущий end-to-end sample:

```text
C:\Users\Абылай\PycharmProjects\rieltor Almaty\cache\entity_dedupe_shadow_v4_sample30_verified.json
```

Результат 30 выбранных пар:

```text
27 имели URL
23 прошли локальное декодирование и дошли до OpenAI
4 были остановлены бесплатно из-за 404/нет декодируемого фото
8 provisional same
13 different
2 uncertain
4 image errors
```

По статусам среди успешно оценённых:

```text
legacy_match:    7 same, 1 different
rescue_review:   1 same, 6 different, 1 uncertain
awaiting_review: 0 same, 6 different, 1 uncertain
```

Вывод: полный `legacy_match` имеет высокий ожидаемый yield и должен идти
первым. Массово отправлять 11 тысяч `rescue_review` нерационально; сначала
нужна стратифицированная выборка 200–300 пар по причинам rescue и lifecycle.

## 9. Важное уточнение о фотографиях

Реализация verified image payload задеплоена на Render, но сам массовый
vision-прогон должен выполняться локально, чтобы не конкурировать за 512 MB RAM
production web service.

Текущий `photo_worker.AssetCache`:

1. скачивает изображение по временному Krisha URL;
2. декодирует его через Pillow;
3. уменьшает до максимальной стороны 512 px;
4. перекодирует в JPEG;
5. держит JPEG только в RAM текущей пары;
6. отправляет модели этот JPEG как `data:image/jpeg;base64,...`;
7. сохраняет на диск только JSON с hash/fingerprint metadata;
8. очищает JPEG из RAM после успеха или любой ошибки.

Это реализовано 2026-10-15. Нормализованный JPEG теперь живёт только в RAM
текущей пары и очищается через `finally`; на диске остаются JSON-хэши,
fingerprints и ответы модели. `PairReviewer` и room-role classifier fail-closed
отказываются вызывать OpenAI без проверенного `data:image/jpeg;base64` payload.
Прямой fallback на временный Krisha URL удалён.

Проверено тестом `800×400 -> 512×256` без crop и тремя свежими API-вызовами.
После smoke-run файлов изображений в новом cache не осталось. Полный suite:
`125 tests — OK`, compileall — OK. Подробности и Render-команды находятся в
`ENTITY_DEDUPE_V4_RENDER_DEPLOY.md` рабочей копии Cleaner.

## 10. Текущее качество и тесты локальной ветки Cleaner

На момент handoff:

```text
python -X utf8 -m unittest discover -q
Ran 125 tests
OK
```

Тесты не используют реальную сеть/OpenAI. Вывод с намеренными stack trace из
failure-path тестов нормален, если финальный итог `OK`.

Зафиксированная версия прошла полный suite и опубликована. Не делать
`git reset --hard` и не переписывать её старыми копиями Cleaner.

## 11. Холодная критическая оценка всей системы

### P0

1. Нельзя переключать production на entity baseline до полного shadow-отчёта
   и ручной проверки projected groups.
2. Один ответ Luna — provisional evidence, не разрешение auto-merge.
3. `mixed_photo_set` и non-clique группы не объединять автоматически.
4. Image preflight должен происходить до платного вызова.
5. Уверенность внешней ремонтной премии нужно показывать отдельно.

### P1

1. Stage 3 смешивает active и старые missing без временного decay. Историю
   удалять нельзя, но fair-market view должен учитывать свежесть.
2. В Almaty-коде остались параметры, чьи комментарии/калибровка происходили на
   Astana baseline. Их нужно заново измерить на Алматы, не менять на глаз.
3. Asking price — не фактическая цена сделки; missing — не доказательство
   сдачи. Нужны lifecycle outcomes и пользовательский feedback.
4. Пока production baseline listing-based, дубли могут завышать effective N и
   загрязнять ремонтную премию.
5. Ступенчатые границы renovation score создают скачки около 5.9/6.0 и других
   порогов. Это challenger-задача, не срочный production hotfix.

### P2

1. Canonical entity сейчас выбирает минимальную active-цену. Для лучшего
   предложения это полезно, но для рынка создаёт downward bias. В будущем
   разделить наиболее полную `canonical_row` и `best_offer_price`.
2. Persisted identity evidence сейчас может инвалидироваться из-за цены,
   продавца или текста. Разделить visual identity revision и commercial
   listing revision.
3. Hotspot нельзя считать простым числом missing/объявлений. Нужны entity-level
   exposure, reactivation, censoring и цена.

## 12. Что решено пока не делать

- Не переоценивать ремонт всех 15–17 тысяч квартир новым prompt прямо сейчас.
- Не отправлять Luna все 55 тысяч metadata-пар.
- Не отправлять все 11 тысяч rescue без измерения yield.
- Не включать auto-merge.
- Не переключать `/baseline/clean` на entity shadow.
- Не строить hot spots до стабилизации identity graph.
- Не считать исчезновение объявления доказанной сдачей.

## 13. Точный следующий порядок работы

### Шаг 1 — исправить хранение изображений локально (выполнено)

Normalized JPEG живёт только в RAM пары; hashes/fingerprints сохраняются.
Прямые URL в AI запрещены. Пройдено 125 тестов и три свежих smoke-вызова.

### Шаг 2 — короткий end-to-end regression (выполнено)

Проверены три свежих платных вызова с пустым cache:

- preflight декодирует фактические байты;
- OpenAI получает data URL;
- нет повторного fetch временного Krisha URL моделью;
- после процесса нет JPEG на диске;
- уверенный дубль: `same_apartment`, confidence 0.99;
- уверенно разные квартиры: `different_apartment`, confidence 0.99;
- сложная пара с перемешанными фото: `uncertain`, confidence 0.98 и
  `mixed_photo_set_risk=strong`, поэтому автоматическое объединение запрещено;
- Render writes=0, production untouched, auto merge=0.

### Шаг 3 — полный рабочий слой legacy (следующий шаг)

Текущая read-only очередь содержит 850 ready `legacy_match`. Прогнать её
локально,
возобновляемо, с cache ответов и отчётом стоимости. Не применять решения в
production. Для `same` желательно независимое подтверждение перед merge.

### Шаг 4 — awaiting и выборка rescue

- затем до 652 ready `awaiting_review`;
- взять 200–300 `rescue_review`, стратифицированных по reason и
  active/missing;
- посчитать yield, precision и типы ошибок отдельно по strata;
- запускать остальной rescue только там, где измерена польза.

### Шаг 5 — shadow graph и ручная приёмка

Сформировать:

- confirmed same edges;
- полные clique groups;
- non-clique блокировки;
- mixed photo warnings;
- image errors;
- provisional и confirmed reduction;
- список всех групп, которые изменят canonical baseline.

Ручная проверка всех projected merged groups обязательна перед первым
production switch.

### Шаг 6 — только затем production entity baseline

После приёмки отдельно реализовать/включить безопасный switch, rollback и
аудит. Не подменять существующий `/baseline/clean` молча.

### Шаг 7 — улучшения цены и hot spots

После стабилизации entity graph:

1. entity-level baseline для цены и ремонта;
2. fresh-market view с temporal decay missing;
3. отдельные confidence цены и премии ремонта;
4. Almaty recalibration Stage 3;
5. lifecycle/hotspot метрики по entity, не listing ID;
6. Telegram feedback о фактической актуальности/сдаче.

## 14. Команды для продолжения локальной работы

Перейти в Cleaner:

```powershell
Set-Location "C:\Users\Абылай\PycharmProjects\codex-publish-20261014\cleaner"
```

Проверить изменения:

```powershell
git status --short
git diff --stat
```

Запустить тесты:

```powershell
if (Test-Path ".\.venv\Scripts\python.exe") {
    & ".\.venv\Scripts\python.exe" -X utf8 -m unittest discover -q
} else {
    python -X utf8 -m unittest discover -q
}
```

План очереди без OpenAI и без записей на Render формируется через
`run_entity_dedupe_shadow.py` в plan-режиме. CLI уже опубликован, но перед
платным запуском всё равно открыть справку и проверить параметры:

```powershell
python -X utf8 .\run_entity_dedupe_shadow.py --help
```

## 15. Секреты и безопасность

Никогда не записывать API keys в код, Markdown, JSON-отчёты или команды,
которые попадут в Git. В предыдущем диалоге Cleaner API key был вставлен в
сообщение вручную. Его значение не перенесено в этот файл. Перед дальнейшей
публичной работой безопаснее ротировать этот ключ в Render и использовать
`Read-Host`/environment.

## 16. Короткая инструкция следующему чату

1. Прочитать этот файл полностью и при споре об истории обратиться к
   `MAINRIELTOR_CONTEXT_2026-10-12.md`.
2. Считать commit `eaea96b` задеплоенным и подтверждённым через `/health`.
3. Пользователь уже дал разрешение продолжить полный локальный shadow-прогон
   850 `legacy_match`; повторно предлагать подготовительный рефакторинг не надо.
4. Перед платным вызовом выполнить plan и показать/проверить число строк,
   модель, ожидаемую стоимость и каталог отчёта. Ключи не печатать.
5. Primary-прогон делать Luna с устойчивым cache/checkpoint. Ничего не POST-ить
   обратно в Cleaner, не merge-ить и не переключать production baseline.
6. После primary отдельно подтвердить только предсказанные `same`, затем
   сформировать projected groups и ручной отчёт.
7. Не отправлять все 11 934 `rescue_review`; после legacy сначала измерить
   strata на выборке 200–300 пар.
8. Сохранять две асимметрии продукта: высокая полнота для Telegram-находок и
   очень высокая точность для объединения дублей.

## 17. Финальный зафиксированный статус deploy и точка продолжения

### Что подтверждено на Render

`GET https://rieltor-cleaner-almaty.onrender.com/health` после deploy вернул:

```text
git_commit:             eaea96bd6ffd5660f3929ed19abaa864b5501e38
candidate_generator:    almaty-rental-metadata-v4
pair_prompt:            almaty-rental-pair-review-v4
pair_policy:            almaty-rental-pair-policy-v2
photo_normalization:    rgb-exif-max512-v1
mode:                   production
api_only:               false
MIN_BATCH_SIZE:         5
MAX_BATCH_SIZE:         3000
AI submission enabled:  true
auto merge:             0
```

Важно: deploy кода не запускает vision-дедупликацию автоматически. Production
Cleaner продолжает обычную очистку и text Batch; `/baseline/clean` остаётся
listing-based. Entity shadow изолирован.

### Последний read-only план очереди

Отчёт сохранён здесь:

```text
C:\Users\Абылай\PycharmProjects\rieltor Almaty\cache\render_postdeploy_plan\plan.json
```

Результат:

```text
legacy_match ready:       850
awaiting_review ready:    654
selected smoke plan:       20
current shadow rows:   16 223
OpenAI called:          false
Render writes:              0
production touched:     false
auto merged:                0
```

### Свежие платные smoke-тесты

Проверены пары:

```text
1016225510 / 1016427348 -> same_apartment, confidence 0.99
1014849792 / 1016215971 -> different_apartment, confidence 0.99
1004355741 / 1015815622 -> uncertain, mixed_photo_set_risk=strong
```

Третий результат является безопасной блокировкой спорного случая, а не
основанием для merge. Всего на три вызова: 12 947 input, 1 575 output,
14 522 tokens. В новом smoke-cache сохранены только JSON; файлов изображений
нет.

### Что запускать следующим

Рабочая папка:

```powershell
Set-Location "C:\Users\Абылай\PycharmProjects\codex-publish-20261014\cleaner"
```

Сначала открыть точную актуальную справку:

```powershell
python -X utf8 .\run_entity_dedupe_shadow.py --help
```

Затем сделать повторный plan на весь `legacy_match`, сверить, что остаётся
около 850 пар, и лишь после этого запустить `--execute` с моделью
`gpt-5.6-luna`. Результат и cache складывать в новый каталог под:

```text
C:\Users\Абылай\PycharmProjects\rieltor Almaty\cache\entity_dedupe_legacy_full_20261015
```

Не включать confirmation в первый проход: сначала получить число первичных
`same` и фактическую стоимость. Вторым проходом отправить только эти `same` на
независимое подтверждение. Благодаря response cache повторный запуск не должен
повторно оплачивать уже завершённые primary-запросы.

Ориентир времени для 850 последовательных пар: примерно 2–3 часа при обычной
скорости API, дольше при rate limits/сетевых ошибках. Это оценка, а не SLA.

### Запреты для следующего шага

- не запускать все 55 тысяч пар;
- не запускать всю rescue-очередь;
- не выполнять прогон внутри Render shell;
- не делать auto-merge;
- не переключать production baseline;
- не сохранять JPEG/фото на диск;
- не выводить содержимое `.env` или API keys.

## 18. Закрытие vision-dedupe и решение для территориального скоринга

Полный локальный primary shadow-прогон `legacy_match` завершён. Он ничего не
писал в Render, не делал auto-merge и не переключал production baseline.

```text
selected:                    850
processed with photos:       805
actual API calls:            714
same / different / uncertain: 506 / 146 / 62
photo preflight errors:       91
elapsed:                     4150.5 s
estimated Luna cost:         ~$1.03
provisional reduction:       370 rows (16 223 -> 15 853 at run time)
```

Primary report:

```text
C:\Users\Абылай\PycharmProjects\rieltor Almaty\cache\entity_dedupe_legacy_full_20261015\primary_report.json
```

Для независимой проверки взяты 20 наиболее информативных, а не наиболее
лёгких `same`: без exact-content match, с локальным `uncertain` и минимальной
primary confidence. Terra подтвердила 12; 8 остались `uncertain`; ни одна не
стала `different`. Это достаточно для оценки precision, но недостаточно для
безопасного автоматического слияния всего графа.

```text
C:\Users\Абылай\PycharmProjects\rieltor Almaty\cache\entity_dedupe_legacy_full_20261015\confirmation_informative20_report.json
```

Итоговое продуктовое решение: vision/entity-граф не применять к production
baseline и не использовать в Stage 3. Production остаётся listing-based,
auto-merge остаётся выключенным. Дополнительные платные проверки этой ветки не
нужны.

Причина проверена не только процентом строк, но и прямым counterfactual Stage 3
на актуальном `/baseline/entity-shadow` version `4c5db97b6765cc52`:

```text
provisional edges treated as true: 506
matched components / removed rows: 343 / 400
hold-out targets scored:           449
all targets, median fair-price shift:  -0.004%
all targets, |shift| p90 / max:         2.147% / 29.221%
all targets, verdict changes:           13 / 449 (2.90%)
broad territorial sample, changes:       3 / 225 (1.33%)
near duplicate clusters, changes:       10 / 224 (4.46%)
near clusters, |shift| p90:              3.079%
```

То есть средний городской эффект мал, но локальные same-building/radius
когорты иногда меняются резко. Для территориального скоринга ложное слияние
опаснее небольшого перевеса дублей. Текущий Stage 3 уже ограничивает влияние
дублей через `effective_n()` и географические когорты 75 м / 0.5 км / 1 км /
3 км. Следующий этап не должен начинаться со смены baseline; отдельные
H3/hotspot/lifecycle-сигналы можно проектировать поверх listing-based данных и
считать только shadow-признаками.

### Решение по territorial/hotspot scoring

На текущем этапе территориальный скоринг не разрабатывать и не добавлять ни в
`base_price_m2`, ни в `benchmark_confidence`, ни в итоговый вердикт. Текущий
Stage 3 оставить без изменений. Cleaner должен только продолжать накапливать
listing-based историю присутствия, цен, статусов и координат. Никакие
entity/vision-слияния для этой истории не включать.

К hotspot/lifecycle-модели вернуться позднее, когда накопится достаточная
временная глубина для оценки экспозиции, снятия, повторного появления и
изменений цены по территориям. До этого момента данные можно исследовать
офлайн, но их вывод не должен влиять на production-оценку или Telegram.
