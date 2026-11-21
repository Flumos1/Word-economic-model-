"""Institutions block (docs/177 block 15; docs/59 ``q(t)``/``rho`` idea; docs/181 section 7).

This is a **research-only** prototype, not a model of institutions and not a source of country
ratings.  It answers one narrow question with explicit arithmetic: *if* the six governance
dimensions of a jurisdiction move by a declared amount, what happens to a composite score, to a
risk premium in basis points and to investment/GDP and growth through declared elasticities?

What is classified how
----------------------
* **ASSUMPTION_BASED** - everything numeric: the six 0-100 indicators, the equal default weights,
  every scenario delta, the risk-premium elasticity (bps per score point), the dispersion premium,
  the investment and growth elasticities.  Nothing is estimated, nothing is calibrated, and no
  country value is shipped, read or implied.  Built-in profiles are labelled ``EXAMPLE-*`` with
  ``source="ILLUSTRATIVE"`` and are geometric illustrations, not measurements.
* **NOT_CALIBRATED** - :data:`DATA_STATUS` is ``ASSUMPTION_BASED`` and every report carries
  ``not_calibrated=true``.  There is no WGI/ICRG/BTI/V-Dem series in this module; those are named
  in ``docs/203`` only as a future calibration plan (with licence questions open).
* **SCENARIO** - ``STATUS-QUO`` / ``REFORM`` / ``DETERIORATION`` / ``POLARIZATION``.  A scenario is
  a "what if", not a forecast, not a probability statement and not a causal estimate.

Key equations
-------------
Composite score (``x_i`` in 0-100, weights ``w_i >= 0``, ``sum(w) = 1``)::

    score = sum_i w_i * x_i                                  in [0, 100]
    dispersion = population standard deviation of the six x_i  (a *balance* measure, not quality)

Overlay of one scenario (deltas ``d_i`` points, magnitude ``m``, ramp ``r_t in (0,1]``)::

    x_i(t)  = clip(x_i + m * d_i * r_t, 0, 100)
    score_t = sum_i w_i * x_i(t)
    gap_t   = score_base - score_t                            positive when quality falls

Risk premium (bps, clipped into ``[0, premium_cap_bps]``)::

    premium_delta_t = beta * gap_t + gamma * (dispersion_t - dispersion_base)
    premium_t       = clip(premium_base + premium_delta_t, 0, premium_cap)

Real channels through declared elasticities (``e_inv`` in pp of GDP per score point,
``e_g`` in pp of potential growth per score point)::

    delta_investment_pp_t = e_inv * (score_t - score_base)
    investment_ratio_t    = clip(investment_base + delta_investment_pp_t / 100, 0, 1)
    delta_growth_pp_t     = e_g * (score_t - score_base)

Ramp (institutional change does not happen in one day; ``H`` = horizon in years)::

    r_t = min(1, t / ramp_years),  t = 1..H

Why these channels: the block is a *modifier* for other scenario libraries (docs/181 section 7:
"the same sanction gives a different result").  It therefore exports
:func:`as_scenario_modifier`: risk premium in bps, investment/GDP and growth in pp, always with
``gating=false``.  It does not model property rights, courts or regulation as mechanisms - only
their declared aggregate effect on three macro channels.

Determinism: pure arithmetic on declared inputs, fixed order, no wall clock, no randomness, no
file/network access inside the engine.  Equal inputs give byte-identical JSON after canonical
serialisation (checked by the CLI digest and the tests).

Status: ``RESEARCH_ONLY``, ``admission=false``, ``gating=false``, ``causal_claim=false``.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------------------- constants
SCHEMA = "amfe.world.institutions.v1"
SCENARIO_SCHEMA = "amfe.world.institutions.scenario.v1"
INDEX_SCHEMA = "amfe.world.institutions.index.v1"
TOOL_VERSION = "1.0.0"
STATUS = "RESEARCH_ONLY"
ADMISSION = False
GATING = False
CAUSAL_CLAIM = False
DATA_STATUS = "ASSUMPTION_BASED"
NOT_CALIBRATED = True
STATUS_NOTE = ("Исследовательский блок институтов: индикаторы и эластичности — допущения, "
               "не прогноз, не допуск и не причинная оценка; admission=false, gating=false")
TOLERANCE = 1e-9
SCENARIO_ID = re.compile(r"^[A-Z][A-Z0-9-]{2,60}$")
SCENARIO_KINDS = ("status_quo", "reform", "deterioration", "polarization")
DEFAULT_HORIZON = 5
MAX_HORIZON = 60

#: The six dimensions.  Keys are short on purpose (CLI ``--indicators voice=60,...``); the long
#: names in ``label_en``/``label_ru`` only name the dimension, they do not import a series.
INDICATORS = (
    {"key": "voice", "label_en": "voice and accountability", "label_ru": "голос и подотчётность",
     "dimension": "accountability"},
    {"key": "stability", "label_en": "political stability / absence of violence",
     "label_ru": "политическая стабильность", "dimension": "state_capacity"},
    {"key": "effectiveness", "label_en": "government effectiveness",
     "label_ru": "эффективность государства", "dimension": "state_capacity"},
    {"key": "regulatory", "label_en": "regulatory quality", "label_ru": "качество регулирования",
     "dimension": "property_rights"},
    {"key": "rule_of_law", "label_en": "rule of law", "label_ru": "верховенство права",
     "dimension": "property_rights"},
    {"key": "corruption", "label_en": "control of corruption", "label_ru": "контроль коррупции",
     "dimension": "accountability"},
)
INDICATOR_KEYS = tuple(item["key"] for item in INDICATORS)
INDICATOR_LABELS_RU = {item["key"]: item["label_ru"] for item in INDICATORS}
DIMENSIONS = ("accountability", "state_capacity", "property_rights")
DIMENSION_LABELS_RU = {
    "accountability": "подотчётность",
    "state_capacity": "государственная состоятельность",
    "property_rights": "права собственности и регулирование",
}
#: Equal weights by default.  Equal weighting is a *convention*, not a finding: no principal
#: components / expert elicitation was run, and the composite is therefore not "the" quality index.
DEFAULT_WEIGHTS = {key: 1.0 / len(INDICATOR_KEYS) for key in INDICATOR_KEYS}
MAX_ABS_DELTA_POINTS = 100.0

#: Explicit assumptions.  Every number in an overlay report must trace back to one of these.
ASSUMPTIONS = (
    {"id": "A1", "kind": "ASSUMPTION_BASED",
     "statement": ("Институциональное качество представлено шестью индикаторами 0–100 "
                   "(voice, stability, effectiveness, regulatory, rule_of_law, corruption); "
                   "индикаторы — объявленные числа, а не измерения и не страновые данные."),
     "replace_with": "лицензированные ряды WGI/ICRG/BTI/V-Dem после проверки лицензий (docs/203)"},
    {"id": "A2", "kind": "ASSUMPTION_BASED",
     "statement": ("Веса по умолчанию равные (1/6); композит — взвешенная сумма без порогов, "
                   "нелинейностей и взаимодействий между измерениями."),
     "replace_with": "оценка весов (PCA/экспертная элицитация) на данных, с проверкой устойчивости"},
    {"id": "A3", "kind": "ASSUMPTION_BASED",
     "statement": ("Шесть индикаторов независимы и аддитивны: ухудшение одного измерения "
                   "компенсируется улучшением другого ровно по весу (нет «критического» измерения)."),
     "replace_with": "пороговые/комплементарные спецификации (например, min-агрегация) с тестом"},
    {"id": "A4", "kind": "ASSUMPTION_BASED",
     "statement": ("Премия за риск: beta=4,0 б.п. на 1 пункт разрыва качества и gamma=2,0 б.п. на "
                   "1 пункт разброса между индикаторами; базовая премия 120 б.п. — условная."),
     "replace_with": "оценённая связь суверенных спредов/стоимости капитала с индексами качества"},
    {"id": "A5", "kind": "ASSUMPTION_BASED",
     "statement": ("Инвестиции: e_inv=0,04 п.п. ВВП на 1 пункт качества; рост: e_g=0,02 п.п. "
                   "потенциального роста на 1 пункт; обе эластичности — допущения, не оценки."),
     "replace_with": "оценённые уравнения инвестиций и роста с институциональными регрессорами"},
    {"id": "A6", "kind": "ASSUMPTION_BASED",
     "statement": ("Сценарии задаются явными сдвигами по каждому индикатору и magnitude; "
                   "рамп r_t=min(1,t/ramp_years) — линейный и одинаковый для всех индикаторов."),
     "replace_with": "датированные институциональные события (реформы/откаты) как PIT-сценарии"},
    {"id": "A7", "kind": "ASSUMPTION_BASED",
     "statement": ("Разброс (стандартное отклонение шести индикаторов) трактуется как мера "
                   "несогласованности правил; положительный вклад в премию — допущение A4."),
     "replace_with": "прямые меры неопределённости правил (частота изменений, споры, индексы)"},
    {"id": "A8", "kind": "SCENARIO",
     "statement": ("Четыре сценария (status_quo/reform/deterioration/polarization) — «что если», "
                   "а не прогноз: вероятности событий не оцениваются, сроки условны."),
     "replace_with": "слой вероятностей (блок 14) и датированные события (блок 16)"},
    {"id": "A9", "kind": "ASSUMPTION_BASED",
     "statement": ("Период — год, горизонт 5 лет по умолчанию; лагов между качеством и каналами, "
                   "кроме рамп-профиля, нет; обратной связи от инвестиций к качеству нет."),
     "replace_with": "квартальная частота и лаговая структура, оценённая на данных"},
    {"id": "A10", "kind": "ASSUMPTION_BASED",
     "statement": ("Блок не читает данные: нет WGI/ICRG/BTI/V-Dem, нет сети и часов; "
                   "пользовательский профиль помечается USER_INPUT и не проверяется на истинность."),
     "replace_with": "калибровка на лицензированных индикаторах с PIT-датами публикации"},
)

LIMITATIONS = (
    "нет данных и калибровки: все индикаторы, веса, сдвиги и эластичности — допущения "
    "(A1–A10), поэтому числа показывают направление и порядок, а не ожидаемый эффект;",
    "композит — взвешенная сумма по объявленным весам; равные веса выбраны по умолчанию, а не "
    "получены из данных, и другой набор весов даст другой уровень (устойчивость не проверялась);",
    "институты не смоделированы как механизм: нет прав собственности, судов, регулирования, "
    "политического процесса; есть только агрегированный сдвиг и три макро-канала;",
    "встроенные профили EXAMPLE-* иллюстративны и не являются страновыми значениями; "
    "пользовательский JSON — ответственность пользователя, блок его не валидирует на истинность;",
    "риск-премия — не суверенный спред и не доходность: это условная величина в б.п., "
    "привязанная к разрыву качества через допущение A4;",
    "сценарий не является прогнозом, вероятности не оцениваются, причинность не утверждается: "
    "admission=false, gating=false, causal_claim=false;",
)


class InstitutionError(ValueError):
    """Invalid input or a broken check: fail closed (CLI exit 1, nothing written)."""


# ----------------------------------------------------------------------------------------- helpers
def _finite(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise InstitutionError(f"{name}: ожидается число, получено {value!r}") from exc
    if not math.isfinite(number):
        raise InstitutionError(f"{name}: число должно быть конечным, получено {value!r}")
    return number


def _non_negative(value: object, name: str) -> float:
    number = _finite(value, name)
    if number < 0.0:
        raise InstitutionError(f"{name}: должно быть >= 0, получено {number!r}")
    return number


def _bounded(value: object, name: str, low: float, high: float) -> float:
    number = _finite(value, name)
    if not low <= number <= high:
        raise InstitutionError(f"{name}: должно лежать в [{low}, {high}], получено {number!r}")
    return number


def _positive(value: object, name: str) -> float:
    number = _finite(value, name)
    if number <= 0.0:
        raise InstitutionError(f"{name}: должно быть > 0, получено {number!r}")
    return number


def _horizon(value: object, name: str = "horizon_years") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        else:
            raise InstitutionError(f"{name}: ожидается целое число, получено {value!r}")
    if not 1 <= int(value) <= MAX_HORIZON:
        raise InstitutionError(f"{name}: должно лежать в [1, {MAX_HORIZON}], получено {value!r}")
    return int(value)


def canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(payload: object) -> str:
    """Stable SHA-256 of a canonical JSON payload (the payload must not contain 'digest')."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def assumptions() -> list[dict]:
    return [dict(item) for item in ASSUMPTIONS]


