# 243. Замена FAS: месячный экспорт США по сое и кукурузе (UN Comtrade)

Дата: 2026-11-26. Задача: `task-78`, роль data/PIT (r2-completion). Контекст: USDA FAS export sales
недоступен без интерактивной ESRQS/ключа ([docs/241](241_USDA_FAS_export_sales_OSINT.md); статус
`USDA_FAS_EXPORT_SALES` остаётся `CATALOGUED`). Правило владельца — `NO_PAID_LICENCES`,
данные только из открытых S1–S3; UN Comtrade уже `VERIFIED` в реестре и его public preview не
требует ключа. **Это реальная замена FAS только на месячном горизонте** — не FAS, не недельные
продажи и не PIT. Не юридическая консультация. Не коммитчено (коммитит Lead).

## 1. Что именно заменяет FAS, а что нет

| | USDA FAS export sales (недоступно) | Этот пин (UN Comtrade) |
|---|---|---|
| Что измеряется | **продажи/контракты**: new/net sales, outstanding sales, shipments | **таможенный экспорт**: стоимость (`primaryValue`, USD FOB) и нетто-вес (`netWgt`, кг) |
| Частота | **недельная** | **месячная** (подтверждено: `freqCode=M`) |
| Горизонт | недельные продажи + marketing-year totals | календарный месяц, 2023-01…2026-08 |
| География | по странам назначения | **только partner = World (0)**, без разбивки по странам |
| Концепция | вперёд-смотрящие обязательства (commitments), включая непогруженные продажи | фактически оформленный экспорт (customs) |
| Статус | `CATALOGUED`, данных нет (docs/241) | публичный preview, HTTP 200, срез запинен |

Разница частот и понятий — принципиальная: **месячный customs export не равен недельным
export sales**. Пинить как «FAS» или «weekly export sales» нельзя. На месячном горизонте это
лучшая доступная законная замена (public domain-подобные условия UN Comtrade: free non-commercial
with attribution, no re-sale), но **forward-looking commitments и разбивку по направлениям она не
восстанавливает**.

## 2. Что реально скачано

- API: `https://comtradeapi.un.org/public/v1/preview/C/M/HS`
- Параметры: `reporterCode=842` (United States), `flowCode=X` (exports), `partnerCode=0` (World),
  `cmdCode=1201` (soya beans) / `1005` (maize/corn), `period=YYYYMM`.
- Публичный preview: **без ключа**, без подписки; **ровно один период на запрос** (`Maximum number
  of periods for preview is 1`), поэтому **88 запросов** = 44 месяца × 2 кода.
- Получено 2026-11-26, User-Agent `AMFE-research/1.0`; 429 «Rate limit is exceeded» встречались,
  обработаны **backoff-паузой** (1.8 с с ростом, без обхода лимитов).
- Сырые JSON-ответы: 88 файлов, **85 304 B** — лежали во временном каталоге **вне репозитория**,
  **не коммитятся**. В git — только компактный CSV.
- Проверка частоты: ответы `freqCode=M`, `refMonth` 1–12, `classificationCode=H6`.
- Контрольный annual-запрос (6 запросов, `C/A/HS`, 2023–2025) — для сверки месячных сумм с
  годовыми итогами (§4).
- Условия: страница `https://comtradeplus.un.org/en/license-agreement` получена 2026-11-26:
  **51 445 B**, sha256 `021d37ed6ef4a6d72bb12e8d5fa0a76257de147652b8bebbc17b97b7803404d6`
  (JS-рендеринг: в реестре для 2026-11-25 записан другой байтовый хеш `cedc18d7…`, 50 828 B — оба
  пина сохранены в `.meta.json`: `terms_sha256` и `terms_previous_pin`; текст лицензии тот же).

## 3. Файлы и пин

| Файл | Байт | sha256 |
|---|---:|---|
| `data/public/un_comtrade_us_soy_corn_monthly.csv` | **5 814** | **`48c9762be8693beda64e303a40fd879c3765210f1149e1821fb3993751c3dfa4`** |
| `data/public/un_comtrade_us_soy_corn_monthly.meta.json` | 4 809 | (sidecar; хеш среза внутри) |
| `docs/243_FAS_замена_Comtrade.md` | этот файл | — |

CSV: UTF-8 без BOM, LF, **88 наблюдений**, 44 месяца × 2 HS-кода. Колонки:
`reporter_code, partner_code, flow, commodity_code, period, frequency, primary_value_usd,
net_weight_kg, classification_code, is_reported`. Период — `YYYY-MM`; значения — как вернул API
(`primaryValue` USD, `netWgt` кг), без пересчёта и округления. `classification_code = H6`.
`is_reported = false` во всех строках (флаг API; значения использованы как есть, без корректировок).

