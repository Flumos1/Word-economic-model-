# 249. BLS: публичный срез CPI-U через API v1 и статус VERIFIED (2026-11-26)
Версия 1.0.0 · 2026-11-26. Задача `task-81`. Правило владельца: платные лицензии не используем, данные только из открытых источников. BLS в реестре — `CATALOGUED`/докет `PARTIAL` («условия прочитаны, данных нет»); этот документ фиксирует реальный публичный срез и даёт значения для перевода в **VERIFIED**. **Это не юридическая консультация.**

## Метод
Официальный **BLS Public Data API v1** (GET, без регистрации и ключа; POST/v2 не используются, лимиты не обходятся). Один ряд: **CUUR0000SA0** (CPI-U, all items, not seasonally adjusted). Запросы последовательные с паузой ≥1 с. Ответ — HTTP 200 `application/json`, **3 379 байт**, `status = REQUEST_SUCCEEDED`, `message = []`.

- `source_url`: `https://api.bls.gov/publicAPI/v1/timeseries/data/CUUR0000SA0`
- Проверка диапазона: повторный запрос с `?startyear=2015&endyear=2025` вернул **тот же** набор из 36 строк — **v1 без ключа отдаёт только последние 3 года**, параметры `startyear`/`endyear` в v1 GET не расширяют окно. Второго пути (v2/POST/ключ) не использовали.

## Лицензия
- Официальная формулировка BLS: **public domain (U.S. Government work), attribution recommended**.
- URL: `https://www.bls.gov/bls/linksite.htm` (BLS Linking and Copyright Information). **Прямой fetch этой страницы из текущего окружения вернул HTTP 403** (бот-политика BLS); текст лицензии берётся как уже зафиксированный в проекте (реестр/лицензионный журнал, `BLS_PRIMARY`) и как указано в задаче, а не как свежепрочитанная страница 2026-11-26. Это ограничение метода, не меняющее статус данных: U.S. Government work — public domain.
- Условия: свободное использование public domain с просьбой указать BLS как источник; ранее скопирайченные фотографии/иллюстрации — исключение (не относятся к числовому ряду).

## Что скачано
| Параметр | Значение |
|---|---|
| Ряд | **CUUR0000SA0** — CPI-U, All Items, U.S. City Average, **not seasonally adjusted** |
| Файл | `data/public/bls_cpi_monthly.csv` |
| Колонки | `series_id,year,period,period_name,value,footnote` |
| Байт | **1 433** |
| SHA-256 | **`fcd6ddf0ed8fe61d65483cefd9bb3efca97554d4fb85d2c5373acc910164d6e4`** |
| Наблюдений | **36**: 34 месячных (M01..M12, 2024 — 12, 2025 — 12, 2026 — M01..M10) + 2 годовых средних M13 (2024, 2025) |
| Покрытие | **2024-01 .. 2026-10** (+ M13), years 2024..2026 |
| Единица | index 1982-84=100, **NSA** (CUUR) |
| Частота | monthly (+ annual average M13) |
| Кодировка | UTF-8 **без BOM**, переводы строк **LF**, 36 строк данных (≤600) |
| Метаданные | `data/public/bls_cpi_monthly.meta.json` (1 650 B), `schema = AMFE-PUBLIC-BLS/1.0.0`: series_id, publisher, source_url, license, license_url, retrieved_at=2026-11-26, unit, frequency, coverage, observations, file_bytes, file_sha256, notes |
| API-ответ | 3 379 байт, `REQUEST_SUCCEEDED`; данные не пересчитывались, значения сохранены строками как отдал API |

Проверка целостности: `sha256(Path("data/public/bls_cpi_monthly.csv").read_bytes()) == fcd6ddf0…d6e4`; в метаданных `file_bytes=1433`, `file_sha256` совпадает; sidecar проходит `_public_domain_aggregates` из `tools/external_review_g2.py` (in-memory allowlist, файл инструмента не менялся): **`problems = []`**.