def limitations() -> list[str]:
    return list(LIMITATIONS)


def indicators_metadata() -> list[dict]:
    return [dict(item) for item in INDICATORS]


#: Report keys that would mean raw/series/micro data leaked into the output.
FORBIDDEN_KEYS = frozenset({
    "Z", "x", "y", "raw", "raw_data", "observations", "micro", "rows", "series",
    "indicator_series", "wgi_values", "icrg_values", "by_sector", "source_rows",
    "country_data", "delta_A",
})


def forbidden_keys(node: object, path: str = "") -> list[str]:
    """Return every path whose key looks like raw data (used for ``checks.no_raw_data``)."""
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


# ------------------------------------------------------------------------------- weights and score
def normalise_weights(weights: Mapping | None = None) -> dict:
    """Validate and normalise indicator weights so that they sum to 1.

    A partial mapping is an error: silently defaulting a missing dimension would change the
    composite without saying so.  Weights must be finite, non-negative and not all zero.
    """
    if weights is None:
        return dict(DEFAULT_WEIGHTS)
    if not isinstance(weights, Mapping):
        raise InstitutionError("weights: ожидается объект {индикатор: вес}")
    keys = set(weights)
    unknown = sorted(str(key) for key in keys if key not in INDICATOR_KEYS)
    if unknown:
        raise InstitutionError(f"weights: неизвестные индикаторы {unknown}")
    missing = [key for key in INDICATOR_KEYS if key not in keys]
    if missing:
        raise InstitutionError(f"weights: не заданы веса для {missing}")
    values = {key: _non_negative(weights[key], f"weights[{key!r}]") for key in INDICATOR_KEYS}
    total = math.fsum(values.values())
    if total <= 0.0:
        raise InstitutionError("weights: сумма весов должна быть > 0")
    return {key: values[key] / total for key in INDICATOR_KEYS}


