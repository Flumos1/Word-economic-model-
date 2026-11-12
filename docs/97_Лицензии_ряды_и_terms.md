# AMFE — лицензии рядов и хеши условий (2026-11-12)

Версия 1.0.0 · 2026-11-12. Задача `task-36`: per-series лицензионные записи для всех 19 рядов [protocols/data_plan.json](../protocols/data_plan.json) v0.4.0 и полный набор хешей условий (**F-5**). Контекст: реестр [protocols/licence_register.json](../protocols/licence_register.json), [docs/84](84_Лицензионный_аудит_2_2026-11-12.md), [docs/90](90_Условия_источников_хеши.md). Связанный тест: [tests/test_licence_series_coverage.py](../tests/test_licence_series_coverage.py). **Это не юридическая консультация.**

## Ограничение независимости и метод
Аудит выполнен отдельным экземпляром той же языковой модели (не человеком) по инструкции Team Lead; он не является юридическим заключением. Страницы извлекались `web_fetch` **2026-11-12** (FRED series pages: GCE, INDPRO, RSAFS; BLS copyright page; BEA FAQ 145). Для остальных BEA-рядов FRED-метка не читалась отдельно: лицензионное основание — публичный домен BEA (первичный якорь `BEA_PRIMARY`, цитата проверена 2026-11-12), FRED-канал — условия `FRED_ALFRED_SERVICES`. Для `PAYEMS` — первичный якорь `BLS_PRIMARY`. Такая «групповая» проверка помечена в реестре полем `label_verification: "publisher_group"`, чтобы не выдавать её за прямое чтение страницы ряда.

**Ограничение хешей.** Хеши считаются по дословным выдержкам условий, а не по байтам HTML: инструмент извлечения возвращает текст, и байтовый хеш был бы невоспроизводим. Выдержки для FRED/Philadelphia/NBER — в [docs/90](90_Условия_источников_хеши.md); для BLS и BEA — ниже в этом документе, поэтому все хеши можно пересчитать офлайн.

## Нормализация `terms-passages-v1`
NFC; каждая последовательность пробельных символов → один ASCII-пробел; обрезка по краям; выдержки соединяются одним `\n` в указанном порядке; `sha256` от UTF-8. Дата извлечения: **2026-11-12**.

## Покрытие рядов data_plan (19/19)

| # | Ряд | Источник / якорь плана | Метка | Проверка метки | Статус | Запись реестра |
|---|---|---|---|---|---|---|
| 1 | `CPIAUCSL` | `ALFRED:CPIAUCSL` | Public Domain: Citation Requested | direct (docs/63) | **OPEN** | `ALFRED:CPIAUCSL` |
| 2 | `CPILFESL` | `ALFRED:CPILFESL` | Public Domain: Citation Requested | direct (docs/63) | **OPEN** | `ALFRED:CPILFESL` |
| 3 | `GDPC1` | `ALFRED:GDPC1` | Public Domain: Citation Requested | direct (docs/63) | **OPEN** | `ALFRED:GDPC1` |
| 4 | `UNRATE` | `ALFRED:UNRATE` | Public Domain: Citation Requested | direct (docs/63) | **OPEN** | `ALFRED:UNRATE` |
| 5 | `GS10` | `ALFRED:GS10` | Public Domain: Citation Requested | direct (docs/63) | **OPEN** | `ALFRED:GS10` |
| 6 | `TB3MS` | `ALFRED:TB3MS` | Public Domain: Citation Requested | direct (docs/63) | **OPEN** | `ALFRED:TB3MS` |
| 7 | `GCE` | `BEA_PRIMARY` | Public Domain: Citation Requested | direct | **OPEN** | `ALFRED:GCE` |
| 8 | `GCEC1` | `BEA_PRIMARY` | Public Domain: Citation Requested | direct (docs/103) | **OPEN** | `ALFRED:GCEC1` |
| 9 | `GPDIC1` | `BEA_PRIMARY` | Public Domain: Citation Requested | direct (docs/103) | **OPEN** | `ALFRED:GPDIC1` |
| 10 | `EXPGSC1` | `BEA_PRIMARY` | Public Domain: Citation Requested | direct (docs/103) | **OPEN** | `ALFRED:EXPGSC1` |
| 11 | `IMPGSC1` | `BEA_PRIMARY` | Public Domain: Citation Requested | direct (docs/103) | **OPEN** | `ALFRED:IMPGSC1` |
| 12 | `GDPDEF` | `BEA_PRIMARY` | Public Domain: Citation Requested | direct (docs/103) | **OPEN** | `ALFRED:GDPDEF` |
| 13 | `A191RL1Q225SBEA` | `BEA_PRIMARY` | Public Domain: Citation Requested | direct (docs/103) | **OPEN** | `ALFRED:A191RL1Q225SBEA` |
| 14 | `PCEPILFE` | `BEA_PRIMARY` | Public Domain: Citation Requested | direct (docs/103) | **OPEN** | `ALFRED:PCEPILFE` |
| 15 | `PAYEMS` | `BLS_PRIMARY` | Public Domain: Citation Requested | direct (docs/103) | **OPEN** | `ALFRED:PAYEMS` |
| 16 | `INDPRO` | `FRED_ALFRED_SERVICES` | Public Domain: Citation Requested | direct | **OPEN** | `ALFRED:INDPRO` |
| 17 | `RSAFS` | `FRED_ALFRED_SERVICES` | Public Domain: Citation Requested | direct | **OPEN** | `ALFRED:RSAFS` |
| 18 | `PCECC96` | `BEA_PRIMARY` | Public Domain: Citation Requested | direct (docs/103) | **OPEN** | `ALFRED:PCECC96` |
| 19 | `USREC` | `ALFRED:USREC` | Copyrighted: Citation Required | direct (docs/63) | **OPEN** | `ALFRED:USREC` |

