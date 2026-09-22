"""
Stage 3 — объективная оценка цена/качество для новых объявлений.

=============================================================================
v5 — что изменено и почему.

ВАЖНО о числах в этом разделе. Все они — записи ИЗМЕРЕНИЙ, сделанных во
время работы над v5, на тогдашнем krisha_astana_baseline.csv (7047
объявлений аренды, 2054 группы зданий). Это обоснования принятых тогда
решений, а не описание сегодняшнего состояния: они объясняют, ПОЧЕМУ код
устроен так, и не пересчитываются задним числом.

Текущий baseline другой — 11 324 строки (11 298 usable), 1028 групп
зданий, class score есть у 83.1% строк. Форма распределения при
этом почти не изменилась (медианная ошибка 0.1053 против 0.106), поэтому
качественные выводы v5 остались в силе — но КАЛИБРОВОЧНЫЕ константы,
которые являются перцентильными срезами, пересчитаны под новый пул. У
каждой такой константы рядом стоит актуальное измерение: см.
CONFIDENCE_TIER_HIGH, VALUE_SCALE, FIND_THRESHOLD_* и
OVERPRICE_THRESHOLD_*.

Что воспроизводится при каждом запуске с --baseline: report_accuracy(),
check_audit_trail(), verify_weight_hierarchy() и проверка возраста
эталона. Ни одно утверждение здесь не следует принимать на слово: если
цифра не сходится после правки, значит правка что-то сломала.
=============================================================================

Опорный факт, вокруг которого построена вся ревизия:

    наивный-1 (медиана города по комнатности)      median |err| = 0.160
    наивный-2 (медиана своего дома, LOO)           median |err| = 0.125
    эта модель                                     median |err| = 0.106

Наивный-2 — это пять строк кода. Он забирает ~70% всего достижимого
выигрыша. Перебор ключевых констант (CLASS_MATCH_THRESHOLD, CREDIBILITY_K,
FULL_CREDIBILITY_N, вес L3, полное отключение L5) сдвигает медианную
ошибку меньше чем на 0.003 в любую сторону — то есть точность упёрлась в
потолок этого набора признаков и настройками не улучшается. Поэтому v5 не
пытается улучшить точность; v5 приводит в порядок то, что модель ГОВОРИТ о
своей точности. Это была настоящая проблема.

1. benchmark_confidence больше не константа с шумом.
   Было: size_factor суммировал effective_n по ВСЕМ уровням без весов,
   поэтому L6 с весом 0.2% приносил свои сотни объявлений в «сколько у нас
   доказательств». Плюс аддитивная форма 0.35*size + 0.65*level давала пол
   в 0.65. Результат: p1 = 0.749, p50 = 0.948, 98% строк в верхней ветке —
   переменная, не различающая ничего.
   Стало: размер взвешен теми же весами, форма мультипликативная.
   p1 = 0.27, p50 = 0.675; по веткам 4488 / 2324 / 234 вместо 7003 / 39 / 3.
   Трёхуровневая лестница порогов наконец работает.

2. Пороги вердикта выведены из измеренной ошибки, а не из круглых чисел.
   Было: находка от +10%, переоценка от -7% при собственной медианной
   ошибке модели 10.6%. 17.6% строк лежали в пределах 2 п.п. от порога.
   Стало: +18% / -15%, привязано к p70-p75 распределения |diff_pct|.
   В зоне неустойчивости теперь 10.2% строк, вердиктов 1909 вместо 3608 —
   меньше и каждый переживает собственную неопределённость модели.

3. «ТРЕБУЕТ ПРОВЕРКИ» отвязано от незаполненного поля.
   Было: 502 из 504 срабатываний шли по is_thin_description (один из его
   признаков был пуст у 40% рынка), по уверенности — 3. Вердикт про
   «ошибку когорты или скрытый дефект» на деле означал «продавец не
   заполнил поле».
   Стало: вердикт — только статистика (43 строки), а неполнота объявления
   уехала в отдельную колонку review_flags (3259 строк). Два разных
   утверждения снова различимы.

4. Оценка разброса одинаковая при любом размере когорты.
   Было: MAD*1.4826 при n<4 и сырой IQR при n>=4 — две разные величины под
   одним порогом. При одинаковом истинном разбросе когорта из 3 объявлений
   показывала dispersion 0.051 и получала homogeneity 0.94, из 20 —
   0.123 и 0.73. Малые когорты премировались за разброс, который они
   физически не могли увидеть.
   Стало: одна оценка на всём диапазоне с поправкой на смещение
   (_MAD_BIAS_CORRECTION, откалибровано симуляцией).

5. Аудит-трейл сходится.
   Было: в смесь шла adjusted_median (после сжатия к приору), в CSV
   печаталась сырая median. Восстановить base_price_m2 из колонок не
   удавалось у 36% строк.
   Стало: публикуются обе плюс credibility; проверка встроена
   (check_audit_trail) и на пуле v5 давала 7046/7046, на текущем —
   11 296/11 296. Печатается при запуске через run() (CLI); оркестратор
   вызывает score_row() напрямую и этот self-check не выполняет.

6. cohort_level — уровень, который дал число, а не тот, на котором
   каскад остановился. Это разные вещи: остановка приходится на L3 и шире
   почти всегда, а вес в 5937 случаях из 7046 приходит с L1/2. Любой
   фильтр по старой колонке отбирал не то. Уровень остановки сохранён под
   именем cohort_stop_level.

7. Санитарные границы цены, которые докстринг обещал, а код не делал.
   Единственной проверкой было `price_m2 is None`. Одна опечатка в цене
   двигала медианы города, квартили классов и приор, то есть бенчмарк
   ВСЕХ объявлений. Добавлены абсолютные границы и робастный тест по
   логарифму (mark_price_outliers). На этих данных отсеяно 3 строки.

8. Учёт отделки (finish_type) удалён целиком. У эталона от rieltor-cleaner
   этого поля нет, а у входящих объявлений оно почти всегда пусто, так что
   фильтр когорт по отделке не различал ничего.

9. Класс здания: фильтр стал фильтром, покрытие выросло.
   CLASS_MATCH_THRESHOLD 0.5 -> 0.25 (пропускал 79.5% зданий, стало 63.6%;
   допуск по уровню цен здания 1.28x -> 1.17x). MIN_FOR_BUILDING_SCORE
   2 -> 1, потому что 1162 из 2054 зданий имеют одно объявление, и
   отсутствие класса означало не «воздержаться», а «удалить кандидата»:
   покрытие 71.7% -> 80.3%.

10. Дубликаты объявлений. effective_n схлопывает не только повторы
    одного владельца, но и одну и ту же квартиру, перевыставленную под
    новым id (комнатность + площадь + цена). Раньше перепост попадал в
    L1/2 — уровень с наибольшим весом — и считался двумя наблюдениями.

11. robust_z не считается на пустом месте. Масштаб определяется
    доминирующей когортой, и при одном-двух объявлениях это одно
    абсолютное отклонение: z взрывался и упирался в клэмп +-10 (17 строк),
    после чего порог 3.5 срабатывал на арифметике. Ниже
    MIN_ROWS_FOR_ROBUST_Z эффективных наблюдений z не определён.

12. Нормализация адресов. Дефис не сворачивался (хотя `Uly-Dala` стоял в
    примерах докстринга), CamelCase не разбирался. 16 написаний одного
    проспекта давали 6 ключей, теперь 3; всего улиц 1104 -> 770 против
    1104 -> 818. Обратное: обрезка хвостового числа больше НЕ применяется
    к complex_name и house_num — `ЖК Арман 2` и `ЖК Арман 3` схлопывались
    в один дом, а это ключ, определяющий L1/2.

13. build_cohort() поднят из мёртвых: обращался к spatial_index и
    building_index, которых не было ни в параметрах, ни в модуле, то есть
    внешний интерфейс для orchestrator.py падал с NameError на любом
    вызове. Плюс терпимость к «сырой» строке без `_`-полей.

14. Убрано: N_MIN, MIN_FOR_BUILDING_CLASS, COORD_BUILDING_TRUST,
    confidence_from_cohort(), compute_price_segments() (считалась и не
    читалась никем), пересчёт квантилей по всему пулу внутри
    target_price_segment на каждой строке.

Что осознанно НЕ тронуто и почему:
    * L5 остаётся, хотя её медиана по построению заперта в ±30% вокруг
      приора, к которому её потом ещё и сжимают, — то есть это приор под
      другим именем. Отключение L5 целиком меняет медианную ошибку на
      0.000. Оставлена как диагностика, но полагаться на неё как на
      независимое свидетельство нельзя.
    * РУЧНАЯ ПРОВЕРКА почти не срабатывает: на пуле v5
      requires_manual_review был False у всех 7047 строк, на текущем —
      True всего у 3 из 11 324. Ветка практически не проверена на
      реальных данных — это не «работает», это «почти не запускалось».

Главный принцип:
    baseline = строгий, чистый и замороженный рынок;
    incoming = реальные новые объявления, которые НЕ проходят жёсткий
               baseline-фильтр.

Новые объявления не исключаются из-за:
    - риелтора;
    - отсутствия фото;
    - red_flags;
    - неполной LLM-разметки;
    - рассрочки.

Вместо этого эти признаки становятся предупреждениями и снижают
уверенность. Жёстко невозможные/сломанные числовые данные не позволяют
сделать осмысленный price/m² score, но строка всё равно остаётся в output.

Вердикт:
    - НАХОДКА — цена заметно ниже объективной базы при достаточной уверенности;
    - СПРАВЕДЛИВАЯ — находится около ожидаемого рынка;
    - ПЕРЕОЦЕНЕНА — заметно выше ожидаемого рынка;
    - НЕДОСТАТОЧНО ДАННЫХ — сравнение слишком слабое;
    - ТРЕБУЕТ ПРОВЕРКИ — отклонение аномально большое
      (>= SUSPICIOUS_DIFF_THRESHOLD) И одновременно уверенность базы низкая.
      Это НЕ "находка": такой разрыв при слабой базе чаще означает ошибку
      когорты, чем отличную цену. Скудость описания сюда БОЛЬШЕ НЕ ВХОДИТ
      (v5, п.3) — она в колонке review_flags;
    - РУЧНАЯ ПРОВЕРКА — есть серьёзный red flag, который нельзя честно
      превращать в ценовую скидку автоматически.

Когорты (collect_cohorts) — КУМУЛЯТИВНАЯ схема, а не каскад
"первый подошедший уровень побеждает":

    L1/2  — «первая когорта»: объединение точных совпадений по зданию
            (complex_key ИЛИ street+house_num ИЛИ, если ни того ни
            другого нет, координаты в пределах 75 м) с той же комнатностью
            (бывшие L1/2 и L2b) ПЛЮС L2.5 — то же здание, ДРУГАЯ
            комнатность, price_m2 пересчитан через отношение ГОРОДСКИХ
            медиан по комнатности. L2.5 отбирается чисто структурно
            (то же здание, другая комнатность) и только потом один раз
            получает городской (не локальный) коэффициент пересчёта —
            цена конкретного дома никак не влияет на свой же бенчмарк.
    L3    — радиус 0.5 км, та же комнатность, ТОТ ЖЕ КЛАСС ЗДАНИЯ.
    L4    — радиус 1 км, та же комнатность, тот же класс здания.
    L5    — радиус 3 км + сужение по цене, та же комнатность, тот же класс.
    L6    — весь город, та же комнатность, тот же класс.

Порядок работы:
    1. Определяется класс здания основной квартиры — по ВСЕМ комнатностям
       этого здания, каждая комнатность сравнивается со СВОИМ городским
       распределением (building_class()).
    2. Собирается L1/2.
    3. Затем ОБЯЗАТЕЛЬНО собирается L3 (даже если L1/2 полная).
    4. Если L3 набрала MIN_COHORT_L3 — стоп. Иначе L4, затем L5, затем L6.
    5. Все собранные до остановки когорты участвуют в итоговой оценке.
    6. Каждое объявление используется ровно один раз: перед сбором
       очередного уровня из кандидатов вычитаются все id, уже попавшие в
       предыдущие когорты.
    7. Итоговый benchmark = взвешенное среднее медиан когорт, вес =
       level_weight × quantity_factor(effective_n) × homogeneity_weight.
       Иерархия строго сохраняется: L1/2 ≫ L3 ≫ L4 ≫ L5 ≫ L6 — более
       широкая когорта не заменяет более точную, а только дополняет её.

Важно:
    Stage 3 не утверждает, что квартира "хорошая" только потому, что она
    дешёвая. Поэтому отдельно выводятся:
        value_score              — насколько цена привлекательна;
        quality_evidence_score   — насколько много подтверждений качества;
        data_confidence          — насколько надёжен сам benchmark;
        seller_class             — хозяин / риелтор / неизвестно.

Для baseline используется только пригодный эталонный пул.
Для incoming текущая квартира НИКОГДА не подмешивается в baseline.
"""

import argparse
import csv
import math
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone


# ============================== CONFIG ==============================

# MIN_COHORT_* thresholds survive, but their MEANING changed in the
# cumulative scheme (ТЗ п.1). They no longer mean "this level is good
# enough to be USED INSTEAD OF the previous one" — every collected cohort
# is always used. They now mean only: "this level gathered enough
# listings that we may STOP widening the search here".
#
# Numerically they are left as they were: they were calibrated against
# the same underlying question ("how many listings does a match of this
# looseness need before its median is worth anything"), and that question
# didn't change. What changed is that failing the threshold is no longer
# destructive — an L3 with 3 listings is still counted in the final
# benchmark, it just doesn't authorize a stop.
#
# MIN_COHORT_L12 / L2B / L25 are gone as separate stop-gates: L1/2 is now
# a single merged first cohort (exact building matches + L2.5 rescaled
# other-room matches) and NEVER stops the cascade — L3 is always
# collected after it (ТЗ п.1).
MIN_COHORT_L3 = 4
MIN_COHORT_L4 = 5
MIN_COHORT_L5 = 6

# Level weight — the top of the weight hierarchy required by ТЗ п.2/п.17.
# Each step down multiplies influence by roughly 4x less, so a wider
# cohort can only ever ADD a correction to a tighter one, never overturn
# it: even L1/2 with a single listing (quantity factor 0.33) outweighs a
# 30-listing L3 (0.22 * 0.94 = 0.21). Strict ordering
# L1/2 >>> L3 >>> L4 >>> L5 >>> L6 holds for every attainable combination
# of cohort size and homogeneity, which is exactly what ТЗ п.11 demands
# ("основная иерархия должна сохраняться").
# The numbers are not free-hand: they are chosen so the hierarchy is
# PROVABLE, not merely typical. The smallest multiplier a level can ever
# suffer from the other factors is
#     min over n of  purity × quantity_factor(n_eff) × homogeneity_weight
# For L1/2 the purity discount (RESCALE_TRUST, applied when the cohort is
# made entirely of rescaled cross-room comparables) compounds with the
# reduced effective count it causes, bottoming out near 0.15; for every
# other level purity is 1 and the bound is 0.25. Each step down the
# ladder therefore multiplies the level weight by less than 0.14, so no
# attainable combination of size, homogeneity and purity can let a wider
# cohort outweigh a tighter one. verify_weight_hierarchy() checks this
# numerically — including the all-rescaled worst case — on every run.
LEVEL_WEIGHT = {
    "1-2": 1.00,
    "3": 0.130,
    "4": 0.028,
    "5": 0.0060,
    "6": 0.0013,
}

# Quantity factor: n / (n + QUANTITY_HALF_N), a smooth saturating curve
# instead of the old hard "cleared the threshold / didn't" switch
# (ТЗ п.3, п.14). QUANTITY_HALF_N = 2 reproduces the gradient the spec
# asked for almost exactly: relative to a 4-listing cohort at 1.00,
# 3 listings score 0.90 (ТЗ's example: 70 -> ~63), 2 listings 0.75,
# 1 listing 0.50. Nothing ever drops to zero for lack of size, so a
# small-but-precise cohort keeps a real say instead of disappearing.
QUANTITY_HALF_N = 2.0

# Homogeneity reference dispersion (IQR/median) per level for the smooth
# weight penalty of ТЗ п.11: a cohort of 620/625/628/631 and one of
# 500/620/710/850 must not carry the same trust at the same n. Weight is
# 1/(1+(d/ref)^2), i.e. full weight at d=0, half weight at d=ref,
# floored at HOMOGENEITY_FLOOR so a noisy cohort is discounted, never
# erased. Refs widen with level because a radius match is heterogeneous
# by construction and shouldn't be punished for the same spread that
# would be alarming inside one building.
DISPERSION_REF = {
    "1-2": 0.20,
    "3": 0.20,
    "4": 0.25,
    "5": 0.28,
    "6": 0.32,
}
# Floor of 0.5 = a maximally scattered cohort is worth half a perfectly
# tight one of the same size. Deliberately bounded: dispersion must be a
# real discount, but it must not be able to eat a whole level's worth of
# advantage, or it would break the hierarchy the spec insists on (see the
# LEVEL_WEIGHT note above for the exact arithmetic).
HOMOGENEITY_FLOOR = 0.5
# Used when a cohort is too small for any dispersion estimate at all
# (n=1): neither rewarded nor punished for homogeneity we cannot observe.
HOMOGENEITY_UNKNOWN = 0.85

# Below this cohort size we no longer trust the cohort's own median at
# face value even after it clears the level's minimum n — see
# credibility_weight() / shrink_to_prior() below (review finding #2:
# "all-or-nothing" cascade vs credibility-weighted blending).
FULL_CREDIBILITY_N = 8

# CREDIBILITY_K is the Bühlmann credibility constant: K = sigma^2 / tau^2,
# i.e. within-building price variance divided by between-building price
# variance. It is NOT a free tuning knob — it was estimated from
# krisha_astana_baseline.csv (7047 usable rental listings, Astana) via a
# one-way random-effects ANOVA on price_m2, grouped by building
# (complex_id, falling back to street+house_num):
#
#   sigma^2 (pooled within-building variance) = mean-square within groups
#   tau^2   (between-building variance)       = (mean-square between - sigma^2) / n0
#
# Four variants of the estimation were run (same room count only vs. any
# room count on a citywide-median-normalized relative price index; with and
# without dropping singleton buildings; with and without winsorizing the
# top/bottom 1% as outlier protection) and converged tightly to K in
# [0.79, 1.07] across all of them — a narrow enough band on real data to
# trust, not an artifact of one particular grouping choice. K=1 is the
# round number nearest the center of that cluster.
#
# This replaces an earlier guessed K=4, which implicitly assumed
# within-building prices were 4x noisier relative to between-building
# spread than they actually are on this market — i.e. it systematically
# under-weighted small-but-precise building-level cohorts exactly where
# review finding #2 said they were being under-weighted. With K=1, a
# single same-building comparable (n=1) already gets Z=0.5 in the blend
# (credibility_weight()) instead of Z=0.2.
#
# Re-derive this periodically as the baseline grows or if the tool is
# pointed at a different city/market — within/between price variance is a
# property of the data, not a universal constant.
CREDIBILITY_K = 1.0

# A room count needs at least this many listings inside the building
# before its own median is allowed to speak for that room count. Below
# it the listings still count toward MIN_FOR_BUILDING_SCORE through the
# other room counts, they just don't get their own median.
MIN_PER_ROOMS_FOR_CLASS = 1

# Minimum OTHER listings in a building before it gets a class score at
# all. Lowered from 3 to 2 because the score's uncertainty is now
# carried explicitly (CLASS_SCORE_SE_1 below) instead of being hidden
# behind a hard cutoff — a 2-listing building is allowed to speak, it
# just speaks less precisely and is matched more loosely.
# v5: 2 -> 1. With 2, a class score required THREE classifiable listings
# in a building (the row itself plus two others), and on this baseline
# 1162 of 2054 building groups hold a single listing. Result: only 5056 of
# 7047 rows (71.7%) had a class score, and _class_filtered() drops every
# candidate without one — so 28% of the reference pool was invisible to
# L3-L6, systematically the small buildings. A 1-listing score is allowed
# now because its uncertainty is already carried explicitly
# (CLASS_SCORE_SE_1 / sqrt(1) = 0.31) and class_match_tolerance() widens
# to match. Abstaining was not neutral: it deleted the candidate.
MIN_FOR_BUILDING_SCORE = 1