@dataclass(frozen=True)
class InstitutionalIndicators:
    """Six declared 0-100 dimensions of institutional quality (higher is better on all six)."""

    voice: float = 50.0
    stability: float = 50.0
    effectiveness: float = 50.0
    regulatory: float = 50.0
    rule_of_law: float = 50.0
    corruption: float = 50.0

    def __post_init__(self) -> None:
        for key in INDICATOR_KEYS:
            value = _bounded(getattr(self, key), key, 0.0, 100.0)
            object.__setattr__(self, key, value)

    @classmethod
    def from_mapping(cls, data: Mapping, name: str = "indicators") -> "InstitutionalIndicators":
        if not isinstance(data, Mapping):
            raise InstitutionError(f"{name}: ожидается объект {{индикатор: значение}}")
        unknown = sorted(str(key) for key in data if key not in INDICATOR_KEYS)
        if unknown:
            raise InstitutionError(f"{name}: неизвестные индикаторы {unknown}")
        missing = [key for key in INDICATOR_KEYS if key not in data]
        if missing:
            raise InstitutionError(f"{name}: не заданы индикаторы {missing}")
        return cls(**{key: data[key] for key in INDICATOR_KEYS})

    def as_dict(self) -> dict:
        return {key: getattr(self, key) for key in INDICATOR_KEYS}

    def as_tuple(self) -> tuple[float, ...]:
        return tuple(getattr(self, key) for key in INDICATOR_KEYS)

    def mean(self) -> float:
        return math.fsum(self.as_tuple()) / len(INDICATOR_KEYS)

    def dispersion(self) -> float:
        """Population standard deviation of the six dimensions (a balance measure, not quality)."""
        values = self.as_tuple()
        mean = math.fsum(values) / len(values)
        return math.sqrt(math.fsum((value - mean) ** 2 for value in values) / len(values))

    def shifted(self, deltas: Mapping | None = None, magnitude: float = 1.0,
                ramp: float = 1.0) -> "InstitutionalIndicators":
        """Apply ``magnitude * delta * ramp`` per dimension and clip each back into [0, 100]."""
        magnitude = _finite(magnitude, "magnitude")
        ramp = _finite(ramp, "ramp")
        if not 0.0 <= ramp <= 1.0:
            raise InstitutionError("ramp: должно лежать в [0, 1]")
        delta_map = _deltas(deltas)
        values = {}
        for key in INDICATOR_KEYS:
            raw = getattr(self, key) + magnitude * delta_map[key] * ramp
            values[key] = min(100.0, max(0.0, raw))
        return InstitutionalIndicators(**values)

    def to_dict(self) -> dict:
        return {
            "indicators": self.as_dict(),
            "mean": self.mean(),
            "dispersion": self.dispersion(),
            "units": "0-100 (выше = лучше)",
            "classification": "ASSUMPTION_BASED",
        }


def _deltas(deltas: Mapping | None) -> dict:
    if deltas is None:
        return {key: 0.0 for key in INDICATOR_KEYS}
    if not isinstance(deltas, Mapping):
        raise InstitutionError("deltas: ожидается объект {индикатор: сдвиг}")
    unknown = sorted(str(key) for key in deltas if key not in INDICATOR_KEYS)
    if unknown:
        raise InstitutionError(f"deltas: неизвестные индикаторы {unknown}")
    values = {key: 0.0 for key in INDICATOR_KEYS}
    for key, value in deltas.items():
        values[key] = _finite(value, f"deltas[{key!r}]")
    return values


def institutional_score(indicators: Mapping | InstitutionalIndicators | None = None,
                        weights: Mapping | None = None) -> float:
    """Weighted composite in [0, 100]; equal weights when ``weights`` is not given."""
    return institutional_score_breakdown(indicators, weights)["score"]


