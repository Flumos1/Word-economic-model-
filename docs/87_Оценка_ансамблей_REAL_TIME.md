# 87. Оценка ансамблей на REAL_TIME (этап 7) — официальный v2

Дата: 2026-11-12. Статус: **отчёт об оценке, допуск не присвоен** (все четыре кандидата —
`CANDIDATE`). Официальные документы:
[protocols/ensemble_evaluation_report_v2.json](../protocols/ensemble_evaluation_report_v2.json)
(SHA-256 `3ac5bcb4a25e1c22bd9a493075522dd1e77744a0325321d66a8c3e91f7757450`) и
[protocols/ensemble_evaluation_summary_v2.json](../protocols/ensemble_evaluation_summary_v2.json)
(SHA-256 `dbfe83e4d1ae945614ccdae14fc442fadf6668e1f438c2f0ace811fe51026fa0`).

**Старые отчёты недействительны**: `protocols/ensemble_evaluation_report.json` /
`..._summary.json` (v1) использовали сырые уровни рядов вместо целей протокола (D-147) и
сохранены только как история; в выводах не используются. V1-раздел заменён этой полной сводкой.

## 1. Постановка

* REAL_TIME-оценка всех четырёх зарегистрированных ансамблей (`protocols/candidates_2026-11-11.json`,
  батч 2026-11-12, регистрация до оценки): `ENSEMBLE_EQUAL`, `ENSEMBLE_INVERSE_MSE`,
  `ENSEMBLE_BAYES`, `ENSEMBLE_ADAPTIVE`; `candidate_coverage.evaluated = 4`, `not_evaluated = []`.
* Цели: **T1_CPI_YOY h=1**, **T2_GDP_GROWTH h=0**; REAL_TIME-начала с 1996-12: W1 — 121
  (1996-12-31…2006-12-31), W2 — 156 (2007-01-31…2019-12-31), `data_class = REAL_TIME`,
  pseudo-начал 0.
* Прогнозы point-in-time (`amfe.forecast`, `mode=auto`); факты — **цели протокола из первой
  публикации** (D-147); вторичные — винтаж через 12 месяцев; компаратор — замороженный состав и
  смесь CDF.
* T2-смесь: исправление экстремальных хвостов (docs/94, D-148 track R); CRPS T2 вычислим и
  отражён в отчёте.
* **Multiplicity: одно семейство Romano–Wolf (FWER 0.10, 10 000 репликаций, seed 20261111) на
  все четыре кандидата — `family_size = 4`** для обеих пар. Расширенный проход (INVERSE_MSE,
  BAYES) выполнен отдельно (per-candidate family_size=1, файлы в
  [protocols/extended_pass/](../protocols/extended_pass/)), а официальная таблица adjusted p-value
  пересчитана совместно; поэтому per-candidate evidence сохраняет свои family-1/2 p-values как
  сырые артефакты, а официальные p-values — в report/summary v2.
* **C4 W1 — INCONCLUSIVE** (8 рецессионных начал < 12) у всех кандидатов/обеих целей: стадия
  `CANDIDATE → SHADOW_TEST` недостижима. **C6 отсутствует** (`NO_RESOLVED_FORECASTS`), **C3** не
  реализован (негейтовый). Никаких присвоений допуска нет.
* Время: T1 h=1 — 761.8 с, T2 h=0 — 776.7 с (два прогона параллельно); узкое место — плотные
  винтажи/`DataContext` на каждого члена и origin.

## 2. T1_CPI_YOY, h=1 — все четыре кандидата (REAL_TIME, W1 121 / W2 156, family_size=4)

Порог C1 = 0.97. Относительные потери = кандидат / компаратор.

| Кандидат | Статус | C1 | C2 (RW adj p, худший) | C4 W1 / W2 | C5 | C6 | RMSE отн. W1 / W2 | CRPS отн. W1 / W2 |
|---|---|---|---|---|---|---|---|---|
| ENSEMBLE_INVERSE_MSE | CANDIDATE | **pass** | fail (0.1285) | INCONCLUSIVE / PASS,PASS | pass | нет | 0.94727277 / 0.94796404 | 0.96766382 / 0.95359295 |
| ENSEMBLE_ADAPTIVE | CANDIDATE | fail | fail (0.2144) | INCONCLUSIVE / FAIL,FAIL | pass | нет | 0.94893504 / 0.96926912 | 0.97448474 / 0.97831677 |
| ENSEMBLE_BAYES | CANDIDATE | fail | fail (0.9631) | INCONCLUSIVE / FAIL,FAIL | pass | нет | 0.95892720 / 1.01179360 | 0.96848346 / 1.01897596 |
| ENSEMBLE_EQUAL | CANDIDATE | fail | fail (0.9631) | INCONCLUSIVE / FAIL,PASS | pass | нет | 1.00000000 / 1.00000000 | 1.00000000 / 1.00000000 |

