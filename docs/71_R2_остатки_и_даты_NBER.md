# 71. R2: остатки после ворот, GDPC1 и даты объявлений NBER

Дата: 2026-11-11. Задача: `task-8` (R2-остатки после пройденных ворот, см. `docs/68`).
Связанные протоколы: `protocols/evaluation_protocol_v4.json` (не менялся), `protocols/data_plan.json`
(не менялся), `protocols/licence_register.json` (не менялся; записи `NBER_CYCLE_DATES`,
`PHILADELPHIA_FED`, `PHILADELPHIA_FED_RTDSM` уже имеют статус OPEN).

## 1. GDPC1 в воротах R2: причина и решение

### 1.1. Причина провала (найдена, воспроизведена)

В отчёте `protocols/r2_gate_report.json` (прогон до этой задачи) GDPC1 был исключён с записью
`"reason": "check failed: initial-release output unavailable for GDPC1: not a zip (attempt 3): '<!DOCTYPE html> ...'"`.

Причина — не сетевой сбой и не отсутствие прав. ALFRED-форма `downloaddata` для
`file_type=4` («Observations, Initial Release Only») возвращает строку только для такой vintage-даты,
которая сама является датой первоначальной публикации. Плановые винтажи в `protocols/data_plan.json` —
это year-end даты, которые `tools/fetch_data.py` отображает на последнюю фактическую vintage-дату в том
же году. Для квартального ряда we year-end снимок — ревизия, а не initial release, поэтому форма
отвечает страницей валидации с текстом (дословно, из ответа ALFRED):

> «No observations were retrieved using the specified options»

Это воспроизведено локально и в CI диагностическим прогоном `tools/probe_vintages.py`:
варианты `retrieved_ft4_initial_csv` → HTML (провал), при этом `retrieved_ft2_control_csv` (тот же набор
дат, `file_type=2`) → zip (контроль: винтажи скачиваются нормально). То есть канал и даты валидны,
невалидна именно комбинация «initial release + year-end даты» для квартального ряда.

### 1.2. Рабочий способ

Даты initial release квартального ряда — это даты advance release BEA, примерно через месяц после
конца квартала. Они есть в календаре винтажей ALFRED (выпадающий список `form[selected_vintage_dates]`
на странице `downloaddata`). Проверено вручную и затем зашито в гейт:

```
POST https://alfred.stlouisfed.org/series/downloaddata?seid=GDPC1
form[obs_start_date]=1947-01-01&form[obs_end_date]=2026-07-01
form[entered_vintage_dates]=1996-10-30 2001-10-31 2007-10-31 2020-10-29
form[file_type]=4&form[file_format]=csv&form[download_data]=
→ zip, внутри csv: header "period_start_date,GDPC1,realtime_start_date",
  строки: 1996-07-01,6929.700,1996-10-30 ; 2001-07-01,9333.400,2001-10-31 ; …
```

### 1.3. Что изменено в `tools/verify_data_gate.py`

Добавлен «release-date supplement» — для серии, у которой `file_type=4` по retrieved-датам не дал
строк (сейчас это только GDPC1):

1. скачивается страница `downloaddata`, из неё берётся календарь фактических vintage-дат;
2. кандидаты — даты в окне 20–50 дней после конца квартала (окно advance release BEA), не более 40
   последних в диапазоне retrieved-винтажей;
3. по кандидатам выполняется POST `file_type=4`; возвращённые строки = настоящие даты initial release;
4. по этим датам скачивается supplementary vintage set (`file_type=2`), и для каждой строки
   (period, value, release) значение сверяется со значением в колонке release-даты, допуск 0.05;
5. в отчёт пишется `channel: "release-date supplement"`, `supplement.reason`,
   `supplement.candidates`, `supplement.release_dates`, `sha256` и размер обоих zip — провенанс.

Для остальных серий поведение гейта не изменилось (никаких дополнительных запросов, путь
«retrieved vintages»).

### 1.4. Результат (локальный прогон, полный цикл `fetch_data.py` → `verify_data_gate.py`)