Схема `.meta.json`: `AMFE-PUBLIC-COMTrade-US-SOY-CORN/1.0.0`; поля `dataset_id`, `name`, `source`,
`publisher`, `source_url` (шаблон фактического запроса), `landing_url`, `terms_page`,
`terms_bytes/sha256/retrieved_at`, `license`, `retrieved_at = 2026-11-26`, `reporter`, `partner`,
`flow`, `commodities`, `unit`, `frequency`, `coverage`, `file`, `file_bytes`, `file_sha256`,
`observations = 88`, `annual_cross_check`, `notes`.

Покрытие: **United States (842) → World (0), exports (X), 2023-01 … 2026-08**; 1201 — 44 месяца,
1005 — 44 месяца. 2026-09 на 2026-11-26 вернул пустой `count` (лаг публикации) — честно зафиксировано.

## 4. Валидация

**Внутренняя сверка**: сумма месячных `primaryValue` точно равна годовому итогу того же API
(`C/A/HS`, отдельные запросы) для обеих культур и 2023–2025 — расхождение **0.0%**:

| HS | Год | Месяцев | Сумма месяцев, USD | Годовой итог, USD | Расхождение |
|---|---|---:|---:|---:|---:|
| 1201 (soya) | 2023 | 12 | 28 006 470 143 | 28 006 470 143 | 0.0% |
| 1201 | 2024 | 12 | 24 570 058 895 | 24 570 058 895 | 0.0% |
| 1201 | 2025 | 12 | 16 459 153 793 | 16 459 153 793 | 0.0% |
| 1005 (corn) | 2023 | 12 | 13 683 227 155 | 13 683 227 155 | 0.0% |
| 1005 | 2024 | 12 | 14 342 041 436 | 14 342 041 436 | 0.0% |
| 1005 | 2025 | 12 | 18 878 400 950 | 18 878 400 950 | 0.0% |

Суммы нетто-веса за 2023–2025 (для контроля): соя 48,72 / 50,43 / 37,07 млн т; кукуруза
45,94 / 61,14 / 78,12 млн т; у одного месяца (**1005, 2025-08**) `netWgt = null` — в CSV
`net_weight_kg` оставлен **пустым, без импутации** (стоимость за этот месяц есть). Спот-проверка
первых/последних строк: 2023-01 соя 5 229 298 537 USD / 8 567 208 834 кг; 2026-08 соя
708 187 754 USD / 1 471 758 478 кг; 2026-08 кукуруза 1 701 051 638 USD / 7 437 905 461 кг.
Пропусков месяцев в 2023-01…2026-07 нет; 2026-08 — последний доступный, 2026-09 пуст.

## 5. Ограничения (честно)

1. **Нет недельной частоты.** FAS — недельный, здесь месячный календарный агрегат. Недельные
   колебания продаж/отгрузок не восстановить.
2. **Нет forward-looking продаж**: Comtrade фиксирует фактический экспорт, а не new/net/outstanding
   sales и не контрактные обязательства; «продажи» и «отгрузки» — разные понятия.
3. **Нет разбивки по странам**: partner = World (0) в этом срезе; bilateral-матрицы не тянулись
   (public preview ограничивает периоды и частоту; полные матрицы — premium).
4. **Нет PIT/винтажей**: один текущий preview-снимок; США/Comtrade могут ревизовать историю,
   архив ревизий не хранится. Использовать как PIT нельзя.
5. **Лаг публикации**: 2026-09 (и позднее) отсутствует на 2026-11-26; срез датирован.
6. **null-поля preview**: в месячных ответах `reporterDesc/partnerDesc/cmdDesc` = null; CSV хранит
   коды, расшифровки — в sidecar (статические, не из ответа). `isReported=false` — флаг API.
7. **Не полная база**: 2 HS-кода, 1 репортёр, 1 партнёр, ~3,7 года; не полный Comtrade и не
   мировой экспорт.
8. **Лицензия**: UN Comtrade terms — free non-commercial use с атрибуцией, **no re-sale**; ссылка
   на UN Comtrade/UNSD обязательна. Полные данные/матрицы не перепродаются и не публикуются как
   «база».
9. **Сравнение с FAS по уровню** (например, «в 2025 соевый экспорт США 16,46 млрд USD») корректно
   как Comtrade-число, но не как FAS-число; разные методологии/ревизии.

## 6. Что нельзя заявлять

- «FAS export sales», «weekly export sales», «new sales / outstanding sales» — этого в данных нет.
- «Недельные продажи», «продажи по странам назначения», «непогруженные продажи» — нет.
- «PIT», «винтажи», «что было известно на дату» — нет: это один снимок.
- «Мировой экспорт», «баланс сои/кукурузы» — нет: только экспорт США, partner World.
- «Прогноз» — нет: фактические месячные значения.
- Перепродажа/перепубликация массива как базы — запрещена условиями UN Comtrade.

## 7. Как пересобрать

```python
# 88 запросов: 44 месяца x 2 кода; один период на запрос; пауза ~1.6-1.8 s и backoff на 429.
# URL-шаблон:
# https://comtradeapi.un.org/public/v1/preview/C/M/HS?reporterCode=842&period=YYYYMM
#     &cmdCode=1201|1005&flowCode=X&partnerCode=0
# Контроль: annual C/A/HS за 2023..2025, сравнить суммы; записать CSV с newline="\n", UTF-8 без BOM.
```

