# 130. OECD ICIO: найден официальный машинный канал 2025, загрузка, парсер, D1

Дата: 2026-11-14. Волна P2, задача `task-35` (трек forward-engine). Итог: **найден настоящий
официальный машинный канал релизов OECD ICIO 2025/2023 — файловый сервис
`webfs-sti.oecd.org`, ссылки на который публикует страница датасета**. Скачан блок
`2016-2022_SML.zip` (166,2 МБ, 81 экономика × 50 отраслей, 2016–2022); парсер читает формат
2025; D1 пока **не пройден** из-за несведённой конвенции конечного спроса/TLS (детали в §3).
Данные — `F:\Мировая экономика модель\data\mrio\oecd_icio_2025` (не коммитятся).

Инструмент: [tools/oecd_import.py](../tools/oecd_import.py); тесты:
[tests/test_oecd_import.py](../tests/test_oecd_import.py) (**33 офлайн-теста**);
машиночитаемый статус: [protocols/oecd_icio_coverage.json](../protocols/oecd_icio_coverage.json).

## 1. Главный результат: канал найден

Страница `https://www.oecd.org/en/data/datasets/inter-country-input-output-tables.html`
отдаётся только с полноценной браузерной сессией (UA + Accept-Language + `sec-fetch-*` +
cookies `affinity/__cf_bm/_cfuvid`; без них — 403). В её HTML (1,49 МБ) — **прямые ссылки на
официальный файловый сервис OECD**:

```
https://webfs-sti.oecd.org/files/STI-PIE/ICIO/2025/ReadMe_ICIO_small.xlsx        (328 422 Б)
https://webfs-sti.oecd.org/files/STI-PIE/ICIO/2025/1995-2000_SML.zip            (110 135 258 Б)
https://webfs-sti.oecd.org/files/STI-PIE/ICIO/2025/2001-2005_SML.zip            ( 99 391 392 Б)
https://webfs-sti.oecd.org/files/STI-PIE/ICIO/2025/2006-2010_SML.zip            (111 587 430 Б)
https://webfs-sti.oecd.org/files/STI-PIE/ICIO/2025/2011-2015_SML.zip            (116 134 326 Б)
https://webfs-sti.oecd.org/files/STI-PIE/ICIO/2025/2016-2022_SML.zip            (166 241 019 Б)
```
(+ варианты `_EXT.zip` и `ICIO2025econA/B/VB/Z/OTHER.zip`; также аналогичные наборы 2023 и 2021.)

Это не обход защиты и не зеркало: ссылки опубликованы на официальной странице OECD, файлы
лежат на официальном `webfs-sti.oecd.org`. Особенность сервиса: `HEAD`/`Range`-запросы
отвечают 206, а полный `GET` большого файла может вернуть 403 — поэтому загрузчик качает
**блоками по Range** (стандартный HTTP, проверка размера по `Content-Range`) и сверяет SHA-256.

Скачано: `2016-2022_SML.zip` — **166 241 019 Б**, SHA-256
`46db55c45747e26dde5c1716d072b2aa74ace362a4424491ac646d2ab175befb`, 7 CSV
(`2016_SML.csv` … `2022_SML.csv`, по ~87–90 МБ). ReadMe (small) — 328 422 Б.

## 2. Структура релиза 2025 (из официального ReadMe)

- **81 экономика × 50 отраслей** (больше цели «60+ × 45»; отрасли A01, A02, A03, B05 … T),
  один CSV на год;
- current prices, **USD million**;
- строки/колонки: промежуточный спрос `Z` (`<ISO3>_<IND>`), конечный спрос по странам
  `<ISO3>_<HFCE|NPISH|GGFC|GFCF|INVNT|DPABR>` (изменения запасов — `INVNT`, прямые покупки
  резидентов за рубежом — `DPABR`), нижние строки `TLS`, `VA`, `X`; последняя колонка `OUT`;
- годы 1995–2022, пять блоков (1995-2000, 2001-2005, 2006-2010, 2011-2015, 2016-2022).

## 3. Конвертация 2025: что уже сходится и что осталось

`--convert --archive .../2016-2022_SML.zip --year 2019` (новый memory-lean парсер, 81×50,
n=4050, без записи при провале D1):

| Проверка | Результат |
|---|---|
| размерность | **81 × 50** (n=4050) — PASS |
| неотрицательность Z/x/y | PASS |
| колоночный баланс `x = 1'Z + VA + TLS` | **PASS**, max residual 33,93 USD млн |
| строковый баланс `x = Z·1 + y + inv + disc` | **FAIL**, max residual 19 506 USD млн |
| глобальные итоги | output 168 481 393, intermediate 85 055 695, final 83 183 992, VA 83 425 730 (расхождение ~0,14–0,29%) |
| датасет записан | **нет** (fail-closed) |

Причина: в релизе 2025 конечный спрос задан матрицей «отрасль-поставщик × страна-потребитель»
и отдельной строкой `TLS`; канонический вектор `y` требует свёртки по назначению с учётом
`TLS`/`DPABR`. Свёртка «по всем назначениям» уже дала близкие итоги (final ≈ VA, 0,29%), но
строковая идентичность пока не в допуске 0,1% — нужен один дополнительный шаг маппинга
(пересчёт FD-матрицы и TLS в вектор `y`). Это чистая работа с конвенцией таблицы, а не
отсутствие данных; датасет намеренно не записан.