def institutional_score_breakdown(indicators: Mapping | InstitutionalIndicators | None = None,
                                  weights: Mapping | None = None) -> dict:
    """Per-indicator contributions plus score, dispersion and the three dimensions."""
    if indicators is None:
        raise InstitutionError("indicators: нужен профиль или объект {индикатор: значение}")
    if isinstance(indicators, InstitutionalIndicators):
        profile = indicators
    else:
        profile = InstitutionalIndicators.from_mapping(indicators)
    normalised = normalise_weights(weights)
    contributions = []
    for key in INDICATOR_KEYS:
        value = getattr(profile, key)
        contributions.append({
            "indicator": key,
            "label_ru": INDICATOR_LABELS_RU[key],
            "value": value,
            "weight": normalised[key],
            "contribution": normalised[key] * value,
        })
    score = math.fsum(item["contribution"] for item in contributions)
    dimensions = {}
    for dimension in DIMENSIONS:
        members = [key for key in INDICATOR_KEYS
                   if next(item for item in INDICATORS if item["key"] == key)["dimension"]
                   == dimension]
        dimensions[dimension] = round(
            math.fsum(getattr(profile, key) for key in members) / len(members), 12)
    return {
        # 12 decimals is far below the meaningful precision of a 0-100 composite and keeps
        # float noise (e.g. 99.99999999999999 for an all-100 profile) out of the reports.
        "score": round(min(100.0, max(0.0, score)), 12),
        "dispersion": round(profile.dispersion(), 12),
        "weights": normalised,
        "weight_sum": math.fsum(normalised.values()),
        "contributions": contributions,
        "dimensions": dimensions,
        "formula": "score = sum_i w_i * x_i; разброс = стандартное отклонение шести индикаторов",
        "classification": "ASSUMPTION_BASED",
    }


# -------------------------------------------------------------------------------------- profiles
@dataclass(frozen=True)
class InstitutionProfile:
    """A named bundle of declared indicators + weights (illustrative or user supplied).

    ``source`` is ``ILLUSTRATIVE`` for the built-in ``EXAMPLE-*`` profiles and ``USER_INPUT`` for
    a user file.  Neither is a country rating; the block has no country table.
    """

    name: str
    indicators: InstitutionalIndicators = field(default_factory=InstitutionalIndicators)
    weights: Mapping | None = None
    source: str = "ILLUSTRATIVE"
    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise InstitutionError("name: не может быть пустым")
        if not isinstance(self.indicators, InstitutionalIndicators):
            if isinstance(self.indicators, Mapping):
                object.__setattr__(self, "indicators",
                                   InstitutionalIndicators.from_mapping(self.indicators))
            else:
                raise InstitutionError("indicators: ожидается InstitutionalIndicators или объект")
        if self.source not in ("ILLUSTRATIVE", "USER_INPUT"):
            raise InstitutionError("source: ожидается ILLUSTRATIVE или USER_INPUT")
        object.__setattr__(self, "weights", normalise_weights(self.weights))
        if not isinstance(self.note, str):
            raise InstitutionError("note: ожидается строка")

    def score(self) -> float:
        return institutional_score(self.indicators, self.weights)

    def to_dict(self) -> dict:
        breakdown = institutional_score_breakdown(self.indicators, self.weights)
        return {
            "name": self.name,
            "source": self.source,
            "note": self.note,
            "user_supplied": self.source == "USER_INPUT",
            "not_calibrated": NOT_CALIBRATED,
            "indicators": self.indicators.as_dict(),
            "indicator_metadata": indicators_metadata(),
            "weights": dict(self.weights),
            "score": breakdown["score"],
            "dispersion": breakdown["dispersion"],
            "dimensions": breakdown["dimensions"],
            "dimension_labels_ru": dict(DIMENSION_LABELS_RU),
            "contributions": breakdown["contributions"],
            "formula": breakdown["formula"],
            "classification": "ASSUMPTION_BASED",
            "disclaimer": ("иллюстративные/пользовательские числа; не страновые данные и не "
                           "результат измерения"),
        }


#: Built-in profiles are geometric examples, not measurements.  They exist so that the CLI has
#: something to run and so that the tests can check monotonicity; they are *not* countries.
_EXAMPLE_VALUES = {
    "EXAMPLE-LOW": (20.0, 25.0, 22.0, 24.0, 18.0, 15.0),
    "EXAMPLE-MID": (55.0, 50.0, 52.0, 54.0, 50.0, 48.0),
    "EXAMPLE-HIGH": (82.0, 78.0, 85.0, 84.0, 88.0, 86.0),
    "EXAMPLE-POLARIZED": (65.0, 35.0, 45.0, 60.0, 40.0, 38.0),
}


def example_profiles() -> tuple[InstitutionProfile, ...]:
    profiles = []
    for name, values in _EXAMPLE_VALUES.items():
        payload = dict(zip(INDICATOR_KEYS, values))
        profiles.append(InstitutionProfile(
            name=name,
            indicators=InstitutionalIndicators(**payload),
            source="ILLUSTRATIVE",
            note=("Геометрический пример (не страна и не измерение): "
                  f"{', '.join(f'{key}={values[index]:.0f}' for index, key in enumerate(INDICATOR_KEYS))}"),
        ))
    return tuple(profiles)


def find_example_profile(name: object) -> InstitutionProfile:
    """Look up an ``EXAMPLE-*`` profile case-insensitively; unknown names fail closed."""
    if not isinstance(name, str) or not name.strip():
        raise InstitutionError("country: пустое имя профиля")
    needle = name.strip().upper()
    for profile in example_profiles():
        if profile.name.upper() == needle:
            return profile
    raise InstitutionError(
        f"country: неизвестный профиль {name!r}; допустимы {[p.name for p in example_profiles()]} "
        "или путь к пользовательскому JSON (реальных стран в блоке нет)")


def profile_from_config(data: Mapping) -> InstitutionProfile:
    """Build a validated profile from a mapping; unknown keys fail closed."""
    if not isinstance(data, Mapping):
        raise InstitutionError("config.profile: ожидается объект")
    allowed = {item.name for item in fields(InstitutionProfile)}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise InstitutionError(f"config.profile: неизвестные поля {unknown}")
    payload = dict(data)
    indicators = payload.get("indicators")
    if isinstance(indicators, Mapping):
        payload["indicators"] = InstitutionalIndicators.from_mapping(indicators,
                                                                     "config.profile.indicators")
    elif indicators is None:
        raise InstitutionError("config.profile: поле indicators обязательно")
    return InstitutionProfile(**payload)


def parse_indicator_text(text: object) -> dict:
    """Parse ``voice=60,stability=55,...`` into a mapping; every number is checked 0-100."""
    if not isinstance(text, str) or not text.strip():
        raise InstitutionError("indicators: пустая строка")
    values: dict = {}
    for chunk in text.split(","):
        part = chunk.strip()
        if not part:
            continue
        if "=" not in part:
            raise InstitutionError(f"indicators: ожидается ключ=значение, получено {part!r}")
        key, _, raw = part.partition("=")
        key = key.strip()
        if key not in INDICATOR_KEYS:
            raise InstitutionError(f"indicators: неизвестный индикатор {key!r}")
        if key in values:
            raise InstitutionError(f"indicators: индикатор {key!r} задан дважды")
        values[key] = _bounded(raw.strip(), f"indicators[{key!r}]", 0.0, 100.0)
    missing = [key for key in INDICATOR_KEYS if key not in values]
    if missing:
        raise InstitutionError(f"indicators: не заданы {missing}")
    return values