# Class as a continuous position on the citywide price scale
# (ТЗ п.6: "средневзвешенное по комнатностям"). Anchors are the citywide
# quartiles OF THAT ROOM COUNT: q25 -> 0.5 (эконом/комфорт boundary),
# q75 -> 1.5 (комфорт/бизнес boundary).
#
# The scale is logarithmic in price and NO LONGER CLIPPED to [0, 2].
# Clipping collapsed every building above the citywide q75 into one
# indistinguishable "бизнес" bucket: on this baseline that bucket runs
# from 6 667 to 13 846 ₸/m² (a factor of 2.1), so a genuinely elite
# building was being compared against merely above-average ones as if
# they were peers. 3.2% of classified buildings sat exactly on the clip
# — and they are the most expensive ones, where the error costs most.
CLASS_SCORE_ECONOM_MAX = 0.5
CLASS_SCORE_COMFORT_MAX = 1.5

# Standard error of a building's class score as a function of how many
# listings it was computed from: se(n) = CLASS_SCORE_SE_1 / sqrt(n).
#
# Measured on the baseline: for the 111 buildings holding >=12 listings
# (whose full-sample score is treated as ground truth), random subsamples
# of size n reproduced it with median absolute error 0.314 (n=1), 0.218
# (n=2), 0.178 (n=3), 0.163 (n=4), 0.123 (n=6), 0.102 (n=8) — a clean
# 0.31/sqrt(n) curve. At n=1 the score lands in the wrong bucket 29.6% of
# the time, at n=2 14.0%, at n=3 8.4%.
#
# Rather than pick a cutoff below which a class is "not known", the
# matching tolerance absorbs this: two buildings whose scores we know
# only vaguely are allowed to match loosely, because we genuinely cannot
# tell them apart. See class_match_tolerance().
CLASS_SCORE_SE_1 = 0.31

# Base tolerance for "same class" when both scores are known precisely,
# in class-score units (1.0 = one full bucket width). Calibrated against
# the median absolute prediction error of the resulting cohort on a
# 700-listing sample: hard 3-bucket matching scored 12.96% error at 89.6%
# fill, while continuous matching scored 11.76% at threshold 0.2 (74.7%
# fill), 12.17% at 0.3, 12.11% at 0.4 and 11.75% at 0.5 (86.7% fill).
# 0.5 keeps essentially the old coverage while cutting error by 9%
# relative.
# v5: lowered 0.5 -> 0.25. Measured on krisha_astana_baseline.csv, the
# 0.5 setting let 79.5% of all classified buildings through for a
# median-class target (4020 of 5056), i.e. it removed a fifth of the city
# while the verdict thresholds it is supposed to protect live at 7-18%.
# In price terms 0.5 + 2*se(n=10) = 0.70 class units allowed comparables
# whose building price level differed by 1.28x. 0.25 brings that to
# roughly 1.15x at n=10 while keeping the uncertainty widening intact.
CLASS_MATCH_THRESHOLD = 0.25

# A (room count × building class) prior cell needs this many listings
# before it is preferred over the blunter citywide-by-room prior. 20 is
# enough for a stable median while still populating every cell that
# matters on the current baseline (see prior_medians_by_rooms_class()).
MIN_FOR_PRIOR_CELL = 20

# How much a cross-room comparable rescaled through citywide medians is
# worth relative to a direct same-room comparable in the same building.
#
# Measured, not chosen. On krisha_astana_baseline.csv (7047 usable
# listings, 2087 buildings) every within-building pair was formed twice:
# directly (same room count, 34 586 pairs) and through the rescale (other
# room count projected via the citywide room-median ratio, 53 040 pairs).
# The robust spread of log price ratios was 0.214 for direct pairs and
# 0.254 for rescaled ones; the inverse-variance relative weight is
# therefore 0.214^2 / 0.254^2 = 0.71. Re-estimated nine ways (MAD-based,
# IQR-based and plain stdev scale; all pairs, and buildings with >=5
# listings only) it stayed inside [0.705, 0.750].
# 0.72 is the centre of that band.
#
# Re-derive alongside CREDIBILITY_K if the tool is pointed at another
# market — how well a citywide room ratio transfers to one building is a
# property of the data.
RESCALE_TRUST = 0.72

# L3 is now a tight radius rather than "same street". Measured on
# krisha_astana_baseline.csv: Astana's avenues run 8-11 km end to end
# (turan 8.6, uly_dala 10.6, tole_bi 11.4), and 46% of all same-street
# listing pairs sit further apart than RADIUS_L4_KM — i.e. the old L3 was
# routinely WIDER than the L4 it outranked by 4.6x in weight.
#
# 0.5 km was chosen from a sweep of 0.2-1.0 km scored by median absolute
# prediction error of the cohort median against the target's own price
# (900-listing sample, class filter applied):
#     0.25 km -> 10.16% error, 61% fill
#     0.30 km -> 10.49% error, 66% fill
#     0.50 km -> 10.34% error, 80% fill
#     1.00 km -> 10.25% error, 94% fill
# Error is essentially flat across that whole range once the building
# class filter is applied — the class filter, not the radius, is what
# does the work (without it the same sweep sits at 12.6-13.6%). 0.5 km
# takes the best fill available at indistinguishable error. For
# reference, the old same-street rule scored 13.89% error at 71% fill:
# the radius beats it on BOTH axes.
# Within one room count, price per m² falls as the flat gets larger.
# Measured on krisha_astana_baseline.csv as the slope of
# log(price_m2) ~ log(square), three ways (plain OLS, OLS after
# winsorizing the top/bottom 1% of area and price, and Theil-Sen on
# 4000 random pairs of the winsorized set):
#     1к  n=2529   -0.364 / -0.372 / -0.455
#     2к  n=2904   -0.440 / -0.466 / -0.530
#     3к  n=1178   +0.040 / +0.016 /  0.000
#     4к  n= 368   -0.010 / -0.014 / -0.120
# Large and consistent for 1- and 2-room flats (doubling the area cuts
# price per m² by 22-26%), indistinguishable from zero for 3- and 4-room
# ones. Left unmodelled, that gradient was being recorded as a discount:
# every large 1к/2к looked like a bargain and every small one looked
# overpriced, purely because of size.
#
# The slope is re-estimated from the pool at runtime rather than frozen
# here — it is a property of the market, and the numbers above are only
# what this baseline produced.
MIN_AREA_MODEL_N = 200
# Below this |slope| the effect is not distinguishable from noise and no
# correction is applied at all, which is what keeps 3к/4к untouched.
AREA_SLOPE_DEADZONE = 0.05
# Slopes outside this range would be an artefact, not a market effect.
AREA_SLOPE_MIN = -0.80
AREA_SLOPE_MAX = 0.20
# Hard bound on how far one comparable may be moved. A 2x area gap
# already sits at the edge of "same kind of flat"; beyond that the
# listing is not really comparable and the correction should not pretend
# it can fix that.
AREA_ADJUST_MIN = 0.75
AREA_ADJUST_MAX = 1.35

# Hard sanity bounds on price/m². The module docstring promised that
# "жёстко невозможные/сломанные числовые данные" would not produce a
# score, but no such check existed: the only filter anywhere was
# `price_m2 is None`. A mistyped price therefore entered the reference
# pool and moved the citywide medians, the class quartiles and the prior.
#
# Two layers, both cheap:
#   * absolute bounds, to catch unit errors (a monthly rent per m² of 50
#     or of 500 000 is not a cheap or expensive flat, it is a broken row);
#   * a robust log-scale outlier test against the pool itself
#     (|z| > PRICE_OUTLIER_MAD_Z on the MAD scale), which adapts if the
#     tool is pointed at a different market or at sale instead of rent.
# Both apply to the BASELINE pool only. An incoming listing is still
# scored — it just gets a warning — because rejecting it silently is how
# a monitoring tool loses the listings it exists to flag.
PRICE_M2_ABS_MIN = 300.0
PRICE_M2_ABS_MAX = 2_000_000.0
PRICE_OUTLIER_MAD_Z = 5.0
SQUARE_ABS_MIN = 8.0
SQUARE_ABS_MAX = 1000.0

RADIUS_L3_KM = 0.5
RADIUS_L4_KM = 1.0
RADIUS_L5_KM = 3.0

# Third-tier building identification. When a listing carries neither a
# complex_key nor a street+house_num, listings within this radius are
# treated as the same building.
#
# Measured, not assumed. Comparing the robust spread of log price ratios
# between same-room pairs: pairs sharing a complex_id score 0.2154, and
# pairs within 75 m score 0.2065 — the coordinate match is slightly
# TIGHTER than complex_id, because a large ЖК can span several separate
# towers while 75 m is one physical building. Restricted to the listings
# that actually need this fallback (no complex_key, no house number) the
# inverse-variance weight came out 1.37 at 40 m, 1.27 at 50 m, 1.09 at
# 75 m, 0.98 at 100 m and 0.89 at 150 m. 75 m is the widest radius still
# at least as good as a complex_id match, and it reaches 37% of the
# listings that currently get no building-level cohort at all.
#
# COORD_BUILDING_TRUST is capped at 1.0 rather than set to the measured
# 1.09: the measurement says a coordinate match is not WORSE than a
# tagged one, which is enough to use it without a discount, but giving a
# geometric guess more credit than an explicit building tag would be
# reading more into a 352-pair sample than it can carry.
RADIUS_COORD_BUILDING_KM = 0.075

# A house number and the same number with a block suffix ("17" and
# "17/3") are often the same building, and treating them as different
# addresses costs a real cohort: 1747 of 7044 baseline rows have no
# same-room building-mate at all, and 109 of them do have one under this
# rule.
#
# Merging on the base number ALONE is not safe, and the data says so
# loudly. Of 367 groups where a base number spans several written house
# numbers, 33% mix different complex_key values outright, half spread
# beyond 150 m, and the worst cases sit 6 to 25 km apart — "Uly Dala 27"
# alone covers five different complexes. So the merge is allowed only
# when the coordinates also agree. The guard is not a formality: it
# accepts 109 rows and rejects 104, i.e. roughly half of all base-number
# matches on this market are different buildings.
RADIUS_BASE_HOUSE_KM = 0.1
_HOUSE_BASE_NUM = re.compile(r"^(\d+)")

# Listings whose coordinates fall outside this box are not in Astana at
# all (11 such rows in the current baseline, including one at latitude
# 44.8 near Kyzylorda and one at longitude 78.1). Harmless while
# coordinates only fed the wide L4/L5 radii, but L3 and the building
# fallback now both key on geometry, so they are dropped up front.
ASTANA_LAT_RANGE = (50.5, 52.0)
ASTANA_LON_RANGE = (70.5, 72.5)
PRICE_RANGE_L5_PCT = 0.30

# There is no extreme-floor price correction. The ground/top-floor
# discount is a real effect in many markets, but it is NOT identifiable
# in this data:
#
#   citywide, controlling only for room count   1к -3.3%  2к -3.8%  3к -6.5%
#   after removing the building effect          +0.00%  (95% CI +0.00%..+0.71%)
#
# The apparent citywide discount is entirely composition: ground- and
# top-floor units are listed disproportionately in cheaper buildings, so
# comparing them across the city measures the buildings, not the floors.
# Within one building and room count the effect vanishes — 313 extreme
# and 3376 ordinary listings, demeaned per building, land on exactly the
# same median.
#
# Since the cohorts this scorer builds are already building- or
# neighbourhood-tight, applying a citywide-derived discount on top would
# subtract an effect that has already been controlled for, i.e. charge
# the flat twice for where it sits. The old estimate_floor_factor() did
# exactly that, with a 0.95 fallback that fired for essentially every
# extreme-floor listing (its local branch required 30 extreme AND 30
# ordinary listings inside one cohort, which never happens at cohort
# sizes of 10-40 — measured: 360 of 400 listings got a factor of exactly
# 1.0 and the rest got the fallback, never a local estimate).
#
# `_is_extreme_floor` is still computed and still reported, as a
# diagnostic. It just no longer moves the price.

# Verdict thresholds.
#
# v5: widened, because the previous set sat INSIDE the model's own error.
# Measured leave-one-out on krisha_astana_baseline.csv (7047 listings),
# the median absolute relative error of base_price_m2 is 10.6%
# (p75 = 19.7%, p90 = 30.7%). An "overpriced" verdict fired at -7%, i.e.
# below the median error: two thirds of those calls were the model's own
# noise. 17.6% of all rows sat within 2 percentage points of a threshold,
# so every sixth listing changed its label under any small perturbation.
#
# The anchor (HIGH_CONF) values are percentile cuts of the COMBINED
# |diff_pct| distribution (both signs pooled into absolute deviation —
# same quantity report_accuracy() calls "median absolute relative
# error"), not of either side's tail alone: FIND_THRESHOLD_HIGH_CONF
# sits at p70 of that pooled distribution, OVERPRICE_THRESHOLD_HIGH_CONF
# at p62. ("just below p75" in an earlier version of this comment was
# an imprecise description of the overprice anchor — reconstructed
# against the p50/p75/p90 numbers actually measured then, -0.15 lines
# up with p62, not p75. The asymmetry is real, not a typo: this
# market's diff_pct is skewed toward overpricing — see verdict_from_diff
# and the D_between discussion — so at the SAME probability of being
# real signal, the overprice side needs a smaller raw magnitude than
# the discount side.) MED_CONF and LOW_CONF are not separately
# re-derived: they are the anchor widened by a fixed step (+0.04 / +0.10
# for discount, -0.04 / -0.10 for overprice) reconstructed from the
# pre-v5 numbers, which fit this exact fixed-step pattern to three
# decimal places. Lower confidence needs a bigger raw deviation to trust
# the signal; the SIZE of that extra margin is a fixed step per
# confidence tier, not itself something to re-derive from percentiles.
#
# Re-derived on krisha_astana_baseline.csv (11 296 usable rows, full
# leave-one-out): median = 0.1056, p62 = 0.1383, p70 = 0.1669, p75 =
# 0.1900, p90 = 0.3103 — nearly the same shape as the pre-v5 measurement
# above, so the update below is a small correction, not a rebalancing.
#
# Re-derive whenever the prediction error changes — see
# CONFIDENCE_TIER_HIGH below and report_accuracy() at the bottom.
FIND_THRESHOLD_HIGH_CONF = 0.17
FIND_THRESHOLD_MED_CONF = 0.21
FIND_THRESHOLD_LOW_CONF = 0.27

OVERPRICE_THRESHOLD_HIGH_CONF = -0.14
OVERPRICE_THRESHOLD_MED_CONF = -0.18
OVERPRICE_THRESHOLD_LOW_CONF = -0.24

# Confidence tier boundaries, recalibrated for the v5 confidence scale.
# The old code compared against a hardcoded 0.75 / 0.55 while the
# confidence variable it read had p1 = 0.749 on this baseline — 99.4% of
# all rows landed in the top tier, which collapsed the three-tier ladder
# above into a single threshold and made SUSPICIOUS_DIFF_CONF_FLOOR fire
# for 3 rows out of 7045. With the rebuilt confidence
# (confidence_from_cohorts) the distribution is spread, so these
# boundaries now actually separate rows.
# Recalibrated again after within-cohort dispersion entered the
# confidence formula (it compresses the scale). On this baseline the
# combined confidence now runs p10 = 0.451, p50 = 0.599, p90 = 0.701, and
# these boundaries split the rows roughly 49 / 41 / 10.
#
# Both numbers are DISTRIBUTION-dependent: they are percentile cuts of a
# score, not physical constants. Re-derive them on any new market rather
# than carrying them over, otherwise the whole ladder silently collapses
# into one tier again — which is exactly what happened in v4.
# Percentile cuts of benchmark_confidence (NOT of the blend — see
# score_row).
#
# Re-derived on krisha_astana_baseline.csv (11 295 usable rows, full
# leave-one-out pass, not a sample). The previous values were cuts of a
# 7 047-row pool where the variable ran p10 = 0.257, p50 = 0.463,
# p90 = 0.604. On the current pool it runs p10 = 0.324, p50 = 0.528,
# p90 = 0.642 — the whole distribution shifted up, and the old cuts put
# 60% of rows in the top tier instead of the intended 24%, i.e. the
# ladder had started to collapse the way v4's did.
#
# The cuts are the p39 and p76 of the current distribution, which is the
# split the tiers were designed around (39 / 37 / 24). Verified: the new
# values reproduce 39 / 37 / 24 exactly.
#
# Re-derive on any new market. These are cuts of a distribution, not
# physical constants, and carrying them over unchecked is how v4 ended up
# with 99.4% of its rows in a single tier.
CONFIDENCE_TIER_HIGH = 0.60
CONFIDENCE_TIER_MED = 0.49

# An apparent discount/overprice this large, combined with weak
# confidence or a thin listing, is more often a sign that the cohort is
# wrong (tiny/noisy cohort, undeclared defect, contaminated radius
# match) than a sign that the number is real.
# Symmetric on purpose (review finding #4): a bad cohort can push
# diff_pct too far in EITHER direction, not just toward "too cheap to be
# true". Previously only the discount side had this guard.
SUSPICIOUS_DIFF_THRESHOLD = 0.35
SUSPICIOUS_DIFF_CONF_FLOOR = 0.35

# v5: thin description NO LONGER produces a ТРЕБУЕТ ПРОВЕРКИ verdict.
#
# Measured: of 504 ТРЕБУЕТ ПРОВЕРКИ verdicts on the baseline, 502 were
# triggered by this set and essentially none by low confidence. A field
# that was blank for 40% of the market made the verdict in practice mean
# "the seller left a field blank", while its text told the reader
# "probably a bad cohort or a hidden defect". Those are different claims
# and only one of them was true.
#
# The two are now separated: statistical doubt about the BENCHMARK stays
# in the verdict, and incompleteness of the LISTING is reported in its
# own output column, review_flags, where it belongs.
THIN_DESCRIPTION_WARNINGS = {"нет_фото", "мало_фото"}

# robust_z was computed but never consulted by verdict_from_diff. It's an
# independent-ish signal from diff_pct (z-scores against MAD rather than
# against the corrected/floor-adjusted price), so a large z at weak
# confidence is a second, cheap way to catch a likely bad cohort match
# even in cases where diff_pct alone doesn't cross SUSPICIOUS_DIFF_THRESHOLD.
SUSPICIOUS_ROBUST_Z = 3.5

# robust_z needs a spread to be measured against; see score_row.
MIN_ROWS_FOR_ROBUST_Z = 4.0

# How benchmark confidence and data confidence are blended into the
# single number the verdict thresholds are read against. Defined once:
# the same 0.72/0.28 split was previously written out separately in
# score_row and in price_quality_score, and the value passed to
# verdict_from_diff was this blend while the parameter was named
# `benchmark_confidence`, so the thresholds it compared against read as
# if they were about the benchmark alone.
BENCHMARK_CONF_SHARE = 0.72
DATA_CONF_SHARE = 1.0 - BENCHMARK_CONF_SHARE

# Scale of the logistic in value_from_diff(). Chosen so that a "strong"
# result on this market lands near 90/100:
#     VALUE_SCALE = P90(|diff_pct|) / ln(0.9/0.1) = P90(|diff_pct|) / 2.197
#
# Measured on a full scoring pass over krisha_astana_baseline.csv
# (1200-listing random sample, every other fix in place): the |diff_pct|
# distribution came out P50 = 0.127, P75 = 0.226, P90 = 0.343,
# P95 = 0.441, giving 0.343 / 2.197 = 0.156. Re-measured after L3 moved
# from "same street" to a 0.5 km radius: P50 = 0.124, P90 = 0.330. And
# again after the floor-area correction, which removed a real chunk of
# the unexplained spread: P50 = 0.109, P90 = 0.285, i.e. 0.130. This
# market's spread around
# any benchmark is genuinely wide, which is precisely why the previous
# formula — saturating at |diff_pct| = 0.10, below the MEDIAN deviation —
# collapsed the whole upper half of the distribution onto a single score.
#
# The derivation rule, which was implicit before and is worth stating:
# VALUE_SCALE = P90(|diff_pct|) / ln(9), so that a p90 deviation scores
# exactly 90 out of 100. (ln(9) = 2.197 — the same 2.197 the two
# measurements above divide by.)
#
# Re-derived on the current baseline over a full leave-one-out pass of
# all 11 295 usable rows: P50 = 0.106, P90 = 0.306, so 0.306 / 2.197.
#
# Re-derive after any change that moves the diff_pct distribution.
VALUE_SCALE = 0.139


