"""Rolling-origin walk-forward validation for the learning loop (wave 21, track F, docs/151).

The skeleton is deliberately small and strict:

* **No future data.**  A fold is ``(train indices, test indices)`` with ``test_start > train_end``;
  the forecast function receives a *copy of the training window only* (a tuple) and must return
  exactly ``horizon`` values.  The folds are validated with :func:`assert_no_leakage` and the result
  records the leakage check, so a caller cannot silently pass the whole series to the model.
* **Standard metrics.**  MAE and RMSE per forecast step and pooled; CRPS when the forecast function
  returns quantiles (``CRPS = 2 * mean pinball loss`` over the supplied quantile levels, the same
  convention as the evaluation harness).
* **Naive benchmark.**  Every run is compared with the random walk (last observed value repeated);
  ``relative_error = model / naive`` and ``beats_naive`` are reported.  A custom benchmark callable
  can be passed instead.
* **Deterministic and offline.**  No randomness, no network, no clock: the same series and forecast
  function produce the same numbers.

Public API
----------
``folds`` / ``assert_no_leakage``      fold construction and the leakage guard.
``run_walk_forward``                   rolling-origin evaluation with metrics and comparison.
``naive_last_forecast`` / ``mae`` / ``rmse`` / ``crps_from_quantiles``   building blocks.
"""
from __future__ import annotations

import math
from typing import Callable, Mapping, Sequence

MIN_TRAIN = 12
DEFAULT_HORIZON = 1
QUANTILE_LEVELS = tuple(round(0.05 * index, 2) for index in range(1, 20))


class WalkForwardError(ValueError):
    """Invalid series/folds/forecast output; the walk-forward run is refused."""