Итого: **19 из 19** рядов плана имеют запись `ALFRED:<ID>` со статусом **OPEN**; первичные якоря `BEA_PRIMARY` (9 BEA-рядов: `GCE`, `GCEC1`, `GPDIC1`, `EXPGSC1`, `IMPGSC1`, `GDPDEF`, `A191RL1Q225SBEA`, `PCEPILFE`, `PCECC96`) и `BLS_PRIMARY` (PAYEMS) — OPEN; `USREC` использует существующую запись `ALFRED:USREC` и `NBER_CYCLE_DATES`.

Примечания:
- **Прямо прочитаны метки** для `GCE`, `INDPRO`, `RSAFS` (все — «Public Domain: Citation Requested») и ранее для шести рядов ALFRED (docs/63) и `USREC` («Copyrighted: Citation Required»).
- **Прямое чтение меток (2026-11-12)**: все девять рядов, ранее проверенных по группе (`GCEC1`, `GPDIC1`, `EXPGSC1`, `IMPGSC1`, `GDPDEF`, `A191RL1Q225SBEA`, `PCEPILFE`, `PCECC96`, `PAYEMS`), прочитаны на своих FRED-страницах — везде «Public Domain: Citation Requested»; `label_verification` заменён на `direct`, см. [docs/103](103_Лицензии_прямые_метки.md).
- `USREC` — «Copyrighted: Citation Required», используется как машиночитаемая хронология NBER с атрибуцией (запись уже существовала).

## Хеши условий (F-5)

| id записи | Источник | URL | Дата | Выдержек | `terms_sha256` | Где выдержки |
|---|---|---|---|---|---|---|
| `FRED_ALFRED_SERVICES` | FRED/ALFRED Services Terms of Use | https://fred.stlouisfed.org/legal/ | 2026-11-12 | 7 | `949f3f97c2f63710f8320a984c7fad13b7e7e6b784bb69ddc6584610425d816e` | docs/90 |
| `PHILADELPHIA_FED` | Philadelphia Fed Online Terms of Use + SPF data-sources PDF | https://www.philadelphiafed.org/about-us/privacy-notice | 2026-11-12 | 8 | `ab17c14ec9ba1c00dd88931b159d7bcd05c0400fab722459471cd2ae39ffa409` | docs/90 |
| `PHILADELPHIA_FED_RTDSM` | Philadelphia Fed Terms of Use + RTDSM page | https://www.philadelphiafed.org/about-us/privacy-notice | 2026-11-12 | 8 | `7b9fa4b73645989a4029dc5261ad3b1cc143dfacdf190e1e3c2c7b3d547f88bb` | docs/90 |
| `NBER_CYCLE_DATES` | NBER US Business Cycle Expansions and Contractions | https://www.nber.org/research/data/us-business-cycle-expansions-and-contractions | 2026-11-12 | 1 | `514a69fac38d2addf3d8146ff0138480482b3086a9b87fc768d90af48dbf0cca` | docs/90 |
| `BLS_PRIMARY` | BLS Linking and Copyright Information | https://www.bls.gov/bls/linksite.htm | 2026-11-12 | 3 | `4fe8c4947677f201c7104c9695d961147528df9104d99eecd0813859725618d1` | docs/97 (ниже) |
| `BEA_PRIMARY` | BEA FAQ: permission to reproduce information | https://www.bea.gov/help/faq/145 | 2026-11-12 | 3 | `aa210a1bf7edb7ec6a39f086a5e6c5c08411e8c402170290f2bacd11cc0482b2` | docs/97 (ниже) |

