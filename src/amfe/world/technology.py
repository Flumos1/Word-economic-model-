"""Block 17 — science, technology and AI: logistic adoption -> TFP -> output and energy.

This module is the executable research prototype demanded by
[docs/177](../../../docs/177_17_блоков_зрелость.md) §2.2 and §3.5 for block 17 and documented in
[docs/204](../../../docs/204_Блок_17_технологии_ИИ.md).  It answers one narrow question:

    *if* a technology bundle diffuses along a logistic path with a declared ceiling, speed and
    starting share, *then* what does growth accounting imply for TFP, output and energy use?

What the module deliberately is
-------------------------------
* **Adoption is a logistic curve with three declared parameters.**  ``start`` (share already
  adopted at year 0), ``ceiling`` (the attainable share of the sector's potential, a physical /
  institutional limit, not a fitted saturation level) and ``speed`` (per-year diffusion rate).
  The curve is the closed-form solution of ``dx/dt = speed * x * (1 - x / ceiling)``, so it is
  monotone, bounded by the ceiling and never overshoots (a discrete recursion can oscillate at
  high speed; that failure mode is removed by construction and stated in docs/204).
* **Transmission runs through one explicit growth-accounting identity.**  With a Cobb–Douglas
  technology ``Y = A * K^alpha * L^(1-alpha)`` and constant returns:

      ln(Y_t / Y_0) = ln(A_t / A_0) + alpha * ln(K_t / K_0) + (1 - alpha) * ln(L_t / L_0)

  and the only place where adoption enters is the *hypothesis* H-005 (docs/07):

      A_t / A_0 = exp(tfp_elasticity * (x_t - x_0)).

  ``tfp_elasticity`` is **not estimated**: it is a declared assumption ("+1 pp of adoption raises
  TFP by ``tfp_elasticity`` percent", log approximation).  Capital and labour are exogenous
  constants; nothing here is an investment or labour-market model.
* **Energy is arithmetic on top of output, not an energy balance.**
  ``E_t / E_0 = (Y_t / Y_0)^energy_output_elasticity * exp(-energy_efficiency_gain * (x_t - x_0))``
  with both parameters declared.  A sector with ``energy_output_elasticity < 1`` decouples energy
  from output by assumption; the module says so instead of claiming a measured decoupling.
* **Aggregates are weighted geometric means with fixed output weights** (a Törnquist-style
  aggregate, not an index chain), so the growth-accounting identity survives aggregation; adoption
  is aggregated arithmetically as a weighted share.  Weights sum to one and are checked.

What it is not
--------------
No data are read, no calibration is performed, no probability or forecast is produced.  Every
number is ``ASSUMPTION_BASED`` and ``NOT_CALIBRATED``; the only claim is internal arithmetic
consistency under the stated assumptions.  The 17-block matrix in docs/177 therefore keeps block
17 at ``prototype`` and no admission is touched: ``admission=false``, ``status=RESEARCH_ONLY``.

Calibration sources are a *plan only* (docs/204 §6): OECD AI surveys / STI, IEA energy balances,
WIPO IP statistics (patents, PCT), Penn World Table TFP.  None of them is read here; each
parameter carries a ``replace_with`` hint naming the series that should replace it.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Sequence

MODULE_ID = "world.technology_diffusion"
MODULE_VERSION = "0.1.0"
BLOCK = 17
SCHEMA_REPORT = "amfe.world.technology_report.v1"
STATUS = "RESEARCH_ONLY"
#: No admission is granted or implied by this module.
ADMISSION = False
#: Every input is a declared assumption; no observation is read.
DATA_CLASS = "ASSUMPTION_BASED"
#: Nothing is fitted to data; the parameters are placeholders with a replacement plan.
CALIBRATION = "NOT_CALIBRATED"

TOLERANCE = 1e-12
DEFAULT_HORIZON_YEARS = 15
MIN_HORIZON_YEARS = 1
MAX_HORIZON_YEARS = 50
DEMO_HORIZON_YEARS = 10
#: Adoption is a share; a ceiling above one would be meaningless.
MAX_CEILING = 1.0

#: The aggregate sectors the prototype distinguishes.  These are not the 50 OECD ICIO sectors:
#: mapping to ICIO is a separate step (docs/179), and no claim of sectoral precision is made.
SECTORS: tuple[str, ...] = ("manufacturing", "services", "agriculture", "energy", "construction")
SECTOR_TITLES_RU = {
    "manufacturing": "Обрабатывающая промышленность",
    "services": "Услуги (включая ИТ и деловые услуги)",
    "agriculture": "Сельское хозяйство",
    "energy": "Энергетика и добыча",
    "construction": "Строительство",
}


class TechnologyError(ValueError):
    """Fail-closed error: an invalid parameter, scenario, sector or override."""


# ------------------------------------------------------------------------------------ helpers
def _finite(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise TechnologyError(f"{name} must be a number, got {value!r}") from None
    if not math.isfinite(number):
        raise TechnologyError(f"{name} must be finite, got {value!r}")
    return number


def _positive(value: object, name: str) -> float:
    number = _finite(value, name)
    if number <= 0.0:
        raise TechnologyError(f"{name} must be positive, got {value!r}")
    return number


def _non_negative(value: object, name: str) -> float:
    number = _finite(value, name)
    if number < 0.0:
        raise TechnologyError(f"{name} must be non-negative, got {value!r}")
    return number


def _unit_open(value: object, name: str) -> float:
    number = _finite(value, name)
    if not 0.0 < number < 1.0:
        raise TechnologyError(f"{name} must lie strictly between 0 and 1, got {value!r}")
    return number


def _q(value: float, digits: int = 12) -> float:
    return round(float(value), digits)


def _sha256_json(payload: Mapping) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------- parameters
@dataclass(frozen=True)
class AdoptionParameters:
    """The three declared parameters of one logistic adoption curve."""

    ceiling: float
    speed: float
    start: float

    def __post_init__(self) -> None:
        ceiling = _positive(self.ceiling, "ceiling")
        if ceiling > MAX_CEILING:
            raise TechnologyError(f"ceiling must not exceed {MAX_CEILING}, got {ceiling!r}")
        speed = _positive(self.speed, "speed")
        start = _unit_open(self.start, "start")
        if start >= ceiling:
            raise TechnologyError(f"start ({start!r}) must be strictly below ceiling ({ceiling!r})")
        object.__setattr__(self, "ceiling", ceiling)
        object.__setattr__(self, "speed", speed)
        object.__setattr__(self, "start", start)

    def as_dict(self) -> dict:
        return {"ceiling": self.ceiling, "speed": self.speed, "start": self.start}


@dataclass(frozen=True)
class SectorProfile:
    """Declared parameters of one aggregate sector (all ``ASSUMPTION_BASED``)."""

    sector: str
    adoption: AdoptionParameters
    capital_share: float
    tfp_elasticity: float
    energy_output_elasticity: float
    energy_efficiency_gain: float
    output_weight: float

    def __post_init__(self) -> None:
        if self.sector not in SECTORS:
            raise TechnologyError(f"unknown sector {self.sector!r}; known {list(SECTORS)}")
        if not isinstance(self.adoption, AdoptionParameters):
            raise TechnologyError("adoption must be an AdoptionParameters instance")
        capital_share = _unit_open(self.capital_share, f"{self.sector}.capital_share")
        tfp_elasticity = _non_negative(self.tfp_elasticity, f"{self.sector}.tfp_elasticity")
        energy_elasticity = _non_negative(self.energy_output_elasticity,
                                          f"{self.sector}.energy_output_elasticity")
        energy_gain = _non_negative(self.energy_efficiency_gain,
                                    f"{self.sector}.energy_efficiency_gain")
        weight = _positive(self.output_weight, f"{self.sector}.output_weight")
        if weight > 1.0:
            raise TechnologyError(f"{self.sector}.output_weight must not exceed 1, got {weight!r}")
        object.__setattr__(self, "capital_share", capital_share)
        object.__setattr__(self, "tfp_elasticity", tfp_elasticity)
        object.__setattr__(self, "energy_output_elasticity", energy_elasticity)
        object.__setattr__(self, "energy_efficiency_gain", energy_gain)
        object.__setattr__(self, "output_weight", weight)

    def as_dict(self) -> dict:
        return {
            "sector": self.sector,
            "title_ru": SECTOR_TITLES_RU[self.sector],
            "adoption": self.adoption.as_dict(),
            "capital_share": self.capital_share,
            "tfp_elasticity": self.tfp_elasticity,
            "energy_output_elasticity": self.energy_output_elasticity,
            "energy_efficiency_gain": self.energy_efficiency_gain,
            "output_weight": self.output_weight,
        }


@dataclass(frozen=True)
class GrowthAssumptions:
    """Exogenous capital and labour growth — constants, not an investment/labour model."""

    capital_growth: float = 0.02
    labour_growth: float = 0.004

    def __post_init__(self) -> None:
        capital = _finite(self.capital_growth, "capital_growth")
        labour = _finite(self.labour_growth, "labour_growth")
        if capital <= -1.0:
            raise TechnologyError("capital_growth must be above -1")
        if labour <= -1.0:
            raise TechnologyError("labour_growth must be above -1")
        object.__setattr__(self, "capital_growth", capital)
        object.__setattr__(self, "labour_growth", labour)

    def as_dict(self) -> dict:
        return {"capital_growth": self.capital_growth, "labour_growth": self.labour_growth}


#: Placeholder profiles.  **None of these numbers is estimated.**  They are round values chosen so
#: that the four scenarios differ in an interpretable direction; each one has a ``replace_with``
#: hint in :data:`REPLACE_WITH` and must be replaced before any claim beyond arithmetic consistency.
DEFAULT_PROFILES: dict[str, SectorProfile] = {
    "manufacturing": SectorProfile(
        sector="manufacturing",
        adoption=AdoptionParameters(ceiling=0.65, speed=0.28, start=0.08),
        capital_share=0.35, tfp_elasticity=0.25,
        energy_output_elasticity=0.85, energy_efficiency_gain=0.30, output_weight=0.30),
    "services": SectorProfile(
        sector="services",
        adoption=AdoptionParameters(ceiling=0.80, speed=0.32, start=0.10),
        capital_share=0.30, tfp_elasticity=0.30,
        energy_output_elasticity=0.75, energy_efficiency_gain=0.20, output_weight=0.45),
    "agriculture": SectorProfile(
        sector="agriculture",
        adoption=AdoptionParameters(ceiling=0.55, speed=0.22, start=0.05),
        capital_share=0.25, tfp_elasticity=0.22,
        energy_output_elasticity=0.80, energy_efficiency_gain=0.25, output_weight=0.07),
    "energy": SectorProfile(
        sector="energy",
        adoption=AdoptionParameters(ceiling=0.60, speed=0.20, start=0.12),
        capital_share=0.45, tfp_elasticity=0.20,
        energy_output_elasticity=1.00, energy_efficiency_gain=0.45, output_weight=0.08),
    "construction": SectorProfile(
        sector="construction",
        adoption=AdoptionParameters(ceiling=0.50, speed=0.18, start=0.04),
        capital_share=0.30, tfp_elasticity=0.18,
        energy_output_elasticity=0.90, energy_efficiency_gain=0.30, output_weight=0.10),
}

#: Where each declared parameter should eventually come from — a **plan**, not data (docs/204 §6).
REPLACE_WITH: dict[str, str] = {
    "ceiling": ("OECD AI surveys / OECD STI Scoreboard (диффузия ИИ по отраслям); IFR World "
                "Robotics (плотность роботов) — после проверки лицензии"),
    "speed": ("OECD diffusion studies и IFR/патентные лаги: оценка скорости по историческим "
              "кривым внедрения (только по лицензированным рядам)"),
    "start": ("OECD AI surveys / IEA (базовый уровень внедрения на год старта); заменить на "
              "наблюдаемую долю, когда ряд доступен"),
    "capital_share": "PWT (доля капитала в доходах) / OECD Productivity Statistics",
    "tfp_elasticity": ("PWT TFP + OECD STI: регрессия Δln TFP на Δвнедрение; до этого — "
                       "гипотеза H-005, не оценка"),
    "energy_output_elasticity": "IEA World Energy Balances (энергоёмкость выпуска по отраслям)",
    "energy_efficiency_gain": ("IEA энергобалансы / OECD: экономия энергии на единицу внедрения; "
                               "после калибровки — заменить"),
    "output_weight": "OECD ICIO 2025 (доли выпуска отраслей); сейчас — круглые допущения",
    "capital_growth": "PWT / IMF WEO (накопление капитала), сейчас — допущение",
    "labour_growth": "OECD / ILO (рост рабочей силы), сейчас — допущение",
}


# ---------------------------------------------------------------------------------- scenarios
@dataclass(frozen=True)
class TechnologyScenario:
    """A named, declared deformation of the baseline adoption and transmission parameters."""

    scenario_id: str
    title_ru: str
    description: str
    speed_multiplier: float = 1.0
    ceiling_multiplier: float = 1.0
    tfp_multiplier: float = 1.0
    energy_efficiency_multiplier: float = 1.0

    def __post_init__(self) -> None:
        if not str(self.scenario_id).strip():
            raise TechnologyError("scenario_id must be a non-empty string")
        for name, value in (("speed_multiplier", self.speed_multiplier),
                            ("ceiling_multiplier", self.ceiling_multiplier),
                            ("tfp_multiplier", self.tfp_multiplier),
                            ("energy_efficiency_multiplier", self.energy_efficiency_multiplier)):
            number = _positive(value, f"{self.scenario_id}.{name}")
            object.__setattr__(self, name, number)

    def apply(self, profile: SectorProfile) -> SectorProfile:
        """Return the scenario-adjusted profile (ceiling stays a share of the potential, <= 1)."""
        adoption = profile.adoption
        ceiling = min(MAX_CEILING, adoption.ceiling * self.ceiling_multiplier)
        if ceiling <= adoption.start:
            raise TechnologyError(
                f"scenario {self.scenario_id!r}: effective ceiling {ceiling!r} is not above the "
                f"starting adoption {adoption.start!r} in sector {profile.sector!r}")
        return replace(
            profile,
            adoption=AdoptionParameters(ceiling=ceiling, speed=adoption.speed * self.speed_multiplier,
                                        start=adoption.start),
            tfp_elasticity=profile.tfp_elasticity * self.tfp_multiplier,
            energy_efficiency_gain=(profile.energy_efficiency_gain
                                    * self.energy_efficiency_multiplier),
        )

    def as_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "title_ru": self.title_ru,
            "description": self.description,
            "speed_multiplier": self.speed_multiplier,
            "ceiling_multiplier": self.ceiling_multiplier,
            "tfp_multiplier": self.tfp_multiplier,
            "energy_efficiency_multiplier": self.energy_efficiency_multiplier,
        }


#: The four scenarios required by the task.  They are **declared deformations**, not forecasts and
#: not probabilities: nothing here says which one is more likely (probability is block 14's job).
SCENARIOS: dict[str, TechnologyScenario] = {
    "baseline": TechnologyScenario(
        scenario_id="baseline",
        title_ru="Базовая диффузия",
        description=("Объявленные профили без изменений: медленный логистический рост, потолки "
                     "как доли потенциала отрасли."),
    ),
    "ai_acceleration": TechnologyScenario(
        scenario_id="ai_acceleration",
        title_ru="Ускорение ИИ-внедрения",
        description=("Скорость диффузии ×1,75 и передача в TFP ×1,30; потолок не растёт (это доля "
                     "потенциала), поэтому ускорение — про скорость, а не про предел."),
        speed_multiplier=1.75, ceiling_multiplier=1.0, tfp_multiplier=1.30,
        energy_efficiency_multiplier=1.15,
    ),
    "ai_slowdown": TechnologyScenario(
        scenario_id="ai_slowdown",
        title_ru="Замедление ИИ-внедрения",
        description=("Скорость ×0,60, достижимый потолок ×0,85 (барьеры: навыки, регулирование, "
                     "инфраструктура), передача в TFP ×0,75."),
        speed_multiplier=0.60, ceiling_multiplier=0.85, tfp_multiplier=0.75,
        energy_efficiency_multiplier=0.90,
    ),
    "energy_transition": TechnologyScenario(
        scenario_id="energy_transition",
        title_ru="Энергопереход",
        description=("Общая диффузия чуть медленнее (×0,95), но энергоэффективность на единицу "
                     "внедрения ×1,60: технологии смещаются в сторону энергосбережения."),
        speed_multiplier=0.95, ceiling_multiplier=1.0, tfp_multiplier=0.90,
        energy_efficiency_multiplier=1.60,
    ),
}

assert set(SCENARIOS) == {"baseline", "ai_acceleration", "ai_slowdown", "energy_transition"}


# ------------------------------------------------------------------------------------ logistic
def logistic_level(start: float, ceiling: float, speed: float, period: float) -> float:
    """Closed-form logistic level at ``period`` years: monotone, bounded, no overshoot."""
    start = _unit_open(start, "start")
    ceiling = _positive(ceiling, "ceiling")
    if ceiling > MAX_CEILING:
        raise TechnologyError(f"ceiling must not exceed {MAX_CEILING}, got {ceiling!r}")
    if start >= ceiling:
        raise TechnologyError(f"start ({start!r}) must be strictly below ceiling ({ceiling!r})")
    speed = _positive(speed, "speed")
    period = _finite(period, "period")
    if period < 0.0:
        raise TechnologyError("period must be non-negative")
    growth = ((ceiling - start) / start) * math.exp(-speed * period)
    return ceiling / (1.0 + growth)


def logistic_path(start: float, ceiling: float, speed: float,
                  years: int) -> tuple[float, ...]:
    """The adoption path from year 0 inclusive to ``years`` (``years + 1`` points)."""
    years = int(years)
    if years < MIN_HORIZON_YEARS or years > MAX_HORIZON_YEARS:
        raise TechnologyError(
            f"years must be an integer in [{MIN_HORIZON_YEARS}, {MAX_HORIZON_YEARS}], got {years!r}")
    return tuple(logistic_level(start, ceiling, speed, float(period))
                 for period in range(years + 1))


def half_time(start: float, ceiling: float, speed: float) -> float:
    """Years until the curve reaches half of its ceiling (analytic, from the closed form)."""
    start = _unit_open(start, "start")
    ceiling = _positive(ceiling, "ceiling")
    if start >= ceiling:
        raise TechnologyError(f"start ({start!r}) must be strictly below ceiling ({ceiling!r})")
    speed = _positive(speed, "speed")
    return math.log((ceiling - start) / start) / speed


# --------------------------------------------------------------------------- growth accounting
def sector_outcome(profile: SectorProfile, scenario: TechnologyScenario,
                   growth: GrowthAssumptions, years: int) -> dict:
    """Run one sector: adoption path, TFP, output, energy and the identity check."""
    years = int(years)
    if years < MIN_HORIZON_YEARS or years > MAX_HORIZON_YEARS:
        raise TechnologyError(
            f"years must be an integer in [{MIN_HORIZON_YEARS}, {MAX_HORIZON_YEARS}], got {years!r}")
    adjusted = scenario.apply(profile)
    adoption = adjusted.adoption
    path = logistic_path(adoption.start, adoption.ceiling, adoption.speed, years)

    alpha = adjusted.capital_share
    tfp_ratio, output_ratio, energy_ratio = [], [], []
    for period, level in enumerate(path):
        delta = level - adoption.start
        tfp = math.exp(adjusted.tfp_elasticity * delta)
        capital = (1.0 + growth.capital_growth) ** period
        labour = (1.0 + growth.labour_growth) ** period
        output = tfp * capital ** alpha * labour ** (1.0 - alpha)
        energy = output ** adjusted.energy_output_elasticity * math.exp(
            -adjusted.energy_efficiency_gain * delta)
        tfp_ratio.append(tfp)
        output_ratio.append(output)
        energy_ratio.append(energy)

    delta_horizon = path[-1] - adoption.start
    tfp_log = adjusted.tfp_elasticity * delta_horizon
    capital_log = alpha * years * math.log1p(growth.capital_growth)
    labour_log = (1.0 - alpha) * years * math.log1p(growth.labour_growth)
    identity_residual = abs(tfp_log + capital_log + labour_log - math.log(output_ratio[-1]))

    increments = [path[index + 1] - path[index] for index in range(years)]
    checks = {
        "adoption_monotone": all(step >= -TOLERANCE for step in increments),
        "adoption_bounded": (min(path) >= -TOLERANCE
                             and max(path) <= adoption.ceiling + TOLERANCE),
        "adoption_within_unit_interval": max(path) <= 1.0 + TOLERANCE,
        "start_matches_curve": abs(path[0] - adoption.start) <= TOLERANCE,
        "tfp_ratio_positive": all(value > 0.0 for value in tfp_ratio),
        "output_ratio_positive": all(value > 0.0 for value in output_ratio),
        "energy_ratio_positive": all(value > 0.0 for value in energy_ratio),
        "growth_accounting_identity": identity_residual <= TOLERANCE,
        "paths_length": len(path) == years + 1,
    }
    return {
        "sector": profile.sector,
        "title_ru": SECTOR_TITLES_RU[profile.sector],
        "scenario_id": scenario.scenario_id,
        "profile": {
            "ceiling_effective": _q(adoption.ceiling),
            "speed_effective": _q(adoption.speed),
            "start": _q(adoption.start),
            "capital_share": _q(alpha),
            "tfp_elasticity": _q(adjusted.tfp_elasticity),
            "energy_output_elasticity": _q(adjusted.energy_output_elasticity),
            "energy_efficiency_gain": _q(adjusted.energy_efficiency_gain),
            "output_weight": _q(adjusted.output_weight),
            "ceiling_capped_by_unit_interval": bool(
                adoption.ceiling * scenario.ceiling_multiplier > MAX_CEILING),
        },
        "paths": {
            "adoption": [_q(value) for value in path],
            "tfp_ratio": [_q(value) for value in tfp_ratio],
            "output_ratio": [_q(value) for value in output_ratio],
            "energy_ratio": [_q(value) for value in energy_ratio],
            "energy_intensity_ratio": [_q(energy_ratio[index] / output_ratio[index])
                                       for index in range(len(path))],
        },
        "horizon": {
            "years": years,
            "adoption": _q(path[-1]),
            "delta_adoption": _q(delta_horizon),
            "adoption_share_of_ceiling": _q(path[-1] / adoption.ceiling),
            "tfp_gain_pct": _q(100.0 * (tfp_ratio[-1] - 1.0), 6),
            "output_gain_pct": _q(100.0 * (output_ratio[-1] - 1.0), 6),
            "energy_change_pct": _q(100.0 * (energy_ratio[-1] - 1.0), 6),
            "energy_intensity_change_pct": _q(
                100.0 * (energy_ratio[-1] / output_ratio[-1] - 1.0), 6),
            "contributions_log": {
                "tfp": _q(tfp_log),
                "capital": _q(capital_log),
                "labour": _q(labour_log),
                "total": _q(tfp_log + capital_log + labour_log),
            },
            "identity_residual": _q(identity_residual),
        },
        "checks": checks,
    }


def aggregate_outcome(profiles: Mapping[str, SectorProfile], outcomes: Mapping[str, dict],
                      weights: Mapping[str, float], years: int) -> dict:
    """Weighted aggregate: arithmetic adoption, geometric output/TFP/energy (identity survives)."""
    order = [sector for sector in SECTORS if sector in outcomes]
    if not order:
        raise TechnologyError("no sectors to aggregate")
    total = math.fsum(weights[sector] for sector in order)
    if abs(total) <= TOLERANCE:
        raise TechnologyError("aggregate weights sum to zero")
    normalised = {sector: weights[sector] / total for sector in order}

    adoption_path = [math.fsum(normalised[sector] * outcomes[sector]["paths"]["adoption"][index]
                               for sector in order)
                     for index in range(years + 1)]
    output_path = [math.exp(math.fsum(normalised[sector]
                                      * math.log(outcomes[sector]["paths"]["output_ratio"][index])
                                      for sector in order))
                   for index in range(years + 1)]
    tfp_path = [math.exp(math.fsum(normalised[sector]
                                   * math.log(outcomes[sector]["paths"]["tfp_ratio"][index])
                                   for sector in order))
                for index in range(years + 1)]
    energy_path = [math.exp(math.fsum(normalised[sector]
                                      * math.log(outcomes[sector]["paths"]["energy_ratio"][index])
                                      for sector in order))
                   for index in range(years + 1)]
    intensity_path = [energy_path[index] / output_path[index] for index in range(years + 1)]

    contributions = {
        key: math.fsum(normalised[sector]
                       * outcomes[sector]["horizon"]["contributions_log"][key]
                       for sector in order)
        for key in ("tfp", "capital", "labour")
    }
    contributions["total"] = math.fsum(contributions[key] for key in ("tfp", "capital", "labour"))
    identity_residual = abs(contributions["total"] - math.log(output_path[-1]))

    start = math.fsum(normalised[sector] * profiles[sector].adoption.start for sector in order)
    weights_sum = math.fsum(normalised.values())
    checks = {
        "weights_sum_to_one": abs(weights_sum - 1.0) <= 1e-9,
        "adoption_monotone": all(adoption_path[index + 1] >= adoption_path[index] - TOLERANCE
                                 for index in range(years)),
        "adoption_bounded": (min(adoption_path) >= -TOLERANCE
                             and max(adoption_path) <= 1.0 + TOLERANCE),
        "output_ratio_positive": all(value > 0.0 for value in output_path),
        "energy_ratio_positive": all(value > 0.0 for value in energy_path),
        "growth_accounting_identity": identity_residual <= 1e-9,
        "paths_length": len(adoption_path) == years + 1,
    }
    return {
        "sectors_included": order,
        "sector_weights": {sector: _q(normalised[sector]) for sector in order},
        "weights_renormalised": bool(abs(total - 1.0) > 1e-9),
        "paths": {
            "adoption": [_q(value) for value in adoption_path],
            "tfp_ratio": [_q(value) for value in tfp_path],
            "output_ratio": [_q(value) for value in output_path],
            "energy_ratio": [_q(value) for value in energy_path],
            "energy_intensity_ratio": [_q(value) for value in intensity_path],
        },
        "horizon": {
            "years": years,
            "adoption": _q(adoption_path[-1]),
            "delta_adoption": _q(adoption_path[-1] - start),
            "tfp_gain_pct": _q(100.0 * (tfp_path[-1] - 1.0), 6),
            "output_gain_pct": _q(100.0 * (output_path[-1] - 1.0), 6),
            "energy_change_pct": _q(100.0 * (energy_path[-1] - 1.0), 6),
            "energy_intensity_change_pct": _q(100.0 * (intensity_path[-1] - 1.0), 6),
            "contributions_log": {key: _q(value) for key, value in contributions.items()},
            "identity_residual": _q(identity_residual),
        },
        "checks": checks,
        "note": ("агрегат — взвешенное геометрическое среднее уровней с фиксированными весами "
                 "выпуска (приём Törnquist, не индексная цепь); внедрение агрегируется "
                 "арифметически как взвешенная доля"),
    }


# ------------------------------------------------------------------------------------ runner
def run_scenario(scenario: TechnologyScenario, profiles: Mapping[str, SectorProfile],
                 growth: GrowthAssumptions, years: int,
                 sectors: Sequence[str] | None = None) -> dict:
    """Run one scenario over the requested sectors and return an aggregate-only document."""
    if not isinstance(scenario, TechnologyScenario):
        raise TechnologyError("scenario must be a TechnologyScenario")
    if not isinstance(growth, GrowthAssumptions):
        raise TechnologyError("growth must be a GrowthAssumptions instance")
    selected = _selected_sectors(profiles, sectors)
    outcomes = {sector: sector_outcome(profiles[sector], scenario, growth, years)
                for sector in selected}
    weights = {sector: profiles[sector].output_weight for sector in selected}
    aggregate = aggregate_outcome(profiles, outcomes, weights, years)
    checks = {
        "all_sectors_ran": set(outcomes) == set(selected),
        "all_sector_checks_pass": all(all(item["checks"].values()) for item in outcomes.values()),
        "aggregate_checks_pass": all(aggregate["checks"].values()),
    }
    return {
        "scenario_id": scenario.scenario_id,
        "title_ru": scenario.title_ru,
        "description": scenario.description,
        "parameters": scenario.as_dict(),
        "sectors": outcomes,
        "aggregate": aggregate,
        "checks": checks,
    }


def _selected_sectors(profiles: Mapping[str, SectorProfile],
                      sectors: Sequence[str] | None) -> list[str]:
    available = [sector for sector in SECTORS if sector in profiles]
    if sectors is None:
        if not available:
            raise TechnologyError("no sector profiles declared")
        return available
    requested = [str(sector) for sector in sectors]
    if not requested:
        raise TechnologyError("--sector was given but no sector was listed")
    unknown = [sector for sector in requested if sector not in profiles]
    if unknown:
        raise TechnologyError(f"unknown sectors {unknown}; available {available}")
    seen: list[str] = []
    for sector in requested:
        if sector not in seen:
            seen.append(sector)
    return seen


# --------------------------------------------------------------------------------- overrides
#: Global structural parameters that ``--params`` may override.
GLOBAL_PARAMETERS: dict[str, float] = {"capital_growth": 0.02, "labour_growth": 0.004}
#: Per-sector structural parameters that ``--params`` may override as ``name.sector=value``.
SECTOR_PARAMETERS: dict[str, float] = {
    "ceiling": 0.5, "speed": 0.2, "start": 0.05, "capital_share": 0.3,
    "tfp_elasticity": 0.2, "energy_output_elasticity": 0.8,
    "energy_efficiency_gain": 0.3, "output_weight": 0.2,
}


#: Declared admissible range of every overridable parameter: ``(low, high, low_inclusive)``.
_PARAMETER_RANGES: dict[str, tuple[float, float, bool]] = {
    "ceiling": (0.0, 1.0, False),
    "speed": (0.0, math.inf, False),
    "start": (0.0, 1.0, False),
    "capital_share": (0.0, 1.0, False),
    "tfp_elasticity": (0.0, math.inf, True),
    "energy_output_elasticity": (0.0, math.inf, True),
    "energy_efficiency_gain": (0.0, math.inf, True),
    "output_weight": (0.0, 1.0, False),
    "capital_growth": (-1.0, math.inf, False),
    "labour_growth": (-1.0, math.inf, False),
}


def _validate_parameter(name: str, number: float) -> float:
    low, high, low_inclusive = _PARAMETER_RANGES[name]
    if low_inclusive:
        if number < low or number > high:
            raise TechnologyError(f"{name} must be in [{low}, {high}], got {number!r}")
    elif not low < number <= high:
        raise TechnologyError(f"{name} must be in ({low}, {high}], got {number!r}")
    return number


def parse_overrides(pairs: Iterable[str] | None) -> dict:
    """Parse ``key=value`` (repeatable) into typed overrides; fail closed on anything unknown."""
    overrides: dict = {}
    for item in pairs or []:
        if not isinstance(item, str) or "=" not in item:
            raise TechnologyError(f"--params expects key=value, got {item!r}")
        key, _, raw = item.partition("=")
        key = key.strip()
        value = raw.strip()
        if not key:
            raise TechnologyError(f"--params has an empty key: {item!r}")
        if not value:
            raise TechnologyError(f"--params {key} has an empty value")
        if "." in key:
            name, _, sector = key.partition(".")
            if name not in SECTOR_PARAMETERS:
                raise TechnologyError(
                    f"unknown sector parameter {name!r}; allowed {sorted(SECTOR_PARAMETERS)}")
            if sector not in SECTORS:
                raise TechnologyError(f"unknown sector {sector!r}; known {list(SECTORS)}")
        elif key not in GLOBAL_PARAMETERS:
            raise TechnologyError(
                f"unknown parameter {key!r}; allowed {sorted(GLOBAL_PARAMETERS)} or "
                f"<name>.<sector> with names {sorted(SECTOR_PARAMETERS)}")
        try:
            number = float(value)
        except ValueError:
            raise TechnologyError(f"{key} expects a numeric value, got {value!r}") from None
        if not math.isfinite(number):
            raise TechnologyError(f"{key} must be finite, got {value!r}")
        overrides[key] = _validate_parameter(key.partition(".")[0], number)
    return overrides


def apply_overrides(profiles: Mapping[str, SectorProfile], growth: GrowthAssumptions,
                    overrides: Mapping) -> tuple[dict[str, SectorProfile], GrowthAssumptions]:
    """Return new profiles and growth assumptions with the overrides applied (fail-closed)."""
    profile_fields = dict(profiles)
    growth_fields = growth.as_dict()
    pending: dict[str, dict] = {}
    for key, value in dict(overrides or {}).items():
        base = key.partition(".")[0] if "." in key else key
        if base not in _PARAMETER_RANGES:
            raise TechnologyError(f"cannot apply unknown override {key!r}")
        number = _validate_parameter(base, _finite(value, key))
        if "." in key:
            name, _, sector = key.partition(".")
            if name not in SECTOR_PARAMETERS or sector not in profiles:
                raise TechnologyError(f"cannot apply override {key!r}")
            pending.setdefault(sector, {})[name] = number
        elif key in GLOBAL_PARAMETERS:
            growth_fields[key] = number
        else:
            raise TechnologyError(f"cannot apply override {key!r}")
    for sector, fields in pending.items():
        profile = profile_fields[sector]
        adoption_fields = {name: fields[name] for name in ("ceiling", "speed", "start")
                           if name in fields}
        adoption = replace(profile.adoption, **adoption_fields) if adoption_fields else profile.adoption
        rest = {name: fields[name] for name in fields
                if name not in ("ceiling", "speed", "start")}
        profile_fields[sector] = replace(profile, adoption=adoption, **rest)
    return profile_fields, GrowthAssumptions(**growth_fields)


# --------------------------------------------------------------------------------- assumptions
def sector_assumptions(profiles: Mapping[str, SectorProfile]) -> list[dict]:
    """Every declared parameter, with its class, units and replacement hint."""
    items: list[dict] = []
    for sector in SECTORS:
        if sector not in profiles:
            continue
        profile = profiles[sector]
        base = {"source_class": "assumption", "sector": sector}
        items.extend([
            dict(base, name=f"ceiling.{sector}", value=profile.adoption.ceiling,
                 unit="доля потенциала отрасли",
                 note="потолок — предел, а не оценка насыщения",
                 replace_with=REPLACE_WITH["ceiling"]),
            dict(base, name=f"speed.{sector}", value=profile.adoption.speed, unit="1/год",
                 note="скорость логистики; НЕ калибрована",
                 replace_with=REPLACE_WITH["speed"]),
            dict(base, name=f"start.{sector}", value=profile.adoption.start,
                 unit="доля потенциала", note="внедрение в год 0",
                 replace_with=REPLACE_WITH["start"]),
            dict(base, name=f"capital_share.{sector}", value=profile.capital_share,
                 unit="доля дохода", note="α в Y = A K^α L^(1-α)",
                 replace_with=REPLACE_WITH["capital_share"]),
            dict(base, name=f"tfp_elasticity.{sector}", value=profile.tfp_elasticity,
                 unit="ln TFP на 1 ед. внедрения",
                 note="гипотеза H-005: Δln A = tfp_elasticity × Δвнедрение; не причинная оценка",
                 replace_with=REPLACE_WITH["tfp_elasticity"]),
            dict(base, name=f"energy_output_elasticity.{sector}",
                 value=profile.energy_output_elasticity, unit="эластичность",
                 note="<1 означает «отрыв» энергии от выпуска — это допущение",
                 replace_with=REPLACE_WITH["energy_output_elasticity"]),
            dict(base, name=f"energy_efficiency_gain.{sector}",
                 value=profile.energy_efficiency_gain, unit="ln энергии на 1 ед. внедрения",
                 note="экономия энергии на единицу внедрения",
                 replace_with=REPLACE_WITH["energy_efficiency_gain"]),
            dict(base, name=f"output_weight.{sector}", value=profile.output_weight,
                 unit="доля выпуска", note="вес агрегата; сумма = 1",
                 replace_with=REPLACE_WITH["output_weight"]),
        ])
    for name, value in GLOBAL_PARAMETERS.items():
        items.append({"name": name, "value": value, "unit": "1/год", "source_class": "assumption",
                      "sector": None, "note": "экзогенный темп, не результат модели",
                      "replace_with": REPLACE_WITH[name]})
    return items


#: Honest restrictions, repeated in every report and in docs/204.
LIMITATIONS: tuple[str, ...] = (
    "все параметры — объявленные допущения; калибровки нет, ни один ряд внедрения, роботизации, "
    "ИИ или НИОКР не читается и не хранится;",
    "одна агрегированная кривая внедрения на отрасль: ИИ, роботизация, НИОКР и связь не "
    "разделены; разложение по технологиям — отдельный слой;",
    "передача в TFP — гипотеза H-005 с постоянной эластичностью; внедрение не экзогенный шок и "
    "не причинная оценка политики;",
    "рост капитала и труда экзогенны и постоянны: нет инвестиционного решения, износа, рынка "
    "труда и обратной связи спроса;",
    "агрегат — взвешенное геометрическое среднее с фиксированными весами выпуска; перераспределение "
    "между отраслями не моделируется (веса не эндогенны);",
    "энергия — арифметика поверх выпуска: нет энергобаланса, структуры топлива, цен и физических "
    "пределов;",
    "модуль не сдвигает коэффициенты MRIO: единственный канал сдвига A остаётся technology_shock в "
    "world/io_core.py; связка «внедрение → ΔA» не реализована;",
    "статус RESEARCH_ONLY, admission=false: сценарий не прогноз, не вероятность и не допуск; "
    "числа нельзя цитировать как наблюдение или предсказание.",
)


def describe() -> dict:
    """Module passport (the same fields the module registry expects)."""
    return {
        "module_id": MODULE_ID,
        "block": BLOCK,
        "title_ru": "Наука, технологии и ИИ: диффузия внедрения, TFP, выпуск и энергия",
        "version": MODULE_VERSION,
        "status": STATUS,
        "admission": ADMISSION,
        "data_class": DATA_CLASS,
        "calibration": CALIBRATION,
        "sectors": list(SECTORS),
        "scenarios": [SCENARIOS[key].scenario_id for key in SCENARIOS],
        "docs": "docs/204_Блок_17_технологии_ИИ.md",
        "tests": "tests/test_technology_scenarios.py",
        "dependencies": ("stdlib only; numpy не требуется"),
        "caveat_ru": ("исследовательский прототип блока 17: логистическая диффузия и growth "
                      "accounting на объявленных допущениях, без данных и без калибровки"),
    }


def build_report(scenario_ids: Sequence[str] | None = None,
                 sectors: Sequence[str] | None = None,
                 horizon_years: int = DEFAULT_HORIZON_YEARS,
                 overrides: Mapping | None = None,
                 profiles: Mapping[str, SectorProfile] | None = None,
                 growth: GrowthAssumptions | None = None) -> dict:
    """Build one deterministic, aggregate-only report over the requested scenarios."""
    scenarios = _selected_scenarios(scenario_ids)
    years = _check_horizon(horizon_years)
    profiles = dict(profiles or DEFAULT_PROFILES)
    growth = growth or GrowthAssumptions()
    profiles, growth = apply_overrides(profiles, growth, overrides or {})
    _check_weights(profiles)
    selected = _selected_sectors(profiles, sectors)
    documents = [run_scenario(SCENARIOS[scenario_id], profiles, growth, years, selected)
                 for scenario_id in scenarios]
    payload = {
        "schema": SCHEMA_REPORT,
        "status": STATUS,
        "admission": ADMISSION,
        "research_only": True,
        "data_class": DATA_CLASS,
        "calibration": CALIBRATION,
        "block": BLOCK,
        "module": describe(),
        "horizon_years": years,
        "scenario_ids": scenarios,
        "sectors": selected,
        "growth": growth.as_dict(),
        "scenarios": documents,
        "assumptions": sector_assumptions(profiles),
        "checks": {
            "all_scenario_checks_pass": all(all(item["checks"].values()) for item in documents),
            "all_sector_checks_pass": all(all(sector_checks["checks"].values())
                                          for item in documents
                                          for sector_checks in item["sectors"].values()),
            "all_aggregate_checks_pass": all(all(item["aggregate"]["checks"].values())
                                             for item in documents),
            "scenario_count": len(documents),
            "scenario_count_expected": len(scenarios),
            "scenario_count_matches": len(documents) == len(scenarios),
            "no_raw_series": _no_raw_series(documents, years),
        },
        "limitations": list(LIMITATIONS),
        "note": ("исследовательский прототип блока 17: сценарная арифметика на объявленных "
                 "допущениях; не прогноз, не допуск, не инвестиционная рекомендация"),
    }
    payload["digest"] = _sha256_json(payload)
    return payload


def _selected_scenarios(scenario_ids: Sequence[str] | None) -> list[str]:
    if scenario_ids is None:
        return list(SCENARIOS)
    requested = [str(scenario_id) for scenario_id in scenario_ids]
    if not requested:
        raise TechnologyError("at least one scenario is required")
    unknown = [scenario_id for scenario_id in requested if scenario_id not in SCENARIOS]
    if unknown:
        raise TechnologyError(f"unknown scenario ids {unknown}; available {list(SCENARIOS)}")
    ordered: list[str] = []
    for scenario_id in requested:
        if scenario_id not in ordered:
            ordered.append(scenario_id)
    return ordered


def _check_horizon(horizon_years: int) -> int:
    if isinstance(horizon_years, bool) or not isinstance(horizon_years, int):
        raise TechnologyError(f"horizon must be an integer, got {horizon_years!r}")
    if not MIN_HORIZON_YEARS <= horizon_years <= MAX_HORIZON_YEARS:
        raise TechnologyError(
            f"horizon must be in [{MIN_HORIZON_YEARS}, {MAX_HORIZON_YEARS}] years, "
            f"got {horizon_years!r}")
    return horizon_years


def _check_weights(profiles: Mapping[str, SectorProfile]) -> None:
    total = math.fsum(profile.output_weight for profile in profiles.values())
    if abs(total - 1.0) > 1e-9:
        raise TechnologyError(
            f"output weights must sum to 1 across sectors, got {total!r}; adjust --params "
            "output_weight.<sector> together")


def _no_raw_series(documents: Sequence[dict], years: int) -> bool:
    """Aggregate-only guard: no numeric list may be longer than the horizon + 1."""
    def walk(node) -> bool:
        if isinstance(node, dict):
            return all(walk(value) for value in node.values())
        if isinstance(node, list):
            if node and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                            for value in node):
                return len(node) <= years + 1
            return all(walk(value) for value in node)
        return True

    return all(walk(document) for document in documents)


def demo_report(horizon_years: int = DEMO_HORIZON_YEARS) -> dict:
    """The demonstration: baseline vs AI acceleration on the aggregate and every sector."""
    years = _check_horizon(horizon_years)
    report = build_report(["baseline", "ai_acceleration"], None, years)
    report.pop("digest", None)
    report["demo"] = {
        "title_ru": "Демонстрация блока 17: базовая диффузия против ускорения ИИ",
        "horizon_years": years,
        "note": ("сравнение двух объявленных сценариев на одних и тех же допущениях; разница — "
                 "следствие множителей сценария, а не оценка эффекта ИИ"),
    }
    report["digest"] = _sha256_json(report)
    return report


__all__ = [
    "ADMISSION", "AdoptionParameters", "BLOCK", "CALIBRATION", "DATA_CLASS",
    "DEFAULT_HORIZON_YEARS", "DEFAULT_PROFILES", "DEMO_HORIZON_YEARS", "GLOBAL_PARAMETERS",
    "GrowthAssumptions", "LIMITATIONS", "MAX_HORIZON_YEARS", "MIN_HORIZON_YEARS", "MODULE_ID",
    "MODULE_VERSION", "REPLACE_WITH", "SCENARIOS", "SCHEMA_REPORT", "SECTORS",
    "SECTOR_PARAMETERS", "SECTOR_TITLES_RU", "STATUS", "SectorProfile", "TechnologyError",
    "TechnologyScenario", "TOLERANCE", "aggregate_outcome", "apply_overrides", "build_report",
    "demo_report", "describe", "half_time", "logistic_level", "logistic_path", "parse_overrides",
    "run_scenario", "sector_assumptions", "sector_outcome",
]