# --------------------------------------------------------------------------------------- helpers
def _series(values: Sequence[float], name: str = "series") -> tuple[float, ...]:
    try:
        array = tuple(float(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise WalkForwardError(f"{name} must be a sequence of numbers") from exc
    if not array:
        raise WalkForwardError(f"{name} must not be empty")
    if not all(math.isfinite(value) for value in array):
        raise WalkForwardError(f"{name} contains non-finite values")
    return array


def _positive_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WalkForwardError(f"{name} must be a positive integer, got {value!r}")
    return value


def mae(errors: Sequence[float]) -> float:
    values = tuple(float(error) for error in errors)
    return math.fsum(abs(value) for value in values) / len(values) if values else float("nan")


def rmse(errors: Sequence[float]) -> float:
    values = tuple(float(error) for error in errors)
    if not values:
        return float("nan")
    return math.sqrt(math.fsum(value * value for value in values) / len(values))


def crps_from_quantiles(quantiles: Mapping[float, float] | Sequence[tuple[float, float]],
                        actual: float) -> float:
    """``2 * mean pinball loss`` over the supplied quantile levels (harness convention)."""
    if isinstance(quantiles, Mapping):
        pairs = [(float(level), float(value)) for level, value in quantiles.items()]
    else:
        pairs = [(float(level), float(value)) for level, value in quantiles]
    if not pairs:
        raise WalkForwardError("quantiles must not be empty for CRPS")
    loss = 0.0
    for level, value in pairs:
        if not 0.0 < level < 1.0:
            raise WalkForwardError(f"quantile level must be in (0, 1), got {level}")
        if not math.isfinite(value) or not math.isfinite(float(actual)):
            raise WalkForwardError("quantile values and actual must be finite")
        diff = float(actual) - value
        loss += 2.0 * (level * diff if diff >= 0.0 else (level - 1.0) * diff)
    return loss / len(pairs)


def naive_last_forecast(values: Sequence[float], horizon: int) -> list[float]:
    """Random-walk benchmark: the last observed value repeated ``horizon`` times."""
    series = _series(values, "benchmark series")
    _positive_int(horizon, "horizon")
    return [series[-1]] * int(horizon)


# ---------------------------------------------------------------------------------------- folds
def folds(n_observations: int, *, min_train: int = MIN_TRAIN, horizon: int = DEFAULT_HORIZON,
          step: int = 1) -> list[dict]:
    """Rolling-origin folds with an expanding training window.

    Fold ``k``: train ``[0, origin]`` (``origin = min_train + k*step - 1``), test
    ``[origin + 1, origin + horizon]``.  The forecast origin is the last training observation.
    """
    if isinstance(n_observations, bool) or not isinstance(n_observations, int) \
            or n_observations < 1:
        raise WalkForwardError("n_observations must be a positive integer")
    minimum = _positive_int(min_train, "min_train")
    h = _positive_int(horizon, "horizon")
    s = _positive_int(step, "step")
    if n_observations < minimum + h:
        raise WalkForwardError(
            f"series too short for a walk-forward run: n={n_observations}, "
            f"min_train={minimum}, horizon={h}")
    out = []
    origin = minimum - 1
    number = 1
    while origin + h <= n_observations - 1:
        out.append({
            "fold": number,
            "origin_index": origin,
            "train_start": 0,
            "train_end_exclusive": origin + 1,
            "test_start": origin + 1,
            "test_end_exclusive": origin + h + 1,
            "horizon": h,
        })
        origin += s
        number += 1
    assert_no_leakage(out)
    return out


def assert_no_leakage(fold_list: Sequence[Mapping]) -> None:
    """Fail-closed guard: every test index must be strictly after the training window."""
    if not fold_list:
        raise WalkForwardError("no folds were produced")
    for fold in fold_list:
        train_end = int(fold["train_end_exclusive"])
        test_start = int(fold["test_start"])
        test_end = int(fold["test_end_exclusive"])
        if test_start <= train_end - 1:
            raise WalkForwardError(
                f"future leakage in fold {fold.get('fold')}: test starts at {test_start} but "
                f"training ends at {train_end - 1}")
        if test_end <= test_start:
            raise WalkForwardError(f"empty test window in fold {fold.get('fold')}")
        if int(fold["horizon"]) != test_end - test_start:
            raise WalkForwardError(f"horizon mismatch in fold {fold.get('fold')}")


# ------------------------------------------------------------------------------------------ run
def _forecast_point(forecast_fn: Callable, train: tuple[float, ...], horizon: int) -> dict:
    """Call the user function on the training window only and validate its output."""
    try:
        raw = forecast_fn(train, horizon)
    except WalkForwardError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface as a validation error
        raise WalkForwardError(f"forecast function failed: {exc!r}") from exc
    if not isinstance(raw, Mapping) or "point" not in raw:
        raise WalkForwardError("forecast function must return a mapping with 'point'")
    point = tuple(float(value) for value in raw["point"])
    if len(point) != horizon:
        raise WalkForwardError(
            f"forecast function returned {len(point)} values for horizon {horizon}")
    if not all(math.isfinite(value) for value in point):
        raise WalkForwardError("forecast point contains non-finite values")
    result = {"point": point}
    if raw.get("quantiles") is not None:
        quantiles = raw["quantiles"]
        if len(quantiles) != horizon:
            raise WalkForwardError(
                f"quantile forecasts must have one mapping per step, got {len(quantiles)}")
        clean = []
        for entry in quantiles:
            base = _validate_quantiles(entry)
            clean.append(base)
        result["quantiles"] = clean
    return result


def _validate_quantiles(entry) -> dict:
    if isinstance(entry, Mapping):
        pairs = {float(level): float(value) for level, value in entry.items()}
    elif isinstance(entry, Sequence):
        pairs = {float(level): float(value) for level, value in entry}
    else:
        raise WalkForwardError("per-step quantiles must be a mapping level -> value")
    if not pairs:
        raise WalkForwardError("per-step quantiles must not be empty")
    for level, value in pairs.items():
        if not 0.0 < level < 1.0:
            raise WalkForwardError(f"quantile level must be in (0, 1), got {level}")
        if not math.isfinite(value):
            raise WalkForwardError("quantile value must be finite")
    return pairs


def run_walk_forward(series: Sequence[float], forecast_fn: Callable, *,
                     min_train: int = MIN_TRAIN, horizon: int = DEFAULT_HORIZON, step: int = 1,
                     benchmark: Callable | None = None, include_raw: bool = False) -> dict:
    """Rolling-origin evaluation with MAE/RMSE/CRPS and the naive-benchmark comparison."""
    values = _series(series)
    folds_list = folds(len(values), min_train=min_train, horizon=horizon, step=step)
    assert_no_leakage(folds_list)
    if not callable(forecast_fn):
        raise WalkForwardError("forecast_fn must be callable")
    bench_fn = naive_last_forecast if benchmark is None else benchmark
    if not callable(bench_fn):
        raise WalkForwardError("benchmark must be callable")
    records: list[dict] = []
    model_errors: list[float] = []
    bench_errors: list[float] = []
    crps_values: list[float] = []
    per_step: dict = {index + 1: {"model": [], "benchmark": [], "crps": []}
                      for index in range(horizon)}
    for fold in folds_list:
        train = tuple(values[:fold["train_end_exclusive"]])
        actual = tuple(values[fold["test_start"]:fold["test_end_exclusive"]])
        forecast = _forecast_point(forecast_fn, train, horizon)
        benchmark_point = tuple(float(value) for value in bench_fn(train, horizon))
        if len(benchmark_point) != horizon:
            raise WalkForwardError(
                f"benchmark returned {len(benchmark_point)} values for horizon {horizon}")
        record = {"fold": fold["fold"], "origin_index": fold["origin_index"],
                  "train_size": len(train), "test_start": fold["test_start"],
                  "test_end_exclusive": fold["test_end_exclusive"],
                  "leakage": {"train_indices": [0, len(train) - 1],
                              "test_start": fold["test_start"],
                              "no_future": fold["test_start"] > len(train) - 1}}
        model_step = []
        bench_step = []
        interval: list[list[float]] = []
        for step_index in range(horizon):
            model_error = forecast["point"][step_index] - actual[step_index]
            bench_error = benchmark_point[step_index] - actual[step_index]
            model_errors.append(model_error)
            bench_errors.append(bench_error)
            model_step.append(round(forecast["point"][step_index], 9))
            bench_step.append(round(benchmark_point[step_index], 9))
            per_step[step_index + 1]["model"].append(model_error)
            per_step[step_index + 1]["benchmark"].append(bench_error)
            if "quantiles" in forecast:
                entry = forecast["quantiles"][step_index]
                levels = sorted(entry)
                interval.append([round(entry[levels[0]], 9), round(entry[levels[-1]], 9)])
                value = crps_from_quantiles(entry, actual[step_index])
                crps_values.append(value)
                per_step[step_index + 1]["crps"].append(value)
        record.update({"model_point": model_step, "benchmark_point": bench_step,
                       "actual": list(actual),
                       "model_errors": [round(value, 9) for value in
                                        (model_errors[-horizon:])]})
        if interval:
            record["interval"] = interval
        if include_raw and "quantiles" in forecast:
            record["quantiles"] = [dict(entry) for entry in forecast["quantiles"]]
        records.append(record)
    model_mae = mae(model_errors)
    bench_mae = mae(bench_errors)
    model_rmse = rmse(model_errors)
    bench_rmse = rmse(bench_errors)
    if bench_mae <= 0.0 or bench_rmse <= 0.0:
        raise WalkForwardError("the naive benchmark has zero error; comparison is undefined")
    model = {"mae": round(model_mae, 9), "rmse": round(model_rmse, 9),
             "n_forecasts": len(model_errors),
             "crps": (round(math.fsum(crps_values) / len(crps_values), 9)
                      if crps_values else None),
             "per_step": {str(index + 1): {
                 "mae": round(mae(per_step[index + 1]["model"]), 9),
                 "rmse": round(rmse(per_step[index + 1]["model"]), 9),
                 "crps": (round(math.fsum(per_step[index + 1]["crps"])
                                / len(per_step[index + 1]["crps"]), 9)
                          if per_step[index + 1]["crps"] else None)}
                 for index in range(horizon)}}
    benchmark_block = {"mae": round(bench_mae, 9), "rmse": round(bench_rmse, 9),
                       "per_step": {str(index + 1): {
                           "mae": round(mae(per_step[index + 1]["benchmark"]), 9),
                           "rmse": round(rmse(per_step[index + 1]["benchmark"]), 9)}
                           for index in range(horizon)}}
    comparison = {
        "relative_mae": round(model["mae"] / bench_mae, 6),
        "relative_rmse": round(model["rmse"] / bench_rmse, 6),
        "beats_naive": bool(model_mae < bench_mae),
        "relative_error_improvement": round(1.0 - model["mae"] / bench_mae, 6),
        "benchmark": "naive_last" if benchmark is None else "custom",
    }
    passes = bool(comparison["beats_naive"] and result_leakage_ok(records))
    result = {
        "schema": "AMFE-WALKFORWARD/1.0.0",
        "n_observations": len(values), "min_train": int(min_train),
        "horizon": int(horizon), "step": int(step), "n_folds": len(records),
        "folds": [{"fold": record["fold"], "origin_index": record["origin_index"],
                   "train_size": record["train_size"], "test_start": record["test_start"],
                   "test_end_exclusive": record["test_end_exclusive"]} for record in records],
        "leakage_checks": {"no_future": all(record["leakage"]["no_future"] for record in records),
                           "test_starts_after_train": True},
        "model": model, "benchmark": benchmark_block, "comparison": comparison,
        "pass": passes,
    }
    # used by the quality function: the caller passes these to evaluate_quality()
    result["quality_inputs"] = {"error": model["mae"], "benchmark_error": bench_mae,
                                "n_oos": model["n_forecasts"],
                                "balance_residual_max": 0.0, "future_leakage": False,
                                "interval_coverage": _coverage(records)}
    if include_raw:
        result["records"] = records
    return result


def result_leakage_ok(records: Sequence[Mapping]) -> bool:
    return all(bool(record["leakage"]["no_future"]) for record in records)


def _coverage(records: Sequence[Mapping]) -> float | None:
    """Fraction of actuals inside the central interval when quantiles were supplied."""
    inside = 0
    total = 0
    for record in records:
        interval = record.get("interval")
        if not interval:
            continue
        for step_index, actual in enumerate(record["actual"]):
            low, high = interval[step_index]
            total += 1
            if low <= actual <= high:
                inside += 1
    return round(inside / total, 9) if total else None


__all__ = ["DEFAULT_HORIZON", "MIN_TRAIN", "QUANTILE_LEVELS", "WalkForwardError",
           "assert_no_leakage", "crps_from_quantiles", "folds", "mae", "naive_last_forecast",
           "result_leakage_ok", "rmse", "run_walk_forward"]