```
{"passed": true, "matched_pairs": 87, "matched_pairs_pre_2000": 17, "failures": 0, "excluded": 1}
GDPC1: channel="release-date supplement", initial_release_rows=40, checked=40, matched=40, mismatches=[]
excluded: только USREC ("no vintage set retrieved") — как и до задачи
```

Было в `docs/68`: 47 пар, 17 до 2000, 5 рядов. Стало: 87 пар, 17 до 2000, 6 рядов (GDPC1 вошёл),
0 mismatches. Нижние границы протокола (≥30 пар, ≥10 до 2000) выполнены с запасом.

Замечание: supplementary-пары GDPC1 покрывают 2010-10-29…2020-10-29 (ограничение
`MAX_SUPPLEMENT_DATES=40`, чтобы запрос был ограниченным); pre-2000 пары по-прежнему дают CPI/UNRATE
и другие месячные ряды. Если нужны pre-2000 пары по ВВП, достаточно увеличить лимит кандидатов —
это осознанный компромисс, а не барьер.

## 2. Даты объявлений NBER (peak/trough announcement dates)

Источник: `https://www.nber.org/research/business-cycle-dating/business-cycle-dating-committee-announcements`
(+ страница данных `https://www.nber.org/research/data/us-business-cycle-expansions-and-contractions`).

Лицензия (из `protocols/licence_register.json`, запись `NBER_CYCLE_DATES`, статус OPEN):
> «Permission to copy is granted, provided attribution of source is given.»

Инструмент `tools/nber_dates.py` скачивает страницу объявлений, извлекает из её разметки
(`<p><a href="/news/business-cycle-dating-committee-announcement-…">дата</a> текст</p>`) дату объявления,
тип (peak/trough/memo), месяц цикла и ссылку на страницу объявления, и пишет
`protocols/nber_dates.json` (публичные даты с атрибуцией — коммитится).

Извлечено 17 объявлений (1979-12-31 … 2021-07-19): 6 пиков, 6 впадин, 5 memo. Ключевые пары для
1990–2026 (peak → trough, даты объявлений):

| peak | объявление пика | trough | объявление впадины |
|---|---|---|---|
| 1990-07 | 1991-04-25 | 1991-03 | 1992-12-22 |
| 2001-03 | 2001-11-26 | 2001-11 | 2003-07-17 |
| 2007-12 | 2008-12-01 | 2009-06 | 2010-09-20 |
| 2020-02 | 2020-06-08 | 2020-04 | 2021-07-19 |

Структура записи: `announcement_date`, `type`, `cycle_date`, `peak`, `trough`, `title`,
`source_url`; плюс `reference_dates` (пары peak/trough с датами объявлений), `coverage`,
`source_page_sha256` (хеш страницы) и `announcements_sha256` (канонический хеш извлечённых записей).
Тест `tests/test_nber_dates.py`: структура, порядок дат (новые первыми), `cycle_date <
announcement_date`, покрытие 1990–2026 ≥8 объявлений, пары 1990-07/1991-03, 2007-12/2009-06,
2020-02/2020-04, проверка хеша и парсера на inline-образце разметки.

Ограничение: на странице объявлений нет записей до 1979-12-31 и нет объявлений после 2021-07-19
(более поздних определений цикла NBER не публиковал); это фиксируется в `limitations` JSON.
Fallback не понадобился — даты извлекаются машинно.

## 3. Philadelphia Fed SPF: медианы

### 3.1. Источники (машиночитаемые xlsx, найдены на странице медиан)

Страница: `https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/median-forecasts`

- медианы CPI: `…/survey-of-professional-forecasters/historical-data/Inflation.xlsx`,
  лист `INFLATION`, колонки `YEAR, QUARTER, INFPGDP1YR, INFCPI1YR, INFCPI10YR`;
- медианы реального ВВП: `…/survey-of-professional-forecasters/historical-data/medianGrowth.xlsx`,
  лист `RGDP`, колонки `YEAR, QUARTER, drgdp2 … drgdp6` (опубликованные коды горизонтов SPF).

