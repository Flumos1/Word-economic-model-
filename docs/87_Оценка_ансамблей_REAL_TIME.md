# 87. Оценка ансамблей на REAL_TIME (этап 7) — v2

Дата: 2026-11-12. Статус: **отчёт об оценке v2, допуск не присвоен** (только `CANDIDATE`).
Актуальные документы:
[protocols/ensemble_evaluation_report_v2.json](../protocols/ensemble_evaluation_report_v2.json)
(SHA-256 `73d7f502ab6993066b658f1fc19d7a0f9f9aa4598e7a6c039f0277a292cbc0e4`) и
[protocols/ensemble_evaluation_summary_v2.json](../protocols/ensemble_evaluation_summary_v2.json)
(SHA-256 `32d83dafdc48f06e83680f49f48be6f57e581d35203699807deab3c6163e8cce`). Старые `protocols/ensemble_evaluation_report.json` /
`..._summary.json` **недействительны** (v1: actuals были сырыми уровнями ряда вместо целей
протокола, D-147) и сохранены только как история; они не используются.

## 1. Постановка

* REAL_TIME-оценка зарегистрированных ансамблей (`protocols/candidates_2026-11-11.json`, батч
  2026-11-12, регистрация до оценки): `ENSEMBLE_EQUAL`, `ENSEMBLE_INVERSE_MSE`,
  `ENSEMBLE_BAYES`, `ENSEMBLE_ADAPTIVE`.
* Цели: **T1_CPI_YOY h=1** и **T2_GDP_GROWTH h=0**.
* Окна W1/W2, только REAL_TIME-начала с 1996-12 (винтажи ALFRED GS10/TB3MS): W1 — 121 начало
  (1996-12-31…2006-12-31), W2 — 156 (2007-01-31…2019-12-31), `data_class = REAL_TIME`,
  pseudo-начал 0.
* Прогнозы point-in-time (`amfe.forecast`, `mode=auto`); факты — **цели протокола из первой
  публикации** (D-147: первый винтаж, содержащий период, и цель, пересчитанная из того же
  винтажа); вторичные факты — винтаж через 12 месяцев; компаратор — замороженный состав и смесь
  CDF (без правок).
* T2-смесь: исправление экстремальных хвостов (D-148 track R, [docs/94](94_Исправление_T2_смеси.md));
  на текущих данных robust == plain, CRPS T2 вычислим; отчёт помечает `mixture_fix.available=true`.
* Multiplicity: Romano–Wolf stepdown, FWER 0.10, 10 000 репликаций, seed 20261111, семьи по 2
  кандидата (EQUAL, ADAPTIVE) на пару — из-за жёсткого лимита времени; `INVERSE_MSE` и `BAYES`
  в этом проходе **не оценены** и явно помечены в отчёте (`candidate_coverage.not_evaluated`) —
  это следующий шаг, а не отказ.
* **C6 отсутствует** (`NO_RESOLVED_FORECASTS`), **C3** не реализован (негейтовый), **C4 W1 —
  INCONCLUSIVE** (8 рецессионных начал < 12): стадия `CANDIDATE → SHADOW_TEST` невозможна
  независимо от качества. Никаких присвоений допуска нет.

Время прогонов: T1 h=1 — 364.5 с (EQUAL 230.1 + ADAPTIVE 133.6), T2 h=0 — 366.3 с
(EQUAL 226.8 + ADAPTIVE 139.1); два прогона шли параллельно. Время тратится на построение
`DataContext` по плотным винтажам на каждого члена/origin; это ограничение скорости.

## 2. T1_CPI_YOY, h=1 — результаты v2

Порог C1 = 0.97. Относительные потери = кандидат / компаратор. Все значения — REAL_TIME.

| Кандидат | Статус | C1 | C1 по потерям (W1/W2) | C2 (RW adj p, худший) | C4 W1 / W2 | C5 | C6 | RMSE отн. W1 / W2 | CRPS отн. W1 / W2 |
|---|---|---|---|---|---|---|---|---|---|
| ENSEMBLE_ADAPTIVE (основной) | CANDIDATE | fail | RMSE pass/pass; CRPS fail/fail | fail (0.1807) | INCONCLUSIVE / FAIL,FAIL | pass | нет | **0.948935 / 0.969269** | 0.974487 / 0.978390 |
| ENSEMBLE_EQUAL (контроль) | CANDIDATE | fail | fail/fail | fail (0.7727) | INCONCLUSIVE / FAIL,PASS | pass | нет | 1.000000 / 1.000000 | 1.000000 / 1.000000 |

