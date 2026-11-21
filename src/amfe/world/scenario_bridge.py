"""Мост блоков 15/17: институты × технологии → пять явных макро-каналов (docs/210).

Блок 15 (:mod:`amfe.world.institutions`) отвечает на вопрос «что если правила сдвинутся на N
пунктов»: композит 0–100, премия в б.п., инвестиции и рост через объявленные эластичности.
Блок 17 (:mod:`amfe.world.technology`) отвечает на вопрос «что если диффузия пойдёт быстрее»:
логистическое внедрение → TFP → выпуск и энергия. Оба блока — исследовательские прототипы на
допущениях, без данных, и оба выдают числа, которые нельзя складывать «как есть»: б.п., п.п. ВВП
и проценты — разные единицы (docs/07, P-010).

Этот модуль не считает новую экономику. Он делает три вещи:

1. **Композит.** Прогоняет один профиль институтов через сценарий блока 15 и один сценарий
   диффузии через блок 17 на **одном горизонте** ``H`` и собирает из них единый отчёт
   ``amfe.world.scenario_bridge.v1``: пять каналов, веса, полярности, проверки, допущения,
   ограничения, digest. Внутренние отчёты блоков не пересчитываются — они вызываются как есть,
   их digest'ы попадают в провенанс (никакой второй реализации уравнений).
2. **Явные каналы (ровно пять).** У каждого канала один владелец-блок, одна единица, один
   знак-полярность и один объявленный опорный масштаб::

       risk_premium_bps        block 15,  б.п.        полярность −1
       investment_delta_pp     block 15,  п.п. ВВП    полярность +1
       tfp_delta_pct           block 17,  %           полярность +1
       output_delta_pct        block 15+17, %         полярность +1
       energy_demand_delta_pct block 17,  %           полярность  0 (направление не «лучше/хуже»)

   Каналы блока 17 считаются **относительно объявленной базовой диффузии** (``baseline``), а
   каналы блока 15 — как отклонение от ``STATUS-QUO`` (нулевой сдвиг). Поэтому пара
   ``STATUS-QUO × baseline`` — контрольная линия: все пять каналов ровно нулевые.
3. **Агрегация с весами.** Из пяти каналов собирается безразмерный ``scenario_index``:
   ``Σ_c w_c · polarity_c · (channel_c / scale_c)``. Веса, полярности и масштабы — **конвенция**
   (допущение B5/B6), а не оценка; индекс не является ни ВВП, ни благосостоянием и не сравнивает
   сценарии «по вероятности». Энергия входит в отчёт, но не в индекс: рост энергоспроса не
   «плох» и не «хорош» сам по себе — это арифметика блока 17 (docs/204 §2.3).

Единственный настоящий «мост» — канал выпуска::

    output_delta_pct = 100·(Y_scen/Y_base − 1)  +  Δgrowth_pp · H
                       └── блок 17 (TFP, K, L)      └── блок 15 (п.п. потенциального роста в год,
                                                         линейное накопление, без компаундинга)

Это аддитивное разложение (проверяется ``checks.additivity_output``), а не произведение
эффектов: взаимодействие «институты × диффузия» не моделируется и заявлено как ограничение.

Чего здесь нет
--------------
* данных: ни одного ряда, ни одной страны, ни сети, ни часов, ни ``random``; профили —
  ``EXAMPLE-*`` (иллюстрация) или пользовательский JSON (``USER_INPUT``), и это не рейтинг;
* калибровки: все числа ``ASSUMPTION_BASED``, ``not_calibrated=true``;
* MRIO: ``technology_shock``/``delta_A`` **не применяются**, ``io_core`` не вызывается и не
  меняется — мост выдаёт числа для сценарных библиотек, а не сдвиг коэффициентов ``A``;
* вероятностей, прогноза и причинности: ``admission=false``, ``gating=false``,
  ``causal_claim=false``; индекс не является ожидаемым эффектом.

Детерминизм: чистовая арифметика на объявленных входах, фиксированный порядок сценариев и
ключей, никакого wall-clock; два прогона на одинаковых входах дают байт-идентичный canonical
JSON и одинаковый SHA-256 (``digest``).
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Mapping, Sequence

from amfe.world import institutions, technology

# ------------------------------------------------------------------------------------ константы
SCHEMA = "amfe.world.scenario_bridge.v1"
MODULE_ID = "world.scenario_bridge"
MODULE_VERSION = "1.0.0"
BLOCKS = (15, 17)
STATUS = "RESEARCH_ONLY"
ADMISSION = False
GATING = False
CAUSAL_CLAIM = False
DATA_STATUS = "ASSUMPTION_BASED"
CALIBRATION = "NOT_CALIBRATED"
NOT_CALIBRATED = True
CLASSIFICATION = "ASSUMPTION_BASED"
STATUS_NOTE = ("Мост блоков 15/17: пять явных каналов и безразмерный индекс на объявленных "
               "допущениях; не прогноз, не допуск, не причинная оценка; admission=false, "
               "gating=false")
TOLERANCE = 1e-9
ZERO_TOLERANCE = 1e-12

#: Горизонт моста — пересечение допустимых горизонтов обоих блоков: [1, 50].
MIN_HORIZON_YEARS = max(technology.MIN_HORIZON_YEARS, 1)
MAX_HORIZON_YEARS = min(technology.MAX_HORIZON_YEARS, institutions.MAX_HORIZON)
DEFAULT_HORIZON_YEARS = 10
DEMO_HORIZON_YEARS = 10

#: Премия за риск не может измениться сильнее, чем позволяет потолок блока 15.
MAX_ABS_RISK_PREMIUM_BPS = institutions.OverlayParameters().premium_cap_bps
#: Инвестиции/ВВП живут в [0, 1], значит |Δ| не больше 100 п.п.
MAX_ABS_INVESTMENT_PP = 100.0
#: Процентные каналы: уровень не может упасть ниже нуля (−100 %), верх — объявленная граница.
MAX_ABS_PCT = 500.0
MIN_PCT_LEVEL = -100.0
#: Безразмерный индекс: объявленная граница правдоподобия, а не экономический смысл.
MAX_ABS_INDEX = 100.0
#: Допуск структурного тождества «технологический выпуск = TFP» при экзогенных K и L. Он шире
#: TOLERANCE, потому что пути блока 17 передаются с округлением до 12 знаков.
IDENTITY_TOLERANCE = 1e-7

#: Пять каналов. ``owner`` — блок-владелец, ``polarity`` — знак «лучше/хуже» для индекса
#: (0 = направление не интерпретируется), ``scale`` — объявленный опорный масштаб.
CHANNELS: tuple[dict, ...] = (
    {
        "id": "risk_premium_bps",
        "label_ru": "Премия за риск",
        "unit": "б.п.",
        "owner": "block_15_institutions",
        "polarity": -1.0,
        "scale": 25.0,
        "in_index": True,
        "formula": ("premium_H − premium_base = β·(score_base − score_H) + "
                    "γ·(dispersion_H − dispersion_base), clip в [0, premium_cap]"),
        "source": "docs/203 §2.3",
    },
    {
        "id": "investment_delta_pp",
        "label_ru": "Инвестиции/ВВП",
        "unit": "п.п. ВВП",
        "owner": "block_15_institutions",
        "polarity": 1.0,
        "scale": 0.25,
        "in_index": True,
        "formula": "e_inv · (score_H − score_base), e_inv = 0,04 п.п. на пункт",
        "source": "docs/203 §2.4",
    },
    {
        "id": "tfp_delta_pct",
        "label_ru": "TFP (производительность)",
        "unit": "% к базовой диффузии",
        "owner": "block_17_technology",
        "polarity": 1.0,
        "scale": 5.0,
        "in_index": True,
        "formula": "100·(A_scen/A_base − 1), A = Π_i A_i^{w_i}, A_i = exp(e_i·(x_i(H) − start_i))",
        "source": "docs/204 §2.2 (гипотеза H-005)",
    },
    {
        "id": "output_delta_pct",
        "label_ru": "Выпуск",
        "unit": "% к базовой диффузии",
        "owner": "block_15_plus_block_17",
        "polarity": 1.0,
        "scale": 5.0,
        "in_index": True,
        "formula": ("100·(Y_scen/Y_base − 1) + Δgrowth_pp·H; первое слагаемое — блок 17 "
                    "(growth accounting), второе — блок 15 (линейное накопление)"),
        "source": "docs/204 §2.2 + docs/203 §2.4",
    },
    {
        "id": "energy_demand_delta_pct",
        "label_ru": "Спрос на энергию",
        "unit": "% к базовой диффузии",
        "owner": "block_17_technology",
        "polarity": 0.0,
        "scale": 5.0,
        "in_index": False,
        "formula": ("100·(E_scen/E_base − 1), E_i = Y_i^{energy_output_elasticity} · "
                    "exp(−energy_efficiency_gain·(x_i(H) − start_i))"),
        "source": "docs/204 §2.3",
    },
)
CHANNEL_IDS: tuple[str, ...] = tuple(item["id"] for item in CHANNELS)
CHANNEL_BY_ID = {item["id"]: item for item in CHANNELS}

#: Веса по умолчанию. Это **конвенция внимания**, а не оценка важности: сумма = 1, и каждый
#: канал получает долю независимо от того, насколько он «важен» в реальности.
DEFAULT_WEIGHTS: dict[str, float] = {
    "risk_premium_bps": 0.22,
    "investment_delta_pp": 0.18,
    "tfp_delta_pct": 0.25,
    "output_delta_pct": 0.25,
    "energy_demand_delta_pct": 0.10,
}

#: Демо-набор пар (институциональный сценарий, технологический сценарий): контроль, чистые
#: эффекты и три комбинации, на которых видно, что каналы разные.
DEMO_PAIRS: tuple[tuple[str, str], ...] = (
    ("STATUS-QUO", "baseline"),
    ("REFORM", "baseline"),
    ("DETERIORATION", "baseline"),
    ("POLARIZATION", "baseline"),
    ("REFORM", "ai_acceleration"),
    ("REFORM", "ai_slowdown"),
    ("REFORM", "energy_transition"),
    ("DETERIORATION", "ai_slowdown"),
)

#: Допущения. Каждое число отчёта трассируется к одному из них.
ASSUMPTIONS: tuple[dict, ...] = (
    {"id": "B1", "kind": "ASSUMPTION_BASED",
     "statement": ("Пять каналов — объявленная арифметика двух исследовательских блоков "
                   "(COMPOSITE), а не оценка эффекта институтов или технологий; ни один вход не "
                   "является наблюдением."),
     "replace_with": "оценённые каналы после калибровки блоков 15/17 на лицензированных рядах"},
    {"id": "B2", "kind": "ASSUMPTION_BASED",
     "statement": ("Канал выпуска аддитивен: output = технологический прирост выпуска (блок 17) + "
                   "Δgrowth_pp·H (блок 15, линейное накопление п.п. потенциального роста за H лет); "
                   "компаундинга, уровневых эффектов и взаимодействия блоков нет."),
     "replace_with": "совместная модель роста с уровневыми эффектами и проверкой пересечения каналов"},
    {"id": "B3", "kind": "ASSUMPTION_BASED",
     "statement": ("Каналы блока 17 считаются ОТНОСИТЕЛЬНО объявленной базовой диффузии "
                   "(сценарий baseline), а не относительно «мира без технологий»; при выборе "
                   "baseline три технологических канала равны нулю по построению."),
     "replace_with": "явная контрфактическая база (сценарий без внедрения) после решения Lead"},
    {"id": "B4", "kind": "ASSUMPTION_BASED",
     "statement": ("Каналы блока 15 берутся в точке горизонта H (не при полной рампе): если "
                   "ramp_years > H, эффект в отчёте частичный, флаг converged_at_horizon = false; "
                   "дисконтирования и приведённой стоимости нет."),
     "replace_with": "квартальная траектория с датированными событиями и ставкой дисконтирования"},
    {"id": "B5", "kind": "ASSUMPTION_BASED",
     "statement": ("Веса (0,22 / 0,18 / 0,25 / 0,25 / 0,10) и полярности каналов — конвенция "
                   "отчёта, а не оценка важности; полярность энергии 0: рост энергоспроса не "
                   "трактуется как «плохо» или «хорошо»; TFP и выпуск при экзогенных K/L "
                   "совпадают, поэтому индекс взвешивает TFP дважды — это свойство конвенции."),
     "replace_with": "веса из предпочтений пользователя/элицитации с проверкой устойчивости"},
    {"id": "B6", "kind": "ASSUMPTION_BASED",
     "statement": ("Опорные масштабы индекса (25 б.п., 0,25 п.п. ВВП, 5 %) — объявленные "
                   "величины для безразмерной нормировки, не пороги и не «значимые» эффекты."),
     "replace_with": "масштабы из исторической волатильности каналов (нужны данные)"},
    {"id": "B7", "kind": "ASSUMPTION_BASED",
     "statement": ("Блоки независимы: институциональный сценарий не меняет параметры диффузии, "
                   "технологический сценарий не меняет качество институтов; вклад институтов в "
                   "энергию не моделируется (энергия — только технологический канал)."),
     "replace_with": "модель взаимодействия (институты → скорость внедрения) с проверкой на данных"},
    {"id": "B8", "kind": "ASSUMPTION_BASED",
     "statement": ("Горизонт годовой и общий для обоих блоков, H ∈ [1, 50]; рампа блока 15 "
                   "линейная (min(1, t/ramp_years)), диффузия блока 17 — логистическая замкнутая "
                   "форма; частота и лаги не оценены."),
     "replace_with": "квартальный шаг и оценённая лаговая структура"},
    {"id": "B9", "kind": "ASSUMPTION_BASED",
     "statement": ("Модуль не читает данные, сеть и часы; профиль — EXAMPLE-* (иллюстрация) или "
                   "пользовательский JSON (USER_INPUT), блока не проверяет его на истинность; "
                   "страновой таблицы нет."),
     "replace_with": "калиброванные страновые профили с PIT-датами публикации"},
    {"id": "B10", "kind": "SCENARIO",
     "statement": ("Мост не применяет сдвиг коэффициентов MRIO: technology_shock/delta_A не "
                   "вызываются, io_core не изменяется; пять каналов — числа для сценарных "
                   "библиотек, admission=false, вероятности не оцениваются, причинность не "
                   "утверждается."),
     "replace_with": "явный маппинг «агрегат → 50 отраслей» и отдельная задача на ΔA (docs/204 §9)"},
)

LIMITATIONS = (
    "данных и калибровки нет: оба блока — допущения (A1–A10 в docs/203, §2 в docs/204), поэтому "
    "каналы показывают направление и порядок при объявленных параметрах, а не ожидаемый эффект;",
    "аддитивность выпуска — линейная аппроксимация: технологический прирост (проценты) и "
    "накопленный Δgrowth_pp·H складываются, а не перемножаются; при больших шоках ошибка растёт;",
    "каналы блока 17 относительны базовой диффузии baseline, а не «мира без технологий»: это "
    "выбор контрфактической базы, а не свойство экономики;",
    "при экзогенных K и L (допущение блока 17) относительный технологический выпуск совпадает с "
    "относительным TFP по построению (проверка identity_output_tfp), поэтому в индексе TFP "
    "фактически взвешивается дважды (0,25 + 0,25); каналы сохранены как разные вопросы, но "
    "двойной счёт в индексе — свойство конвенции, а не экономики;",
    "энергия — только технологический канал: институциональный сценарий энергоспроса не меняет "
    "(нет эластичности «качество → энергия»);",
    "scenario_index — конвенция (веса, полярности, масштабы), а не благосостояние, не ВВП и не "
    "вероятность; энергия в индекс не входит; смена весов меняет индекс, не меняя каналы;",
    "взаимодействия блоков нет: институты не ускоряют диффузию, технологии не улучшают "
    "институты; обратных связей и лагов (кроме рамп) нет;",
    "мост не трогает MRIO: ΔA не считается, technology_shock не вызывается, коэффициенты A не "
    "меняются; это числа для сценарного слоя, а не шок модели;",
    "не прогноз, не вероятность и не причинная оценка: admission=false, gating=false, "
    "causal_claim=false; числа нельзя цитировать как ожидаемый эффект реформы или ИИ.",
)

#: Ключи, которых в отчёте быть не может (сырые ряды, матрицы, страновые таблицы).
FORBIDDEN_KEYS = frozenset({
    "Z", "x", "y", "raw", "raw_data", "observations", "micro", "rows", "series",
    "indicator_series", "wgi_values", "icrg_values", "by_sector", "source_rows",
    "country_data", "delta_A", "matrix", "mrio", "coefficients",
})


class BridgeError(ValueError):
    """Недопустимый вход или проваленная проверка: fail-closed (CLI exit 1, ничего не пишется)."""


# ---------------------------------------------------------------------------------------- helpers
def _finite(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise BridgeError(f"{name}: ожидается число, получено {value!r}") from exc
    if not math.isfinite(number):
        raise BridgeError(f"{name}: число должно быть конечным, получено {value!r}")
    return number


def _non_negative(value: object, name: str) -> float:
    number = _finite(value, name)
    if number < 0.0:
        raise BridgeError(f"{name}: должно быть >= 0, получено {number!r}")
    return number


def _positive(value: object, name: str) -> float:
    number = _finite(value, name)
    if number <= 0.0:
        raise BridgeError(f"{name}: должно быть > 0, получено {number!r}")
    return number


def _horizon(value: object, name: str = "horizon_years") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        else:
            raise BridgeError(f"{name}: ожидается целое число, получено {value!r}")
    if not MIN_HORIZON_YEARS <= int(value) <= MAX_HORIZON_YEARS:
        raise BridgeError(f"{name}: должно лежать в [{MIN_HORIZON_YEARS}, {MAX_HORIZON_YEARS}], "
                          f"получено {value!r}")
    return int(value)


def _q(value: float, digits: int = 12) -> float:
    return round(float(value), digits)


def canonical_json(payload: object) -> str:
    """Стабильный canonical JSON (sort_keys, без пробелов) — база для digest."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(payload: object) -> str:
    """SHA-256 от canonical JSON; payload не должен содержать собственный ``digest``."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def assumptions() -> list[dict]:
    return [dict(item) for item in ASSUMPTIONS]


def limitations() -> list[str]:
    return list(LIMITATIONS)


def channels_metadata() -> list[dict]:
    return [dict(item) for item in CHANNELS]


def normalise_weights(weights: Mapping | None = None) -> dict:
    """Проверить и нормировать веса каналов так, чтобы сумма равнялась 1.

    Частичный набор — ошибка: молча подставить вес по умолчанию значило бы изменить отчёт
    незаметно. Веса конечны, неотрицательны и не все нулевые.
    """
    if weights is None:
        values = dict(DEFAULT_WEIGHTS)
    else:
        if not isinstance(weights, Mapping):
            raise BridgeError("weights: ожидается объект {канал: вес}")
        unknown = sorted(str(key) for key in weights if key not in CHANNEL_IDS)
        if unknown:
            raise BridgeError(f"weights: неизвестные каналы {unknown}")
        missing = [key for key in CHANNEL_IDS if key not in weights]
        if missing:
            raise BridgeError(f"weights: не заданы веса для {missing}")
        values = {key: _non_negative(weights[key], f"weights[{key!r}]") for key in CHANNEL_IDS}
    total = math.fsum(values.values())
    if total <= 0.0:
        raise BridgeError("weights: сумма весов должна быть > 0")
    return {key: values[key] / total for key in CHANNEL_IDS}


def channel_universe(weights: Mapping | None = None) -> list[dict]:
    """Метаданные каналов с весами и флагом участия в индексе (для отчёта и Markdown)."""
    normalised = normalise_weights(weights)
    rows = []
    for item in channels_metadata():
        row = dict(item)
        row["weight"] = normalised[item["id"]]
        row["included_in_index"] = bool(item["polarity"] != 0.0)
        row["index_term_scale"] = _positive(item["scale"], f"scale[{item['id']!r}]")
        rows.append(row)
    return rows


def _same_sign(left: float, right: float) -> bool:
    if abs(left) <= ZERO_TOLERANCE and abs(right) <= ZERO_TOLERANCE:
        return True
    return (left > 0.0 and right > 0.0) or (left < 0.0 and right < 0.0)


def _opposite_sign(left: float, right: float) -> bool:
    if abs(left) <= ZERO_TOLERANCE and abs(right) <= ZERO_TOLERANCE:
        return True
    return (left > 0.0 and right < 0.0) or (left < 0.0 and right > 0.0)


def _relative_pct(numerator: float, denominator: float, name: str) -> float:
    if denominator <= 0.0:
        raise BridgeError(f"{name}: база должна быть > 0, получено {denominator!r}")
    return 100.0 * (numerator / denominator - 1.0)


def forbidden_keys(node: object, path: str = "") -> list[str]:
    """Пути ключей, похожих на сырые данные (для ``checks.no_raw_data``)."""
    found: list[str] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            if str(key) in FORBIDDEN_KEYS:
                found.append(f"{path}/{key}")
            found.extend(forbidden_keys(value, f"{path}/{key}"))
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            found.extend(forbidden_keys(value, f"{path}[{index}]"))
    return found


def _long_numeric_lists(node: object, horizon: int, path: str = "") -> list[str]:
    """Числовые списки длиннее ``horizon + 1`` — признак сырого ряда, а не агрегата."""
    found: list[str] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            found.extend(_long_numeric_lists(value, horizon, f"{path}/{key}"))
    elif isinstance(node, (list, tuple)):
        if node and all(isinstance(item, (int, float)) and not isinstance(item, bool)
                        for item in node):
            if len(node) > horizon + 1:
                found.append(f"{path}[len={len(node)}]")
            return found
        for index, value in enumerate(node):
            found.extend(_long_numeric_lists(value, horizon, f"{path}[{index}]"))
    return found


# -------------------------------------------------------------------------------------- профиль
def resolve_profile(profile: object = None) -> institutions.InstitutionProfile:
    """``None`` → EXAMPLE-MID, ``EXAMPLE-*`` → встроенный профиль, путь → пользовательский JSON."""
    if profile is None:
        return institutions.find_example_profile("EXAMPLE-MID")
    if isinstance(profile, institutions.InstitutionProfile):
        return profile
    if not isinstance(profile, (str, Path)):
        raise BridgeError("profile: ожидается EXAMPLE-*, путь к JSON или InstitutionProfile")
    text = str(profile).strip()
    if not text:
        raise BridgeError("profile: пустое имя профиля")
    looks_like_file = text.lower().endswith(".json") or Path(text).is_file()
    if looks_like_file:
        # FileNotFoundError пробрасывается намеренно: CLI отвечает на него exit 2 (BLOCKED_INPUT).
        return institutions.load_profile_file(text)
    return institutions.find_example_profile(text)


def _normalise_institution_id(value: object) -> str:
    text = str(value).strip().upper().replace("_", "-")
    if not text:
        raise BridgeError("institutions-scenario: пустое значение")
    return text


def _normalise_technology_id(value: object) -> str:
    text = str(value).strip().lower().replace("-", "_")
    if not text:
        raise BridgeError("technology-scenario: пустое значение")
    return text


def resolve_institution_scenarios(ids: Sequence[str] | None) -> list[str]:
    """Идентификаторы сценариев блока 15 в порядке объявления, без дублей."""
    declared = [scenario.scenario_id for scenario in institutions.default_scenarios()]
    if ids is None:
        return declared
    wanted = []
    for item in ids:
        normalised = _normalise_institution_id(item)
        if normalised not in declared:
            raise BridgeError(f"institutions-scenario: неизвестный сценарий {item!r}; "
                              f"допустимы {declared}")
        if normalised not in wanted:
            wanted.append(normalised)
    if not wanted:
        raise BridgeError("institutions-scenario: не выбрано ни одного сценария")
    return [scenario_id for scenario_id in declared if scenario_id in wanted]


def resolve_technology_scenarios(ids: Sequence[str] | None) -> list[str]:
    """Идентификаторы сценариев блока 17 в порядке объявления, без дублей."""
    declared = list(technology.SCENARIOS)
    if ids is None:
        return declared
    wanted = []
    for item in ids:
        normalised = _normalise_technology_id(item)
        if normalised not in declared:
            raise BridgeError(f"technology-scenario: неизвестный сценарий {item!r}; "
                              f"допустимы {declared}")
        if normalised not in wanted:
            wanted.append(normalised)
    if not wanted:
        raise BridgeError("technology-scenario: не выбрано ни одного сценария")
    return [scenario_id for scenario_id in declared if scenario_id in wanted]


# ------------------------------------------------------------------------------------ блок 15
def _institution_row(profile, scenario, parameters) -> dict:
    report = institutions.scenario_overlay(profile, scenario, parameters)
    point = report["overlay"]["path"][-1]
    return {
        "scenario_id": scenario.scenario_id,
        "title_ru": scenario.title_ru,
        "kind": scenario.kind,
        "magnitude": scenario.magnitude,
        "ramp_years": scenario.ramp_years,
        "ramp_at_horizon": point["ramp"],
        "converged_at_horizon": abs(point["ramp"] - 1.0) <= institutions.TOLERANCE,
        "score_base": report["overlay"]["base"]["score"],
        "score_at_horizon": point["score"],
        "delta_score_points": point["delta_score_points"],
        "dispersion_base": report["overlay"]["base"]["dispersion"],
        "dispersion_at_horizon": point["dispersion"],
        "risk_premium_base_bps": report["overlay"]["base"]["risk_premium_bps"],
        "risk_premium_bps": point["risk_premium_bps"],
        "delta_risk_premium_bps": point["delta_risk_premium_bps"],
        "investment_base_ratio": report["overlay"]["base"]["investment_ratio"],
        "investment_ratio_at_horizon": point["investment_ratio"],
        "delta_investment_pp": point["delta_investment_pp"],
        "delta_growth_pp": point["delta_growth_pp"],
        "digest": report["digest"],
    }


def _institution_inputs(profile, scenario_ids, parameters) -> dict:
    rows = {}
    for scenario in institutions.default_scenarios():
        if scenario.scenario_id in scenario_ids:
            rows[scenario.scenario_id] = _institution_row(profile, scenario, parameters)
    return rows


# ------------------------------------------------------------------------------------ блок 17
def _technology_row(document: dict, horizon: int) -> dict:
    aggregate = document["aggregate"]
    summary = aggregate["horizon"]
    return {
        "scenario_id": document["scenario_id"],
        "title_ru": document["title_ru"],
        "multipliers": {
            "speed_multiplier": document["parameters"]["speed_multiplier"],
            "ceiling_multiplier": document["parameters"]["ceiling_multiplier"],
            "tfp_multiplier": document["parameters"]["tfp_multiplier"],
            "energy_efficiency_multiplier":
                document["parameters"]["energy_efficiency_multiplier"],
        },
        "years": summary["years"],
        "adoption": summary["adoption"],
        "delta_adoption": summary["delta_adoption"],
        "tfp_ratio": aggregate["paths"]["tfp_ratio"][horizon],
        "output_ratio": aggregate["paths"]["output_ratio"][horizon],
        "energy_ratio": aggregate["paths"]["energy_ratio"][horizon],
        "energy_intensity_ratio": aggregate["paths"]["energy_intensity_ratio"][horizon],
        "tfp_gain_pct": summary["tfp_gain_pct"],
        "output_gain_pct": summary["output_gain_pct"],
        "energy_change_pct": summary["energy_change_pct"],
        "energy_intensity_change_pct": summary["energy_intensity_change_pct"],
        "identity_residual": summary["identity_residual"],
        "sector_weights": dict(aggregate["sector_weights"]),
        "checks_pass": bool(all(document["checks"].values())
                            and all(aggregate["checks"].values())),
    }


def _technology_inputs(scenario_ids, horizon: int) -> tuple[dict, dict]:
    """Один отчёт блока 17 на все нужные сценарии (baseline добавляется всегда) + строки."""
    wanted = set(scenario_ids) | {"baseline"}
    requested = [scenario_id for scenario_id in technology.SCENARIOS if scenario_id in wanted]
    report = technology.build_report(requested, None, horizon)
    documents = {item["scenario_id"]: item for item in report["scenarios"]}
    # Базовая диффузия нужна всегда: относительно неё считаются три технологических канала.
    rows = {scenario_id: _technology_row(documents[scenario_id], horizon)
            for scenario_id in requested}
    return report, rows


# ------------------------------------------------------------------------------------------ кейс
def _compose_case(institution: dict, tech: dict, base: dict, weights: Mapping,
                  horizon: int) -> dict:
    """Собрать один кейс (сценарий 15 × сценарий 17): пять каналов, разложение, индекс, проверки."""
    technology_output_pct = _relative_pct(tech["output_ratio"], base["output_ratio"],
                                          "technology.output_ratio")
    technology_energy_pct = _relative_pct(tech["energy_ratio"], base["energy_ratio"],
                                          "technology.energy_ratio")
    technology_tfp_pct = _relative_pct(tech["tfp_ratio"], base["tfp_ratio"],
                                       "technology.tfp_ratio")
    institutions_growth_pct = institution["delta_growth_pp"] * horizon
    output_delta_pct = technology_output_pct + institutions_growth_pct
    adoption_delta = tech["adoption"] - base["adoption"]

    values = {
        "risk_premium_bps": _q(institution["delta_risk_premium_bps"]),
        "investment_delta_pp": _q(institution["delta_investment_pp"]),
        "tfp_delta_pct": _q(technology_tfp_pct),
        "output_delta_pct": _q(output_delta_pct),
        "energy_demand_delta_pct": _q(technology_energy_pct),
    }

    terms = {channel: weights[channel] * CHANNEL_BY_ID[channel]["polarity"]
             * values[channel] / CHANNEL_BY_ID[channel]["scale"]
             for channel in CHANNEL_IDS}
    contributions = {channel: _q(terms[channel]) for channel in CHANNEL_IDS}
    index_value = _q(math.fsum(contributions.values()))
    raw_index = math.fsum(terms.values())

    parts = {
        "technology_output_pct": _q(technology_output_pct),
        "institutions_growth_pct": _q(institutions_growth_pct),
    }
    decomposition = {
        "output": {
            **parts,
            "total_pct": values["output_delta_pct"],
            "residual_pct": _q(output_delta_pct - parts["technology_output_pct"]
                               - parts["institutions_growth_pct"]),
            "note": ("output = технологический прирост выпуска (блок 17) + Δgrowth_pp·H "
                     "(блок 15); линейная аддитивность — допущение B2"),
        },
        "tfp": {"technology_tfp_pct": values["tfp_delta_pct"],
                "note": "TFP — только технологический канал (гипотеза H-005)"},
        "energy": {"technology_energy_pct": values["energy_demand_delta_pct"],
                   "note": ("энергия — только технологический канал; блок 15 её не меняет "
                            "(допущение B7)")},
        "adoption": {"delta_adoption_share": _q(adoption_delta),
                     "scenario_adoption": tech["adoption"],
                     "baseline_adoption": base["adoption"]},
    }

    case = {
        "case_id": f"{institution['scenario_id']}+{tech['scenario_id']}",
        "institutions_scenario": institution["scenario_id"],
        "technology_scenario": tech["scenario_id"],
        "channels": values,
        "channel_units": {channel: CHANNEL_BY_ID[channel]["unit"] for channel in CHANNEL_IDS},
        "decomposition": decomposition,
        "index": {
            "value": index_value,
            "raw_value": _q(raw_index),
            "units": "доли объявленных опорных масштабов (безразмерно)",
            "contributions": contributions,
            "weight_sum": _q(math.fsum(weights.values())),
            "included_weight_sum": _q(math.fsum(
                weights[channel] for channel in CHANNEL_IDS
                if CHANNEL_BY_ID[channel]["polarity"] != 0.0)),
            "interpretation": ("> 0 — сценарий смещает каналы в сторону объявленных полярностей, "
                               "< 0 — против; 0 — смещения нет; индекс не является ни ВВП, ни "
                               "благосостоянием, ни вероятностью"),
        },
        "blocks": {
            "institutions": {
                "scenario_id": institution["scenario_id"],
                "kind": institution["kind"],
                "magnitude": institution["magnitude"],
                "ramp_years": institution["ramp_years"],
                "ramp_at_horizon": institution["ramp_at_horizon"],
                "converged_at_horizon": institution["converged_at_horizon"],
                "score_base": institution["score_base"],
                "score_at_horizon": institution["score_at_horizon"],
                "delta_score_points": institution["delta_score_points"],
                "dispersion_base": institution["dispersion_base"],
                "dispersion_at_horizon": institution["dispersion_at_horizon"],
                "risk_premium_base_bps": institution["risk_premium_base_bps"],
                "risk_premium_bps": institution["risk_premium_bps"],
                "investment_ratio_at_horizon": institution["investment_ratio_at_horizon"],
                "delta_growth_pp": institution["delta_growth_pp"],
                "digest": institution["digest"],
            },
            "technology": {
                "scenario_id": tech["scenario_id"],
                "multipliers": dict(tech["multipliers"]),
                "adoption_at_horizon": tech["adoption"],
                "delta_adoption": tech["delta_adoption"],
                "tfp_gain_pct_absolute": tech["tfp_gain_pct"],
                "output_gain_pct_absolute": tech["output_gain_pct"],
                "energy_change_pct_absolute": tech["energy_change_pct"],
                "energy_intensity_change_pct_absolute": tech["energy_intensity_change_pct"],
                "identity_residual": tech["identity_residual"],
            },
        },
        "checks": {},
    }

    checks = {
        "channels_finite": all(math.isfinite(value) for value in values.values()),
        "risk_premium_bounds": abs(values["risk_premium_bps"]) <= MAX_ABS_RISK_PREMIUM_BPS,
        "investment_bounds": abs(values["investment_delta_pp"]) <= MAX_ABS_INVESTMENT_PP,
        "tfp_bounds": abs(values["tfp_delta_pct"]) <= MAX_ABS_PCT,
        "output_bounds": abs(values["output_delta_pct"]) <= MAX_ABS_PCT,
        "energy_bounds": abs(values["energy_demand_delta_pct"]) <= MAX_ABS_PCT,
        "levels_non_negative": (values["output_delta_pct"] > MIN_PCT_LEVEL
                                and values["energy_demand_delta_pct"] > MIN_PCT_LEVEL
                                and values["tfp_delta_pct"] > MIN_PCT_LEVEL),
        "index_bounds": abs(index_value) <= MAX_ABS_INDEX,
        "weights_normalised": abs(math.fsum(weights.values()) - 1.0) <= TOLERANCE,
        "additivity_index": (abs(math.fsum(contributions.values()) - index_value) <= TOLERANCE
                             and abs(raw_index - index_value) <= TOLERANCE),
        "additivity_output": abs(parts["technology_output_pct"]
                                 + parts["institutions_growth_pct"]
                                 - values["output_delta_pct"]) <= TOLERANCE,
        "sign_premium_opposes_quality": _opposite_sign(values["risk_premium_bps"],
                                                       institution["delta_score_points"]),
        "sign_investment_follows_quality": _same_sign(values["investment_delta_pp"],
                                                      institution["delta_score_points"]),
        "sign_tfp_follows_adoption": _same_sign(values["tfp_delta_pct"], adoption_delta),
        "sign_output_follows_parts": _same_sign(
            values["output_delta_pct"], parts["technology_output_pct"]
            + parts["institutions_growth_pct"]),
        # K и L экзогенны и одинаковы во всех сценариях блока 17, поэтому относительный
        # технологический выпуск обязан совпасть с относительным TFP (growth accounting).
        "identity_output_tfp": abs(parts["technology_output_pct"]
                                   - values["tfp_delta_pct"]) <= IDENTITY_TOLERANCE,
        "horizon_consistent": (institution["ramp_at_horizon"] is not None
                               and tech["years"] == horizon),
        "gating": GATING,
        "causal_claim": CAUSAL_CLAIM,
    }
    case["checks"] = checks
    failures = [name for name, ok in checks.items()
                if ok is False and name not in ("gating", "causal_claim")]
    if failures:
        raise BridgeError(f"мост нарушил проверки кейса {case['case_id']} (fail-closed): "
                          f"{failures}")
    return case


# ----------------------------------------------------------------------------------------- отчёт
def describe() -> dict:
    """Паспорт модуля (для отчёта и, при отдельном решении, для реестра)."""
    return {
        "module_id": MODULE_ID,
        "blocks": list(BLOCKS),
        "title_ru": "Мост блоков 15/17: институты × технологии → пять макро-каналов",
        "version": MODULE_VERSION,
        "status": STATUS,
        "admission": ADMISSION,
        "gating": GATING,
        "causal_claim": CAUSAL_CLAIM,
        "data_status": DATA_STATUS,
        "calibration": CALIBRATION,
        "channels": list(CHANNEL_IDS),
        "docs": "docs/210_Мост_блоков_15_17.md",
        "tests": "tests/test_scenario_bridge.py",
        "upstream": ["amfe.world.institutions", "amfe.world.technology"],
        "dependencies": "stdlib + два исследовательских блока (numpy не нужен)",
        "caveat_ru": ("композит двух прототипов на объявленных допущениях: числа показывают "
                      "направление и порядок, а не ожидаемый эффект; admission=false"),
    }


def _profile_summary(profile) -> dict:
    return {
        "name": profile.name,
        "source": profile.source,
        "user_supplied": profile.source == "USER_INPUT",
        "score": profile.score(),
        "dispersion": profile.indicators.dispersion(),
        "indicators": profile.indicators.as_dict(),
        "weights": dict(profile.weights),
        "note": profile.note,
        "not_calibrated": NOT_CALIBRATED,
        "disclaimer": ("иллюстративные/пользовательские числа; не страновые данные и не "
                       "результат измерения"),
    }


def build_bridge(profile: object = None,
                 institution_scenarios: Sequence[str] | None = None,
                 technology_scenarios: Sequence[str] | None = None,
                 horizon_years: int = DEFAULT_HORIZON_YEARS,
                 weights: Mapping | None = None,
                 pairs: Sequence[Sequence[str]] | None = None) -> dict:
    """Полный отчёт моста: декартово произведение сценариев 15 × 17 (или явные ``pairs``)."""
    horizon = _horizon(horizon_years)
    normalised_weights = normalise_weights(weights)
    resolved_profile = resolve_profile(profile)
    institution_ids = resolve_institution_scenarios(institution_scenarios)
    technology_ids = resolve_technology_scenarios(technology_scenarios)

    parameters = institutions.OverlayParameters(horizon_years=horizon)
    institution_rows = _institution_inputs(resolved_profile, institution_ids, parameters)
    technology_report, technology_rows = _technology_inputs(technology_ids, horizon)

    if pairs is None:
        combos = [(institution_id, technology_id)
                  for institution_id in institution_ids
                  for technology_id in technology_ids]
    else:
        combos = []
        for pair in pairs:
            if isinstance(pair, (str, bytes)) or len(tuple(pair)) != 2:
                raise BridgeError(f"pairs: ожидается пара (сценарий 15, сценарий 17), "
                                  f"получено {pair!r}")
            institution_id, technology_id = (_normalise_institution_id(pair[0]),
                                             _normalise_technology_id(pair[1]))
            if institution_id not in institution_ids:
                raise BridgeError(f"pairs: сценарий {pair[0]!r} не запрошен")
            if technology_id not in technology_ids:
                raise BridgeError(f"pairs: сценарий {pair[1]!r} не запрошен")
            combos.append((institution_id, technology_id))
    if not combos:
        raise BridgeError("pairs: пустой список кейсов")

    cases = [
        _compose_case(institution_rows[institution_id], technology_rows[technology_id],
                      technology_rows["baseline"], normalised_weights, horizon)
        for institution_id, technology_id in combos
    ]

    payload = {
        "schema": SCHEMA,
        "module": describe(),
        "status": STATUS,
        "status_note": STATUS_NOTE,
        "admission": ADMISSION,
        "gating": GATING,
        "causal_claim": CAUSAL_CLAIM,
        "data_status": DATA_STATUS,
        "calibration": CALIBRATION,
        "not_calibrated": NOT_CALIBRATED,
        "classification": CLASSIFICATION,
        "horizon_years": horizon,
        "profile": _profile_summary(resolved_profile),
        "channel_universe": channel_universe(normalised_weights),
        "weights": dict(normalised_weights),
        "inputs": {
            "institutions": {
                "block": 15,
                "parameters": parameters.to_dict(),
                "profile_name": resolved_profile.name,
                "profile_source": resolved_profile.source,
                "scenarios": institution_rows,
            },
            "technology": {
                "block": 17,
                "requested_scenario_ids": list(technology_ids),
                "scenario_ids": [item["scenario_id"] for item in
                                 technology_report["scenarios"]],
                "sectors": list(technology_report["sectors"]),
                "growth": dict(technology_report["growth"]),
                "digest": technology_report["digest"],
                "checks": {
                    "all_scenario_checks_pass":
                        technology_report["checks"]["all_scenario_checks_pass"],
                    "all_aggregate_checks_pass":
                        technology_report["checks"]["all_aggregate_checks_pass"],
                    "no_raw_series": technology_report["checks"]["no_raw_series"],
                },
                "scenarios": technology_rows,
            },
        },
        "cases": cases,
        "summary": {},
        "checks": {},
        "assumptions": assumptions(),
        "limitations": limitations(),
        "provenance": {
            "data_read": False,
            "network": False,
            "clock": False,
            "randomness": False,
            "country_table": False,
            "calibration": "NONE",
            "mrio_touched": False,
            "delta_a_applied": False,
            "note": ("мост только вызывает два исследовательских блока и складывает их числа; "
                     "MRIO/io_core не читается и не меняется, delta_A не применяется"),
        },
    }
    payload["summary"] = _summary(cases)
    payload["checks"] = _document_checks(payload, horizon, normalised_weights, cases)
    failures = [name for name, ok in payload["checks"].items()
                if ok is False and name not in ("gating", "causal_claim")]
    if failures:
        raise BridgeError(f"мост нарушил проверки отчёта (fail-closed): {failures}")
    payload["digest"] = digest(payload)
    return payload


def _summary(cases: Sequence[dict]) -> dict:
    by_institution: dict[str, list[float]] = {}
    by_technology: dict[str, list[float]] = {}
    for case in cases:
        by_institution.setdefault(case["institutions_scenario"], []).append(case["index"]["value"])
        by_technology.setdefault(case["technology_scenario"], []).append(case["index"]["value"])
    best = max(cases, key=lambda case: (case["index"]["value"], case["case_id"]))
    worst = min(cases, key=lambda case: (case["index"]["value"], case["case_id"]))
    return {
        "case_count": len(cases),
        "max_abs_index": max(abs(case["index"]["value"]) for case in cases),
        "best_case": {"case_id": best["case_id"], "index": best["index"]["value"]},
        "worst_case": {"case_id": worst["case_id"], "index": worst["index"]["value"]},
        "mean_index_by_institutions": {
            key: _q(math.fsum(values) / len(values)) for key, values in by_institution.items()},
        "mean_index_by_technology": {
            key: _q(math.fsum(values) / len(values)) for key, values in by_technology.items()},
        "channel_extremes": {
            channel: {
                "min": min(case["channels"][channel] for case in cases),
                "max": max(case["channels"][channel] for case in cases),
            }
            for channel in CHANNEL_IDS
        },
    }


def _document_checks(payload: dict, horizon: int, weights: Mapping,
                     cases: Sequence[dict]) -> dict:
    channel_ids = [item["id"] for item in payload["channel_universe"]]
    return {
        "case_count": len(cases),
        "all_cases_pass": all(
            all(value for key, value in case["checks"].items()
                if key not in ("gating", "causal_claim"))
            for case in cases),
        "all_channels_declared": channel_ids == list(CHANNEL_IDS),
        "weights_sum_to_one": abs(math.fsum(weights.values()) - 1.0) <= TOLERANCE,
        "horizon_matches_cases": all(
            case["checks"]["horizon_consistent"] for case in cases),
        "additivity_all_cases": all(case["checks"]["additivity_index"]
                                    and case["checks"]["additivity_output"] for case in cases),
        "signs_all_cases": all(case["checks"]["sign_premium_opposes_quality"]
                               and case["checks"]["sign_investment_follows_quality"]
                               and case["checks"]["sign_tfp_follows_adoption"]
                               and case["checks"]["sign_output_follows_parts"] for case in cases),
        "no_raw_data": not forbidden_keys(payload)
                        and not _long_numeric_lists(payload, horizon),
        "status_flags": (payload["status"] == STATUS
                         and payload["admission"] is False
                         and payload["gating"] is False
                         and payload["causal_claim"] is False
                         and payload["data_status"] == DATA_STATUS
                         and payload["not_calibrated"] is True),
        "upstream_checks": bool(payload["inputs"]["technology"]["checks"][
            "all_scenario_checks_pass"]),
        "gating": GATING,
        "causal_claim": CAUSAL_CLAIM,
    }


def demo_report(horizon_years: int = DEMO_HORIZON_YEARS,
                profile: object = None) -> dict:
    """Демонстрация: контрольная линия, чистые эффекты блоков и три комбинации."""
    horizon = _horizon(horizon_years)
    report = build_bridge(profile=profile or "EXAMPLE-MID",
                          institution_scenarios=[item[0] for item in DEMO_PAIRS],
                          technology_scenarios=[item[1] for item in DEMO_PAIRS],
                          horizon_years=horizon,
                          pairs=DEMO_PAIRS)
    report.pop("digest", None)
    report["demo"] = {
        "title_ru": "Демонстрация моста 15/17: контроль, чистые эффекты и комбинации",
        "horizon_years": horizon,
        "pairs": [{"case_id": f"{item[0]}+{item[1]}"} for item in DEMO_PAIRS],
        "notes_ru": [
            "контрольная линия STATUS-QUO+baseline даёт пять нулевых каналов — это свойство "
            "объявленной базы, а не «в реальности ничего не меняется»;",
            "REFORM двигает премию вниз, инвестиции и выпуск вверх; DETERIORATION — наоборот; "
            "POLARIZATION теряет меньше по композиту, но премия растёт за счёт разброса;",
            "технологические каналы ai_acceleration/ai_slowdown двигают TFP, выпуск и энергию "
            "относительно baseline, не меняя институциональные каналы;",
            "energy_transition даёт отрицательный канал энергоспроса при близком выпуске — это "
            "следствие множителей сценария, а не измеренное decoupling;",
            "все числа — арифметика при допущениях B1–B10; сравнивать кейсы по индексу можно "
            "только как «смещение по объявленным полярностям», не как «лучше для экономики».",
        ],
        "note": ("демо показывает механику моста, а не эффект реформы или ИИ; вероятности не "
                 "оцениваются, причинность не утверждается"),
    }
    report["digest"] = digest(report)
    return report


__all__ = [
    "SCHEMA", "MODULE_ID", "MODULE_VERSION", "BLOCKS", "STATUS", "ADMISSION", "GATING",
    "CAUSAL_CLAIM", "DATA_STATUS", "CALIBRATION", "NOT_CALIBRATED", "CLASSIFICATION",
    "STATUS_NOTE", "TOLERANCE", "ZERO_TOLERANCE", "MIN_HORIZON_YEARS", "MAX_HORIZON_YEARS",
    "DEFAULT_HORIZON_YEARS", "DEMO_HORIZON_YEARS", "MAX_ABS_RISK_PREMIUM_BPS",
    "MAX_ABS_INVESTMENT_PP", "MAX_ABS_PCT", "MIN_PCT_LEVEL", "MAX_ABS_INDEX", "CHANNELS",
    "CHANNEL_IDS", "CHANNEL_BY_ID", "DEFAULT_WEIGHTS", "DEMO_PAIRS", "ASSUMPTIONS",
    "LIMITATIONS", "FORBIDDEN_KEYS", "BridgeError", "normalise_weights", "channel_universe",
    "canonical_json", "digest", "assumptions", "limitations", "channels_metadata",
    "forbidden_keys", "resolve_profile", "resolve_institution_scenarios",
    "resolve_technology_scenarios", "describe", "build_bridge", "demo_report",
]