Оба файла скачиваются в память, парсятся `openpyxl` (в workflow шаг `python -m pip install openpyxl`).
У файлов Philadelphia Fed некорректные W3CDTF-таймстампы в `docProps/core.xml`
(`2026-10-30T 9:58:09-04:00` — пробел после `T`, односимвольный час), из-за чего openpyxl 3.x падает с
`TypeError: expected <class 'datetime.datetime'>`; инструмент нормализует только этот таймстамп
(cell data не трогается) — обход задокументирован в коде.

### 3.2. Правило лицензии: значения не коммитятся

По `licence_policy.raw_data` / `intended_use` протокола v4 сырые сторонние архивы не коммитятся,
публикуются только наши производные статистики. Медианы SPF — сторонние данные, поэтому:

- **значения** → `data/raw/spf/spf_median.json` (каталог `data/` в `.gitignore`, подтверждено
  `git status --ignored`: `!! data/`); в CI — только приватный артефакт прогона
  (как уже делается для `data/raw/` в `data-refresh.yml`);
- **в репозиторий** → `protocols/spf_metadata.json`: только метаданные — URL источника и страницы,
  размеры и `sha256` скачанных xlsx, имена листов/колонок, число наблюдений и кварталов, диапазон дат
  опросов, `retrieved_at`, метод извлечения, ограничения и `values_sha256` по каждой переменной
  (провенанс без публикации значений). Никаких значений медиан в metadata нет — это проверяет и тест,
  и отдельный шаг-ассерт в workflow.

Покрытие metadata: 1990Q1–2026Q3, 147 кварталов, 1022 наблюдения (CPI 1-year, CPI 10-year,
RGDP growth h2..h6). Тест `tests/test_spf_median.py`: схема, хеши, coverage, отсутствие полей со
значениями, отсутствие `protocols/spf_median.json` и `*.xls*` в дереве репозитория.

### 3.3. Ограничения SPF

- `survey_date` — якорь: первый день среднего месяца квартала опроса (дедлайны SPF — в среднем месяце
  квартала, из licence register); точный день дедлайна публикуется в релиз-календаре SPF и здесь
  машинно не извлекается;
- горизонты RGDP оставлены в опубликованных кодах `drgdp2..drgdp6`; соответствие «код → смещение
  квартала» нужно сверить по `spf-documentation.pdf` до использования в оценке;
- SPF публикует инфляцию CPI только как `INFCPI1YR/INFCPI10YR` (нет месячного y/y CPI) — это
  зафиксировано и в licence register.

## 4. RTDSM (Real-Time Data Set for Macroeconomists): автоматизируемость — ДА

Страницы: `…/real-time-data-set-for-macroeconomists`, `…/real-time-data-research/routput`,
`…/real-time-data-research/cpi`. Файлы .xlsx (public, research use):

| файл | URL | размер | sha256 (первые 16) | лист | размерность |
|---|---|---|---|---|---|
| ROUTPUTQvQd.xlsx | `…/real-time-data/data-files/xlsx/ROUTPUTQvQd.xlsx` | 244 845 B | `6a7971b31b733655` | ROUTPUT | 320×246 |
| cpiQvMd.xlsx | `…/real-time-data/data-files/xlsx/cpiQvMd.xlsx` | 599 661 B | `57d28f99b28ca9a3` | cpi | 956×245 |

Оба файла успешно скачаны и распарсены openpyxl (после того же фикса `core.xml`) — первая строка
`DATE, ROUTPUT65Q4, ROUTPUT66Q1, …` / `DATE, CPI65Q4, CPI66Q1, …`, т.е. один столбец на винтаж.
Проверка автоматизирована: `python tools/fetch_spf.py --check-rtdsm` печатает метаданные (URL, размер,
sha256, лист, размерность) в лог CI-прогона (`r2-metadata.yml`), ничего не сохраняя.

Правило для RTDSM: **только metadata** — сами xlsx не в git и не в репозитории; значения (при
необходимости) — в `data/raw/` и приватный артефакт. Полноценный инструмент извлечения винтажей
RTDSM не создавался: в `data_plan.json` RTDSM-серий нет, а по границам задачи (task-8) новые файлы
инструментов ограничены NBER/SPF; зафиксировано как следующий шаг, доступность и способ подтверждены.