`ENSEMBLE_INVERSE_MSE` — единственный кандидат, проходящий **C1 по всем потерям и обоим окнам**
(RMSE −5.3%/−5.2%, CRPS −3.2%/−4.6%), но C2 не проходит по W1/W2-adjusted p (0.0995–0.1285) и
C4 W1 INCONCLUSIVE блокирует стадию → `CANDIDATE`. `ENSEMBLE_ADAPTIVE` проходит C1 только по RMSE,
по CRPS не хватает (0.9745/0.9783 ≥ 0.97). `ENSEMBLE_EQUAL` механически равен компаратору (rel=1.0).

## 3. T2_GDP_GROWTH, h=0 — все четыре кандидата (REAL_TIME, W1 121 / W2 156, family_size=4)

| Кандидат | Статус | C1 | C2 (RW adj p, худший) | C4 W1 / W2 | C5 | C6 | RMSE отн. W1 / W2 | CRPS отн. W1 / W2 |
|---|---|---|---|---|---|---|---|---|
| ENSEMBLE_EQUAL | CANDIDATE | fail (W1), pass W2 | **pass** (0.0363) | INCONCLUSIVE / PASS,PASS | pass | нет | 0.98540139 / 0.96171386 | 0.98425338 / 0.95964067 |
| ENSEMBLE_INVERSE_MSE | CANDIDATE | fail (W1), pass W2 | fail (0.2572) | INCONCLUSIVE / PASS,PASS | pass | нет | 0.99029211 / 0.94117606 | 0.98607331 / 0.93887198 |
| ENSEMBLE_ADAPTIVE | CANDIDATE | fail (W1), pass W2 | fail (0.5855) | INCONCLUSIVE / PASS,PASS | pass | нет | 0.99847463 / 0.94687976 | 0.99513716 / 0.94659170 |
| ENSEMBLE_BAYES | CANDIDATE | fail | fail (0.9474) | INCONCLUSIVE / PASS,PASS | **fail** | нет | 1.03472799 / 0.98205527 | 1.03094626 / 0.98111812 |

На W2 все четыре кандидата лучше компаратора по обеим потерям (лучший — INVERSE_MSE: RMSE −5.9%,
CRPS −6.1%), но на W1 улучшение недостаточно (порог 0.97), кроме отдельных потерь; C1 требует
прохождения каждой потери на каждом окне. `ENSEMBLE_EQUAL` проходит C2 (adj p 0.0037–0.0363), но
C1 блокируется W1. T2-кандидаты могут включать SPF на SPF-дедлайн-началах (компаратор SPF не
содержит), поэтому для допуска нужна sensitivity без SPF (docs/95) — в этом прогоне не
пересчитывалась.

## 4. Согласованность evidence и кодовых хешей

* `protocols/extended_pass/` (INVERSE_MSE, BAYES) и корневые per-candidate отчёты (EQUAL,
  ADAPTIVE) сверены с joint-прогоном: **статусы и RMSE совпадают точно**, CRPS может отличаться
  ≤1.2·10⁻⁴ (относительно) из-за weighted-mixture robust path, добавленного в
  `src/amfe/ensemble/mixture.py` коммитом `571891d` уже после генерации evidence; официальные
  CRPS/p-values — из joint-прогона (family_size=4). `evidence_consistency` в отчёте — `true` по
  RMSE/статусам/допуску.
* `code_hash_check`: `src/amfe/ensemble/mixture.py` (текущий `14971818…`) не совпадает с
  зарегистрированным `697792a7…` — Lead должен **перепинить code_files** кандидатов (изменение
  касается robust-path смеси, не весов). Остальные 5 файлов `src/amfe/ensemble/*` совпадают.

## 5. Ограничения