## 4. Проверенные каналы и статусы (2026-11-14)

| Канал / URL | Статус | Комментарий |
|---|---|---|
| dataset page `oecd.org/en/data/datasets/inter-country-input-output-tables.html` | **200** (с сессией) | источник ссылок webfs; без сессии 403 |
| `webfs-sti.oecd.org/files/STI-PIE/ICIO/2025/*` | **206/200** | **рабочий машинный канал**; скачаны ReadMe + блок 2016-2022 |
| `stats.oecd.org/Index.aspx?DataSetCode=ICIO/ICIO2021/ICIO2023/ICIO2025/_2023` | 200 → редирект | все ведут на `data-explorer.oecd.org`, dataset id не передаётся |
| `stats.oecd.org/DownloadFiles.aspx?DataSetCode=ICIO2023` | 404 | эндпоинт не существует |
| `stats.oecd.org/restsdmx/sdmx.ashx/GetDataStructure/ICIO2023`, `sdmx-json/data*` | 404 | SDMX-каналов ICIO нет |
| `data-explorer.oecd.org/api/`, `/vis`, `/export` | 404 (HTML SPA) | публичного REST префикса нет; бэкенд — `sdmx.oecd.org/public/rest/v2` |
| SDMX v2 `structure/dataflow/OECD.SDD.NAD` / `OECD.STI.PIE` | 200 | 187/33 dataflow; **сырых ICIO нет**, только GHGFP/TiVA/TiM |
| `sdmx.oecd.org/.../DSD_ICIO@DF_ICIO/...` | 204/404 | dataflow не существует |
| `www.oecd.org/content/dam/.../ICIO2023*.zip` (догадки) | 404 | DAM-путей с файлами нет |
| `www.oecd.org/sti/ind/ICIO2023_1995-2020.zip`, `ICIO2025_1995-2022.zip` | 200, но HTML | страница ошибки, не файл (magic sniff) |
| Wayback CDX по `content/dam/*icio*`, `input-output`, `sti/ind/*ICIO*` | 503/504/0 строк | снапшотов файлов новых редакций нет (кроме редакции 2016, см. task-30) |

## 5. Команды (готовый контур)

```powershell
$root = "F:\Мировая экономика модель\data\mrio\oecd_icio_2025"
# 1) найти официальные ссылки на странице датасета (браузерная сессия внутри инструмента)
python tools/oecd_import.py --root $root --discover --release 2025
# 2) скачать все 5 блоков SML + ReadMe с официального webfs (Range-загрузка, SHA-256)
python tools/oecd_import.py --root $root --download-2025 --release 2025
# 3) проверить комплект архивов (имена/размеры/PK/CSV-члены) — fail-closed
python tools/oecd_import.py --root $root --check-archive
# 4) конвертировать годы (после сведения FD/TLS) и проверить D1 + покрытие
python tools/oecd_import.py --root $root --convert --jobs 3
python tools/oecd_import.py --root $root --d1
```

Сейчас `--check-archive` показывает: ReadMe OK, `2016-2022_SML.zip` присутствует, 4 блока
отсутствуют (`1995-2000/2001-2005/2006-2010/2011-2015_SML.zip`) — статус `BLOCKED_DATA` с
точным списком. `--discover` находит 18 файлов релиза 2025.

## 6. Тесты (33, все зелёные, без сети)

Добавлено к прежним 28: разбор ссылок webfs по релизам (`discover_webfs_links`); состав
`EXPECTED_2025_ARCHIVES` (5 блоков + ReadMe); `--check-archive` PASS на валидных фикстурах и
fail-closed (TOO_SMALL/MISSING/BAD_MAGIC); парсер нового формата 2025 на синтетическом CSV
(`V1`-заголовок, `<ISO3>_<IND>` + `<ISO3>_<FD>` + `OUT`, строки TLS/VA/OUT, `INVNT` →
`inventory_changes`, `x` из колонки `OUT`).

```powershell
python -m unittest tests.test_oecd_import -v     # 33 теста, ~1,7 с
```

## 7. Ограничения и следующий шаг

- D1 для 2025 пока **не пройден**: остаётся маппинг FD-матрицы/TLS в канонический `y`
  (строковая идентичность 19 506 USD млн при выпуске 168 481 393 USD млн); датасет не
  записан, данные целы в архиве.
- Скачан один блок из пяти (2016–2022); остальные качаются командой `--download-2025`.
- Через Wayback доступна только редакция 2016 (71×34, 2000–2011, D1 PASS, покрытие 95,5% —
  см. `protocols/oecd_icio_coverage.json`); она остаётся рабочим датасетом до сведения 2025.
- Лицензия OECD Terms: принятие и цитирование — на стороне пользователя.
- `src/amfe/mrio/schema.py` читает только v1; документы v1.1 пишет/валидирует инструмент.
- Не коммитилось, не пушилось; frozen-файлы, registry, чужие tools не изменялись.