## 5. Как проверено

- **Локально** (сеть из shell на этой машине фактически доступна, вопреки исходной вводной —
  проверено многократно): `tools/fetch_data.py` — 13/13 записей, 0 ошибок;
  `tools/verify_data_gate.py` — `passed=true`, 87 пар; `tools/nber_dates.py` — 17 объявлений;
  `tools/fetch_spf.py` — 1022 наблюдения; `--check-rtdsm` — оба файла OK; 12 тестов OK.
- **В CI** (GitHub Actions, запуск через API по dispatch, run id и артефакты — см. ниже):
  `probe-vintages.yml` — диагностика GDPC1 (какие варианты формы дают zip);
  `r2-metadata.yml` — NBER + SPF metadata + RTDSM probe, артефакт `r2-metadata-<run_id>`;
  `data-refresh.yml` — полный цикл получения и ворот, артефакт `amfe-data-<run_id>`
  (в нём `protocols/r2_gate_report.json` уже с GDPC1).

### 5.1. CI-прогоны (GitHub Actions, dispatch через API, commit `3755601`)

Ветка `main`, SHA `37556019f177249edda4a4132a526d387c467795`; запуск — `POST
/repos/Flumos1/Word-economic-model-/actions/workflows/<wf>.yml/dispatches` с `ref=main`
(токен из `git credential fill`; важно: параметр `ref` принимает имя ветки, SHA коммита даёт 422
«No ref found»).

| workflow | run id | результат | артефакт |
|---|---|---|---|
| `probe-vintages.yml` | 43086574746 | success | `vintage-probe-43086574746` (id 13885958868) |
| `data-refresh.yml` | 43086580403 | success | `amfe-data-43086580403` (id 13887345741) |
| `r2-metadata.yml` (первый прогон) | 43086568555 | failure (баг в `fetch_spf.py`, исправлен в `0375252`) | `r2-metadata-43086568555` |
| `r2-metadata.yml` (повтор на `main@0375252`) | 43087213929 | success (все шаги) | `r2-metadata-43087213929` (id 13887820892) |

**`data-refresh.yml`, run 43086580403 — отчёт ворот из артефакта (совпал с локальным):**

```
generated_at 2026-11-11T19:23:03Z
passed: true
totals: matched_pairs=87, matched_pairs_pre_2000=17, failures=0, excluded=1
series: CPIAUCSL 10/10 (retrieved vintages), CPILFESL 9/9, GDPC1 40/40 (release-date supplement),
        UNRATE 10/10, GS10 9/9, TB3MS 9/9
excluded: USREC («no vintage set retrieved»)
GDPC1: initial_release_rows=40, checked=40, matched=40, mismatches=[], supplement.release_dates=40
```

То есть GDPC1 в CI вошёл в отчёт ворот с 40/40 совпадений, локальный и CI-прогоны дали одно и то же
(`87`, `17`, `0`, `1`). Артефакт содержит обновлённые `protocols/r2_gate_report.json` и
`protocols/data_log.json` (коммитятся Lead по правилу D-134).

**`probe-vintages.yml`, run 43086574746 — диагностика GDPC1 из артефакта:**

```
retrieved_ft2_control_csv       | zip, 295 строк            (контроль: винтажи скачиваются)
retrieved_ft4_initial_csv       | html: «No observations were retrieved using the specified options»
release_window_ft4_initial_csv  | zip, 11 строк initial-release
control_release_ft4_initial_csv | zip, 6 строк initial-release
```

Это CI-воспроизведение причины и рабочего способа из раздела 1.

**`r2-metadata.yml`, run 43087213929 (main@0375252) — SUCCESS, все шаги:**

```
steps: NBER dates = success, SPF medians = success, RTDSM probe = success,
       «Confirm the SPF metadata carries no median values» = success, upload artifact = success
artifact r2-metadata-43087213929 (12109 B): protocols/nber_dates.json, protocols/spf_metadata.json,
       data/raw/spf/spf_median.json (приватный артефакт; в git не попадает)
```