# -------------------------------------------------------------------------------------- scenarios
@dataclass(frozen=True)
class InstitutionScenario:
    """One declared scenario: per-indicator deltas, magnitude and activation ramp.

    ``kind`` is one of :data:`SCENARIO_KINDS`; ``deltas`` are points of the 0-100 scale, so the
    scenario is readable without any data.  A scenario is not a forecast.
    """

    scenario_id: str
    title_ru: str
    kind: str
    deltas: Mapping
    magnitude: float = 1.0
    ramp_years: int = 1
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_id, str) or not SCENARIO_ID.match(self.scenario_id):
            raise InstitutionError(f"scenario_id: недопустимый id {self.scenario_id!r}")
        if not isinstance(self.title_ru, str) or not self.title_ru.strip():
            raise InstitutionError("title_ru: не может быть пустым")
        if self.kind not in SCENARIO_KINDS:
            raise InstitutionError(f"kind: должен быть одним из {SCENARIO_KINDS}")
        deltas = _deltas(self.deltas)
        magnitude = _positive(self.magnitude, "magnitude")
        if magnitude > 10.0:
            raise InstitutionError("magnitude: должна лежать в (0, 10]")
        for key, value in deltas.items():
            if abs(value * magnitude) > MAX_ABS_DELTA_POINTS + TOLERANCE:
                raise InstitutionError(
                    f"deltas[{key!r}]: |сдвиг|*magnitude не может превышать "
                    f"{MAX_ABS_DELTA_POINTS} пунктов, получено {abs(value * magnitude)!r}")
        ramp = _horizon(self.ramp_years, "ramp_years")
        if not isinstance(self.notes, str):
            raise InstitutionError("notes: ожидается строка")
        object.__setattr__(self, "deltas", deltas)
        object.__setattr__(self, "magnitude", magnitude)
        object.__setattr__(self, "ramp_years", ramp)

    @property
    def effective_deltas(self) -> dict:
        return {key: self.deltas[key] * self.magnitude for key in INDICATOR_KEYS}

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "title_ru": self.title_ru,
            "kind": self.kind,
            "deltas": dict(self.deltas),
            "magnitude": self.magnitude,
            "effective_deltas": self.effective_deltas,
            "ramp_years": self.ramp_years,
            "notes": self.notes,
            "classification": "SCENARIO",
            "causal_claim": False,
        }


def default_scenarios() -> tuple[InstitutionScenario, ...]:
    """The four declared scenarios.  Deltas are points on the 0-100 scale (assumption A6)."""
    return (
        InstitutionScenario(
            scenario_id="STATUS-QUO",
            title_ru="Статус-кво: правила не меняются",
            kind="status_quo",
            deltas={key: 0.0 for key in INDICATOR_KEYS},
            ramp_years=1,
            notes="Контрольная линия: нулевой сдвиг, премия и инвестиции не меняются.",
        ),
        InstitutionScenario(
            scenario_id="REFORM",
            title_ru="Реформа: улучшение правил и правоприменения",
            kind="reform",
            deltas={"voice": 6.0, "stability": 4.0, "effectiveness": 6.0,
                    "regulatory": 6.0, "rule_of_law": 8.0, "corruption": 6.0},
            ramp_years=4,
            notes=("Средний сдвиг +6 пунктов с приоритетом правоприменения; рампа 4 года — "
                   "институты меняются медленнее, чем шоки (допущение A6)."),
        ),
        InstitutionScenario(
            scenario_id="DETERIORATION",
            title_ru="Ухудшение: эрозия правил и контроля",
            kind="deterioration",
            deltas={"voice": -5.0, "stability": -8.0, "effectiveness": -6.0,
                    "regulatory": -4.0, "rule_of_law": -9.0, "corruption": -8.0},
            ramp_years=2,
            notes=("Средний сдвиг −6,67 пункта; эрозия быстрее реформы (рампа 2 года) — "
                   "допущение, а не эмпирический факт."),
        ),
        InstitutionScenario(
            scenario_id="POLARIZATION",
            title_ru="Поляризация: правила спорны, исполнение непоследовательно",
            kind="polarization",
            deltas={"voice": 3.0, "stability": -12.0, "effectiveness": -8.0,
                    "regulatory": 1.0, "rule_of_law": -5.0, "corruption": -4.0},
            ramp_years=2,
            notes=("Не только уровень, но и разброс: устойчивость и правоприменение падают, "
                   "голос/регулирование растут, поэтому разброс между индикаторами расширяется "
                   "и добавляется премия за несогласованность (A7)."),
        ),
    )