`ENSEMBLE_EQUAL` механически совпадает с компаратором (тот же состав, равные веса, та же смесь) →
относительная потеря ровно 1.0. `ENSEMBLE_ADAPTIVE` **значимо улучшает точечный прогноз**
(RMSE −5.1% на W1 и −3.1% на W2; C1-RMSE пройден) и близок к порогу по CRPS (0.9745/0.9784 при
0.97), но C2 не проходит по W2 (adj p 0.18/0.15), C4 W2 — FAIL по обеим потерям. C4 W1
INCONCLUSIVE — структурное ограничение данных.

Сверка с ранее выпущенным corrected-отчётом: RMSE воспроизведён **точно**
(0.948935/0.969269); CRPS отличается на ≤4·10⁻⁴ из-за исправления хвостов смеси (docs/94;
`adaptive_vs_corrected_report.max_crps_delta`), это отражено в отчёте.

## 3. T2_GDP_GROWTH, h=0 — результаты v2

| Кандидат | Статус | C1 | C1 по потерям (W1/W2) | C2 (RW adj p, худший) | C4 W1 / W2 | C5 | C6 | RMSE отн. W1 / W2 | CRPS отн. W1 / W2 |
|---|---|---|---|---|---|---|---|---|---|
| ENSEMBLE_EQUAL (контроль) | CANDIDATE | fail | W1 fail, W2 pass | pass (0.0269) | INCONCLUSIVE / PASS,PASS | pass | нет | 0.985401 / 0.961714 | 0.984253 / 0.959641 |
| ENSEMBLE_ADAPTIVE (основной) | CANDIDATE | fail | W1 fail, W2 pass | fail (0.4414) | INCONCLUSIVE / PASS,PASS | pass | нет | 0.998475 / **0.946880** | 0.995137 / **0.946592** |

T2 впервые оценён на корректных actuals и исправленной смеси. `ENSEMBLE_EQUAL` улучшает
компаратор на **W2 по всему**: RMSE −3.8%, CRPS −4.0% (C2 adj p 0.027/0.011), но на W1 улучшение
недостаточно (−1.5%, при пороге 0.97 нужно −3%), поэтому C1 не пройден на обоих окнах.
`ENSEMBLE_ADAPTIVE` на W2 лучше всех (RMSE/CRPS ≈ −5.3%), но на W1 ≈ 1.00 и C2 не проходит.
C4 W1 INCONCLUSIVE (общий блокер), C4 W2 PASS/PASS.

SPF: на T2 кандидат может включать SPF-медиану на SPF-дедлайн-началах (официальный маппинг —
[docs/86](86_SPF_маппинг_и_внешние_прогнозы.md)); компаратор SPF не содержит, поэтому для
допуска нужна отдельная sensitivity без SPF (трек U, docs/95). В этом прогоне sensitivity не
пересчитывалась — это ограничение результата, а не допуска.

## 4. Проверка кодовых хешей (важно для регистрации)

`code_hash_check` в v2-отчёте: из 6 файлов `src/amfe/ensemble/*` совпадают с регистрацией 5;
**`src/amfe/ensemble/sources.py` имеет расхождение** — зарегистрирован `a2f54eb4…`, текущий
`882c25c7…` (коммит `8aaa54a`, исправление доступности SPF по официальным датам релизов).
Оценка выполнена на текущем коде; для использования результатов в допуске Lead должен
**перепинить `code_files` кандидатов** (новая запись регистрации) — алгоритм весов/смеси не
менялся, изменение касается правил доступности SPF.

## 5. Ограничения

1. **C4 W1 INCONCLUSIVE** (8 рецессионных начал < 12) — блокирует `CANDIDATE → SHADOW_TEST`
   независимо от потерь; варианты в [docs/88](88_C4_W1_варианты_и_черновик_v5.md).
2. **C6 отсутствует** — в журнале нет разрешённых прогнозов; C3 не реализован.
3. **Неполный набор кандидатов**: `INVERSE_MSE` и `BAYES` не оценены в этом проходе (лимит
   времени); статусы v1 для них недействительны и не переносятся.
4. T2: улучшение частично опирается на SPF (кандидат), компаратор SPF не содержит; нужна
   sensitivity без SPF. Значения SPF — сторонние, не коммитятся (data/raw/spf, git-ignored).
5. Первые ~12 начал W1 используют калибровочную историю, где BVAR-члены могли быть
   PSEUDO_REAL_TIME (винтажи ставок начинаются 1996-12); текущие прогнозы REAL_TIME, но веса этих
   начал частично опираются на псевдо-историю — свойство зарегистрированного кода.