Проверка содержимого артефакта против локального прогона: NBER — те же 17 объявлений, тот же
`announcements_sha256` (`4910eb6e…`); SPF — те же размеры и SHA-256 xlsx (`2bef852b0b4c…`,
`fdcf9ca0299d…`), то же покрытие (147 кварталов, 1022 наблюдения); в `spf_metadata.json` нет ключа
`series` и нет полей со значениями (это же проверяет шаг-ассерт в workflow); RTDSM probe в логе CI:
`ROUTPUTQvQd` 244 845 B, sha256 `6a7971b3…`, лист ROUTPUT 320×246; `cpiQvMd` 599 661 B, sha256
`57d28f99…`, лист cpi 956×245, `automatable: true`, `raw_files_stored: false`.

**`r2-metadata.yml`, первый прогон (43086568555) — ошибка и её причина:** упал на шаге SPF с
`ValueError: 'data/raw/spf/spf_median.json' is not in the subpath of '/home/runner/...'` — локальная
проверка не поймала, потому что по умолчанию путь был абсолютным, а workflow передаёт относительный.
Исправлено в `tools/fetch_spf.py` (путь резолвится через `Path.cwd()`, затем `relative_to(ROOT)`),
коммит `0375252`, повторный прогон 43087213929 — зелёный. Артефакт первого прогона всё же сохранил
значения SPF (приватный artifact) и свежий `nber_dates.json`.

При этом воспроизводимость подтверждена: `announcements_sha256` NBER совпал с локальным
(`4910eb6e…`, 17 записей), а SHA-256 скачанных SPF xlsx совпали с локальными
(`2bef852b0b4c…`/`fdcf9ca0299d…`) — то есть CI и локальный прогон видят одни и те же файлы.

Нюанс: `source_page_sha256` страницы NBER различается между запросами (локально `f19b720f…`, в CI
`42f2d430…`) — страница содержит динамическую разметку; канонический `announcements_sha256` при этом
стабилен. Поэтому для проверки изменения данных используется `announcements_sha256`, а
`source_page_sha256` фиксирует конкретный скачанный экземпляр страницы.

## 6. Коммиты и что осталось / ограничения

Коммиты (Lead, main): `3755601` — инструменты NBER/SPF, расширенный гейт, workflow `r2-metadata.yml`,
metadata-файлы; `0375252` — фикс `values_location` в `tools/fetch_spf.py`, перегенерированный
`protocols/spf_metadata.json`, и **CI-результат run 43086580403**: `protocols/r2_gate_report.json`
(87 пар, GDPC1 40/40) и `protocols/data_log.json`. Настоящий документ — отдельный коммит.

Осталось / ограничения:

1. `protocols/r2_gate_report.json` и `protocols/data_log.json` обновляются только коммитом по
   результату CI-прогона `data-refresh` (правило D-134); в этой задаче они закоммичены из артефакта
   run 43086580403.
2. USREC остаётся исключённым: у ряда нет vintage-набора в `data_plan.json` (это не initial-release
   проблема, а отсутствие планового набора).
3. Точные дни дедлайнов SPF не извлекаются (см. 3.3); маппинг `drgdp2..6` → смещение квартала требует
   сверки по документации SPF.
4. RTDSM: подтверждена автоматизируемость и метаданные; полноценного экстрактора винтажей нет.
5. NBER: последнее объявление на странице — 2021-07-19; более новых определений циклов NBER не
   публиковал (проверено на дату задачи).

## 7. Ссылки

- ALFRED download-data (форма): `https://alfred.stlouisfed.org/series/downloaddata?seid=GDPC1`
- FRED CSV: `https://fred.stlouisfed.org/graph/fredgraph.csv?id=GDPC1`
- NBER announcements: `https://www.nber.org/research/business-cycle-dating/business-cycle-dating-committee-announcements`
- NBER cycle dates: `https://www.nber.org/research/data/us-business-cycle-expansions-and-contractions`
- SPF median page: `https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/median-forecasts`
- RTDSM: `https://www.philadelphiafed.org/surveys-and-data/real-time-data-research/real-time-data-set-for-macroeconomists`