1. **C4 W1 INCONCLUSIVE** (8 рецессионных начал < 12) у всех кандидатов и обеих целей — структурный
   блокер `CANDIDATE → SHADOW_TEST`; варианты — [docs/88](88_C4_W1_варианты_и_черновик_v5.md).
2. **C6 отсутствует** (журнал без разрешённых прогнозов); **C3** не реализован (негейтовый).
3. T2: улучшение опирается на SPF-член в кандидате (не в компараторе); нужна sensitivity без SPF
   (docs/95). SPF-значения — сторонние, не коммитятся (data/raw/spf, git-ignored), атрибуция
   Philadelphia Fed обязательна.
4. Первые ~12 начал W1: калибровочная история может содержать PSEUDO_REAL_TIME прогнозы членов,
   требующих ставок (винтажи GS10/TB3MS с 1996-12) — свойство зарегистрированного кода, не менялось.
5. Все четыре кандидата — `CANDIDATE`; допуск не присваивался, и при C4/W1 + отсутствии C6 он
   невозможен.

## 6. Файлы и хеши (LF-normalised)

| Файл | SHA-256 |
|---|---|
| `protocols/ensemble_evaluation_report_v2.json` | `3ac5bcb4a25e1c22bd9a493075522dd1e77744a0325321d66a8c3e91f7757450` |
| `protocols/ensemble_evaluation_summary_v2.json` | `dbfe83e4d1ae945614ccdae14fc442fadf6668e1f438c2f0ace811fe51026fa0` |
| `protocols/ensemble_eval_ENSEMBLE_EQUAL_T1_h1_v2.json` | `2f94893fbf38e7e07483bfc1dd086617938bce48d6121fa9ca34d3b7bdef9269` |
| `protocols/extended_pass/ensemble_eval_ENSEMBLE_INVERSE_MSE_T1_h1_v2.json` | `92340167cad95fc22c9e97adb5bb81c64439a04b4160ce5abc940ca60fadbc0b` |
| `protocols/extended_pass/ensemble_eval_ENSEMBLE_BAYES_T1_h1_v2.json` | `45ecbbb1931452973f187c61e359fae608ec3fb39a2dfab6e054876b98e4a3cc` |
| `protocols/ensemble_eval_ENSEMBLE_ADAPTIVE_T1_h1_v2.json` | `1f72553299b3eb565a1c576ed67ac920f7d435d3315e20badf7c02be8546455a` |
| `protocols/ensemble_eval_ENSEMBLE_EQUAL_T2_h0_v2.json` | `6e1613e3b4bfee3b6c4be46b25c62b3aa71f4885432719749a8332fc83b3fdf0` |
| `protocols/extended_pass/ensemble_eval_ENSEMBLE_INVERSE_MSE_T2_h0_v2.json` | `03efa398d8603e298de61097b4d97c6199d8d8ad19738fb6d72bf99cb83fd6b6` |
| `protocols/extended_pass/ensemble_eval_ENSEMBLE_BAYES_T2_h0_v2.json` | `07df98e1ac30f0bfb12cae0986795f6cc817d09ffe2fedb6c6839bd173fabb13` |
| `protocols/ensemble_eval_ENSEMBLE_ADAPTIVE_T2_h0_v2.json` | `f447828d7ebcb0e34ba75dce578a174217d7f8823bb404e5e161c6a8fe74847b` |
| `tests/test_ensemble_evaluation_v2.py` | `7a1e9d04065ac70b72268084ccb043333bad8950be08ba8ee1e0a8e95833a071` |

Отчёты хранят только дайджесты рядов/прогнозов и производные метрики (licence policy, docs/84);
v1-отчёты не перезаписаны. Тесты: `python tests/test_ensemble_evaluation_v2.py` (структура,
дайджест-only, provenance, family_size=4, coverage=4, согласованность report/summary,
evidence consistency, T2). 

## 7. Следующие шаги

1. Lead: перепинить `code_files` кандидатов (`src/amfe/ensemble/mixture.py`).
2. C4/W1: решение по вариантам [docs/88](88_C4_W1_варианты_и_черновик_v5.md) — без него SHADOW_TEST
   недостижим для всех кандидатов.
3. T2 sensitivity без SPF (docs/95) и открытие журнала (C6).
4. Прогнать остальные пары (T2 h=1,2,4; T1 h=3,6,12; T3 h=3,6,12).
5. Не использовать v1-отчёты и старые T2-числа (уровни + хвосты смеси) ни в каких выводах.