def is_thin_description(warnings):
    return bool(THIN_DESCRIPTION_WARNINGS.intersection(warnings))


def review_flags(target, warnings, cohort_notes=None):
    """Reasons a human should look at this row, kept strictly separate
    from the price verdict.

    Everything here is a statement about the INPUT (an incomplete
    listing, a collapsed comparison set), never about the price being
    good or bad. Mixing the two is what made the old ТРЕБУЕТ ПРОВЕРКИ
    verdict unreadable: 502 of its 504 firings meant "a description field
    is blank", not "this price looks wrong".
    """
    flags = []
    if is_thin_description(warnings):
        flags.append("описание_неполное")
    if to_bool(target.get("requires_manual_review")):
        flags.append("red_flag")
    if "рассрочка" in warnings:
        flags.append("рассрочка")
    if "нет_координат" in warnings:
        flags.append("нет_координат")
    if "site_price_m2_unverified" in warnings:
        flags.append("price_m2_из_недоверенного_поля")
    for note in cohort_notes or ():
        flags.append(note)
    return flags

OUTPUT_EXTRA_FIELDNAMES = [
    "status",
    "verdict",
    "verdict_reason",
    "price_segment",
    "building_class",
    "building_class_score",
    "building_class_n",
    "building_class_basis",
    # cohort_level now means "the level the cascade STOPPED at", and
    # cohort_size the TOTAL number of distinct listings pooled across all
    # collected cohorts — not the size of one winning level (ТЗ п.15/16).
    # v5: cohort_level is the level that actually PRODUCED the number
    # (the one carrying the most weight). It used to be the level the
    # cascade stopped at, which is a different thing: on this baseline
    # the cascade stops at L3 or wider for every row, while the median
    # share of L1/2 in the final weight is 0.85. Any downstream filter of
    # the form "keep only building-level results" was reading the wrong
    # column. The stop level is still reported, under its own name.
    "cohort_level",
    "cohort_stop_level",
    "cohort_size",
    "cohort_levels_used",
    "cohort_dispersion",
    "cohort_disagreement",
    "prior_price_m2",
    # Per-cohort audit trail (ТЗ п.16): why this apartment got this
    # number. Human-readable summary plus one flat column trio per level
    # so the CSV can be filtered/pivoted without parsing strings.
    "cohort_breakdown",
    # Each level reports BOTH its raw median and the credibility-adjusted
    # median that actually entered the blend. Only the raw one was
    # published before, so multiplying the printed medians by the printed
    # weights did not reproduce base_price_m2 — it missed by more than 2%
    # on 36% of rows, which defeats the point of an audit trail.
    "l12_n",
    "l12_median",
    "l12_adjusted_median",
    "l12_credibility",
    "l12_weight",
    "l12_direct_n",
    "l12_rescaled_n",
    "l12_purity",
    "l3_n",
    "l3_median",
    "l3_adjusted_median",
    "l3_credibility",
    "l3_weight",
    "l4_n",
    "l4_median",
    "l4_adjusted_median",
    "l4_credibility",
    "l4_weight",
    "l5_n",
    "l5_median",
    "l5_adjusted_median",
    "l5_credibility",
    "l5_weight",
    "l6_n",
    "l6_median",
    "l6_adjusted_median",
    "l6_credibility",
    "l6_weight",
    "confidence_weight",
    "benchmark_confidence",
    "is_extreme_floor",
    "base_price_m2",
    "diff_pct",
    "robust_z",
    "value_score",
    "quality_evidence_score",
    "price_quality_score",
    "price_quality_label",
    "data_confidence",
    "seller_class",
    "seller_confidence",
    "seller_reason",
    "data_warnings",
    "review_flags",
    "cohort_notes",
]


# ============================== HELPERS ==============================


def to_float(x):
    try:
        if x is None or str(x).strip() == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def to_bool(x):
    return str(x).strip().lower() in ("true", "1", "yes", "y")


def parse_jsonish(value, default):
    """Small tolerant parser for CSV fields containing JSON-like lists/dicts."""
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    text = str(value).strip()
    if not text:
        return default

    import json
    try:
        return json.loads(text)
    except Exception:
        # Some historical CSVs contain Python-ish single-quoted lists.
        try:
            import ast
            return ast.literal_eval(text)
        except Exception:
            return default


_STREET_TRANSLIT = str.maketrans({
    "ұ": "u", "ү": "u", "қ": "k", "ғ": "g", "ң": "n", "ө": "o", "һ": "h",
    "і": "i", "ә": "a", "ş": "s", "ı": "i", "ğ": "g", "ö": "o", "ü": "u",
    "ç": "c",
})
# The trailing \s+ is load-bearing: without a word boundary the short
# alternatives swallow the start of ordinary names — "pr" turned
# "Premium" into "emium", "ul" turned "Uly dala" into "y dala" and
# "st" turned "Stolichnyy" into "olichnyy". A prefix only counts when it
# stands as its own word.
_STREET_PREFIX = re.compile(
    r"^(prospekt|prospect|prosp|pr|ulitsa|ulica|ul|mikrorayon|mkr|zhk|per"
    r"|pereulok|avenue|ave|st|street)\s+",
    re.IGNORECASE,
)
_STREET_TRAILING_NUM = re.compile(r"\s+\d+([/\-]\d+)?[a-z]?$", re.IGNORECASE)


