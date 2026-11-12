"""Shared contract for the AMFE-17 dynamic mechanisms (stage 5).

The point of the package is not to add "one more model" but to keep five epistemically different
objects apart, as required by docs/38 (I10) and the roadmap (docs/42, stage 5):

    IDENTITY            - an accounting/mathematical identity; can be a hard constraint;
    PHYSICAL_LIMIT      - a physical or capacity bound (adoption <= market size, shares sum to 1);
    INSTITUTIONAL_RULE  - an institutional arrangement accepted as an exogenous assumption;
    HYPOTHESIS          - a behavioural/statistical hypothesis that must be falsifiable and tested;
    SCENARIO            - an exogenous "what if" path; never a causal estimate and never a gate.

A Mechanism object carries its own admission rule: a hypothesis cannot be reported as a fact and
must name the tests that could refute it, a scenario must not claim causality and must not gate
admission, and an identity cannot claim causality either.

This module is deliberately independent of the forecast/evaluation/harness layers: it imports only
the standard library and numpy and never reads protocols or data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

KIND_IDENTITY = "IDENTITY"
KIND_PHYSICAL_LIMIT = "PHYSICAL_LIMIT"
KIND_INSTITUTIONAL_RULE = "INSTITUTIONAL_RULE"
KIND_HYPOTHESIS = "HYPOTHESIS"
KIND_SCENARIO = "SCENARIO"
MECHANISM_KINDS = (KIND_IDENTITY, KIND_PHYSICAL_LIMIT, KIND_INSTITUTIONAL_RULE,
                   KIND_HYPOTHESIS, KIND_SCENARIO)

KIND_LABELS_RU = {
    KIND_IDENTITY: "тождество",
    KIND_PHYSICAL_LIMIT: "физическое ограничение",
    KIND_INSTITUTIONAL_RULE: "институциональное правило",
    KIND_HYPOTHESIS: "гипотеза (проверяемая)",
    KIND_SCENARIO: "сценарий (экзогенный)",
}

ADMISSION_RULES_RU = {
    KIND_IDENTITY: "может входить как жёсткое ограничение; проверяется на согласованность данных",
    KIND_PHYSICAL_LIMIT: "может входить как жёсткое ограничение; нарушение запрещено по построению",
    KIND_INSTITUTIONAL_RULE: "принимается как экзогенное правило; требуется источник и дата",
    KIND_HYPOTHESIS: "не является фактом; обязана назвать тесты/данные, способные её опровергнуть",
    KIND_SCENARIO: "не гейтит допуск, не является оценкой причинного эффекта; публикуется как сценарий",
}


class DynamicsError(ValueError):
    """Raised for invalid dynamic-mechanism inputs or contract violations."""


def _check_kind(kind: str) -> None:
    if kind not in MECHANISM_KINDS:
        raise DynamicsError(f"unknown mechanism kind {kind!r}; expected one of {MECHANISM_KINDS}")


@dataclass(frozen=True)
class Mechanism:
    """One dynamic mechanism with its epistemic class and admission contract."""

    mechanism_id: str
    title_ru: str
    kind: str
    description_ru: str
    assumptions: tuple[str, ...] = ()
    tested_on: tuple[str, ...] = ()
    source: str = "docs/38"
    causal_claim: bool = False
    gating: bool = False

    def __post_init__(self) -> None:
        _check_kind(self.kind)
        if not isinstance(self.mechanism_id, str) or not self.mechanism_id.strip():
            raise DynamicsError("mechanism_id must be a non-empty string")
        if self.kind != KIND_INSTITUTIONAL_RULE and not isinstance(self.tested_on, tuple):
            raise DynamicsError("tested_on must be a tuple of test/data references")
        if self.kind == KIND_HYPOTHESIS and not self.tested_on:
            raise DynamicsError(
                f"hypothesis {self.mechanism_id} must name the tests/data that could refute it")
        if self.kind == KIND_SCENARIO and self.causal_claim:
            raise DynamicsError(f"scenario {self.mechanism_id} must not claim a causal effect")
        if self.kind == KIND_SCENARIO and self.gating:
            raise DynamicsError(f"scenario {self.mechanism_id} must not gate admission")
        if self.kind in (KIND_IDENTITY, KIND_PHYSICAL_LIMIT) and self.causal_claim:
            raise DynamicsError(f"{self.kind} {self.mechanism_id} is not a causal claim")
        if not self.description_ru:
            raise DynamicsError("description_ru must not be empty")

    @property
    def label_ru(self) -> str:
        return KIND_LABELS_RU[self.kind]

    @property
    def falsifiable(self) -> bool:
        return self.kind == KIND_HYPOTHESIS and bool(self.tested_on)

    def admission_rule_ru(self) -> str:
        return ADMISSION_RULES_RU[self.kind]

    def validate(self) -> "Mechanism":
        self.__post_init__()  # frozen dataclass: re-run the contract checks
        return self

    def to_dict(self) -> dict:
        return {
            "mechanism_id": self.mechanism_id,
            "title_ru": self.title_ru,
            "kind": self.kind,
            "label_ru": self.label_ru,
            "falsifiable": self.falsifiable,
            "causal_claim": self.causal_claim,
            "gating": self.gating,
            "description_ru": self.description_ru,
            "assumptions": list(self.assumptions),
            "tested_on": list(self.tested_on),
            "source": self.source,
            "admission_rule_ru": self.admission_rule_ru(),
        }


@dataclass(frozen=True)
class Scenario:
    """A deterministic exogenous shock path (kind == SCENARIO, never causal, never gating)."""

    scenario_id: str
    title_ru: str
    shocks: dict
    horizon: int
    description_ru: str = ""
    causal_claim: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_id, str) or not self.scenario_id.strip():
            raise DynamicsError("scenario_id must be a non-empty string")
        if not isinstance(self.shocks, dict) or not self.shocks:
            raise DynamicsError("shocks must be a non-empty dict {variable: size}")
        for name, value in self.shocks.items():
            if not isinstance(name, str) or not name:
                raise DynamicsError("shock names must be non-empty strings")
            if not math.isfinite(float(value)):
                raise DynamicsError(f"shock {name!r} must be finite")
        if not isinstance(self.horizon, int) or self.horizon < 1:
            raise DynamicsError("horizon must be a positive integer")
        if self.causal_claim:
            raise DynamicsError("a Scenario describes a path, not a causal effect")

    def path(self) -> dict:
        """Step path: every shock stays at its size for the whole horizon."""
        return {name: [float(value)] * self.horizon for name, value in self.shocks.items()}

    def mechanism(self) -> Mechanism:
        return Mechanism(
            mechanism_id=f"SCENARIO-{self.scenario_id}",
            title_ru=self.title_ru,
            kind=KIND_SCENARIO,
            description_ru=self.description_ru or self.title_ru,
            assumptions=("размеры шоков заданы сценарием, а не оценены на данных",),
            source="docs/42 stage 5 / scenario layer",
        )


def validate_catalog(catalog: tuple) -> tuple:
    """Validate the whole mechanism catalog and return it (raises on any contract violation)."""
    if not catalog:
        raise DynamicsError("empty mechanism catalog")
    seen = set()
    for mechanism in catalog:
        if not isinstance(mechanism, Mechanism):
            raise DynamicsError(f"catalog entry is not a Mechanism: {mechanism!r}")
        mechanism.validate()
        if mechanism.mechanism_id in seen:
            raise DynamicsError(f"duplicate mechanism id {mechanism.mechanism_id}")
        seen.add(mechanism.mechanism_id)
    missing = [kind for kind in MECHANISM_KINDS
               if kind not in {mechanism.kind for mechanism in catalog}]
    if missing:
        raise DynamicsError(f"catalog does not cover mechanism kinds: {missing}")
    return catalog


def catalog_table(catalog: tuple) -> list:
    """Human/JSON-friendly table of the catalog for docs and reports."""
    return [mechanism.to_dict() for mechanism in validate_catalog(catalog)]


def mechanism_counts(catalog: tuple) -> dict:
    counts = {kind: 0 for kind in MECHANISM_KINDS}
    for mechanism in validate_catalog(catalog):
        counts[mechanism.kind] += 1
    return counts
