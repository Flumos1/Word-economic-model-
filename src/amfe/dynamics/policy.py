"""Policy reaction: the Taylor rule as a HYPOTHESIS, not an institutional fact.

Two objects are kept apart:

* ``InflationTargetRegime`` - an INSTITUTIONAL_RULE: the target and the mandate are accepted as an
  exogenous arrangement (with a source and a date), they are not estimated here;
* ``TaylorRule`` and ``NewKeynesianDeterminacySystem`` - HYPOTHESES about the reaction function and
  about the transmission channels.  The Taylor principle (phi_pi > 1) is a *determinacy* condition
  for the linearised forward-looking system: it is a theoretical statement under stated
  assumptions, not evidence that the central bank follows the rule.

The determinacy check follows Blanchard-Kahn: with both variables non-predetermined, a unique
bounded rational-expectations solution requires every eigenvalue of the expectational matrix
E_t z_{t+1} = A z_t to lie strictly outside the unit circle.  The caveat of
:mod:`amfe.dynamics.stability` applies: this is a local, linearised statement and not a crisis
forecast.
"""
from __future__ import annotations

import math

import numpy as np

from .base import DynamicsError, KIND_HYPOTHESIS, KIND_INSTITUTIONAL_RULE, Mechanism
from .stability import LOCAL_STABILITY_CAVEAT, analyse_jacobian

TAYLOR_RULE_MECHANISM = Mechanism(
    mechanism_id="DYN-POLICY-TAYLOR",
    title_ru="Правило Тейлора (гипотеза реакции)",
    kind=KIND_HYPOTHESIS,
    description_ru=(
        "Ставка политики реагирует на отклонение инфляции от цели и на разрыв выпуска: "
        "i = r* + pi + phi_pi*(pi - pi*) + phi_y*gap. Коэффициенты — гипотеза о поведении, "
        "а не институциональный факт; проверяются по данным и на устойчивость линеаризованной системы."),
    assumptions=("цель по инфляции известна", "реакция линейна и постоянна внутри режима"),
    tested_on=("tests/test_dynamics.py",),
    source="docs/38 block 11; docs/34 M06",
)

INFLATION_TARGET_MECHANISM = Mechanism(
    mechanism_id="DYN-POLICY-INFLATION-TARGET",
    title_ru="Режим инфляционного таргетирования",
    kind=KIND_INSTITUTIONAL_RULE,
    description_ru=(
        "Институциональное правило: цель по инфляции и мандат задаются извне модели на дату решения. "
        "Параметр является допущением, а не оценкой; при смене режима меняется и правило."),
    assumptions=("цель объявлена и стабильна в пределах режима",),
    source="docs/38 block 11",
)


class InflationTargetRegime:
    """Institutional arrangement: an inflation target accepted as an exogenous rule."""

    def __init__(self, target: float = 2.0, source: str = "not stated"):
        target = float(target)
        if not math.isfinite(target):
            raise DynamicsError("inflation target must be finite")
        if not isinstance(source, str) or not source:
            raise DynamicsError("an institutional rule needs a source string and a date")
        self.target = target
        self.source = source

    def mechanism(self) -> Mechanism:
        return Mechanism(
            mechanism_id=INFLATION_TARGET_MECHANISM.mechanism_id,
            title_ru=INFLATION_TARGET_MECHANISM.title_ru,
            kind=INFLATION_TARGET_MECHANISM.kind,
            description_ru=(INFLATION_TARGET_MECHANISM.description_ru
                            + f" Текущая цель: {self.target} п.п.; источник: {self.source}."),
            assumptions=INFLATION_TARGET_MECHANISM.assumptions,
            source=self.source,
        )


