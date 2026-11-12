"""Local stability analysis via Jacobian eigenvalues - with an explicit epistemic caveat.

For a discrete-time system z_{t+1} = F(z_t, theta) the dynamics are locally stable at a point z*
when every eigenvalue of the Jacobian dF/dz at z* lies strictly inside the unit circle; for the
continuous-time analogue z' = F(z) stable means every eigenvalue has a strictly negative real part.

What this DOES say: given the equations, the parameters and the linearisation point, a small
perturbation decays (stable) or grows (unstable), and the classification is reproducible.

What this DOES NOT say: that a real economy will or will not enter a crisis.  Linearisation is
local, ignores non-linearities, regime switches, constraints and stochastic shocks, and says
nothing about the probability of a crisis or about the sign of the eventual outcome.  The caveat
is attached to every report so that a stability verdict cannot be quoted as a crisis forecast.
"""
from __future__ import annotations

import math

import numpy as np

from .base import DynamicsError

LOCAL_STABILITY_CAVEAT = (
    "Анализ локальной устойчивости по собственным значениям якобиана описывает только поведение в "
    "окрестности точки линеаризации при заданных уравнениях и параметрах. Он не доказывает наступление "
    "реального кризиса, не оценивает его вероятность, не учитывает нелинейности, ограничения, переключения "
    "режимов и стохастические шоки и не заменяет out-of-sample проверку."
)


def numerical_jacobian(function, point, epsilon: float = 1e-6) -> np.ndarray:
    """Central-difference Jacobian of a vector function at a point."""
    x0 = np.asarray(point, dtype=float)
    if x0.ndim != 1 or x0.size == 0:
        raise DynamicsError("point must be a non-empty 1-D array")
    epsilon = float(epsilon)
    if not math.isfinite(epsilon) or epsilon <= 0.0:
        raise DynamicsError("epsilon must be positive")
    base = np.asarray(function(x0), dtype=float)
    if base.ndim != 1:
        raise DynamicsError("function must return a 1-D array")
    jacobian = np.empty((base.size, x0.size))
    for column in range(x0.size):
        step = np.zeros_like(x0)
        step[column] = epsilon
        plus = np.asarray(function(x0 + step), dtype=float)
        minus = np.asarray(function(x0 - step), dtype=float)
        if plus.size != base.size or minus.size != base.size:
            raise DynamicsError("function returned inconsistent shapes")
        jacobian[:, column] = (plus - minus) / (2.0 * epsilon)
    if not np.all(np.isfinite(jacobian)):
        raise DynamicsError("non-finite Jacobian entry")
    return jacobian


def analyse_jacobian(matrix, discrete: bool = True, tolerance: float = 1e-9) -> dict:
    """Eigenvalues of a Jacobian and the local-stability verdict (with caveat)."""
    jacobian = np.asarray(matrix, dtype=float)
    if jacobian.ndim != 2 or jacobian.shape[0] != jacobian.shape[1]:
        raise DynamicsError("Jacobian must be a square matrix")
    if jacobian.shape[0] == 0:
        raise DynamicsError("Jacobian must not be empty")
    if not np.all(np.isfinite(jacobian)):
        raise DynamicsError("Jacobian contains non-finite values")
    tolerance = float(tolerance)
    eigenvalues = np.linalg.eigvals(jacobian)
    if discrete:
        magnitudes = np.abs(eigenvalues)
        spectral_radius = float(magnitudes.max())
        stable = bool(np.all(magnitudes < 1.0 - tolerance))
        marginal = bool(np.any(np.abs(magnitudes - 1.0) <= tolerance))
        if stable:
            classification = "STABLE"
        elif marginal:
            classification = "MARGINAL"
        else:
            classification = "UNSTABLE"
        margin = float(1.0 - spectral_radius)
        criterion = "все |lambda| < 1 (дискретное время)"
    else:
        real_parts = np.real(eigenvalues)
        spectral_radius = float(np.abs(eigenvalues).max())
        stable = bool(np.all(real_parts < -tolerance))
        has_positive = bool(np.any(real_parts > tolerance))
        has_negative = bool(np.any(real_parts < -tolerance))
        if stable:
            classification = "STABLE"
        elif has_positive and has_negative:
            classification = "SADDLE"
        elif np.any(np.abs(real_parts) <= tolerance):
            classification = "MARGINAL"
        else:
            classification = "UNSTABLE"
        margin = float(-float(real_parts.max()))
        criterion = "все Re(lambda) < 0 (непрерывное время)"
    return {
        "eigenvalues": [{"real": float(value.real), "imag": float(value.imag)}
                        for value in eigenvalues],
        "spectral_radius": spectral_radius,
        "discrete": bool(discrete),
        "criterion": criterion,
        "stable": stable,
        "classification": classification,
        "margin": margin,
        "caveat": LOCAL_STABILITY_CAVEAT,
        "note": "локальная линеаризация; классификация не является прогнозом кризиса",
    }


def analyse_system(function, point, discrete: bool = True, epsilon: float = 1e-6) -> dict:
    """Numerical Jacobian of `function` at `point` plus the local-stability verdict."""
    jacobian = numerical_jacobian(function, point, epsilon=epsilon)
    report = analyse_jacobian(jacobian, discrete=discrete)
    report["jacobian"] = jacobian.tolist()
    report["point"] = [float(value) for value in np.asarray(point, dtype=float)]
    return report


def simulate_linear(matrix, initial, periods: int) -> dict:
    """Deterministic linear map z_{t+1} = A @ z_t; useful for convergence/divergence checks."""
    matrix = np.asarray(matrix, dtype=float)
    state = np.asarray(initial, dtype=float)
    periods = int(periods)
    if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1]:
        raise DynamicsError("matrix must be square")
    if state.shape != (matrix.shape[0],):
        raise DynamicsError("initial state does not match the matrix")
    if periods < 1:
        raise DynamicsError("periods must be a positive integer")
    trajectory = np.empty((periods, state.size))
    for step in range(periods):
        trajectory[step] = state
        state = matrix @ state
    return {"path": trajectory, "final": trajectory[-1].copy(),
            "max_abs_final": float(np.max(np.abs(trajectory[-1])))}