6. Все кандидаты — **CANDIDATE**; допуск не присваивался и не может быть присвоен при C4/W1 и
   отсутствии C6.

## 6. Файлы и соответствие

| Файл | SHA-256 (LF-normalised) |
|---|---|
| `protocols/ensemble_evaluation_report_v2.json` | `73d7f502ab6993066b658f1fc19d7a0f9f9aa4598e7a6c039f0277a292cbc0e4` |
| `protocols/ensemble_evaluation_summary_v2.json` | `32d83dafdc48f06e83680f49f48be6f57e581d35203699807deab3c6163e8cce` |
| `protocols/ensemble_eval_ENSEMBLE_ADAPTIVE_T1_h1_v2.json` | `d8e11379c4b221362065faa7e54d29d818d8c366bf6341aab717bef3e889a41f` |
| `protocols/ensemble_eval_ENSEMBLE_EQUAL_T1_h1_v2.json` | `bbe737c412796fe8626965493a095652b997e7aa3a9cb4412f7fb705358c0b89` |
| `protocols/ensemble_eval_ENSEMBLE_ADAPTIVE_T2_h0_v2.json` | `0d55b15aa1fccbb1e9eab46321e6d618e908e5c01683dfd2bedf94f34ff2ccf3` |
| `protocols/ensemble_eval_ENSEMBLE_EQUAL_T2_h0_v2.json` | `ed3dc6e99bfb46f2066904b671d056b40958b6cb15acf72da0d10f545afb1581` |
| `tests/test_ensemble_evaluation_v2.py` | `e0b65b9e3a33d3843abe90391c31d35e658cbb8caa1e25e4b919665980bbc610` |

Отчёты хранят только дайджесты рядов/прогнозов и производные метрики (licence policy, docs/84);
старые v1-файлы не перезаписаны. Тесты: `python tests/test_ensemble_evaluation_v2.py`
(структура, дайджест-only, provenance, хеши, статусы, согласованность сводки, T2, docs/87).

## 7. Следующие шаги

1. Lead: перепинить code_files (sources.py) в регистрации кандидатов.
2. Домерить `INVERSE_MSE` и `BAYES` на T1 h=1 и T2 h=0; добавить T2 sensitivity без SPF (docs/95).
3. C4/W1: решение по вариантам docs/88 (данные/протокол v5) — без него SHADOW_TEST недостижим.
4. Открыть журнал (C6) и прогнать остальные пары (T2 h=1,2,4; T1 h=3,6,12; T3 h=3,6,12).
5. Не использовать v1-отчёты и старые T2-числа (уровни + хвосты смеси) ни в каких выводах.

## Расширенный проход: все четыре зарегистрированных кандидата (2026-11-12)
После сокращённого первого прохода (EQUAL+ADAPTIVE) досчитаны ENSEMBLE_INVERSE_MSE и ENSEMBLE_BAYES на T1 h=1 и T2 h=0 в том же режиме REAL_TIME/исправленных actuals/robust-смеси. Допуск не присваивается: у всех CANDIDATE, C4 W1 INCONCLUSIVE (8 рецессионных начал < 12), C6 отсутствует.
### T1_CPI_YOY h=1 (расширенный проход)
| кандидат | C1 W1 | C1 W2 | C1 | C2 | C4 | C5 | статус |
|---|---|---|---|---|---|---|---|
| ENSEMBLE_INVERSE_MSE | {'RMSE': 0.94727, 'CRPS': 0.96766} | {'RMSE': 0.94796, 'CRPS': 0.95362} | pass | pass | fail | pass | CANDIDATE |
| ENSEMBLE_BAYES | {'RMSE': 0.95893, 'CRPS': 0.96849} | {'RMSE': 1.01179, 'CRPS': 1.0191} | fail | fail | fail | pass | CANDIDATE |

### T2_GDP_GROWTH h=0 (расширенный проход)
| кандидат | C1 W1 | C1 W2 | C1 | C2 | C4 | C5 | статус |
|---|---|---|---|---|---|---|---|
| ENSEMBLE_INVERSE_MSE | {'RMSE': 0.99029, 'CRPS': 0.98607} | {'RMSE': 0.94118, 'CRPS': 0.93887} | fail | fail | fail | pass | CANDIDATE |
| ENSEMBLE_BAYES | {'RMSE': 1.03473, 'CRPS': 1.03095} | {'RMSE': 0.98206, 'CRPS': 0.98112} | fail | fail | fail | fail | CANDIDATE |