Ключи не нужны и не использовались. Сырые ответы держать **вне репозитория** (временный каталог).
Проверка целостности:

```powershell
python -c "import hashlib,pathlib; p=pathlib.Path('data/public/un_comtrade_us_soy_corn_monthly.csv'); print(hashlib.sha256(p.read_bytes()).hexdigest())"
# 48c9762be8693beda64e303a40fd879c3765210f1149e1821fb3993751c3dfa4

python -m unittest tests.test_licence_hygiene tests.test_external_review_g2
# Ran 32 tests ... OK
```

Провенанс-sidecar проверен dry-run'ом `tools/external_review_g2._public_domain_aggregates` с
будущей allowlist: `allowed` включает наш файл, `problems = []` (sidecar: licence, source_url,
retrieved_at, coverage, sha256 — всё на месте).

## 8. Что осталось Lead'у (реестр/allowlists/тесты)

**`protocols/source_registry.json`, источник `UN_COMTRADE`** (статус `VERIFIED` не меняется):
- `license` — без изменений: `"UN Comtrade terms of use (free for non-commercial use, attribution; premium API limits apply)"`;
- `url` — без изменений: `"https://comtradeplus.un.org/"`; `access = "api"`;
- `retrieved_at` — можно оставить `"2026-11-25"` (уровень источника) или обновить `"2026-11-26"`;
- в `notes` добавить (предлагаемая строка): `"US soy/corn monthly exports pinned 2026-11-26: data/public/un_comtrade_us_soy_corn_monthly.csv (+ .meta.json), 88 observations (2023-01..2026-08, 44 months x HS 1201/1005), reporter US (842) -> partner World (0), flow X, file_bytes 5814, sha256 48c9762be8693beda64e303a40fd879c3765210f1149e1821fb3993751c3dfa4. Public preview /C/M/HS, one period per request, no key; 88 requests with 429 backoff. Monthly frequency confirmed (freqCode=M); this replaces FAS export sales only at a MONTHLY horizon (not weekly sales/commitments, no destination breakdown). No PIT/vintage history; one current preview snapshot (2026-09 empty on 2026-11-26). Terms page pins: 2026-11-26 51445 B sha256 021d37ed6ef4a6d72bb12e8d5fa0a76257de147652b8bebbc17b97b7803404d6; 2026-11-25 50828 B sha256 cedc18d78dfe1ab0ad92face2a8b62ec9a36591392953b181652b013405673db (page is JS-rendered). No re-sale; attribution to UN Comtrade/UNSD. See docs/243."`
- `USDA_FAS_EXPORT_SALES` не меняется — остаётся `CATALOGUED` (docs/241).

**`tools/external_review_g2.py` → `ALLOWED_AGGREGATES`:**
```python
    "data/public/un_comtrade_us_soy_corn_monthly.csv": "data/public/un_comtrade_us_soy_corn_monthly.meta.json",
```

**`tests/test_licence_hygiene.py` → `ALLOWED_TRACKED_RAW`:**
```python
    "data/public/un_comtrade_us_soy_corn_monthly.csv",
    "data/public/un_comtrade_us_soy_corn_monthly.meta.json",
```

**Документы**: `docs/238` §3/§4 (строка UN_COMTRADE — расширить цитируемый срез этим файлом),
`docs/205` (докет/счётчики не меняются по статусу, но evidence по UN Comtrade дополнить). Правит
Lead (вне моих scopes). `docs/212` правило владельца не меняется.

## 9. Файлы задачи

| Файл | Байт | sha256 |
|---|---:|---|
| `data/public/un_comtrade_us_soy_corn_monthly.csv` | 5 814 | `48c9762be8693beda64e303a40fd879c3765210f1149e1821fb3993751c3dfa4` |
| `data/public/un_comtrade_us_soy_corn_monthly.meta.json` | 4 809 | sidecar (хеш среза внутри) |
| `docs/243_FAS_замена_Comtrade.md` | этот файл | — |

Не коммичено. Не изменялись: реестр, журнал, allowlists, тесты, tools, protocols, data/oil, frozen.
Сырые JSON и HTML условий лежат только во временном каталоге вне репозитория.

## 10. Что осталось невозможным без FAS/лицензий

- недельные export sales (new/net/outstanding) и marketing-year commitments — недоступны;
- разбивка экспорта США по странам назначения в этом срезе — не тянулась (public preview, 1 период
  на запрос; bilateral—отдельная задача, но и она даст monthly customs, не weekly sales);
- PIT/винтажная история — нет ни у FAS-замены, ни у Comtrade-среза;
- альтернативы с destination detail (Census USA Trade Online и т.п.) в этой задаче не
  проверялись — отдельный OSINT-шаг, если нужен месячный bilateral.