class TaylorRule:
    """Linear Taylor-type reaction function: a behavioural hypothesis."""

    def __init__(self, r_star: float = 0.5, pi_target: float = 2.0,
                 phi_pi: float = 1.5, phi_y: float = 0.5):
        r_star, pi_target, phi_pi, phi_y = (float(r_star), float(pi_target),
                                            float(phi_pi), float(phi_y))
        if not all(math.isfinite(item) for item in (r_star, pi_target, phi_pi, phi_y)):
            raise DynamicsError("Taylor-rule parameters must be finite")
        if phi_pi <= 0.0:
            raise DynamicsError("phi_pi must be positive")
        if phi_y < 0.0:
            raise DynamicsError("phi_y must be non-negative")
        if pi_target <= 0.0:
            raise DynamicsError("pi_target must be positive")
        self.r_star = r_star
        self.pi_target = pi_target
        self.phi_pi = phi_pi
        self.phi_y = phi_y

    @property
    def neutral_rate(self) -> float:
        """Nominal rate when inflation is on target and the output gap is zero."""
        return self.r_star + self.pi_target

    @property
    def taylor_principle(self) -> bool:
        """The reaction is 'active' when the nominal rate rises more than one-for-one with inflation."""
        return self.phi_pi > 1.0

    def rate(self, inflation: float, output_gap: float = 0.0) -> float:
        inflation, output_gap = float(inflation), float(output_gap)
        if not math.isfinite(inflation) or not math.isfinite(output_gap):
            raise DynamicsError("inflation and output_gap must be finite")
        return (self.r_star + inflation
                + self.phi_pi * (inflation - self.pi_target)
                + self.phi_y * output_gap)

    def mechanism(self) -> Mechanism:
        return TAYLOR_RULE_MECHANISM

    def to_dict(self) -> dict:
        return {"r_star": self.r_star, "pi_target": self.pi_target,
                "phi_pi": self.phi_pi, "phi_y": self.phi_y,
                "neutral_rate": self.neutral_rate,
                "taylor_principle": self.taylor_principle,
                "mechanism": self.mechanism().to_dict()}


class NewKeynesianDeterminacySystem:
    """Linearised forward-looking New Keynesian system: determinacy as a hypothesis check.

    Deviations from the target/steady state:

        pi_t = beta * E_t pi_{t+1} + kappa * y_t          (forward-looking Phillips curve)
        y_t  = E_t y_{t+1} - (1/sigma) * (i_t - E_t pi_{t+1})   (IS curve)
        i_t  = phi_pi * pi_t + phi_y * y_t                (Taylor rule, deviations)

    Substituting the policy rule and the Phillips curve gives E_t z_{t+1} = A z_t with
    z = (pi, y).  With both variables non-predetermined, the rational-expectations solution is
    unique (DETERMINATE) exactly when both eigenvalues of A lie strictly outside the unit circle
    (Blanchard-Kahn).  Under the standard calibration the Taylor principle phi_pi > 1 is what
    delivers determinacy; that is a theoretical statement conditional on the model, not evidence
    that the central bank behaves this way.
    """

    def __init__(self, beta: float = 0.99, kappa: float = 0.30, sigma: float = 1.0,
                 rule: TaylorRule | None = None):
        beta, kappa, sigma = float(beta), float(kappa), float(sigma)
        if not all(math.isfinite(item) for item in (beta, kappa, sigma)):
            raise DynamicsError("system parameters must be finite")
        if not 0.0 < beta < 1.0:
            raise DynamicsError("beta must lie in (0, 1)")
        if kappa <= 0.0 or sigma <= 0.0:
            raise DynamicsError("kappa and sigma must be positive")
        self.beta = beta
        self.kappa = kappa
        self.sigma = sigma
        self.rule = TaylorRule() if rule is None else rule

    def expectational_matrix(self) -> np.ndarray:
        beta, kappa, sigma = self.beta, self.kappa, self.sigma
        phi_pi, phi_y = self.rule.phi_pi, self.rule.phi_y
        return np.array([
            [1.0 / beta, -kappa / beta],
            [phi_pi / sigma - 1.0 / (sigma * beta),
             1.0 + phi_y / sigma + kappa / (sigma * beta)],
        ])

    def determinacy(self) -> dict:
        matrix = self.expectational_matrix()
        eigenvalues = np.linalg.eigvals(matrix)
        n_outside = int(np.count_nonzero(np.abs(eigenvalues) > 1.0 + 1e-9))
        n_inside = int(np.count_nonzero(np.abs(eigenvalues) < 1.0 - 1e-9))
        # both variables are non-predetermined -> determinacy needs two eigenvalues outside
        determinate = n_outside == 2
        stability = analyse_jacobian(matrix, discrete=True)
        return {
            "expectational_matrix": matrix.tolist(),
            "eigenvalues": [{"real": float(value.real), "imag": float(value.imag)}
                            for value in eigenvalues],
            "n_outside_unit_circle": n_outside,
            "n_inside_unit_circle": n_inside,
            "taylor_principle": self.rule.taylor_principle,
            "classification": "DETERMINATE" if determinate else "INDETERMINATE",
            "determinate": bool(determinate),
            "criterion": ("Blanchard-Kahn: обе неопределённые переменные требуют двух собственных "
                          "значений вне единичного круга"),
            "linearisation_stability_check": stability["classification"],
            "caveat": LOCAL_STABILITY_CAVEAT,
            "note": ("детерминированность — локальное свойство линеаризованной модели при данных "
                     "параметрах; не утверждает, как ведёт себя реальная политика"),
            "mechanism": TAYLOR_RULE_MECHANISM.to_dict(),
        }