## Ограничения
- **Не PIT/не винтаж**: сохранены последние опубликованные значения; первая публикация, ревизии и архив винтажей BLS не загружались. Нельзя утверждать, какие значения были доступны на исторические даты.
- **Сезонная корректировка не смешивается**: ряд **CUUR** (NSA). Нельзя объединять его с CUSR (SA) или с сезонно скорректированными рядами без явной оговорки.
- **Не полная история**: v1 без ключа вернул только 3 года (2024–2026), а не историю с 1913; `startyear/endyear` в v1 GET не сработали. v2/POST/ключ не использовались.
- API v1: 25 рядов и 10 лет на запрос — лимиты не обходились; запросы последовательные (≤1/с), 429 не возникал; при 429 была бы пауза, а не обход.
- В слайсе все `footnote` пустые (API вернул `[{}]`); поле оставлено на будущее — если BLS отдаст примечания, они сохраняются в колонке.
- Значения могут быть пересмотрены BLS после даты пина; пин фиксирует конкретные байты/хеш, а не «истину».
- Страница лицензии BLS сейчас отдаёт 403 (бот-политика) — цитата лицензии не перепроверена по странице 2026-11-26.

## Что НЕ заявляется
- Что это сезонно скорректированный CPI (это CUUR, NSA).
- Что это PIT-ряд или архив винтажей BLS/ALFRED.
- Что получена полная история CPI с 1913 года.
- Что использованы BLS API v2, POST, ключ, payrolls (CES0000000001) или bulk-выгрузки — нет, только один ряд через v1 GET.
- Что ряд можно перепродавать/распространять как продукт; только публичный агрегат public domain с атрибуцией BLS.

## Что осталось сделать Lead'у (реестр/allowlists — не трогал)
- `data/public/bls_cpi_monthly.csv` + `.meta.json` уже готовы; sidecar валиден.
- `tools/external_review_g2.py`, `ALLOWED_AGGREGATES`:
  `"data/public/bls_cpi_monthly.csv": "data/public/bls_cpi_monthly.meta.json",`
- `tests/test_licence_hygiene.py`, `ALLOWED_TRACKED_RAW`:
  `"data/public/bls_cpi_monthly.csv",`
  `"data/public/bls_cpi_monthly.meta.json",`
- `protocols/source_registry.json`, источник **BLS**: `status="VERIFIED"`, `retrieved_at="2026-11-26"`; `license` — **`public domain (U.S. Government work), attribution recommended`** (точная строка; в текущей записи стоит вариант с `;` — можно привести к этой); в `notes` добавить: `data/public/bls_cpi_monthly.csv`, 1 433 B, sha256 `fcd6ddf0…d6e4`, 36 наблюдений (2024-01..2026-10, CUUR NSA + M13), «один ряд через BLS API v1 (no key); не PIT/винтаж, не полная история, без v2/POST», ссылку на этот документ. Тогда докет BLS переходит `PARTIAL → VERIFIED`.
- `protocols/source_verification_log.jsonl` — evidence-запись по BLS (файл/sha/obs) по вашему формату.

## Воспроизведение
```
python -c "import urllib.request,json; r=urllib.request.urlopen(urllib.request.Request('https://api.bls.gov/publicAPI/v1/timeseries/data/CUUR0000SA0', headers={'User-Agent':'AMFE-research/1.0'})); d=json.load(r); print(d['status'], len(d['Results']['series'][0]['data']))"
# -> REQUEST_SUCCEEDED 36
python -c "import hashlib,pathlib; b=pathlib.Path('data/public/bls_cpi_monthly.csv').read_bytes(); print(len(b), hashlib.sha256(b).hexdigest())"
# -> 1433 fcd6ddf0ed8fe61d65483cefd9bb3efca97554d4fb85d2c5373acc910164d6e4
```

## Файлы
`data/public/bls_cpi_monthly.csv`, `data/public/bls_cpi_monthly.meta.json`, `docs/249_BLS_VERIFIED.md`. Реестр, журнал, allowlists, tests, tools, src, frozen и data/oil не менялись; не закоммичено.