def scenarios_from_config(items: Sequence[Mapping]) -> tuple[InstitutionScenario, ...]:
    if isinstance(items, (str, bytes)) or not isinstance(items, Iterable):
        raise InstitutionError("config.scenarios: ожидается список объектов")
    allowed = {item.name for item in fields(InstitutionScenario)}
    scenarios = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise InstitutionError(f"config.scenarios[{index}]: ожидается объект")
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise InstitutionError(f"config.scenarios[{index}]: неизвестные поля {unknown}")
        scenarios.append(InstitutionScenario(**dict(item)))
    ids = [scenario.scenario_id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise InstitutionError("config.scenarios: id сценариев должны быть уникальны")
    if not scenarios:
        raise InstitutionError("config.scenarios: список не должен быть пустым")
    return tuple(scenarios)


# ------------------------------------------------------------------------------------- parameters
@dataclass(frozen=True)
class OverlayParameters:
    """Every number that translates a score change into a macro channel (all assumptions)."""

    base_risk_premium_bps: float = 120.0
    beta_bps_per_point: float = 4.0
    gamma_bps_per_point_dispersion: float = 2.0
    premium_cap_bps: float = 2000.0
    base_investment_ratio: float = 0.22
    elasticity_investment_pp_per_point: float = 0.04
    elasticity_growth_pp_per_point: float = 0.02
    horizon_years: int = DEFAULT_HORIZON

    def __post_init__(self) -> None:
        premium = _non_negative(self.base_risk_premium_bps, "base_risk_premium_bps")
        beta = _non_negative(self.beta_bps_per_point, "beta_bps_per_point")
        gamma = _non_negative(self.gamma_bps_per_point_dispersion,
                              "gamma_bps_per_point_dispersion")
        cap = _positive(self.premium_cap_bps, "premium_cap_bps")
        if premium > cap:
            raise InstitutionError("base_risk_premium_bps: не может превышать premium_cap_bps")
        investment = _bounded(self.base_investment_ratio, "base_investment_ratio", 0.0, 1.0)
        e_inv = _non_negative(self.elasticity_investment_pp_per_point,
                              "elasticity_investment_pp_per_point")
        e_growth = _finite(self.elasticity_growth_pp_per_point, "elasticity_growth_pp_per_point")
        horizon = _horizon(self.horizon_years)
        object.__setattr__(self, "base_risk_premium_bps", premium)
        object.__setattr__(self, "beta_bps_per_point", beta)
        object.__setattr__(self, "gamma_bps_per_point_dispersion", gamma)
        object.__setattr__(self, "premium_cap_bps", cap)
        object.__setattr__(self, "base_investment_ratio", investment)
        object.__setattr__(self, "elasticity_investment_pp_per_point", e_inv)
        object.__setattr__(self, "elasticity_growth_pp_per_point", e_growth)
        object.__setattr__(self, "horizon_years", horizon)

    def to_dict(self) -> dict:
        payload = {item.name: getattr(self, item.name) for item in fields(self)}
        payload.update({
            "units": {"risk_premium": "б.п. к базовой премии",
                      "investment": "п.п. ВВП на 1 пункт качества",
                      "growth": "п.п. потенциального роста на 1 пункт качества",
                      "score": "пункты шкалы 0–100"},
            "not_calibrated": NOT_CALIBRATED,
            "classification": "ASSUMPTION_BASED",
            "formula": ("premium_delta = beta*(score_base-score) + gamma*(dispersion-"
                        "dispersion_base); d_inv = e_inv*delta_score; d_growth = e_g*delta_score"),
        })
        return payload


def parameters_from_config(data: Mapping) -> OverlayParameters:
    if not isinstance(data, Mapping):
        raise InstitutionError("config.parameters: ожидается объект")
    allowed = {item.name for item in fields(OverlayParameters)}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise InstitutionError(f"config.parameters: неизвестные поля {unknown}")
    return OverlayParameters(**dict(data))


# ----------------------------------------------------------------------------------- overlay engine
def _clip(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _premium(parameters: OverlayParameters, score_gap: float, dispersion_delta: float) -> dict:
    score_component = parameters.beta_bps_per_point * score_gap
    dispersion_component = parameters.gamma_bps_per_point_dispersion * dispersion_delta
    requested = parameters.base_risk_premium_bps + score_component + dispersion_component
    level = _clip(requested, 0.0, parameters.premium_cap_bps)
    return {
        "base_bps": parameters.base_risk_premium_bps,
        "level_bps": level,
        "delta_bps": level - parameters.base_risk_premium_bps,
        "requested_bps": requested,
        "clipped": abs(requested - level) > TOLERANCE,
        "score_component_bps": score_component,
        "dispersion_component_bps": dispersion_component,
        "beta_bps_per_point": parameters.beta_bps_per_point,
        "gamma_bps_per_point_dispersion": parameters.gamma_bps_per_point_dispersion,
        "cap_bps": parameters.premium_cap_bps,
    }


def _investment(parameters: OverlayParameters, score_delta: float) -> dict:
    delta_pp = parameters.elasticity_investment_pp_per_point * score_delta
    requested = parameters.base_investment_ratio + delta_pp / 100.0
    level = _clip(requested, 0.0, 1.0)
    return {
        "base_ratio": parameters.base_investment_ratio,
        "level_ratio": level,
        "delta_pp": delta_pp,
        "requested_ratio": requested,
        "clipped": abs(requested - level) > TOLERANCE,
        "elasticity_pp_per_point": parameters.elasticity_investment_pp_per_point,
    }


def _growth(parameters: OverlayParameters, score_delta: float) -> dict:
    delta_pp = parameters.elasticity_growth_pp_per_point * score_delta
    return {"delta_pp": delta_pp, "elasticity_pp_per_point": parameters.elasticity_growth_pp_per_point}


def scenario_overlay(profile: InstitutionProfile, scenario: InstitutionScenario,
                     parameters: OverlayParameters | None = None) -> dict:
    """Overlay one scenario on one profile; return the full research-only report."""
    if not isinstance(profile, InstitutionProfile):
        raise InstitutionError("profile: ожидается InstitutionProfile")
    if not isinstance(scenario, InstitutionScenario):
        raise InstitutionError("scenario: ожидается InstitutionScenario")
    parameters = OverlayParameters() if parameters is None else parameters
    if not isinstance(parameters, OverlayParameters):
        raise InstitutionError("parameters: ожидается OverlayParameters")

    base = profile.indicators
    base_score = profile.score()
    base_dispersion = base.dispersion()
    deltas = scenario.effective_deltas

    path: list[dict] = []
    previous_ramp = 0.0
    for year in range(1, parameters.horizon_years + 1):
        ramp = min(1.0, year / scenario.ramp_years)
        shifted = base.shifted(deltas, magnitude=1.0, ramp=ramp)
        score = institutional_score(shifted, profile.weights)
        dispersion = shifted.dispersion()
        score_gap = base_score - score
        premium = _premium(parameters, score_gap, dispersion - base_dispersion)
        investment = _investment(parameters, score - base_score)
        growth = _growth(parameters, score - base_score)
        if ramp + TOLERANCE < previous_ramp:  # pragma: no cover - min() guarantees monotonicity
            raise InstitutionError("рамп-профиль немонотонен (fail-closed)")
        previous_ramp = ramp
        path.append({
            "year": year,
            "ramp": ramp,
            "score": score,
            "delta_score_points": score - base_score,
            "dispersion": dispersion,
            "risk_premium_bps": premium["level_bps"],
            "delta_risk_premium_bps": premium["delta_bps"],
            "investment_ratio": investment["level_ratio"],
            "delta_investment_pp": investment["delta_pp"],
            "delta_growth_pp": growth["delta_pp"],
        })

    full = base.shifted(deltas, magnitude=1.0, ramp=1.0)
    full_score = institutional_score(full, profile.weights)
    full_dispersion = full.dispersion()
    full_gap = base_score - full_score
    full_premium = _premium(parameters, full_gap, full_dispersion - base_dispersion)
    full_investment = _investment(parameters, full_score - base_score)
    full_growth = _growth(parameters, full_score - base_score)

    requested_delta = math.fsum(profile.weights[key] * deltas[key] for key in INDICATOR_KEYS)
    applied_delta = full_score - base_score
    converged = abs(path[-1]["ramp"] - 1.0) <= TOLERANCE

    payload = {
        "schema": SCENARIO_SCHEMA,
        "status": STATUS,
        "status_note": STATUS_NOTE,
        "admission": ADMISSION,
        "gating": GATING,
        "causal_claim": CAUSAL_CLAIM,
        "data_status": DATA_STATUS,
        "not_calibrated": NOT_CALIBRATED,
        "classification": "SCENARIO",
        "provenance": {
            "indicator_source": profile.source,
            "calibration": "NONE",
            "data_read": False,
            "country_table": False,
            "note": ("страновая таблица отсутствует: индикаторы — допущение или ввод "
                     "пользователя; калибровка не проводилась"),
        },
        "profile": profile.to_dict(),
        "scenario": scenario.to_dict(),
        "parameters": parameters.to_dict(),
        "overlay": {
            "base": {
                "score": base_score,
                "dispersion": base_dispersion,
                "risk_premium_bps": parameters.base_risk_premium_bps,
                "investment_ratio": parameters.base_investment_ratio,
                "growth_pp": 0.0,
            },
            "full_effect": {
                "indicators": full.as_dict(),
                "score": full_score,
                "delta_score_points": full_score - base_score,
                "requested_delta_points": requested_delta,
                "applied_delta_points": applied_delta,
                "clipped": abs(requested_delta - applied_delta) > TOLERANCE,
                "dispersion": full_dispersion,
                "delta_dispersion": full_dispersion - base_dispersion,
                "risk_premium": full_premium,
                "investment": full_investment,
                "growth": full_growth,
            },
            "path": path,
            "converged": converged,
            "horizon_years": parameters.horizon_years,
            "ramp_years": scenario.ramp_years,
            "note": ("эффект разворачивается по рампе r_t=min(1,t/ramp_years); "
                     "full_effect — при r=1"),
        },
        "checks": {},
        "assumptions": assumptions(),
        "limitations": limitations(),
    }

    checks = {
        "score_bounds": all(0.0 <= row["score"] <= 100.0 for row in path)
        and 0.0 <= base_score <= 100.0 and 0.0 <= full_score <= 100.0,
        "indicator_bounds": all(0.0 <= value <= 100.0 for value in full.as_tuple())
        and all(0.0 <= value <= 100.0 for value in base.as_tuple()),
        "weights_normalised": abs(math.fsum(profile.weights.values()) - 1.0) <= TOLERANCE,
        "premium_non_negative": all(row["risk_premium_bps"] >= 0.0 for row in path),
        "investment_bounds": all(0.0 <= row["investment_ratio"] <= 1.0 for row in path),
        "path_monotone_ramp": all(path[index]["ramp"] <= path[index + 1]["ramp"] + TOLERANCE
                                  for index in range(len(path) - 1)) if len(path) > 1 else True,
        "no_raw_data": not forbidden_keys(payload),
        "gating": GATING,
        "causal_claim": CAUSAL_CLAIM,
    }
    payload["checks"] = checks
    failures = [name for name, ok in checks.items()
                if ok is False and name not in ("gating", "causal_claim")]
    if failures:
        raise InstitutionError(f"сценарный блок нарушил проверки (fail-closed): {failures}")
    payload["digest"] = digest(payload)
    return payload


def as_scenario_modifier(report: Mapping) -> dict:
    """Export the overlay as a modifier for other scenario libraries (docs/181 section 7).

    The modifier carries three numbers plus the provenance; ``gating`` stays false, so nothing
    downstream may treat it as an admission or a forecast.
    """
    if not isinstance(report, Mapping) or report.get("schema") != SCENARIO_SCHEMA:
        raise InstitutionError("as_scenario_modifier: ожидается отчёт scenario_overlay")
    full = report["overlay"]["full_effect"]
    return {
        "kind": "institutional_modifier",
        "schema": "amfe.world.institutions.modifier.v1",
        "profile": report["profile"]["name"],
        "profile_source": report["profile"]["source"],
        "scenario_id": report["scenario"]["scenario_id"],
        "risk_premium_bps": full["risk_premium"]["delta_bps"],
        "investment_ratio_delta": full["investment"]["delta_pp"] / 100.0,
        "investment_delta_pp": full["investment"]["delta_pp"],
        "growth_delta_pp": full["growth"]["delta_pp"],
        "delta_score_points": full["delta_score_points"],
        "applies_to": ["block_5_investment", "block_11_12_policy_fiscal",
                       "block_16_geopolitics"],
        "data_status": DATA_STATUS,
        "not_calibrated": NOT_CALIBRATED,
        "gating": GATING,
        "causal_claim": CAUSAL_CLAIM,
        "note": ("модификатор сценария: три канала при допущениях A1–A10; не прогноз, не "
                 "допуск и не причинная оценка"),
    }


# ----------------------------------------------------------------------------------------- runner
def _normalise_id(value: object) -> str:
    text = str(value).strip()
    return text.upper().replace("_", "-")


def run_all(profile: InstitutionProfile | None = None,
            scenarios: Sequence[InstitutionScenario] | None = None,
            only: Sequence[str] | None = None,
            parameters: OverlayParameters | None = None) -> dict:
    """Run the declared scenarios for one profile and return an index plus per-scenario reports."""
    if profile is None:
        profile = find_example_profile("EXAMPLE-MID")
    if not isinstance(profile, InstitutionProfile):
        raise InstitutionError("profile: ожидается InstitutionProfile")
    parameters = OverlayParameters() if parameters is None else parameters
    if not isinstance(parameters, OverlayParameters):
        raise InstitutionError("parameters: ожидается OverlayParameters")
    pool = list(default_scenarios() if scenarios is None else scenarios)
    if not pool:
        raise InstitutionError("scenarios: список не должен быть пустым")
    ids = [scenario.scenario_id for scenario in pool]
    if len(ids) != len(set(ids)):
        raise InstitutionError("scenarios: id должны быть уникальны")
    if only is not None:
        wanted = {_normalise_id(item) for item in only if str(item).strip()}
        known = {_normalise_id(item) for item in ids}
        unknown = sorted(wanted - known)
        if unknown:
            raise InstitutionError(f"only: неизвестные сценарии {unknown}")
        pool = [scenario for scenario in pool if _normalise_id(scenario.scenario_id) in wanted]
        if not pool:
            raise InstitutionError("only: не выбрано ни одного сценария")
    reports = [scenario_overlay(profile, scenario, parameters) for scenario in pool]
    entries = []
    for report in reports:
        full = report["overlay"]["full_effect"]
        entries.append({
            "id": report["scenario"]["scenario_id"],
            "title_ru": report["scenario"]["title_ru"],
            "kind": report["scenario"]["kind"],
            "magnitude": report["scenario"]["magnitude"],
            "ramp_years": report["scenario"]["ramp_years"],
            "digest": report["digest"],
            "headline": {
                "delta_score_points": full["delta_score_points"],
                "delta_risk_premium_bps": full["risk_premium"]["delta_bps"],
                "delta_investment_pp": full["investment"]["delta_pp"],
                "delta_growth_pp": full["growth"]["delta_pp"],
            },
        })
    index = {
        "schema": INDEX_SCHEMA,
        "status": STATUS,
        "status_note": STATUS_NOTE,
        "admission": ADMISSION,
        "gating": GATING,
        "causal_claim": CAUSAL_CLAIM,
        "data_status": DATA_STATUS,
        "not_calibrated": NOT_CALIBRATED,
        "tool_version": TOOL_VERSION,
        "profile": profile.to_dict(),
        "parameters": parameters.to_dict(),
        "scenarios": entries,
        "checks": {
            "scenario_count": len(reports),
            "all_score_bounds": all(report["checks"]["score_bounds"] for report in reports),
            "all_indicator_bounds": all(report["checks"]["indicator_bounds"] for report in reports),
            "all_weights_normalised": all(report["checks"]["weights_normalised"]
                                          for report in reports),
            "all_premiums_non_negative": all(report["checks"]["premium_non_negative"]
                                             for report in reports),
            "all_investment_bounds": all(report["checks"]["investment_bounds"]
                                         for report in reports),
            "all_no_raw_data": all(report["checks"]["no_raw_data"] for report in reports),
            "gating": GATING,
            "causal_claim": CAUSAL_CLAIM,
        },
        "max_abs_delta_score": max((abs(entry["headline"]["delta_score_points"])
                                    for entry in entries), default=0.0),
        "assumptions": assumptions(),
        "limitations": limitations(),
    }
    index["digest"] = digest(index)
    return {"index": index, "reports": reports}


def load_bundle(path: object, name: str | None = None) -> tuple:
    """Read a user JSON bundle -> ``(profile, scenarios | None, parameters | None)``.

    Accepted shapes::

        {"profile": {"name": ..., "indicators": {...}}, "scenarios": [...], "parameters": {...}}
        {"name": ..., "indicators": {...}, "scenarios": [...]}          # flat

    Everything is fail-closed: a missing file is :class:`FileNotFoundError` (CLI exit 2), broken
    JSON, unknown keys and out-of-range numbers raise :class:`InstitutionError` (CLI exit 1).
    The block does not judge the numbers: they are marked ``USER_INPUT`` in every report.
    """
    target = Path(str(path))
    if not target.is_file():
        raise FileNotFoundError(f"профиль не найден: {target}")
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstitutionError(f"профиль не читается: {exc}") from exc
    if not isinstance(data, Mapping):
        raise InstitutionError("профиль: ожидается JSON-объект")
    payload = dict(data)
    nested = payload.pop("profile", None)
    if nested is not None:
        if not isinstance(nested, Mapping):
            raise InstitutionError("профиль: поле profile должно быть объектом")
        extra = sorted(set(payload) - {"scenarios", "parameters"})
        if extra:
            raise InstitutionError(f"профиль: неизвестные ключи {extra}")
        profile_data = dict(nested)
    else:
        profile_data = {key: value for key, value in payload.items()
                        if key not in ("scenarios", "parameters")}
    if name:
        profile_data["name"] = name
    profile_data.setdefault("source", "USER_INPUT")
    profile = profile_from_config(profile_data)
    raw_scenarios = payload.get("scenarios")
    scenarios = None if raw_scenarios is None else scenarios_from_config(raw_scenarios)
    raw_parameters = payload.get("parameters")
    parameters = None if raw_parameters is None else parameters_from_config(raw_parameters)
    return profile, scenarios, parameters


def load_profile_file(path: object, name: str | None = None) -> InstitutionProfile:
    """Read a *profile-only* user JSON; a bundle with scenarios/parameters is rejected."""
    profile, scenarios, parameters = load_bundle(path, name=name)
    if scenarios is not None or parameters is not None:
        raise InstitutionError("профиль содержит scenarios/parameters: используйте load_bundle")
    return profile


__all__ = [
    "SCHEMA", "SCENARIO_SCHEMA", "INDEX_SCHEMA", "TOOL_VERSION", "STATUS", "ADMISSION", "GATING",
    "CAUSAL_CLAIM", "DATA_STATUS", "NOT_CALIBRATED", "STATUS_NOTE", "TOLERANCE",
    "SCENARIO_KINDS", "DEFAULT_HORIZON", "MAX_HORIZON", "INDICATORS", "INDICATOR_KEYS",
    "INDICATOR_LABELS_RU", "DIMENSIONS", "DIMENSION_LABELS_RU", "DEFAULT_WEIGHTS",
    "MAX_ABS_DELTA_POINTS", "ASSUMPTIONS", "LIMITATIONS", "FORBIDDEN_KEYS", "InstitutionError",
    "InstitutionalIndicators", "InstitutionProfile", "InstitutionScenario", "OverlayParameters",
    "normalise_weights", "institutional_score", "institutional_score_breakdown", "forbidden_keys",
    "example_profiles", "find_example_profile", "profile_from_config", "parse_indicator_text",
    "default_scenarios", "scenarios_from_config", "parameters_from_config", "scenario_overlay",
    "as_scenario_modifier", "run_all", "load_bundle", "load_profile_file", "canonical_json",
    "digest", "assumptions", "limitations", "indicators_metadata",
]
