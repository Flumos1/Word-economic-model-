# 225. Human Counsel: лицензированный юрист по всем юрисдикциям

Дата: 2026-11-22. Статус: **HUMAN_REQUIRED**. Это не AI-юрист и не «человек, нанятый одной
кнопкой»: это контур, который **обязывает** подключить реального лицензированного юриста в каждой
юрисдикции. AI готовит research memo; юридическую силу имеет только подпись человека.

## 0. Почему нельзя «одного юриста на все страны»

Ни один человек не лицензирован во всех 190+ юрисдикциях. Профессиональная модель:

```
international-counsel (AI research)
        ↓ research memo + source plan
lead counsel (лицензированный человек)
        ↓ координация
local counsel (лицензированный человек в стране вопроса)
        ↓ подпись
SIGNED LEGAL OPINION
```

Без local counsel в конкретной стране заключение по этой стране **не выдаётся**. Это не
ограничение, а требование профессиональной ответственности.

## 1. Что создано

| Артефакт | Что |
|---|---|
| `agents/legal/human_counsel/jurisdiction_matrix.json` | **217 стран/экономик** (World Bank list), у каждой: ISO2/ISO3, регион, правовая система, `local_counsel_required=true`, статус `UNASSIGNED`/`PRIORITY_UNASSIGNED`, `counsel_id=null` |
| `panel_registry.json` | пустой реестр реальных юристов; требования: лицензия, good standing, страховка, conflict/sanctions check, engagement letter, NDA, `human_verified_by` |
| `charter.md` | что делает человек, что не делает AI |
| `skills.md` | навыки human/local counsel |
| `engagement_protocol.md` | 9 шагов подключения, NDA, data room |
| `conflict_check.md` | конфликт интересов, санкции, KYC |
| `signoff.md` | формат подписи, запрет AI-подписи |
| `tools/human_counsel.py` | валидатор матрицы/реестра, matter brief, signoff, `--check` |
| `tests/test_human_counsel.py` | 21 тест |

## 2. Матрица

- 217 записей; 12 приоритетных: US, GB, DE, FR, NL, CH, AE, KZ, UA, PL, TR, SG.
- `legal_system`: `COMMON_LAW`, `CIVIL_LAW`, `TO_BE_CONFIRMED` — **не выдумываем** систему права
  там, где её нужно подтверждать local counsel.
- **ни одного назначенного юриста**: все `counsel_id=null`; это честный старт, а не фиктивный пул.
- `PRIORITY_UNASSIGNED` — «сюда ищем юриста в первую очередь».

## 3. Как подключить

1. Lead фиксирует вопрос, юрисдикции, сторону, срок, бюджет.
2. `python tools/human_counsel.py --matter --jurisdiction DE --question "..."`.
3. Если `status=UNASSIGNED`, инструмент **отказывает**: сначала engagement.
4. Проверка лицензии/бара/good standing/страховки/конфликта/санкций.
5. NDA/engagement letter; data room.
6. Human counsel проверяет мемо и подписывает `signoff`.
7. Результат — в `knowledge_archive`, access=PROJECT/RESTRICTED, без контактов в git.

## 4. Что запрещено

- AI-подпись, AI-верификация, «модель проверила лицензию»;
- выдуманные имена, бары, лицензии, телефоны, email;
- заключение по юрисдикции без local counsel;
- обход санкций/конфликта/NDA;
- обещание результата;
- публикация персональных данных юристов в git.

## 5. Ограничения

- Пул пуст: реальных юристов нужно нанять/законтрактовать; матрица — карта покрытия, не
  подтверждение компетенции.
- `legal_system` у части стран `TO_BE_CONFIRMED` — это честно.
- AI-мемо — research, не legal advice; подпись только human.
- Персональные данные юристов — вне git, в защищённом реестре.

## 6. Команды

```powershell
python tools/human_counsel.py --check
python tools/human_counsel.py --matrix --json
python tools/human_counsel.py --jurisdictions --region Europe
python tools/human_counsel.py --matter --jurisdiction DE --question "..."
python tools/human_counsel.py --signoff .tmp/signoff.json
python -m unittest tests.test_human_counsel
```