def normalize_street(value, strip_trailing_number=True):
    """Normalized street name for matching.

    Review finding #6: raw `street` strings were compared with no
    normalization at all, so scraping noise silently split one street
    into several and quietly pushed cohorts to a wider, noisier level.
    Case and whitespace folding was the first fix; this is the second,
    because the source turned out to be far messier than that. One
    avenue appeared under 18 spellings on the baseline — Uly_Dala,
    Uly_dala, Ұly_dala, Prosp__Uly_Dala, prospekt_Uly_dala, Uly-Dala,
    UlyDala, Uly_dala_65 and so on.

    So this also folds separators to spaces, transliterates Kazakh and
    Turkish letters to their Latin counterparts, strips leading street-type
    prefixes (prospekt/ulitsa/mkr/...), and drops a trailing house number
    that was concatenated into the street field. Result on the baseline:
    1040 distinct "streets" collapse to 819, and single-listing streets
    drop from 558 to 383.

    The trailing-number strip is guarded: it only applies when at least
    four characters and something non-numeric survive. Astana really has
    streets called Е-10, Е-117 and Е-915, and without the guard they all
    collapsed into the single "street" `e` — 356 listings from opposite
    ends of the city merged into one building-and-street group. Every
    remaining merge was checked geographically; the only groups still
    spanning more than 3 km are two genuinely long highways.

    Also used for house numbers and complex names — with
    strip_trailing_number=False, because there the number IS the
    identity. The old code claimed "the prefix/number rules simply do not
    fire" for those fields; they fire. `ЖК Арман 2` and `ЖК Арман 3`
    normalized to the same key, which merges two different buildings into
    one L1/2 cohort. Latent on this baseline (every complex row carries a
    complex_id, so the name branch never runs) and destructive the moment
    a source leaves complex_id blank, which is exactly the case
    _complex_key was tagged to protect against.

    v5 additions, measured on krisha_astana_baseline.csv:
      * hyphens fold to spaces. They did not before, although the
        docstring listed `Uly-Dala` among the spellings it collapses.
      * CamelCase splits, because the source concatenates
        (`UlyDala`, `ProspektUlyDala`).
    Together these take the 16 raw spellings of one avenue from 6
    distinct keys down to 3.
    """
    if value is None:
        return None
    text = str(value).strip()
    # Split CamelCase before folding case, or the information is lost.
    text = re.sub(r"(?<=[a-zа-яё])(?=[A-ZА-ЯЁ])", " ", text)
    text = text.lower().translate(_STREET_TRANSLIT)
    text = re.sub(r"[_\.,\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    for _ in range(2):
        shortened = _STREET_PREFIX.sub("", text).strip()
        if shortened == text:
            break
        text = shortened

    if strip_trailing_number:
        stripped = _STREET_TRAILING_NUM.sub("", text).strip()
        if len(stripped) >= 4 and not stripped.isdigit():
            text = stripped

    return text or None


def weighted_median(pairs):
    """Median of (value, weight) pairs — the value at which cumulative
    weight first reaches half of the total.

    Needed in three places that all have the same problem: a plain median
    treats every listing as one vote, but our listings are not equal
    votes. Inside a cohort a rescaled cross-room comparable is worth less
    than a direct one; across cohorts an L6 listing is worth a fraction
    of an L1/2 one. Using a plain median there silently hands the centre
    of the estimate to whichever group happens to be most numerous, which
    is usually the least trustworthy one.
    """
    pairs = [(v, w) for v, w in pairs if v is not None and w and w > 0]
    if not pairs:
        return None
    pairs.sort(key=lambda vw: vw[0])
    total = sum(w for _, w in pairs)
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= total / 2.0:
            return value
    return pairs[-1][0]


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def load_rows(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def safe_rooms(row):
    raw = row.get("rooms")
    if raw is None:
        return None
    s = str(raw).strip().lower()
    # Preserve "студия" as a separate category if it exists.
    if s in ("studio", "студия"):
        return "studio"
    try:
        return str(int(float(s)))
    except Exception:
        return s or None


def infer_price_m2(row):
    """price/m², computed from the listing's own price and square_m2
    wherever possible, falling back to the site's priceM2 field only
    when that computation is unavailable.

    This priority is inverted from earlier versions, which trusted
    advert_extra["priceM2"] first. Two independent pieces of evidence
    say that field cannot be trusted for this pipeline's data:

    1. This parser scrapes CATEGORY = "arenda" (rent), but the module's
       own header docstring says the price/m² methodology "сформулирована
       под ПОКУПКУ (price/m2 продажи), а не аренду" — i.e. the concept
       this field is meant to carry (purchase price per m²) does not
       apply to what is being scraped.
    2. Confirms it in the data: price_m2_text — the label the site
       attaches to this exact field — reads "за месяц" (a rent-PERIOD
       label) in 100% of krisha_astana_baseline.csv (11 324/11 324
       rows), never anything resembling a "тг/м²" price-per-area label.
       Whatever advert_extra["priceM2"]/["priceM2Text"] actually holds
       for a rental listing, it is not what its name claims.

    Measured effect of the old priority on krisha_astana_baseline.csv:
    10.0% of rows (1129/11324) disagreed with price/square_m2 by more
    than 2%, 43 rows by more than 50%, one by a factor of 10.9 — and
    _price_m2 feeds every downstream median, quartile and prior in this
    file, so those rows were distorting the benchmark for everyone
    compared against them, not just themselves.

    price/square_m2 is self-consistent by construction (both terms come
    from the same row) and is bounded downstream by mark_price_outliers'
    PRICE_M2_ABS_MIN/MAX and SQUARE_ABS_MIN/MAX checks. The site field is
    used only when price or square_m2 is missing, so a listing is never
    thrown away over this; see the "site_price_m2_unverified" warning in
    data_warnings() for rows that had to take that fallback.
    """
    price = to_float(row.get("price"))
    square = to_float(row.get("square_m2"))
    if price and price > 0 and square and square > 0:
        return price / square

    pm2 = to_float(row.get("price_m2"))
    if pm2 is not None and pm2 > 0:
        return pm2

    return None


def _price_m2_is_unverified(row):
    """True when _price_m2 had to fall back to the site's untrusted
    priceM2 field because price/square_m2 wasn't computable — see
    infer_price_m2()."""
    price = to_float(row.get("price"))
    square = to_float(row.get("square_m2"))
    computable = bool(price and price > 0 and square and square > 0)
    return (not computable) and row.get("_price_m2") is not None


def seller_class(row):
    """
    Сначала используем структурированный seller_type, затем owner_name.
    По текущей логике источника 'Хозяин' — наиболее сильный маркер владельца.
    Конкретное имя/компания без этого маркера считаем агентом/риелтором,
    но НЕ штрафуем цену за это.
    """
    owner = (row.get("owner_name") or "").strip()
    seller = (row.get("seller_type") or "").strip().lower()
    identity = to_bool(row.get("is_identity_confirmed"))

    if owner == "Хозяин":
        return "owner", 1.0, "owner_name=Хозяин"
    if any(x in seller for x in ("owner", "хозя", "собствен")):
        return "owner", 0.85 if not identity else 1.0, "seller_type указывает на владельца"
    if any(x in seller for x in ("agent", "риел", "агент", "company", "компан")):
        return "agent", 0.90, "seller_type указывает на агентство/риелтора"
    if owner:
        return "agent", 0.80, "указано конкретное имя вместо 'Хозяин'"
    return "unknown", 0.35, "маркер владельца/риелтора не определён"


def enrich(row):
    row = dict(row)
    row["_price_m2"] = infer_price_m2(row)
    row["_rooms"] = safe_rooms(row)
    row["_lat"] = to_float(row.get("latitude"))
    row["_lon"] = to_float(row.get("longitude"))
    # Typed complex key. This used to be `complex_id or complex_name`,
    # i.e. sometimes a numeric id and sometimes a free-text name in the
    # same slot. On the current baseline every row has an id so nothing
    # collides, but the moment a source leaves complex_id blank, two
    # unrelated buildings sharing a common ЖК name ("Астана",
    # "Сарыарка") merge into one, and L1/2 quietly fills with listings
    # from a different building. Tagging the key with its source makes
    # that impossible: an id can only ever match an id.
    complex_id = (row.get("complex_id") or "").strip()
    # strip_trailing_number=False: "Арман 2" and "Арман 3" are different
    # buildings, and this key decides L1/2 membership.
    complex_name = normalize_street(
        row.get("complex_name"), strip_trailing_number=False
    )
    if complex_id:
        row["_complex_key"] = ("cid", complex_id)
    elif complex_name:
        row["_complex_key"] = ("cname", complex_name)
    else:
        row["_complex_key"] = None
    row["_street_norm"] = normalize_street(row.get("street"))
    row["_house_num_norm"] = normalize_street(
        row.get("house_num"), strip_trailing_number=False
    )

    # Coordinates outside Astana are data errors, not distant listings.
    # Both the L3 radius and the coordinate-based building fallback key
    # on geometry now, so a stray point is neutralised at the source
    # rather than silently failing every distance test downstream.
    row["_geo_ok"] = bool(
        row["_lat"] is not None and row["_lon"] is not None
        and ASTANA_LAT_RANGE[0] < row["_lat"] < ASTANA_LAT_RANGE[1]
        and ASTANA_LON_RANGE[0] < row["_lon"] < ASTANA_LON_RANGE[1]
    )
    if not row["_geo_ok"]:
        row["_lat"] = None
        row["_lon"] = None

    floor = to_float(row.get("floor"))
    floor_total = to_float(row.get("floor_total"))
    row["_floor"] = floor
    row["_floor_total"] = floor_total
    # Extreme = ground floor or top floor, both of which trade at a
    # discount. Two bugs fixed here:
    #
    # 1. The old condition required floor_total, so 632 baseline rows
    #    (9%) that state a floor but no building height were silently
    #    treated as ordinary — including every ground-floor flat among
    #    them, which is detectable from `floor` alone and needs no
    #    building height at all.
    # 2. floor == floor_total == 1 was flagged as extreme. A
    #    single-storey building has neither a ground-floor nor a
    #    top-floor discount in any market sense; it is just a house.
    if floor is None:
        row["_is_extreme_floor"] = False
    elif floor_total is None:
        row["_is_extreme_floor"] = bool(floor == 1)
    elif floor_total < floor or floor_total <= 1:
        # Inconsistent data, or a one-storey building: no penalty.
        row["_is_extreme_floor"] = False
    else:
        row["_is_extreme_floor"] = bool(floor == 1 or floor == floor_total)

    # Only baseline rows are allowed to be excluded from the reference pool.
    row["_excluded_baseline"] = (
        to_bool(row.get("requires_manual_review"))
        or to_bool(row.get("is_installment_segment"))
        or not row.get("id")
        or row["_price_m2"] is None
        or row["_rooms"] is None
    )
    sc, sc_conf, sc_reason = seller_class(row)
    row["_seller_class"] = sc
    row["_seller_confidence"] = sc_conf
    row["_seller_reason"] = sc_reason
    return row


def _excluded_baseline_reason(row):
    """Which of the five _excluded_baseline conditions actually fired.

    score_row used to guess this from a two-way ternary
    (requires_manual_review -> "excluded_manual_review", else ALWAYS
    "excluded_installment") even though _excluded_baseline is set by
    FIVE independent conditions (see enrich()). is_installment_segment
    is a stage1_clean.py stub that is always False, so
    "excluded_installment" could never actually be the true reason for
    any row — whatever the real cause was, the label lied about it.
    Kept in sync with enrich() by construction: same five checks, same
    order, first match wins.
    """
    if to_bool(row.get("requires_manual_review")):
        return "excluded_manual_review"
    if to_bool(row.get("is_installment_segment")):
        return "excluded_installment"
    if not row.get("id"):
        return "excluded_missing_id"
    if row.get("_price_m2") is None:
        return "excluded_missing_price_m2"
    if row.get("_rooms") is None:
        return "excluded_missing_rooms"
    return "excluded_unknown"  # should be unreachable if _excluded_baseline is True


def citywide_median_by_rooms(pool):
    by_rooms = {}
    for r in pool:
        if r["_price_m2"] is not None and r["_rooms"] is not None:
            by_rooms.setdefault(r["_rooms"], []).append(r["_price_m2"])
    return {
        rooms: statistics.median(values)
        for rooms, values in by_rooms.items()
        if values
    }


def prior_medians_by_rooms_class(pool, score_index):
    """Median price/m² for each (room count × building class) cell.

    This is the prior that cohort medians are shrunk toward. The old
    prior was citywide_median_by_rooms() — one number per room count,
    averaged over эконом, комфорт and бизнес together — which meant every
    thin cohort was dragged toward the city average regardless of what
    kind of building it was in. On a two-room cohort of three listings in
    a business-class building that is a systematic push DOWNWARD of
    ~10%, and a symmetric push upward for эконом; against verdict
    thresholds of -7%..-15% that alone can manufacture a ПЕРЕОЦЕНЕНА for
    a fairly priced apartment in an expensive building, and a НАХОДКА for
    a fairly priced one in a cheap building.

    This matters more, not less, given that the whole model is built on
    location and building class as the primary price drivers: a prior
    that is blind to building class contradicts the model's own premise.

    Cells thinner than MIN_FOR_PRIOR_CELL are omitted; callers fall back
    to the citywide room median for those (see cohort_prior()).

    Non-circular: every row's class comes from score_index, i.e. from
    OTHER listings in its building, and the scored target is never part
    of the pool this is computed from.
    """
    cells = defaultdict(list)
    for r in pool:
        if r.get("_price_m2") is None or r.get("_rooms") is None:
            continue
        score, _ = resolve_class_score(r.get("id"), score_index)
        cls = _label_from_class_score(score)
        if cls is None:
            continue
        cells[(r["_rooms"], cls)].append(r["_price_m2"])

    out = {}
    for key, values in cells.items():
        if len(values) >= MIN_FOR_PRIOR_CELL:
            out[key] = statistics.median(values)
    return out


def cohort_prior(rooms, building_class_label, class_priors, citywide_median):
    """The value a thin cohort's median is shrunk toward: the median of
    its own (room count × building class) cell when that cell has enough
    data, otherwise the citywide median for the room count.

    The fallback keeps behaviour defined when the building could not be
    classified at all (ТЗ п.9) or when a class/room combination is too
    rare to estimate — in both cases the old, blunter prior is still
    better than none.
    """
    if building_class_label is not None and class_priors:
        cell = class_priors.get((rooms, building_class_label))
        if cell:
            return cell
    return citywide_median.get(rooms)


class SpatialIndex:
    """Grid index over listing coordinates, so a radius query touches
    only nearby cells instead of the whole pool.

    Purely a performance structure: it returns the same set of listings a
    linear scan would, because every candidate that survives the cell
    lookup is still checked with the exact haversine distance. The cell
    size is a plain latitude/longitude step, and the number of cells
    searched is derived from the radius, so a larger radius simply reads
    more cells.

    Longitude degrees are shorter than latitude ones away from the
    equator, so the longitude span is widened by 1/cos(lat) — at Astana's
    latitude that is about 1.6x. Getting this backwards would silently
    drop listings due east and west of the target, which is why the cell
    span is computed rather than assumed square.

    Cohort building scans the pool several times per listing, which made
    a full run quadratic: 7047 listings took ~86 s, and a 50 000-listing
    pool would take roughly an hour.
    """

    CELL_DEG = 0.01  # ~1.1 km in latitude

    def __init__(self, rows):
        self._cells = defaultdict(list)
        for r in rows:
            if r.get("_lat") is not None and r.get("_lon") is not None:
                self._cells[self._cell_of(r["_lat"], r["_lon"])].append(r)

    @classmethod
    def _cell_of(cls, lat, lon):
        return (int(lat / cls.CELL_DEG), int(lon / cls.CELL_DEG))

    def within(self, lat, lon, radius_km):
        """Every indexed row within `radius_km` of the point."""
        if lat is None or lon is None:
            return []

        lat_span = radius_km / 111.0
        cos_lat = math.cos(math.radians(lat))
        lon_span = radius_km / (111.0 * cos_lat) if abs(cos_lat) > 1e-6 else 180.0
        lat_cells = int(lat_span / self.CELL_DEG) + 1
        lon_cells = int(lon_span / self.CELL_DEG) + 1

        ci, cj = self._cell_of(lat, lon)
        found = []
        for i in range(ci - lat_cells, ci + lat_cells + 1):
            for j in range(cj - lon_cells, cj + lon_cells + 1):
                for r in self._cells.get((i, j), ()):
                    if haversine_km(lat, lon, r["_lat"], r["_lon"]) <= radius_km:
                        found.append(r)
        return found


class BuildingIndex:
    """Lookup from a building's identifying keys to its listings.

    Replaces the pool-wide scan that building_listings_for() and the
    cohort builder's in_target_building() each performed for every scored
    listing. Profiling a 150-listing run put 2.1 million same_building()
    calls at the top — that pair of scans WAS the quadratic term.

    Every row is registered under BOTH of its keys when it has both, and
    a lookup unions the target's keys, so the result is identical to
    same_building()'s tier 1 and 2 rather than merely similar. Indexing
    by one preferred key would quietly change matching: a target tagged
    with both a complex and an address would stop seeing building-mates
    that carry only the address.

    Tier 3 (the coordinate fallback for listings with neither key) is
    served by the spatial index, exactly as same_building() specifies.
    """

    def __init__(self, rows):
        self._by_key = defaultdict(list)
        self._by_base = defaultdict(list)
        for r in rows:
            for key in self._keys_of(r):
                self._by_key[key].append(r)
            base = self._base_key(r)
            if base is not None:
                self._by_base[base].append(r)

    @staticmethod
    def _keys_of(r):
        keys = []
        if r.get("_complex_key"):
            keys.append(("complex", r["_complex_key"]))
        if r.get("_street_norm") and r.get("_house_num_norm"):
            keys.append(("addr", r["_street_norm"], r["_house_num_norm"]))
        return keys

    @staticmethod
    def _base_key(r):
        """Street plus the leading digits of the house number.

        Deliberately NOT part of _keys_of(): this key is loose enough to
        merge separate buildings and is only ever used as a guarded
        fallback in members(), never as a primary identity.
        """
        street = r.get("_street_norm")
        house = r.get("_house_num_norm")
        if not street or not house:
            return None
        m = _HOUSE_BASE_NUM.match(house)
        return ("base", street, m.group(1)) if m else None

    def members(self, target, spatial_index=None):
        """Other listings in the target's building."""
        keys = self._keys_of(target)
        if keys:
            seen = set()
            out = []
            for key in keys:
                for r in self._by_key.get(key, ()):
                    if id(r) in seen or same_target_id(r, target):
                        continue
                    seen.add(id(r))
                    out.append(r)
            if out:
                return out
            # Nothing at the literal address: try the base house number,
            # but only for rows whose coordinates also agree. See
            # RADIUS_BASE_HOUSE_KM for why the coordinate guard is not
            # optional here.
            return self._base_members(target)

        if (
            spatial_index is not None
            and target.get("_lat") is not None
            and target.get("_lon") is not None
        ):
            return [
                r for r in spatial_index.within(
                    target["_lat"], target["_lon"], RADIUS_COORD_BUILDING_KM
                )
                if not same_target_id(r, target)
            ]
        return []

    def _base_members(self, target):
        base = self._base_key(target)
        if base is None:
            return []
        lat, lon = target.get("_lat"), target.get("_lon")
        if lat is None or lon is None:
            return []
        out = []
        for r in self._by_base.get(base, ()):
            if same_target_id(r, target):
                continue
            if r.get("_lat") is None or r.get("_lon") is None:
                continue
            if haversine_km(lat, lon, r["_lat"], r["_lon"]) <= RADIUS_BASE_HOUSE_KM:
                out.append(r)
        return out


def same_target_id(r, target):
    return str(r.get("id")) == str(target.get("id")) and r.get("id") not in (None, "")


def price_segment_boundaries(pool):
    values = sorted(r["_price_m2"] for r in pool if r["_price_m2"] is not None)
    if len(values) < 8:
        return None, None
    qs = statistics.quantiles(values, n=4, method="inclusive")
    return qs[0], qs[2]


def room_segment_boundaries(pool):
    """Citywide q25/q75 of price_m2 computed SEPARATELY for each room
    count (ТЗ п.6/п.10).

    A building's 1-room units must be judged against the city's 1-room
    market, its 2-room units against the city's 2-room market, and so on.
    Comparing one pooled building median against one pooled citywide
    median — what the previous building_class() did — silently mislabels
    any building whose room mix differs from the city's, because
    price/m² is systematically higher for small units.

    Room counts with too few citywide listings to support quartiles get
    no entry; callers fall back to the global boundaries for those.
    """
    by_rooms = defaultdict(list)
    for r in pool:
        if r.get("_price_m2") is not None and r.get("_rooms") is not None:
            by_rooms[r["_rooms"]].append(r["_price_m2"])

    result = {}
    for rooms, values in by_rooms.items():
        if len(values) < 8:
            continue
        qs = statistics.quantiles(sorted(values), n=4, method="inclusive")
        result[rooms] = (qs[0], qs[2])
    return result


def classify_segment(pm2, q25, q75):
    if pm2 is None:
        return None
    if q25 is None or q75 is None:
        return "комфорт"
    if pm2 <= q25:
        return "эконом"
    if pm2 >= q75:
        return "бизнес"
    return "комфорт"


def target_price_segment(target, pool, q25=None, q75=None):
    """Target's OWN price bucket — cheap/mid/premium by citywide
    price/m² quartile, independent of building_class().

    Historically this doubled as building_class()'s last-resort fallback
    when it had no data, which was circular (see building_class's
    docstring for why that was wrong). v5 removed that fallback —
    score_row now leaves target_class as None instead — so this function
    is no longer a fallback for anything: it is called unconditionally
    from score_row to populate the diagnostic `price_segment` output
    column, and nothing else in this module reads its return value."""
    pm2 = target.get("_price_m2")
    if pm2 is None:
        return "неизвестен"
    # v4 accepted q25/q75 and then immediately overwrote them by
    # re-deriving the quartiles from the whole pool — for every scored
    # row, an O(n log n) sort of 7000 values to produce a diagnostic
    # column nothing consumes. The caller has already computed them once.
    if q25 is None or q75 is None:
        q25, q75 = price_segment_boundaries(pool)
    result = classify_segment(pm2, q25, q75)
    return result if result is not None else "неизвестен"


def same_building(target, r):
    """Is `r` in the same building as `target`? Three tiers, tried in
    order of how directly they assert it:

      1. complex_key  — an explicit ЖК tag on both sides;
      2. street + house_num — the literal same address;
      3. coordinates within RADIUS_COORD_BUILDING_KM — a geometric
         fallback used ONLY when the target has neither of the above.

    Tier 3 exists because roughly a fifth of listings carry no complex
    tag and no house number (the current baseline has street names but
    frequently nothing more precise), and for those the building-level
    cohort was empty by construction: no L1/2, no building class, and
    therefore not even a class filter on the wider levels. Everything
    downstream then rested on a single loose cohort.

    It is a fallback rather than an additional matcher on purpose: when
    the target DOES carry a complex tag or a house number, that
    statement is authoritative and a geometric guess must not widen it
    to include the building next door.

    Returns (matched, via_coordinates) so callers can tell an explicit
    match from an inferred one.
    """
    complex_key = target.get("_complex_key")
    street = target.get("_street_norm")
    house = target.get("_house_num_norm")

    if complex_key and r.get("_complex_key") == complex_key:
        return True, False
    if (
        street and house
        and r.get("_street_norm") == street
        and r.get("_house_num_norm") == house
    ):
        return True, False

    if complex_key or (street and house):
        return False, False

    if (
        target.get("_lat") is not None and r.get("_lat") is not None
        and haversine_km(
            target["_lat"], target["_lon"], r["_lat"], r["_lon"]
        ) <= RADIUS_COORD_BUILDING_KM
    ):
        return True, True
    return False, False


def building_listings_for(target, pool, building_index=None, spatial_index=None):
    """All OTHER listings belonging to the same building/ЖК as target,
    across every room count — see same_building() for the three matching
    tiers.

    Uses the prebuilt indexes when supplied and falls back to a linear
    scan otherwise, so the function stays correct standalone."""
    if building_index is not None:
        return building_index.members(target, spatial_index)
    matched = []
    for r in pool:
        if same_target_id(r, target):
            continue
        ok, _ = same_building(target, r)
        if ok:
            matched.append(r)
    return matched


def _class_score_from_price(pm2, rooms, room_bounds, q25, q75):
    """Continuous class position of one price on the citywide scale FOR
    ITS OWN ROOM COUNT. 0.5 = эконом/комфорт boundary (citywide q25 of
    that room count), 1.5 = комфорт/бизнес boundary (citywide q75).

    Logarithmic in price and deliberately UNBOUNDED. Prices are spread
    multiplicatively, so a log scale keeps one score unit meaning the
    same relative price step everywhere instead of compressing the cheap
    end and stretching the expensive one. Removing the old [0, 2] clip
    matters most at the top: everything above the citywide q75 used to
    collapse onto the single value 2.00, which is why an elite building
    at 10 655 ₸/m² and an ordinary above-average one at 6 667 were
    indistinguishable to the class filter.
    """
    if pm2 is None or pm2 <= 0:
        return None
    lo, hi = room_bounds.get(rooms, (q25, q75))
    if lo is None or hi is None or hi <= lo:
        lo, hi = q25, q75
    if lo is None or hi is None or hi <= lo or lo <= 0:
        return None
    return 0.5 + (math.log(pm2) - math.log(lo)) / (math.log(hi) - math.log(lo))


def _label_from_class_score(score):
    """Human-readable bucket. Used for OUTPUT and for selecting the
    (rooms x class) prior cell — never for deciding which candidates a
    target may be compared against, which now works on the continuous
    score directly (see class_scores_match())."""
    if score is None:
        return None
    if score < CLASS_SCORE_ECONOM_MAX:
        return "эконом"
    if score < CLASS_SCORE_COMFORT_MAX:
        return "комфорт"
    return "бизнес"


def class_score_se(n):
    """Uncertainty of a class score built from `n` listings."""
    if not n or n <= 0:
        return None
    return CLASS_SCORE_SE_1 / math.sqrt(n)


def class_match_tolerance(n_target, n_candidate):
    """How far apart two class scores may be and still count as the same
    class: the base threshold, widened by the uncertainty of both scores.

    This replaces the old "3 listings or no class at all" cutoff. A
    building known from two listings is not silently promoted to a
    confident label, nor is it discarded — its score carries a larger
    error bar, and the tolerance grows to match, so buildings we cannot
    reliably tell apart are not forced into different peer groups by a
    difference that is pure noise.
    """
    return (
        CLASS_MATCH_THRESHOLD
        + (class_score_se(n_target) or 0.0)
        + (class_score_se(n_candidate) or 0.0)
    )


def class_scores_match(target_score, target_n, cand_score, cand_n):
    if target_score is None or cand_score is None:
        return False
    return abs(target_score - cand_score) <= class_match_tolerance(
        target_n, cand_n
    )


def building_class_from_listings(listings, room_bounds, q25, q75):
    """Building/ЖК class from ALL of the building's listings, room count
    by room count (ТЗ п.5, п.6, п.10).

    Procedure, exactly as specified:
      1. group the building's listings by room count;
      2. take each group's median price/m²;
      3. compare THAT group against the citywide market FOR THE SAME
         room count (never against one pooled citywide median);
      4. average the resulting class positions, weighting each room count
         by its share of the building's classifiable listings, so a
         shortage of data in one room count is compensated by the others
         rather than blocking or skewing the answer.

    Returns (score, n, basis): the continuous class position, the number
    of listings it rests on (which sets its uncertainty), and a short
    audit string like "1к:2×0.40|2к:7×1.62". Returns (None, 0, None) when
    the building has fewer than MIN_FOR_BUILDING_SCORE classifiable
    listings — still a real answer, and per ТЗ п.9 it must NOT be papered
    over with the target's own price.
    """
    by_rooms = defaultdict(list)
    for r in listings:
        if r.get("_price_m2") is not None and r.get("_rooms") is not None:
            by_rooms[r["_rooms"]].append(r["_price_m2"])

    total = sum(len(v) for v in by_rooms.values())
    if total < MIN_FOR_BUILDING_SCORE:
        return None, 0, None

    num = 0.0
    den = 0.0
    parts = []
    for rooms, values in sorted(by_rooms.items(), key=lambda kv: str(kv[0])):
        if len(values) < MIN_PER_ROOMS_FOR_CLASS:
            continue
        score = _class_score_from_price(
            statistics.median(values), rooms, room_bounds, q25, q75
        )
        if score is None:
            continue
        # Weight = this room count's share of the building's classifiable
        # listings. 7 two-room listings therefore outvote 2 one-room ones,
        # which is precisely the case ТЗ п.6 spells out.
        weight = float(len(values))
        num += score * weight
        den += weight
        parts.append(f"{rooms}к:{len(values)}×{score:.2f}")

    if den <= 0:
        return None, 0, None
    return num / den, int(den), "|".join(parts)


def building_class(
    target, pool, q25, q75, room_bounds=None,
    building_index=None, spatial_index=None,
):
    """Class SCORE of the TARGET's building, computed from OTHER listings
    in that building across every room count.

    Review finding #3 (preserved): the old target_price_segment()
    classified a listing using its OWN price, then only compared it
    against others in that same self-assigned bucket — an underpriced
    target would label itself "эконом" and be compared only against cheap
    comparables, masking exactly the deviation Stage 3 exists to catch.
    Deriving the class from OTHER units removes the target's own price
    from its own classification entirely.

    ТЗ п.5/п.6/п.10 additionally require that the building be judged room
    count by room count against the matching citywide market.

    Returns (score, n, basis).
    """
    room_bounds = room_bounds or {}
    listings = building_listings_for(target, pool, building_index, spatial_index)
    return building_class_from_listings(listings, room_bounds, q25, q75)


def _building_key(r):
    """Canonical building-grouping key: complex_key if present, else
    street+house_num. Unlike building_listings_for()'s matching (which
    anchors on ONE row's own complex_key presence to decide which fallback
    branch to use), this is a plain per-row key — every row is grouped by
    its own tag, independent of which row is being looked up. Used only for
    building_scores_index() below, where we need every row's group
    membership up front. The two approaches agree except in the rare case
    of inconsistent tagging within one physical building (some listings
    carry complex_key, others don't) — an existing data-quality edge case,
    not something this indexing introduces."""
    if r.get("_complex_key"):
        return ("complex", r["_complex_key"])
    if r.get("_street_norm") and r.get("_house_num_norm"):
        return ("addr", r["_street_norm"], r["_house_num_norm"])
    return None


def building_scores_index(pool, q25, q75, room_bounds=None):
    """Precompute every row's BUILDING class score in one pass, instead
    of paying building_listings_for()'s O(pool) scan per row.

    Candidates are scored with exactly the same per-room-count weighted
    procedure as the target (ТЗ п.6/п.10), so the two sides of every L3+
    class comparison are measured on the same ruler. Each member is
    excluded from its own building's aggregate, so a candidate can never
    help decide the class that then admits it.

    Returns {row_id: (score, n)}. A row missing from the mapping means
    its building had fewer than MIN_FOR_BUILDING_SCORE other listings —
    and, unlike before, that is the END of the matter: such a candidate
    simply does not take part in class filtering.

    It used to fall back to its own price bucket. Measured against the
    rows where a real building class WAS available, that fallback
    disagreed with the truth 37.5% of the time — it would have said
    "бизнес" for 690 buildings that are actually комфорт, "эконом" for
    495 more, and so on. A coin flip that decides peer-group membership
    is worse than an abstention: a cheap flat in an expensive building
    labelled itself эконом and dropped out of exactly the cohort that
    described it best, while an overpriced flat in a cheap building
    labelled itself бизнес and contaminated the cohort it joined.
    """
    room_bounds = room_bounds or {}
    groups = defaultdict(list)
    for r in pool:
        bk = _building_key(r)
        if bk is not None:
            groups[bk].append(r)

    result = {}
    for members in groups.values():
        classifiable = [
            m for m in members
            if m.get("_price_m2") is not None and m.get("_rooms") is not None
        ]
        if len(classifiable) - 1 < MIN_FOR_BUILDING_SCORE:
            continue
        for r in members:
            others = [m for m in classifiable if m.get("id") != r.get("id")]
            score, n, _ = building_class_from_listings(
                others, room_bounds, q25, q75
            )
            if score is not None:
                result[r.get("id")] = (score, n)
    return result


def resolve_class_score(row_id, score_index):
    """Class score of an arbitrary pool row, or (None, 0) when its
    building could not be scored. There is deliberately no fallback to
    the row's own price bucket — see building_scores_index()."""
    return score_index.get(row_id, (None, 0))


def credibility_weight(n):
    """Shrinkage weight Z for partial pooling: how much weight a signal
    built from `n` (effective, owner-aware) listings gets against the
    prior it's blended with. Z -> 1 as n grows large; Z = 0.5 at
    n = CREDIBILITY_K. (FULL_CREDIBILITY_N is a separate threshold —
    below it shrinkage applies at all; it does not appear in this
    formula, which approaches 1 asymptotically rather than at a cutoff.)

    Used in analyze_cohorts to blend each cohort's own (purity- and
    owner-)adjusted median toward the (room count x building class)
    prior: adjusted_median = Z*median + (1-Z)*prior. This runs inside
    EVERY level of the cascade — a thin L1/2 is tempered before it
    reaches the cross-level blend, not just the level the cascade
    happened to stop at. There is no separate `building_near` signal;
    that was the pre-v5 shape of this blend, back when L1/2 and the
    level the cascade selected were two different things to reconcile.
    They no longer are — see collect_cohorts' L1/2 docstring.

    IMPORTANT: `n` here should usually be an owner-aware effective sample
    size (see effective_n() below), not a raw listing count — two listings
    from the same seller are not two independent price observations."""
    return n / (n + CREDIBILITY_K)


# krisha listings frequently hide the seller behind a literal placeholder
# name rather than leaving owner_name blank — comparing against "" alone
# would miss the overwhelming majority of anonymous listings (~68% of
# krisha_astana_baseline.csv uses "Хозяин" specifically). These cannot be
# deduplicated against each other: there is no signal distinguishing one
# anonymous seller from another, so each still counts as its own
# observation. That's a real, acknowledged blind spot, not a claim that
# anonymous listings are independent — it's the best we can do without an
# identity signal (phone number, etc.) that this dataset doesn't provide.
GENERIC_OWNER_NAMES = {"", "хозяин", "собственник"}


def effective_n(listings):
    """Owner-aware effective sample size for credibility weighting.

    Two or more listings from the same IDENTIFIABLE (non-generic)
    owner_name in the same cohort are not independent draws from the
    market — they're one seller's pricing decisions, and sellers who list
    several units in the same building sometimes price them very
    differently (in krisha_astana_baseline.csv, ~10% of building cohorts
    with n>=2 have >=2 listings from the same named owner, and among those
    duplicate-owner groups ~28% differ in price_m2 by more than 30%, up to
    2.5x). Giving raw n=2 full Bühlmann credibility in that situation means
    trusting what might be a single seller's one-off pricing decision,
    duplicated, as if it were two independent market observations.

    This collapses repeat listings from the same named owner within a
    cohort to a single effective observation. Listings sharing the generic
    placeholder owner_name (see GENERIC_OWNER_NAMES) are NOT deduplicated
    against each other, since we cannot tell anonymous sellers apart.

    v5 also collapses re-posts of the SAME FLAT. Nothing did this before,
    and it matters most exactly where it hurts most: a flat relisted under
    a new id lands in L1/2, the cohort that carries almost all the weight,
    and counts twice. Two listings in one cohort with the same room count,
    the same area to 0.1 m² and the same price are one flat, not two
    market observations. This is deliberately narrow — a genuine pair of
    identical neighbouring units is possible and is the price paid for
    not chasing near-duplicates with fuzzy matching."""
    seen_named_owners = set()
    seen_flats = set()
    n = 0
    for r in listings:
        square = to_float(r.get("square_m2"))
        price = to_float(r.get("price"))
        if square and price:
            fingerprint = (r.get("_rooms"), round(square, 1), round(price, 2))
            if fingerprint in seen_flats:
                continue
            seen_flats.add(fingerprint)

        owner = (r.get("owner_name") or "").strip().lower()
        if owner and owner not in GENERIC_OWNER_NAMES:
            if owner in seen_named_owners:
                continue
            seen_named_owners.add(owner)
        n += 1
    return n


def _class_filtered(candidates, score_index, target_score, target_n):
    """Narrow `candidates` to buildings of the target's class (ТЗ п.7).

    Matching is on the CONTINUOUS class score, not on a three-way label.
    The label version put buildings at 1.49 and 1.51 into disjoint peer
    groups while treating 1.50 and 3.59 as identical; on this baseline
    that meant an elite building was compared against merely
    above-average ones as equals, and near-identical neighbours either
    side of a bucket edge were forbidden from being compared at all.
    Measured against the label version, continuous matching cut median
    prediction error from 12.96% to 11.75% at comparable coverage.

    The tolerance widens with the uncertainty of both scores
    (class_match_tolerance()), so thinly-observed buildings are matched
    loosely rather than being either over-trusted or discarded.

    STRICT on quantity, per ТЗ п.12: a short L3 stays short and hands
    over to L4 rather than being padded with other-class buildings.

    When the target has no class score at all the candidates pass through
    unfiltered — the ТЗ п.9 fallback: compare against nearby apartments
    without class rather than invent a class from the target's own price.
    Candidates without a score are dropped when filtering is active,
    since an unscored candidate cannot be shown to belong.
    """
    if target_score is None:
        return candidates
    out = []
    for r in candidates:
        cand_score, cand_n = resolve_class_score(r.get("id"), score_index)
        if class_scores_match(target_score, target_n, cand_score, cand_n):
            out.append(r)
    return out


def area_slopes_by_rooms(pool):
    """Estimate, per room count, the elasticity of price/m² with respect
    to floor area: the slope b of log(price_m2) ~ b * log(square).

    Winsorized least squares — the top and bottom 1% of both area and
    price are dropped before fitting, so a single 300 m² penthouse or a
    mistyped area cannot tilt the line. Checked against Theil-Sen on the
    same data: the two agree in sign and stay within about 0.09 of each
    other on every room count with enough listings.

    Room counts with fewer than MIN_AREA_MODEL_N listings get no slope,
    and slopes inside AREA_SLOPE_DEADZONE are returned as 0.0 — that is
    what leaves 3- and 4-room flats alone, where the measured effect is
    indistinguishable from zero.
    """
    by_rooms = defaultdict(list)
    for r in pool:
        square = to_float(r.get("square_m2"))
        if (
            r.get("_rooms") is not None
            and r.get("_price_m2") is not None and r["_price_m2"] > 0
            and square is not None and square > 15
        ):
            by_rooms[r["_rooms"]].append((square, r["_price_m2"]))

    slopes = {}
    for rooms, points in by_rooms.items():
        if len(points) < MIN_AREA_MODEL_N:
            continue

        areas = sorted(a for a, _ in points)
        prices = sorted(p for _, p in points)
        n = len(points)
        lo_a, hi_a = areas[int(0.01 * n)], areas[int(0.99 * n) - 1]
        lo_p, hi_p = prices[int(0.01 * n)], prices[int(0.99 * n) - 1]
        kept = [
            (a, p) for a, p in points
            if lo_a <= a <= hi_a and lo_p <= p <= hi_p
        ]
        if len(kept) < MIN_AREA_MODEL_N:
            continue

        xs = [math.log(a) for a, _ in kept]
        ys = [math.log(p) for _, p in kept]
        mx = statistics.mean(xs)
        my = statistics.mean(ys)
        denom = sum((x - mx) ** 2 for x in xs)
        if denom <= 0:
            continue
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
        slope = max(AREA_SLOPE_MIN, min(AREA_SLOPE_MAX, slope))
        if abs(slope) < AREA_SLOPE_DEADZONE:
            slope = 0.0
        slopes[rooms] = slope
    return slopes


def area_adjust_factor(target_square, comparable_square, slope):
    """Factor projecting a comparable's price/m² onto the target's floor
    area: (A_target / A_comparable) ** slope.

    With a negative slope a comparable that is larger than the target is
    scaled UP, because the target's smaller size would command more per
    square metre — which is the whole point. Returns 1.0 whenever either
    area is missing or the slope is zero, so the correction silently
    disappears where it cannot be justified rather than guessing.
    """
    if not slope:
        return 1.0
    if not target_square or not comparable_square:
        return 1.0
    if target_square <= 0 or comparable_square <= 0:
        return 1.0
    factor = (target_square / comparable_square) ** slope
    return max(AREA_ADJUST_MIN, min(AREA_ADJUST_MAX, factor))


def _apply_area_adjustment(rows, target, area_slopes):
    """Project same-room comparables onto the target's floor area.

    Applied only to direct same-room comparables. Rows that arrived
    through the cross-room rescale (_rescaled) are skipped on purpose:
    the citywide room-median ratio they already went through embeds the
    typical size difference between room counts, so adjusting them again
    for area would count the same effect twice.
    """
    target_square = to_float(target.get("square_m2"))
    slope = area_slopes.get(target.get("_rooms")) if area_slopes else None
    if not slope or not target_square:
        return rows

    out = []
    for r in rows:
        if r.get("_rescaled") or r.get("_price_m2") is None:
            out.append(r)
            continue
        factor = area_adjust_factor(
            target_square, to_float(r.get("square_m2")), slope
        )
        if factor == 1.0:
            out.append(r)
            continue
        adjusted = dict(r)
        adjusted["_price_m2"] = r["_price_m2"] * factor
        adjusted["_area_factor"] = factor
        out.append(adjusted)
    return out


def _rescale_other_rooms(rows, target_rooms, citywide_median):
    """Project listings of a DIFFERENT room count onto the target's room
    count using the ratio of CITYWIDE medians (the old L2.5 mechanic,
    unchanged and now folded into L1/2).

    Non-circular by construction: membership was decided structurally
    (same building, any other room count) and the rescale uses a citywide
    ratio, never a per-building one, so a building's own asking prices
    cannot feed back into its own benchmark.

    The rows stay real listings — only _price_m2 is replaced — so every
    downstream consumer (robust_stats, effective_n, area adjustment)
    handles them exactly like any other comparable.
    """
    target_room_median = citywide_median.get(target_rooms)
    if not target_room_median:
        return []

    out = []
    for r in rows:
        if r["_rooms"] is None or r["_rooms"] == target_rooms:
            continue
        if r["_price_m2"] is None:
            continue
        other_room_median = citywide_median.get(r["_rooms"])
        if not other_room_median:
            continue
        adjusted = dict(r)
        adjusted["_price_m2"] = r["_price_m2"] * (target_room_median / other_room_median)
        adjusted["_rooms_source"] = r["_rooms"]
        adjusted["_rescaled"] = True
        out.append(adjusted)
    return out


def collect_cohorts(
    target, pool, citywide_median, score_index,
    target_score=None, target_n=0, area_slopes=None,
    class_priors=None, spatial_index=None, building_index=None,
):
    """Collect cohorts CUMULATIVELY from tightest to widest (ТЗ п.1).

    Returns (cohorts, stop_level, notes):
        cohorts    — ordered list of {"level": str, "rows": [...]} for
                     every level actually collected, including levels
                     that came up short;
        stop_level — the level the cascade stopped at;
        notes      — machine-readable markers for any filter that had to
                     be relaxed to build the comparison at all.

    Cascade rule, exactly as specified:
        L1/2 is always collected;
        L3 is then ALWAYS collected, even when L1/2 is already full;
        from L3 on, a level that reaches its MIN_COHORT_* stops the
        cascade, otherwise the next level is collected;
        L6 is terminal.

    Every level is deduplicated against everything already collected
    (ТЗ п.4): the exclusion is by listing id, accumulated across levels,
    and applies regardless of HOW a listing entered an earlier cohort —
    so no apartment can be counted twice and pick up a double weight. The
    one exception by design is a listing that entered L1/2 rescaled from
    another room count: it is keyed by the same id and therefore still
    blocked from reappearing later.
    """
    notes = []

    # L1/2 needs rows of OTHER room counts too, so the self-exclusion runs
    # once, up front, against the WHOLE pool rather than an already
    # room-filtered list.
    candidates_all = [r for r in pool if not same_target_id(r, target)]
    comparable_rooms = [r for r in candidates_all if r["_rooms"] == target["_rooms"]]

    if building_index is not None:
        building_member_ids = {
            id(r) for r in building_index.members(target, spatial_index)
        }

        def in_target_building(r):
            return id(r) in building_member_ids
    else:
        def in_target_building(r):
            ok, _ = same_building(target, r)
            return ok

    has_coords = target["_lat"] is not None and target["_lon"] is not None

    def within_radius(candidates, radius_km):
        """Rows from `candidates` inside `radius_km` of the target.

        Uses the prebuilt spatial index when one is supplied, and falls
        back to a linear scan otherwise, so the function is correct with
        or without it. The index is filtered down to `candidates` by id
        rather than trusted wholesale, because `candidates` has already
        had the room/self filters applied to it.
        """
        if not has_coords:
            return []
        if spatial_index is None:
            return [
                r for r in candidates
                if r["_lat"] is not None and r["_lon"] is not None
                and haversine_km(
                    target["_lat"], target["_lon"], r["_lat"], r["_lon"]
                ) <= radius_km
            ]
        allowed = {id(r) for r in candidates}
        return [
            r for r in spatial_index.within(
                target["_lat"], target["_lon"], radius_km
            )
            if id(r) in allowed
        ]

    used_ids = set()

    def take(rows):
        """Drop anything already consumed by an earlier cohort and
        anything without a usable price, register what's left, and
        project it onto the target's floor area (area_slopes).

        Every pool that reaches collect_cohorts today is filtered by
        _excluded_baseline first (see prepare_baseline() / run()'s
        offline-mode branch), which already requires a non-empty id —
        so the id-less branch below is unreachable via any live call
        path right now. It exists anyway: build_cohort() is a public,
        documented compatibility shim ("orchestrator.py and other
        external callers"), and nothing enforces that a future or
        external caller passes an already-filtered pool. Without this
        fallback, an id-less row could be picked up by two different
        cohort levels and counted as two independent observations,
        contradicting the "no apartment can be counted twice" promise
        in this function's caller (collect_cohorts). Same "same flat"
        fingerprint effective_n() uses for owner re-post detection —
        rooms + area to 0.1 m² + price — since that is already this
        file's accepted proxy for listing identity when no id is
        available.
        """
        rows = _apply_area_adjustment(rows, target, area_slopes)
        fresh = []
        for r in rows:
            rid = r.get("id")
            if rid not in (None, ""):
                key = ("id", str(rid))
            else:
                square = to_float(r.get("square_m2"))
                price = to_float(r.get("price"))
                if square is not None and price is not None:
                    key = ("fp", r.get("_rooms"), round(square, 1), round(price, 2))
                else:
                    key = None  # nothing to dedupe against; let it through
            if key is not None and key in used_ids:
                continue
            if r.get("_price_m2") is None:
                continue
            if key is not None:
                used_ids.add(key)
            fresh.append(r)
        return fresh

    cohorts = []

    # ---- L1/2: the merged "first cohort" -----------------------------
    # Exact building matches with the same room count (what used to be
    # L1/2 and L2b — complex_key match and street+house_num match are the
    # same physical claim, so they are no longer two competing levels),
    # PLUS the rescaled other-room listings of that same building (L2.5).
    # Merging them means a building with 3 same-room and 4 other-room
    # listings now produces one 7-listing first cohort instead of
    # skipping straight to a street-level match.
    l12_same_rooms = [r for r in comparable_rooms if in_target_building(r)]
    l12_other_rooms = []
    if target["_rooms"] is not None:
        l12_other_rooms = _rescale_other_rooms(
            [r for r in candidates_all if in_target_building(r)],
            target["_rooms"],
            citywide_median,
        )
    l12 = take(l12_same_rooms) + take(l12_other_rooms)
    if l12:
        cohorts.append({"level": "1-2", "rows": l12})

    # ---- L3: always attempted, whatever L1/2 produced ----------------
    # Tight radius (RADIUS_L3_KM), same room count, same building class.
    #
    # This used to be "same street". On this market that was a mistake:
    # Astana's avenues run 8-11 km, so a same-street cohort was wider
    # than the 1 km L4 in 46% of pairs while carrying 4.6x its weight,
    # and it depended on street-name spelling that the source data does
    # not keep consistent (the baseline holds 18 spellings of one
    # avenue). A radius is both tighter and immune to that. Measured on
    # the baseline it beats the street rule on both accuracy and
    # coverage — see RADIUS_L3_KM.
    #
    # Class filtering is strict (ТЗ п.7/п.12): a short L3 stays short and
    # hands over to L4 rather than being padded with other-class
    # buildings.
    l3_candidates = within_radius(comparable_rooms, RADIUS_L3_KM)
    l3 = take(_class_filtered(l3_candidates, score_index, target_score, target_n))
    if l3:
        cohorts.append({"level": "3", "rows": l3})
    if len(l3) >= MIN_COHORT_L3:
        return cohorts, "3", notes

    # ---- L4: 1 km radius, same room count, same class ----------------
    if has_coords:
        l4_candidates = within_radius(comparable_rooms, RADIUS_L4_KM)
        l4 = take(
            _class_filtered(l4_candidates, score_index, target_score, target_n)
        )
        if l4:
            cohorts.append({"level": "4", "rows": l4})
        if len(l4) >= MIN_COHORT_L4:
            return cohorts, "4", notes

        # ---- L5: 3 km radius narrowed by price, same class -----------
        # The band is centred on the target's OWN (room x class) median,
        # not on the citywide room median. Centring it citywide
        # put the two selection criteria at war with each other: for a
        # business-class 2-room target the citywide band ran 3 889-7 223
        # ₸/m², which admitted only 34.5% of business-class 2-room
        # listings and filled the level with cheaper ones instead. On
        # La Vie that produced an L5 median of 5 385 against the
        # building's own 10 655 — a "comparable" set containing nothing
        # comparable.
        median_for_rooms = cohort_prior(
            target["_rooms"], _label_from_class_score(target_score),
            class_priors or {}, citywide_median,
        )
        if median_for_rooms:
            lo = median_for_rooms * (1 - PRICE_RANGE_L5_PCT)
            hi = median_for_rooms * (1 + PRICE_RANGE_L5_PCT)
            l5_candidates = [
                r for r in within_radius(comparable_rooms, RADIUS_L5_KM)
                if r["_price_m2"] is not None and lo <= r["_price_m2"] <= hi
            ]
            l5 = take(
                _class_filtered(l5_candidates, score_index, target_score, target_n)
            )
            if l5:
                cohorts.append({"level": "5", "rows": l5})
            if len(l5) >= MIN_COHORT_L5:
                return cohorts, "5", notes

    # ---- L6: citywide, same room count, same class (terminal) --------
    l6 = take(_class_filtered(comparable_rooms, score_index, target_score, target_n))
    if l6:
        cohorts.append({"level": "6", "rows": l6})
    return cohorts, "6", notes


def build_cohort(
    target, pool, citywide_median, score_index,
    target_score=None, target_n=0, area_slopes=None,
    class_priors=None, spatial_index=None, building_index=None,
):
    """Backwards-compatible shim for orchestrator.py and other external
    callers that expect the old (level, cohort, tight_cohort) triple.

    It now reports the level the CUMULATIVE cascade stopped at, the union
    of every collected cohort as the cohort, and the L1/2 rows as the
    tight cohort. Internal scoring no longer goes through here — see
    collect_cohorts(), which preserves the per-level split that the
    weighting scheme needs.
    """
    # Tolerate a raw (un-enriched) row: this is an external entry point,
    # and requiring the caller to know about the private `_` fields is
    # exactly the kind of coupling a compatibility shim exists to avoid.
    if "_price_m2" not in target:
        target = enrich(target)

    # v4 referenced spatial_index / building_index here without either
    # being a parameter or a global, so every call raised NameError. This
    # is the entry point orchestrator.py uses, i.e. the external interface
    # was broken outright; the indexes are now optional parameters and
    # collect_cohorts falls back to linear scans when they are absent.
    cohorts, stop_level, _notes = collect_cohorts(
        target, pool, citywide_median, score_index, target_score, target_n,
        area_slopes, class_priors, spatial_index, building_index,
    )
    combined = [r for c in cohorts for r in c["rows"]]
    tight = next((c["rows"] for c in cohorts if c["level"] == "1-2"), [])
    if not combined:
        return "none", [], []
    return stop_level, combined, tight


# Small-sample bias of a MAD-based scale estimate, by cohort size.
#
# Every dispersion estimator underestimates spread on few points, and the
# previous code made this worse by switching estimator at n=4: below it,
# MAD*1.4826; at and above it, the raw IQR. Two different quantities on
# two different scales, with one shared set of thresholds.
#
# Measured by simulation (20 000 draws per size from a distribution with
# a true IQR/median of 0.1349): the uncorrected estimate came out 0.071 at
# n=2, 0.054 at n=3, 0.068 at n=4, 0.085 at n=8, 0.095 at n=20, 0.099 at
# n=100. The consequence in the old code was backwards incentives — a
# 3-listing cohort measured dispersion 0.051 and earned homogeneity
# weight 0.94, while a 20-listing cohort with the SAME underlying spread
# measured 0.123 and earned 0.73. Small cohorts were being rewarded for
# a scatter they simply could not observe.
#
# These factors bring the estimate back to the true value at every size,
# so DISPERSION_REF means one thing across the whole range. The n=3 entry
# is genuinely larger than its neighbours: for odd n the MAD is a single
# order statistic and is biased hardest there.
_MAD_BIAS_CORRECTION = {
    2: 1.417, 3: 1.870, 4: 1.482, 5: 1.335, 6: 1.263,
    8: 1.172, 12: 1.105, 20: 1.059, 40: 1.027, 100: 1.011,
}


def _mad_correction(n):
    if n < 2:
        return None
    keys = sorted(_MAD_BIAS_CORRECTION)
    # Exact match first: n == keys[-1] (100) used to fall into the
    # `n >= keys[-1]` branch below and return the untabulated 1.0
    # instead of the measured 1.011, because that check ran first.
    if n in _MAD_BIAS_CORRECTION:
        return _MAD_BIAS_CORRECTION[n]
    if n > keys[-1]:
        return 1.0
    lo = max(k for k in keys if k < n)
    hi = min(k for k in keys if k > n)
    t = (n - lo) / (hi - lo)
    return _MAD_BIAS_CORRECTION[lo] * (1 - t) + _MAD_BIAS_CORRECTION[hi] * t


def robust_stats(values):
    """(median, MAD, IQR-equivalent spread).

    The third value is a bias-corrected, normal-consistent scale
    expressed on the IQR scale, computed the SAME way for every cohort
    size — see _MAD_BIAS_CORRECTION for why the old n<4 / n>=4 split had
    to go.

    At n=1 spread is not measurable at all and the third value is None,
    so callers can tell "measured as tight" from "not measurable" and
    route single-listing cohorts to the neutral HOMOGENEITY_UNKNOWN
    factor instead of rewarding them with full marks for a homogeneity
    that cannot exist.
    """
    values = [v for v in values if v is not None and math.isfinite(v)]
    if not values:
        return None, None, None

    median = statistics.median(values)
    abs_dev = [abs(v - median) for v in values]
    mad = statistics.median(abs_dev)

    n = len(values)
    correction = _mad_correction(n)
    if correction is None or mad is None:
        iqr = None
    else:
        # 1.4826 -> sigma, 1.349 -> IQR of a normal with that sigma.
        iqr = mad * 1.4826 * 1.349 * correction
    return median, mad, iqr


def data_warnings(target):
    warnings = []

    if target["_price_m2"] is None:
        warnings.append("нет_price_m2")
    elif _price_m2_is_unverified(target):
        # price/square_m2 wasn't computable, so _price_m2 fell back to
        # the site's priceM2 field — see infer_price_m2() for why that
        # field is not trusted as a primary source for this category.
        warnings.append("site_price_m2_unverified")
    if target["_rooms"] is None:
        warnings.append("нет_rooms")
    if to_float(target.get("square_m2")) is None:
        warnings.append("нет_square_m2")
    if target["_lat"] is None or target["_lon"] is None:
        warnings.append("нет_координат")
    try:
        photos = int(float(target.get("photo_count") or 0))
    except Exception:
        photos = 0
    if photos <= 0:
        warnings.append("нет_фото")
    elif photos < 5:
        warnings.append("мало_фото")

    if to_bool(target.get("requires_manual_review")):
        warnings.append("есть_red_flag")
    if to_bool(target.get("is_installment_segment")):
        warnings.append("рассрочка")
    if target["_seller_class"] == "agent":
        warnings.append("риелтор_или_агентство")
    elif target["_seller_class"] == "unknown":
        warnings.append("продавец_не_определён")

    return warnings


def quality_evidence_score(target):
    """
    Не пытается угадать "красивый ремонт" из воздуха.
    Это именно score наличия наблюдаемых доказательств качества,
    а не денежная поправка к цене.

    Сильные сигналы:
        явно заявленные premium markers,
        меблировка,
        дополнительные удобства,
        фотографии.
    """
    score = 40.0

    premium = parse_jsonish(target.get("premium_markers"), [])
    if isinstance(premium, list):
        # Was a flat +5 per listed marker (capped at 15, i.e. hit the
        # ceiling at exactly 3 items regardless of content) — a pure
        # word-count reward that let a listing rack up full marks just by
        # naming three near-synonyms for the same one renovation
        # ("евроремонт", "современный ремонт", "качественный ремонт").
        # Fix has two parts: (1) case/whitespace-normalize and dedupe so
        # literal repeats can't inflate the count at all, and (2) score
        # the distinct count on a sub-linear (log) curve instead of a
        # flat per-item rate, so the first distinct marker carries most
        # of the weight and each additional one adds progressively less
        # — rewarding genuine breadth of evidence without letting a
        # longer, more repetitive listing simply out-count a shorter,
        # equally strong one.
        distinct_markers = {
            re.sub(r"\s+", " ", str(m).strip().lower())
            for m in premium
            if str(m).strip()
        }
        score += round(min(15.0, 6.5 * math.log1p(len(distinct_markers))), 1)
    elif premium:
        score += 8

    furniture = (target.get("furniture") or "").strip().lower()
    if furniture and furniture not in ("нет", "—", "-"):
        score += 8
    if furniture in ("полностью", "полностью меблирована", "полностью меблирован"):
        score += 5

    extra = parse_jsonish(target.get("extra_attributes"), {})
    if isinstance(extra, dict):
        for key in ("parking", "security", "balcony", "view"):
            value = extra.get(key)
            if value not in (None, "", False, "не указано", "нет"):
                score += 2

    try:
        photos = int(float(target.get("photo_count") or 0))
    except Exception:
        photos = 0
    if photos >= 10:
        score += 10
    elif photos >= 5:
        score += 6
    elif photos >= 1:
        score += 2
    else:
        score -= 10

    # Red flags do not create an automatic "bad quality" price discount;
    # they create a manual-review requirement.
    if to_bool(target.get("requires_manual_review")):
        score -= 15

    return round(max(0.0, min(100.0, score)), 1)


def cohort_homogeneity_factor(level, dispersion):
    """Downweight confidence when the cohort spans too wide a price range
    for its level to be believed at face value — e.g. a radius-based
    cohort that's quietly straddling two different building qualities.

    Thresholds are calibrated per level group, not globally: precise
    levels (same complex/building/street+segment) run tight in practice
    (median IQR/median ~0.11-0.12, p90 ~0.15-0.20 on the current baseline),
    so the same dispersion that's unremarkable for a radius-based L4/L5
    cohort (median ~0.21, p90 ~0.31) would already be an outlier at L1-3.
    """
    if dispersion is None:
        return 1.0
    if level in ("1-2", "2b", "2.5"):
        # L1/2 now merges exact same-room building matches with rescaled
        # other-room ones. The rescale step adds noise of its own (a
        # citywide room-count ratio isn't guaranteed to hold exactly for
        # one specific building), so the merged first cohort gets a
        # little more room than a pure same-room match had before
        # treating dispersion as suspicious — sitting between the old
        # L1/2 (0.22/0.32) and the old L2.5 (0.26/0.38) values.
        moderate, severe = 0.24, 0.35
    elif level == "3":
        moderate, severe = 0.22, 0.32
    else:
        # Review finding #5: L4/L5 previously had the LOOSEST dispersion
        # thresholds (0.33/0.48) despite being the least precise match
        # method by construction — backwards, since a radius match is
        # exactly where undetected contamination (mixed building classes
        # nearby) is most likely. Now that build_cohort applies a building
        # class filter to L4/L5 when data allows (see
        # _class_filtered_or_fallback), a genuinely class-matched cohort
        # should already run tighter, so pulling these thresholds toward
        # L1-3 doesn't manufacture false positives on well-matched cohorts
        # — it mainly catches the cases where the class filter couldn't
        # apply (target_class unknown) and the radius match is doing all
        # the work alone.
        moderate, severe = 0.27, 0.40
    if dispersion >= severe:
        return 0.55
    if dispersion >= moderate:
        return 0.8
    return 1.0


# Locality quality dominates: a large citywide cohort is still weaker
# evidence than a small same-building one.
#
# Levels "2b" and "2.5" existed in earlier versions and were merged into
# "1-2" (see collect_cohorts docstring). They used to be kept here as
# aliases "for external callers still passing the old level strings",
# but confidence_from_cohorts has exactly one caller (score_row, same
# file), which only ever builds cohort_infos from collect_cohorts —
# and that never produces "2b" or "2.5". No caller, internal or
# external, can reach these keys; removed rather than left to imply a
# compatibility surface that does not exist.
LEVEL_CONFIDENCE = {
    "1-2": 1.00,
    "3": 0.90,
    "4": 0.78,
    "5": 0.62,
    "6": 0.42,
    "none": 0.0,
}


def quantity_factor(n):
    """Smooth, thresholdless trust-in-sample-size (ТЗ п.3, п.14).

    n / (n + QUANTITY_HALF_N). Never reaches zero and never jumps: the
    old "cleared MIN_COHORT_* -> full weight / missed it -> weight 0"
    switch is gone, which was the mechanism that let a 10-listing L3
    silently delete a 3-listing same-building L1/2 (ТЗ п.14).

    `n` should be an effective_n(), not a raw count — see ТЗ п.13.
    """
    n = max(0.0, float(n))
    return n / (n + QUANTITY_HALF_N)


def homogeneity_weight(dispersion, level):
    """Smooth cohort-quality factor (ТЗ п.11): how tightly the cohort's
    own prices cluster, expressed as IQR/median.

    620/625/628/631 (dispersion ~0.01) keeps essentially full weight;
    500/620/710/850 (dispersion ~0.30) is cut roughly in half at a tight
    level. Floored at HOMOGENEITY_FLOOR because a scattered cohort is
    still evidence, just weak evidence — and continuous, so there is no
    cliff a cohort can be pushed over by one listing.
    """
    if dispersion is None:
        return HOMOGENEITY_UNKNOWN
    ref = DISPERSION_REF.get(level, 0.25)
    factor = 1.0 / (1.0 + (max(0.0, dispersion) / ref) ** 2)
    return max(HOMOGENEITY_FLOOR, min(1.0, factor))


def cohort_weight(level, eff_n, dispersion, purity=1.0):
    """Final weight of one cohort in the blended benchmark (ТЗ п.11):

        level weight  ×  purity  ×  quantity  ×  homogeneity

    `purity` is 1.0 for every level except L1/2, where it is the share of
    the cohort's effective observations that are direct same-room matches
    rather than cross-room comparables rescaled through citywide medians
    (see cohort_purity()). Without it, merging L2.5 into L1/2 handed
    rescaled proxies the full weight and full level confidence of an
    exact match — so a building with 2 same-room and 20 other-room
    listings produced a benchmark that was 90% rescale, flagged as the
    most trustworthy level available, with nothing in the output to say
    so.

    The level term still dominates by design, so the required hierarchy
    L1/2 ≫ L3 ≫ L4 ≫ L5 ≫ L6 survives every attainable combination of
    the other three factors (ТЗ п.17) — verify_weight_hierarchy() proves
    it numerically rather than asserting it.
    """
    return (
        LEVEL_WEIGHT.get(level, 0.01)
        * max(0.0, min(1.0, purity))
        * quantity_factor(eff_n)
        * homogeneity_weight(dispersion, level)
    )


def dispersion_components(cohort_infos, base_price_m2):
    """Split the evidence's spread into the two things it can mean.

    D_within  — how noisy each cohort is inside itself, averaged by the
                weights the cohorts actually carry. Already priced into
                those weights via homogeneity_weight().
    D_between — how much the cohorts DISAGREE with each other, as a
                weight-aware relative spread of their medians around the
                blended benchmark. Nothing prices this in yet, and it is
                a completely different kind of doubt: L1/2 saying
                1 050 000 while L6 says 700 000 can leave both cohorts
                internally immaculate (D_within ~ 0) and the answer still
                untrustworthy.

    The previous single number was neither of these — it was the IQR of
    the raw UNION of all cohorts, i.e. a description of a pool dominated
    by whichever level happened to contribute the most rows, usually the
    widest and least relevant one. A target with a flawless L1/2 that
    merely happened to continue the cascade down to L6 was penalised for
    L6's spread even though L6 carried ~0.2% of the weight.

    Returns (D_within, D_between, D_total) with D_total the quadrature
    sum. With a single cohort D_between is 0 and D_total reduces to that
    cohort's own dispersion, exactly as before.
    """
    if not cohort_infos or not base_price_m2:
        return None, None, None

    total_w = sum(c["weight"] for c in cohort_infos)
    if total_w <= 0:
        return None, None, None

    measured = [c for c in cohort_infos if c["dispersion"] is not None]
    if measured:
        w_measured = sum(c["weight"] for c in measured)
        d_within = (
            sum(c["dispersion"] * c["weight"] for c in measured) / w_measured
            if w_measured > 0 else None
        )
    else:
        d_within = None

    d_between = math.sqrt(
        sum(
            c["weight"] * (c["adjusted_median"] - base_price_m2) ** 2
            for c in cohort_infos
        ) / total_w
    ) / base_price_m2

    if d_within is None:
        return None, d_between, d_between
    return d_within, d_between, math.hypot(d_within, d_between)


def weighted_robust_scale(cohort_infos, base_price_m2):
    """Robust scale of the evidence AROUND THE BLENDED BENCHMARK, with
    each cohort contributing its own weight spread across its rows.

    The previous version took the MAD of the raw union around the UNION's
    median, then divided a deviation measured from base_price_m2 by it —
    two different centres. When cohorts disagree the union's median lands
    in the empty space between them and its MAD is inflated by the gap,
    so robust_z collapses toward zero exactly when the cohort match is
    worst. Worked example from the current baseline shape: a tight
    same-building L1/2 near 1 050 000 (98% of weight) alongside a
    700 000-960 000 citywide L6 (2%), benchmark 1 040 000, target at
    900 000 gives z = -0.67 the old way and z = -4.29 this way. The
    SUSPICIOUS_ROBUST_Z = 3.5 guard fires on the second and never on the
    first — i.e. it was structurally unable to catch the case it exists
    for.

    Measuring deviations from base also means cohort disagreement widens
    the scale on its own, without a separate term.
    """
    if not base_price_m2:
        return None
    pairs = []
    for c in cohort_infos:
        rows = [r for r in c["rows"] if r.get("_price_m2") is not None]
        if not rows or c["weight"] <= 0:
            continue
        per_row = c["weight"] / len(rows)
        for r in rows:
            pairs.append((abs(r["_price_m2"] - base_price_m2), per_row))
    if not pairs:
        return None
    mad = weighted_median(pairs)
    if mad is None or mad <= 0:
        return None
    return 1.4826 * mad


def value_from_diff(diff_pct):
    """Ranking score for how attractive the price is, 0..100.

    Logistic rather than the old clip(50 + 500*diff_pct): that formula
    saturated at diff_pct = +-0.10, which was ALSO where the НАХОДКА
    threshold sat at the time — so literally every apartment that earned
    a НАХОДКА verdict scored exactly 100, and a 10% discount was
    indistinguishable from a 45% one. Since value carries 65% of
    price_quality_score, the "best deals" list was in practice being
    ordered by photo count. (Both numbers describe that earlier state;
    the НАХОДКА threshold has since moved to FIND_THRESHOLD_HIGH = 0.18 —
    see there for the current, re-derived value. The logistic itself has
    no saturation point to keep in sync.)

    The logistic is strictly monotone over the whole range, needs no
    clipping, returns exactly 50 at a fair price, and handles the natural
    asymmetry of diff_pct (bounded above by 1, unbounded below) without
    pretending the scale is symmetric.

    Defined once and used by both score_row and price_quality_score,
    which previously carried two copies of the same expression.
    """
    if diff_pct is None:
        return None
    # diff_pct = (base - price)/base is bounded above by 1 but unbounded
    # below: a listing priced several times its benchmark produces a
    # large negative number, and math.exp() overflows well before that
    # becomes implausible (found immediately on the real baseline). The
    # logistic is saturated to within floating-point resolution long
    # before |x| = 700, so clamping the exponent changes no representable
    # output value.
    x = diff_pct / VALUE_SCALE
    if x <= -700.0:
        return 0.0
    if x >= 700.0:
        return 100.0
    return 100.0 / (1.0 + math.exp(-x))


# Effective observations at which the size term reaches one half.
# 3 means a benchmark needs roughly three independent comparables before
# its size stops being the limiting factor. Measured effect on the
# baseline: benchmark_confidence went from p1=0.749 / p50=0.948 (98.2% of
# all rows in the top verdict tier, i.e. a variable that separated
# nothing) to p1=0.28 / p50=0.70, with 37% high and 22% low.
CONFIDENCE_HALF_N = 3.0

# Reference within-cohort dispersion (IQR/median) at which confidence is
# halved, and the floor below which the penalty stops biting. 0.45 sits
# near the 90th percentile of observed cohort dispersion (p50 = 0.203,
# p75 = 0.282, p90 = 0.399), so ordinary cohorts are barely touched and
# only genuinely scattered ones are marked down.
WITHIN_DISPERSION_REF = 0.45
WITHIN_DISPERSION_FLOOR = 0.40


def confidence_from_cohorts(cohort_infos, disagreement):
    """Benchmark confidence for a cumulative, multi-level blend.

    The level term is the weight-weighted average of the participating
    levels' confidences: a result that is 85% L1/2 and 15% L3 is nearly
    as trustworthy as pure L1/2, while one that is mostly L6 is not.
    Size uses the summed effective_n across cohorts (ТЗ п.13).

    The homogeneity multiplier is applied to `disagreement` (D_between)
    ONLY. Within-cohort spread has already shaped every cohort's weight
    through homogeneity_weight(), and those weights are what produce the
    level term above — feeding the same spread in a second time here (as
    the previous version did, via the union's dispersion) double-counted
    it under two different formulas with two different thresholds. What
    is left uncounted is disagreement BETWEEN cohorts, so that is what
    this multiplier now measures.
    """
    if not cohort_infos:
        return 0.0

    total_w = sum(c["weight"] for c in cohort_infos)
    if total_w <= 0:
        return 0.0

    level_factor = sum(
        LEVEL_CONFIDENCE.get(c["level"], 0.25) * c["weight"] for c in cohort_infos
    ) / total_w

    # WEIGHT-AWARE effective size. The previous version summed
    # effective_n over every cohort with no weighting at all, while the
    # level term next to it was weighted — so an L6 carrying 0.2% of the
    # weight contributed its two hundred listings to "how much evidence is
    # this built on" as if they were the evidence. Because the cascade
    # always keeps collecting wider levels, that sum was large for nearly
    # every row.
    effective_size = sum(
        c["effective_n"] * c["weight"] for c in cohort_infos
    ) / total_w

    # MULTIPLICATIVE, not 0.35*size + 0.65*level. The additive form had a
    # floor: with level_factor at 1.0 the result could not fall below
    # 0.65 no matter how thin the evidence, so a benchmark resting on a
    # single same-building listing scored 0.95+. One observation cannot
    # produce high confidence, whatever its provenance.
    size_factor = effective_size / (effective_size + CONFIDENCE_HALF_N)
    base = level_factor * size_factor

    # WITHIN-cohort spread. Confidence previously reacted only to
    # disagreement BETWEEN levels and ignored how scattered the winning
    # cohort's own listings were — so a benchmark built from one
    # building whose rents ran from 6 652 to 13 158 ₸/m² (dispersion
    # 0.42) still scored 0.674.
    #
    # This is not a cosmetic addition. Measured against the model's
    # actual prediction error, within-cohort dispersion is the single
    # best predictor available — better than confidence itself was:
    #
    #   by dispersion quintile   median |err| 0.149 -> 0.092  (span 62%)
    #   by v5 confidence quintile             0.125 -> 0.100  (span 25%)
    #   by cohort size quintile               0.108 -> 0.100  (span  7%)
    #   by data_confidence quintile           0.114 -> 0.112  (no signal)
    #
    # Folding it in takes the confidence variable's own span from 25% to
    # 36%: it now separates a benchmark that will be off by 14% from one
    # that will be off by 9%, which is what a confidence number is for.
    spread = 0.0
    measured = 0.0
    for c in cohort_infos:
        if c.get("dispersion") is not None:
            spread += c["dispersion"] * c["weight"]
            measured += c["weight"]
    if measured > 0:
        within = spread / measured
        base *= max(
            WITHIN_DISPERSION_FLOOR,
            1.0 / (1.0 + within / WITHIN_DISPERSION_REF),
        )

    dominant = max(cohort_infos, key=lambda c: c["weight"])["level"]
    base *= cohort_homogeneity_factor(dominant, disagreement)
    return round(max(0.0, min(1.0, base)), 3)


def quality_confidence(target):
    warnings = data_warnings(target)
    # Missing core fields are much more damaging than being an agent.
    core = {"нет_price_m2", "нет_rooms"}
    missing_core = len(core.intersection(warnings))
    confidence_warnings = [w for w in warnings if w not in {"риелтор_или_агентство", "продавец_не_определён"}]
    score = 1.0 - 0.10 * len(confidence_warnings) - 0.25 * missing_core
    return max(0.0, min(1.0, score))


def price_quality_score(diff_pct, quality_score, benchmark_confidence, data_confidence):
    """
    Composite score for ranking, not a probability of a sale.

    65% = market-relative price advantage.
    25% = observable quality evidence.
    10% = confidence in the evidence/benchmark.

    Quality is intentionally not allowed to overwhelm market price: a model
    should not call an expensive apartment a bargain merely because its text
    says "designer renovation".
    """
    if diff_pct is None:
        return None
    value = value_from_diff(diff_pct)
    confidence = 100.0 * (
        BENCHMARK_CONF_SHARE * benchmark_confidence
        + DATA_CONF_SHARE * data_confidence
    )
    score = 0.65 * value + 0.25 * quality_score + 0.10 * confidence
    return round(max(0.0, min(100.0, score)), 1)


def price_quality_label(diff_pct, quality_score, warnings):
    if diff_pct is None:
        return "нет оценки"
    if diff_pct >= 0.10 and quality_score >= 55:
        label = "сильное цена/качество"
    elif diff_pct >= 0.05:
        label = "хорошее цена/качество"
    elif diff_pct <= -0.10:
        label = "слабое цена/качество"
    else:
        label = "обычное цена/качество"

    if "нет_фото" in warnings or "мало_фото" in warnings:
        label += "; качество не подтверждено фото"
    return label


def verdict_from_diff(diff_pct, benchmark_confidence, target, warnings, robust_z=None):
    if diff_pct is None:
        return "НЕДОСТАТОЧНО ДАННЫХ", "нет устойчивой рыночной базы"

    # Severe red flags are not priced automatically.
    if to_bool(target.get("requires_manual_review")):
        return "РУЧНАЯ ПРОВЕРКА", "есть red_flag: ценовую скидку нельзя честно оценить автоматически"

    if benchmark_confidence >= CONFIDENCE_TIER_HIGH:
        find_t = FIND_THRESHOLD_HIGH_CONF
        over_t = OVERPRICE_THRESHOLD_HIGH_CONF
    elif benchmark_confidence >= CONFIDENCE_TIER_MED:
        find_t = FIND_THRESHOLD_MED_CONF
        over_t = OVERPRICE_THRESHOLD_MED_CONF
    else:
        find_t = FIND_THRESHOLD_LOW_CONF
        over_t = OVERPRICE_THRESHOLD_LOW_CONF

    # Review finding #4: robust_z was computed upstream but never consulted
    # here. It's a second, independent-ish signal (z-score against MAD,
    # rather than diff_pct's floor-corrected percentage) — a large |z| at
    # weak confidence flags a likely bad cohort match even in cases where
    # diff_pct itself hasn't crossed SUSPICIOUS_DIFF_THRESHOLD yet.
    z_is_suspicious = (
        robust_z is not None
        and abs(robust_z) >= SUSPICIOUS_ROBUST_Z
        and benchmark_confidence < SUSPICIOUS_DIFF_CONF_FLOOR
    )

    # v5: is_thin_description() is NOT part of this test any more. It used
    # to be an OR-branch here, and since one of its fields was blank on
    # 40% of this market it accounted for 502 of 504 ТРЕБУЕТ ПРОВЕРКИ verdicts
    # while the confidence branch fired 3 times. The verdict now means
    # what its text says — the BENCHMARK looks unreliable — and the
    # incompleteness of the listing is reported separately in
    # review_flags, where a reader can act on it without it masquerading
    # as a statistical finding.
    if diff_pct >= find_t:
        if z_is_suspicious or (
            diff_pct >= SUSPICIOUS_DIFF_THRESHOLD
            and benchmark_confidence < SUSPICIOUS_DIFF_CONF_FLOOR
        ):
            return (
                "ТРЕБУЕТ ПРОВЕРКИ",
                f"аномально большая скидка ({diff_pct:.1%}) при низкой "
                f"уверенности базы — вероятнее ошибка когорты, чем находка",
            )
        return "НАХОДКА", f"цена ниже базы на {diff_pct:.1%}"
    if diff_pct <= over_t:
        # Symmetric counterpart to the НАХОДКА suspicious-check above
        # (review finding #4): a bad/contaminated cohort can just as easily
        # push a target too far in the OVERPRICED direction as in the
        # underpriced one — e.g. a radius-based L4/L5 match that pulled in
        # a cheaper, lower-class building nearby. Previously only the
        # discount side had this guard, so a fair listing near a bad
        # cohort would confidently get labeled "ПЕРЕОЦЕНЕНА" instead of
        # being flagged for a human to check the comparison itself.
        if z_is_suspicious or (
            diff_pct <= -SUSPICIOUS_DIFF_THRESHOLD
            and benchmark_confidence < SUSPICIOUS_DIFF_CONF_FLOOR
        ):
            return (
                "ТРЕБУЕТ ПРОВЕРКИ",
                f"аномально большое отклонение вверх ({abs(diff_pct):.1%}) при "
                f"низкой уверенности базы — вероятнее ошибка когорты, чем "
                f"реальная переоценка",
            )
        return "ПЕРЕОЦЕНЕНА", f"цена выше базы на {abs(diff_pct):.1%}"
    return "СПРАВЕДЛИВАЯ", f"цена близка к базе ({diff_pct:+.1%})"


_COHORT_FIELD_PREFIX = {
    "1-2": "l12",
    "3": "l3",
    "4": "l4",
    "5": "l5",
    "6": "l6",
}


def _empty_cohort_fields():
    fields = {}
    for prefix in _COHORT_FIELD_PREFIX.values():
        fields[f"{prefix}_n"] = None
        fields[f"{prefix}_median"] = None
        fields[f"{prefix}_adjusted_median"] = None
        fields[f"{prefix}_credibility"] = None
        fields[f"{prefix}_weight"] = None
    fields["l12_direct_n"] = None
    fields["l12_rescaled_n"] = None
    fields["l12_purity"] = None
    fields["cohort_levels_used"] = ""
    fields["cohort_breakdown"] = ""
    return fields


def _cohort_output_fields(cohort_infos):
    """Flatten the per-cohort audit trail required by ТЗ п.16 — for each
    cohort actually used: how many listings, what its median was, and
    what weight it carried. Both as a readable summary string and as flat
    columns, so the CSV can be checked by eye and pivoted by machine."""
    fields = _empty_cohort_fields()
    if not cohort_infos:
        return fields

    total_w = sum(c["weight"] for c in cohort_infos) or 1.0
    parts = []
    for c in cohort_infos:
        prefix = _COHORT_FIELD_PREFIX.get(c["level"])
        share = c["weight"] / total_w
        if prefix:
            fields[f"{prefix}_n"] = c["size"]
            fields[f"{prefix}_median"] = round(c["median"], 1)
            fields[f"{prefix}_adjusted_median"] = round(c["adjusted_median"], 1)
            fields[f"{prefix}_credibility"] = round(c["credibility"], 3)
            fields[f"{prefix}_weight"] = round(share, 4)
        disp = (
            f"{c['dispersion']:.3f}" if c["dispersion"] is not None else "n/a"
        )
        label = "L1/2" if c["level"] == "1-2" else f"L{c['level']}"
        if c["level"] == "1-2":
            fields["l12_direct_n"] = c["direct_n"]
            fields["l12_rescaled_n"] = c["rescaled_n"]
            fields["l12_purity"] = round(c["purity"], 3)
            composition = (
                f" (прямых {c['direct_n']} + пересчитанных {c['rescaled_n']}, "
                f"purity={c['purity']:.2f})"
            )
        else:
            composition = ""
        # adj= is the number that actually entered the blend; median=
        # is what the cohort said before shrinkage. Printing only the
        # latter, as v4 did, made the trail unverifiable.
        parts.append(
            f"{label}: n={c['size']}{composition}, "
            f"eff_n={c['effective_n']:.1f}, "
            f"median={c['median']:.0f}, adj={c['adjusted_median']:.0f} "
            f"(Z={c['credibility']:.2f}), disp={disp}, w={share:.3f}"
        )

    fields["cohort_levels_used"] = ";".join(c["level"] for c in cohort_infos)
    fields["cohort_breakdown"] = " | ".join(parts)
    return fields


def cohort_purity(rows):
    """(effective direct count, effective rescaled count, purity) for a
    cohort, where "rescaled" means a cross-room comparable projected onto
    the target's room count through citywide medians (the L2.5 mechanic,
    now merged into L1/2).

    purity = (n_direct + RESCALE_TRUST*n_rescaled) / (n_direct + n_rescaled)

    so a cohort of nothing but direct matches scores 1.0 and one of
    nothing but rescaled proxies scores RESCALE_TRUST. Both counts are
    owner-aware (effective_n), so the discount composes with, rather than
    replaces, the independence adjustment of ТЗ п.13.
    """
    direct = [r for r in rows if not r.get("_rescaled")]
    rescaled = [r for r in rows if r.get("_rescaled")]
    n_direct = effective_n(direct)
    n_rescaled = effective_n(rescaled)
    total = n_direct + n_rescaled
    if total <= 0:
        return 0, 0, 1.0
    purity = (n_direct + RESCALE_TRUST * n_rescaled) / total
    return n_direct, n_rescaled, purity


def analyze_cohorts(
    cohorts, citywide_median, target_rooms,
    class_priors=None, building_class_label=None,
):
    """Turn raw collected cohorts into weighted, credibility-adjusted
    building blocks of the final benchmark.

    For each cohort independently (ТЗ п.11, п.13): its robust median, its
    own dispersion, its owner-aware effective_n, its purity, a shrink of
    the median toward the prior proportional to the effective count, and
    finally its weight.

    Two things differ from a naive per-cohort summary:

    * The median is a WEIGHTED median — rescaled cross-room comparables
      count RESCALE_TRUST of a direct match. With a plain median, a
      building holding 2 same-room and 20 other-room listings had its
      centre chosen entirely by the rescaled proxies while the two real
      matches contributed nothing.
    * The prior is the (room count x building class) median rather than
      the citywide room median — see prior_medians_by_rooms_class() for
      why the class-blind prior systematically pushed expensive buildings
      down and cheap ones up.

    Shrinkage lives here, inside each level, rather than as a single
    final step applied to one selected level, so a thin cohort is
    tempered before it is combined and the combination step is left to do
    one job only.
    """
    infos = []
    prior = cohort_prior(
        target_rooms, building_class_label, class_priors or {}, citywide_median,
    )

    for cohort in cohorts:
        rows = [r for r in cohort["rows"] if r.get("_price_m2") is not None]
        if not rows:
            continue

        n_direct, n_rescaled, purity = cohort_purity(rows)
        median = weighted_median(
            (
                r["_price_m2"],
                RESCALE_TRUST if r.get("_rescaled") else 1.0,
            )
            for r in rows
        )
        if median is None or median <= 0:
            continue

        # Dispersion and MAD stay unweighted: they describe the spread of
        # the listings as observed, which is a property of the data, not
        # of how much we choose to trust each row.
        _, mad, iqr = robust_stats([r["_price_m2"] for r in rows])
        dispersion = (iqr / median) if (iqr is not None and median) else None
        eff_n = n_direct + RESCALE_TRUST * n_rescaled

        # Credibility blend toward the prior (ТЗ п.13): the effective,
        # purity-discounted count, so neither several listings by one
        # identifiable seller nor a pile of rescaled proxies can buy the
        # trust of independent direct observations.
        if prior and eff_n < FULL_CREDIBILITY_N:
            cred = credibility_weight(eff_n)
            adjusted_median = cred * median + (1 - cred) * prior
        else:
            cred = 1.0
            adjusted_median = median

        infos.append({
            "level": cohort["level"],
            "rows": rows,
            "size": len(rows),
            "direct_n": n_direct,
            "rescaled_n": n_rescaled,
            "purity": purity,
            "effective_n": eff_n,
            "median": median,
            "adjusted_median": adjusted_median,
            "credibility": cred,
            "dispersion": dispersion,
            "mad": mad,
            "iqr": iqr,
            "prior": prior,
            "weight": cohort_weight(
                cohort["level"], eff_n, dispersion, purity
            ),
        })

    return infos


def blended_base_price(cohort_infos):
    """Weighted average of the cohorts' credibility-adjusted medians
    (ТЗ п.15): every cohort collected before the stop contributes, with
    L1/2 carrying the main weight, L3 much less, L4 less still."""
    total_w = sum(c["weight"] for c in cohort_infos)
    if total_w <= 0:
        return None
    return sum(c["adjusted_median"] * c["weight"] for c in cohort_infos) / total_w


def cohort_members(cohorts, limit=None):
    """Flat, display-ready list of the listings a verdict was built from.

    Exists so a human can audit a verdict: "cheaper than base by 21%"
    is unfalsifiable on its own, but becomes checkable the moment you
    can see the comparables and their prices.

    Returned under the private key `_cohort_members`, which keeps it out
    of every CSV writer in this project by construction — write_output()
    skips `_`-prefixed keys and OUTPUT_EXTRA_FIELDNAMES doesn't list it,
    so a list-of-dicts can never leak into a CSV cell.

    Ordered by cohort level (tightest evidence first — L1/2 is the same
    building and carries ~86% of the blend weight), then by price/m²
    ascending inside a level, so the cheapest comparables read first.
    `limit` caps the list; the caller is told what was dropped via the
    returned `omitted` count rather than silently losing rows.
    """
    order = {"1-2": 0, "3": 1, "4": 2, "5": 3, "6": 4}
    items = []
    for c in cohorts:
        for r in c["rows"]:
            items.append({
                "level": c["level"],
                "id": r.get("id"),
                "url": r.get("url"),
                "price": r.get("price"),
                "square_m2": r.get("square_m2"),
                "rooms": r.get("rooms"),
                # The area-adjusted value actually compared against, which
                # is why it can differ from price/square_m2 on screen.
                "price_m2": r.get("_price_m2"),
                "rescaled": bool(r.get("_rescaled")),
            })
    items.sort(key=lambda x: (
        order.get(x["level"], 99),
        x["price_m2"] if x["price_m2"] is not None else float("inf"),
    ))
    total = len(items)
    if limit is not None and total > limit:
        return items[:limit], total - limit
    return items, 0


def score_row(
    target, pool, citywide_median, score_index, q25, q75,
    soft_target=True, room_bounds=None, class_priors=None, area_slopes=None,
    spatial_index=None, building_index=None,
):
    """
    score_row сохраняется module-level для совместимости с прежним
    orchestrator.py и внешними скриптами.

    `segments` (per-price-bucket boundaries) used to be a positional
    parameter here. It was computed by the caller, threaded through, and
    never read inside this function — the segment classification it fed
    was removed from the scoring path in v5 (target_price_segment() is
    now called directly for the diagnostic `price_segment` output column,
    not through this parameter). Kept as dead weight for one release
    while orchestrator.py still passed it positionally; both callers now
    agree on the current signature, so it is gone rather than silently
    ignored.

    `room_bounds` (per-room-count citywide quartiles) is optional and
    keyword-only in practice, so existing positional callers keep
    working; without it the classifier falls back to the global
    quartiles, which is the pre-existing behaviour.
    """
    target = enrich(target)
    room_bounds = room_bounds or {}
    class_priors = class_priors or {}

    warnings = data_warnings(target)
    seller_cls, seller_conf, seller_reason = (
        target["_seller_class"],
        target["_seller_confidence"],
        target["_seller_reason"],
    )

    if target["_price_m2"] is None:
        return {
            "status": "insufficient_numeric_data",
            "verdict": "НЕДОСТАТОЧНО ДАННЫХ",
            "verdict_reason": "нет корректной цены и/или площади для price/m²",
            "seller_class": seller_cls,
            "seller_confidence": seller_conf,
            "seller_reason": seller_reason,
            "data_warnings": ";".join(warnings),
            "review_flags": ";".join(review_flags(target, warnings)),
            "quality_evidence_score": quality_evidence_score(target),
            "data_confidence": round(quality_confidence(target), 3),
        }

    # Baseline exclusions are only relevant when scoring a baseline row in
    # offline mode. Incoming rows remain scoreable.
    if not soft_target and target["_excluded_baseline"]:
        reason = _excluded_baseline_reason(target)
        return {
            "status": reason,
            "verdict": "РУЧНАЯ ПРОВЕРКА",
            "verdict_reason": "строка не является пригодной для benchmark-пула",
            "seller_class": seller_cls,
            "seller_confidence": seller_conf,
            "seller_reason": seller_reason,
            "data_warnings": ";".join(warnings),
            "review_flags": ";".join(review_flags(target, warnings)),
            "quality_evidence_score": quality_evidence_score(target),
            "data_confidence": 0.0,
        }

    # This pool only excludes self and baseline-ineligible rows.
    cohort_pool = [
        r for r in pool
        if not same_target_id(r, target)
        and not r.get("_excluded_baseline")
    ]

    # STEP 1 (ТЗ п.5): establish the class of the target's BUILDING before
    # any wide cohort is collected — from other units of that building,
    # room count by room count, each against its own citywide market.
    #
    # When the building cannot be classified we now leave target_class as
    # None instead of falling back to target_price_segment(). That
    # fallback was exactly the circular loop ТЗ п.9 forbids: the target's
    # own price would decide its class, the class would decide its peer
    # group, and the peer group would decide whether the price was fair.
    # None makes L3+ compare against nearby same-room apartments without
    # a class filter, which п.9 explicitly permits.
    target_score, target_n, class_basis = building_class(
        target, cohort_pool, q25, q75, room_bounds,
        building_index, spatial_index,
    )
    target_class = _label_from_class_score(target_score)
    if target_score is None:
        class_basis = "недостаточно данных по зданию — сравнение без класса"

    # STEP 2 (ТЗ п.1): collect cohorts cumulatively.
    cohorts, stop_level, cohort_notes = collect_cohorts(
        target, cohort_pool, citywide_median, score_index,
        target_score=target_score, target_n=target_n,
        area_slopes=area_slopes, class_priors=class_priors,
        spatial_index=spatial_index, building_index=building_index,
    )
    combined_rows = [r for c in cohorts for r in c["rows"]]

    if not combined_rows:
        result = {
            "status": "no_comparables",
            "verdict": "НЕДОСТАТОЧНО ДАННЫХ",
            "verdict_reason": "нет сопоставимых объявлений в эталоне",
            "building_class": target_class,
            "building_class_basis": class_basis,
            "cohort_level": "none",
            "cohort_stop_level": "none",
            "cohort_size": 0,
            "review_flags": ";".join(review_flags(target, warnings, cohort_notes)),
            "cohort_notes": ";".join(cohort_notes),
            "confidence_weight": 0.0,
            "benchmark_confidence": 0.0,
            "seller_class": seller_cls,
            "seller_confidence": seller_conf,
            "seller_reason": seller_reason,
            "data_warnings": ";".join(warnings),
            "quality_evidence_score": quality_evidence_score(target),
            "data_confidence": round(quality_confidence(target), 3),
        }
        result.update(_empty_cohort_fields())
        return result

    # STEP 3 (ТЗ п.11, п.13): weight and credibility-adjust each cohort
    # on its own before any combination happens.
    cohort_infos = analyze_cohorts(
        cohorts, citywide_median, target["_rooms"],
        class_priors=class_priors, building_class_label=target_class,
    )
    base_price_m2 = blended_base_price(cohort_infos)

    if base_price_m2 is None:
        result = {
            "status": "no_comparables",
            "verdict": "НЕДОСТАТОЧНО ДАННЫХ",
            "verdict_reason": "в когортах нет корректных price/m²",
            "building_class": target_class,
            "building_class_basis": class_basis,
            "cohort_level": stop_level,
            "cohort_stop_level": stop_level,
            "cohort_size": len(combined_rows),
            "review_flags": ";".join(review_flags(target, warnings, cohort_notes)),
            "cohort_notes": ";".join(cohort_notes),
            "confidence_weight": 0.0,
            "benchmark_confidence": 0.0,
            "seller_class": seller_cls,
            "seller_confidence": seller_conf,
            "seller_reason": seller_reason,
            "data_warnings": ";".join(warnings),
            "quality_evidence_score": quality_evidence_score(target),
            "data_confidence": round(quality_confidence(target), 3),
        }
        result.update(_empty_cohort_fields())
        return result

    # STEP 4: diagnostics. Within-cohort spread has already shaped the
    # weights, so what the verdict is read against here is the part that
    # nothing has priced in yet — how much the cohorts disagree with each
    # other — plus a robust scale measured from the same centre as
    # diff_pct rather than from the raw union's median.
    d_within, d_between, d_total = dispersion_components(
        cohort_infos, base_price_m2
    )

    # robust_z is only meaningful when there is a spread to measure it
    # against. Because per-row weights are the cohort weight divided by
    # its row count, the scale is dominated by whichever cohort carries
    # the weight — and when that cohort holds one or two rows, the
    # "robust scale" is one absolute deviation, so z explodes and the
    # SUSPICIOUS_ROBUST_Z guard fires on arithmetic rather than evidence
    # (17 rows hit the +-10 clamp on the baseline). Below
    # MIN_ROWS_FOR_ROBUST_Z effective observations it is left undefined.
    total_effective = sum(c["effective_n"] for c in cohort_infos)
    robust_scale = weighted_robust_scale(cohort_infos, base_price_m2)
    if robust_scale and total_effective >= MIN_ROWS_FOR_ROBUST_Z:
        robust_z = (target["_price_m2"] - base_price_m2) / robust_scale
        robust_z = max(-10.0, min(10.0, robust_z))
    else:
        robust_z = None

    diff_pct = (
        (base_price_m2 - target["_price_m2"]) / base_price_m2
        if base_price_m2
        else None
    )

    benchmark_conf = confidence_from_cohorts(cohort_infos, d_between)
    data_conf = quality_confidence(target)

    # The verdict THRESHOLD is chosen by benchmark_confidence alone, not
    # by a blend with data_confidence — mixing it in was wrong on the
    # evidence: split by data_confidence quintile, the model's median
    # absolute error runs 0.114 / 0.100 / 0.090 / 0.120 / 0.112 — not
    # monotone, no signal. It was carrying 28% of the weight in a number
    # whose only job is to say how far the price may stray before the
    # call is made. data_confidence measures whether the LISTING can be
    # trusted, which is a real thing worth reporting and is reported, in
    # review_flags — it just says nothing about how accurate the
    # BENCHMARK is. (A combined_conf blend used to be computed here for
    # exactly that purpose and was never wired into the output — removed
    # rather than resurrected, since nothing downstream expects it.)
    verdict, verdict_reason = verdict_from_diff(
        diff_pct, benchmark_conf, target, warnings, robust_z=robust_z
    )

    value_score = value_from_diff(diff_pct)

    pq_score = price_quality_score(
        diff_pct,
        quality_evidence_score(target),
        benchmark_conf,
        data_conf,
    )
    pq_label = price_quality_label(
        diff_pct,
        quality_evidence_score(target),
        warnings,
    )

    total_w = sum(c["weight"] for c in cohort_infos) or 1.0
    l12_share = sum(
        c["weight"] for c in cohort_infos if c["level"] == "1-2"
    ) / total_w
    # The level that actually produced the number, as opposed to the
    # level the cascade happened to stop at.
    dominant_level = max(cohort_infos, key=lambda c: c["weight"])["level"]

    result = {
        "status": "scored",
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "price_segment": target_price_segment(target, pool, q25, q75),
        "building_class": target_class,
        "building_class_score": (
            round(target_score, 3) if target_score is not None else None
        ),
        "building_class_n": target_n,
        "building_class_basis": class_basis,
        # The level that actually produced the number (by weight in the
        # blend) — see dominant_level above. NOT the level the cascade
        # stopped at; that is cohort_stop_level, right below. The two
        # differ whenever a later, wider level ends up carrying more
        # weight than L1/2 despite the cascade having stopped earlier —
        # rare (L1/2's LEVEL_WEIGHT dominates by construction), but this
        # field is what should be read as "where the number came from".
        "cohort_level": dominant_level,
        # The level the cascade STOPPED at, and the total number of
        # distinct listings pooled across every cohort used — not the
        # size of a single winning level (ТЗ п.15/16). The per-level
        # detail lives in cohort_breakdown / l*_n / l*_median / l*_weight.
        "cohort_stop_level": stop_level,
        "cohort_size": len(combined_rows),
        # Private (`_`-prefixed) so no CSV writer picks it up — this is a
        # list of dicts for the notification layer, not a table column.
        "_cohort_members": cohort_members(cohorts)[0],
        # Split, because the two mean different things: cohort_dispersion
        # is the honest overall spread of the evidence, while
        # cohort_disagreement isolates the part that the weights have NOT
        # already accounted for — cohorts contradicting each other. A
        # high disagreement with a low within-spread is the signature of
        # a bad cohort match, and it used to be invisible.
        "cohort_dispersion": round(d_total, 4) if d_total is not None else None,
        "cohort_disagreement": round(d_between, 4) if d_between is not None else None,
        "prior_price_m2": (
            round(cohort_infos[0]["prior"], 1)
            if cohort_infos and cohort_infos[0].get("prior")
            else None
        ),
        # Share of the final benchmark that came from the tightest
        # (same-building) cohort. Replaces the old meaning of this field
        # (the credibility weight of a single blend step) with the
        # directly comparable question: how much of this number is
        # actually building-level evidence?
        "confidence_weight": round(l12_share, 3),
        "benchmark_confidence": benchmark_conf,
        # Reported for diagnostics only — it no longer adjusts the price.
        "is_extreme_floor": target["_is_extreme_floor"],
        "base_price_m2": round(base_price_m2, 1),
        "diff_pct": round(diff_pct, 4) if diff_pct is not None else None,
        "robust_z": round(robust_z, 2) if robust_z is not None else None,
        "value_score": round(value_score, 1) if value_score is not None else None,
        "quality_evidence_score": quality_evidence_score(target),
        "price_quality_score": pq_score,
        "price_quality_label": pq_label,
        "data_confidence": round(data_conf, 3),
        "seller_class": seller_cls,
        "seller_confidence": seller_conf,
        "seller_reason": seller_reason,
        "data_warnings": ";".join(warnings),
        # Kept apart from `verdict` on purpose: these say the INPUT needs
        # a human, never that the price is good or bad.
        "review_flags": ";".join(review_flags(target, warnings, cohort_notes)),
        "cohort_notes": ";".join(cohort_notes),
    }
    result.update(_cohort_output_fields(cohort_infos))
    return result


def verify_weight_hierarchy():
    """Check the claim made in the LEVEL_WEIGHT comment: no attainable
    (size, homogeneity) combination lets a wider cohort outweigh a
    tighter one (ТЗ п.2, п.11, п.17).

    Compares each level's BEST case (huge, perfectly homogeneous, fully
    direct) against the next-tighter level's WORST case. The worst case
    now includes the purity discount: an L1/2 built entirely out of
    cross-room comparables rescaled through citywide medians is penalised
    twice over — once through RESCALE_TRUST as a weight multiplier and
    once through the effective count it feeds into quantity_factor — and
    the ladder has to stay intact even there. Without this case in the
    search, the check would pass while the hierarchy silently broke for
    exactly the cohorts most at risk of being wrong.

    Returns a list of violation strings — empty means the hierarchy holds.
    """
    order = ["1-2", "3", "4", "5", "6"]
    violations = []
    for tighter, wider in zip(order, order[1:]):
        candidates = []
        # purity < 1 is only attainable on L1/2, which is the only level
        # that can contain rescaled rows.
        purities = [1.0, RESCALE_TRUST] if tighter == "1-2" else [1.0]
        for purity in purities:
            for raw_n in (1, 2, 3, 4):
                eff_n = raw_n * purity
                # Only ATTAINABLE combinations: at n=1 robust_stats()
                # cannot measure dispersion at all and returns None, so
                # pairing n=1 with a huge dispersion would test a state
                # the code can never reach and force the ladder to be
                # tuned against a phantom.
                dispersions = [None] if raw_n < 2 else [0.0, 10.0]
                for dispersion in dispersions:
                    candidates.append(
                        cohort_weight(tighter, eff_n, dispersion, purity)
                    )
        worst_tight = min(candidates)
        best_wide = cohort_weight(wider, 10_000, 0.0, 1.0)
        if best_wide >= worst_tight:
            violations.append(
                f"L{wider} (max {best_wide:.5f}) может перевесить "
                f"L{tighter} (min {worst_tight:.5f})"
            )
    return violations


def report_accuracy(baseline_path, results=None):
    """Measure the model against the two trivial alternatives it has to beat.

    Every accuracy claim in this file used to compare one variant of the
    model against another variant of the model. That answers "did this
    change help", never "is the whole apparatus worth its complexity".
    The reference points here are:

      naive-1  citywide median price/m² for the room count;
      naive-2  median of the SAME BUILDING and same room count, leaving
               the row itself out, falling back to naive-1 — five lines
               of code;
      model    base_price_m2 as produced by this file.

    naive-2 and model are properly leave-one-out (naive-2 explicitly
    excludes the row via `x is not r`; model excludes it via
    same_target_id inside collect_cohorts). naive-1 is NOT: `citywide`
    is computed once over the whole pool and reused for every row,
    including itself. This matters in proportion to how large the
    room-count group is — on krisha_astana_baseline.csv the 1- and
    2-room groups run ~4100-4800 rows each, so leaving one out shifts
    their median by at most ~1/(2*4100) ≈ 0.0001, well below this
    report's precision. The tail is where it stops being negligible:
    the 9-room group has exactly 1 row, so that row's naive-1
    "prediction" is itself. That's a real inflation of naive-1's
    apparent accuracy for those specific rows — but describe() below
    pools ALL rows into one median/p75, so a handful of self-compared
    large-apartment rows (room counts 6-9, ~27 of ~11 300 total)
    cannot move the reported headline numbers. Not fixed with a true
    leave-one-out naive-1 because the aggregate report is insensitive
    to it either way, and a LOO-median-of-a-large-array implementation
    is more moving parts than this self-check is worth.

    Measured on krisha_astana_baseline.csv the numbers came out 0.160 /
    0.125 / 0.106 median absolute relative error: the five-line version
    captures about 70% of the total available improvement, and
    everything else in this module buys the remaining 30%. That is
    worth knowing before adding the next mechanism, and it is why the
    v5 work went into calibration rather than into more modelling — a
    knob sweep over the class threshold, CREDIBILITY_K,
    FULL_CREDIBILITY_N, the L3 weight and disabling L5 entirely moved
    the median error by less than 0.003 in every direction, i.e. the
    residual is not reachable from here.
    """
    rows = [enrich(r) for r in load_rows(baseline_path)]
    mark_price_outliers(rows)
    pool = [r for r in rows if not r["_excluded_baseline"]]
    citywide = citywide_median_by_rooms(pool)

    by_building = defaultdict(list)
    for r in pool:
        key = _building_key(r)
        if key is not None:
            by_building[(key, r["_rooms"])].append(r)

    naive1, naive2 = [], []
    for r in pool:
        truth = r["_price_m2"]
        city = citywide.get(r["_rooms"])
        if city:
            naive1.append(abs(city - truth) / city)
        key = _building_key(r)
        others = [
            x["_price_m2"] for x in by_building.get((key, r["_rooms"]), [])
            if x is not r and x["_price_m2"] is not None
        ] if key is not None else []
        predicted = statistics.median(others) if others else city
        if predicted:
            naive2.append(abs(predicted - truth) / predicted)

    def describe(label, errors):
        if not errors:
            return f"{label}: нет данных"
        errors = sorted(errors)
        return (
            f"{label}: median={errors[len(errors) // 2]:.4f} "
            f"p75={errors[int(0.75 * len(errors))]:.4f} n={len(errors)}"
        )

    print(describe("наивный-1 (город × комнатность)", naive1))
    print(describe("наивный-2 (свой дом × комнатность)", naive2))
    if results:
        model = [
            abs(r["diff_pct"]) for r in results
            if r.get("status") == "scored" and r.get("diff_pct") is not None
        ]
        print(describe("модель", model))
        if model and naive2:
            gain = statistics.median(sorted(naive2)) - statistics.median(sorted(model))
            print(f"выигрыш модели над наивным-2: {gain:+.4f}")


def check_audit_trail(results, tolerance=0.02):
    """Can a reader reproduce base_price_m2 from the published columns?

    In v4 they could not: the blend used each cohort's credibility-
    adjusted median while the CSV printed the raw one, so recomputing the
    weighted average from the published numbers missed by more than 2% on
    36% of rows. An audit trail that does not reconstruct the answer is
    decoration. This turns that into a check that fails loudly.
    """
    prefixes = list(_COHORT_FIELD_PREFIX.values())
    bad = 0
    checked = 0
    for r in results:
        if r.get("status") != "scored" or not r.get("base_price_m2"):
            continue
        total = 0.0
        acc = 0.0
        for prefix in prefixes:
            weight = r.get(f"{prefix}_weight")
            value = r.get(f"{prefix}_adjusted_median")
            if weight and value:
                acc += weight * value
                total += weight
        if total <= 0:
            continue
        checked += 1
        if abs(acc / total - r["base_price_m2"]) / r["base_price_m2"] > tolerance:
            bad += 1
    return bad, checked


def write_output(path, rows, results):
    base_fields = []
    for row in rows:
        for k in row.keys():
            if not k.startswith("_") and k not in base_fields:
                base_fields.append(k)

    fields = base_fields + [
        x for x in OUTPUT_EXTRA_FIELDNAMES if x not in base_fields
    ]

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row, result in zip(rows, results):
            out = {k: v for k, v in row.items() if not k.startswith("_")}
            out.update(result)
            writer.writerow(out)


def mark_price_outliers(rows):
    """Exclude physically impossible and statistically impossible
    price/m² values from the REFERENCE pool.

    The module docstring promised this ("жёстко невозможные/сломанные
    числовые данные"), but no such check existed anywhere: the only
    filter was `price_m2 is None`. A row with a mistyped price therefore
    entered the pool and moved the citywide medians, the per-room
    quartiles that define building class, and the shrinkage prior — i.e.
    one bad row shifted the benchmark of every listing, not just its own.

    Two layers. The absolute bounds catch unit errors. The robust test is
    relative to the pool itself, on a log scale with a MAD spread, so it
    adapts to whichever market the tool is pointed at instead of encoding
    Astana rents as a constant. Returns the number of rows excluded.
    """
    values = [
        math.log(r["_price_m2"]) for r in rows
        if r.get("_price_m2") and r["_price_m2"] > 0
    ]
    lo = hi = None
    if len(values) >= 50:
        centre = statistics.median(values)
        mad = statistics.median([abs(v - centre) for v in values])
        if mad > 0:
            spread = 1.4826 * mad
            lo = math.exp(centre - PRICE_OUTLIER_MAD_Z * spread)
            hi = math.exp(centre + PRICE_OUTLIER_MAD_Z * spread)

    excluded = 0
    for r in rows:
        pm2 = r.get("_price_m2")
        square = to_float(r.get("square_m2"))
        bad = False
        if pm2 is None or pm2 < PRICE_M2_ABS_MIN or pm2 > PRICE_M2_ABS_MAX:
            bad = True
        elif lo is not None and not (lo <= pm2 <= hi):
            bad = True
        elif square is not None and not (SQUARE_ABS_MIN <= square <= SQUARE_ABS_MAX):
            bad = True
        if bad and not r["_excluded_baseline"]:
            r["_excluded_baseline"] = True
            excluded += 1
        r["_price_outlier"] = bad
    return excluded


# Baseline staleness thresholds, in days since the newest scraped_at.
#
# The baseline is frozen and nothing in this repository rebuilds it, so
# it can only get older. Nothing else in this file would notice: cohorts
# are built from whatever rows the pool contains, and
# benchmark_confidence measures how DENSE a cohort is, not how RECENT —
# a stale baseline yields the same high confidence it always did while
# comparing today's asking prices against last season's. Rent moves
# seasonally, so that failure is silent and directional, not random.
BASELINE_STALE_WARN_DAYS = 45
BASELINE_STALE_ALERT_DAYS = 90


def baseline_age_days(rows):
    """Days since the most recent scraped_at in the pool, or None.

    scraped_at is when the row was actually read off the site, which is
    what "how old is this benchmark" means — added_at is when the seller
    posted, and a freshly scraped listing can carry an old added_at.
    """
    newest = None
    for r in rows:
        raw = (r.get("scraped_at") or "").strip()
        if not raw:
            continue
        text = raw.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if newest is None or dt > newest:
            newest = dt
    if newest is None:
        return None
    return (datetime.now(timezone.utc) - newest).days


def check_baseline_freshness(rows):
    """Warn when the frozen baseline has aged out. Returns age in days."""
    age = baseline_age_days(rows)
    if age is None:
        print("⚠️ Эталон: не удалось определить возраст (нет разбираемого scraped_at)")
        return None
    if age >= BASELINE_STALE_ALERT_DAYS:
        print(
            f"🛑 Эталон устарел: свежайшая запись {age} дн. назад "
            f"(порог {BASELINE_STALE_ALERT_DAYS}). Вердикты сравнивают "
            f"сегодняшние цены с устаревшим рынком — пересоберите baseline."
        )
    elif age >= BASELINE_STALE_WARN_DAYS:
        print(
            f"⚠️ Эталон стареет: свежайшая запись {age} дн. назад "
            f"(порог {BASELINE_STALE_WARN_DAYS})."
        )
    return age


def prepare_baseline(path):
    rows = [enrich(r) for r in load_rows(path)]
    dropped = mark_price_outliers(rows)
    if dropped:
        print(f"Отброшено из эталона по санитарным границам цены: {dropped}")
    check_baseline_freshness(rows)
    usable = [r for r in rows if not r["_excluded_baseline"]]
    return rows, usable


def run(input_path, output_path, baseline_path=None, soft_target=True):
    hierarchy_problems = verify_weight_hierarchy()
    if hierarchy_problems:
        print("⚠️ Нарушена иерархия весов когорт: " + "; ".join(hierarchy_problems))

    rows = [enrich(r) for r in load_rows(input_path)]
    print(f"Загружено записей: {len(rows)}")

    if baseline_path:
        baseline_rows, usable_pool = prepare_baseline(baseline_path)
        print(
            f"Эталон: {baseline_path}; всего={len(baseline_rows)}, "
            f"usable={len(usable_pool)}"
        )
    else:
        # Backward-compatible offline mode.
        mark_price_outliers(rows)
        usable_pool = [
            r for r in rows
            if not r["_excluded_baseline"]
        ]
        print(
            "⚠️ --baseline не задан: используется self-referential pool. "
            "Для реального мониторинга это НЕ рекомендуется."
        )

    citywide_median = citywide_median_by_rooms(usable_pool)
    q25, q75 = price_segment_boundaries(usable_pool)
    # Per-room-count citywide quartiles (ТЗ п.6/п.10): each room count of
    # a building is judged against the city's market for THAT room count.
    room_bounds = room_segment_boundaries(usable_pool)
    score_index = building_scores_index(usable_pool, q25, q75, room_bounds)
    # Prior for the per-cohort credibility shrink, resolved by
    # (room count x building class) instead of room count alone.
    class_priors = prior_medians_by_rooms_class(usable_pool, score_index)
    # Price/m² elasticity with respect to floor area, per room count.
    area_slopes = area_slopes_by_rooms(usable_pool)
    spatial_index = SpatialIndex(usable_pool)
    building_index = BuildingIndex(usable_pool)
    if area_slopes:
        shown = ", ".join(
            f"{rooms}к={slope:+.2f}"
            for rooms, slope in sorted(area_slopes.items(), key=lambda kv: str(kv[0]))
        )
        print(f"Поправка на площадь (наклон log-log): {shown}")

    results = [
        score_row(
            r, usable_pool, citywide_median, score_index, q25, q75,
            soft_target=soft_target, room_bounds=room_bounds,
            class_priors=class_priors, area_slopes=area_slopes,
            spatial_index=spatial_index, building_index=building_index,
        )
        for r in rows
    ]

    write_output(output_path, rows, results)

    scored = [r for r in results if r.get("status") == "scored"]
    verdicts = {}
    for r in scored:
        verdicts[r.get("verdict")] = verdicts.get(r.get("verdict"), 0) + 1

    print(f"✅ {output_path}: scored={len(scored)}")
    print(f"   verdicts={verdicts}")

    bad, checked = check_audit_trail(results)
    if checked:
        status = "✅" if bad == 0 else "⚠️"
        print(
            f"{status} Аудит-трейл: base_price_m2 воспроизводится из колонок "
            f"l*_adjusted_median × l*_weight у {checked - bad}/{checked} строк"
        )

    if baseline_path:
        report_accuracy(baseline_path, results)
        print(
            "ℹ️ Incoming rows не чистятся по owner/photo/red_flags: "
            "эти признаки отражены как warnings/confidence."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="krisha_astana_analyzed.csv")
    parser.add_argument("--output", default="krisha_astana_benchmark.csv")
    parser.add_argument("--baseline", default=None)
    parser.add_argument(
        "--strict-target",
        action="store_true",
        help="использовать старое жёсткое поведение для target; "
             "для новых объявлений НЕ включать",
    )
    args = parser.parse_args()

    try:
        run(
            args.input,
            args.output,
            args.baseline,
            soft_target=not args.strict_target,
        )
    except FileNotFoundError as e:
        print(f"❌ Файл не найден: {e}")
        sys.exit(1)