### `BLS_PRIMARY` — BLS Linking and Copyright Information

- URL: https://www.bls.gov/bls/linksite.htm; дата извлечения 2026-11-12; `terms_sha256`: `4fe8c4947677f201c7104c9695d961147528df9104d99eecd0813859725618d1`; выдержек: 3.
- Дословно: «The Bureau of Labor Statistics (BLS) is a Federal government agency and everything that we publish, both in hard copy and electronically, is in the public domain, except for previously copyrighted photographs and illustrations.»; «You are free to use our public domain material without specific permission, although we do ask that you cite the Bureau of Labor Statistics as the source.»; «The BLS emblem, which is displayed on the BLS homepage, on BLS publications, and other BLS products, is a federally registered trademark. Unauthorized use of the emblem is prohibited. All rights reserved.».

Вход хеша (`terms-passages-v1`):

<!-- terms-passages: BLS_PRIMARY -->
```text terms-passages
The Bureau of Labor Statistics (BLS) is a Federal government agency and everything that we publish, both in hard copy and electronically, is in the public domain, except for previously copyrighted photographs and illustrations.
You are free to use our public domain material without specific permission, although we do ask that you cite the Bureau of Labor Statistics as the source.
The BLS emblem, which is displayed on the BLS homepage, on BLS publications, and other BLS products, is a federally registered trademark. Unauthorized use of the emblem is prohibited. All rights reserved.
```
<!-- /terms-passages -->

### `BEA_PRIMARY` — BEA FAQ 145

- URL: https://www.bea.gov/help/faq/145; дата извлечения 2026-11-12; `terms_sha256`: `aa210a1bf7edb7ec6a39f086a5e6c5c08411e8c402170290f2bacd11cc0482b2`; выдержек: 3.
- Дословно: «Unless stated otherwise, the information posted on the BEA web site is in the public domain and may be used or reproduced without specific permission.»; «A citation such as "Source: U.S. Bureau of Economic Analysis" would be appreciated.»; «Organizations may use the BEA logo on a web page as an aid to establishing link identity, but use of the logo for any other purpose is prohibited.».

Вход хеша (`terms-passages-v1`):

<!-- terms-passages: BEA_PRIMARY -->
```text terms-passages
Unless stated otherwise, the information posted on the BEA web site is in the public domain and may be used or reproduced without specific permission.
A citation such as "Source: U.S. Bureau of Economic Analysis" would be appreciated.
Organizations may use the BEA logo on a web page as an aid to establishing link identity, but use of the logo for any other purpose is prohibited.
```
<!-- /terms-passages -->

## Что осталось
- **Метки**: все 19 рядов прочитаны напрямую 2026-11-12 (см. docs/103); `publisher_group` в реестре больше не встречается.
- **CI-проверка условий** (предложение, `tools/` вне границ задачи): workflow-шаг перечитывает URL из таблицы, извлекает те же выдержки, нормализует `terms-passages-v1` и сравнивает с `terms_sha256`; расхождение — падение «terms changed, new dated register entry required». Сейчас офлайн-проверку выполняют `tests/test_terms_hashes.py` (4 записи docs/90) и `tests/test_licence_series_coverage.py` (BLS/BEA из docs/97).
- **FRED-MD_FRED-QD** остаётся UNVERIFIED (не используется). **T10Y3M** не используется (docs/90, F-6).
- Условия могут меняться без уведомления: при изменении — новая датированная запись, история не переписывается.

## Итог
Покрытие рядов: **19/19** OPEN-записей в реестре (12 новых `ALFRED:<ID>`, 7 существующих). Хеши условий: 4 источника из docs/90 (FRED, Philadelphia SPF, Philadelphia RTDSM, NBER) + **BLS** и **BEA** в этом документе; все `terms_sha256` 64-hex с датой 2026-11-12. Остаток — только автоматическая CI-проверка изменения условий.
