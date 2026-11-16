"""Learning and validation loop (wave 21, track F, docs/145, docs/151).

Public surface:
  * :mod:`amfe.learning.quality` — ``evaluate_quality`` (Q = f(E, C, S, R)) with hard constraints
    (balances, no future leakage, minimum OOS count, naive benchmark) and ``compare_versions``;
  * :mod:`amfe.learning.registry` — append-only JSONL model registry with statuses
    CANDIDATE/SHADOW/LIMITED/ACTIVE/REJECTED, preregistered-criteria and independent-review gates
    for ACTIVE, ``reject`` and ``revert`` (rollback);
  * :mod:`amfe.learning.walkforward` — rolling-origin validation with MAE/RMSE/CRPS, a naive
    benchmark and an explicit no-future-leakage guard.

The loop automates *parameters and evidence*; a change of architecture or an ACTIVE promotion
always requires a human (independent review), per docs/145.
"""

from .quality import (  # noqa: F401
    DEFAULT_WEIGHTS,
    QualityError,
    QualityResult,
    check_hard_constraints,
    compare_versions,
    evaluate_quality,
    grade_for,
)
from .registry import (  # noqa: F401
    ACTIVE_MIN_Q,
    ALLOWED_TRANSITIONS,
    SCHEMA as REGISTRY_SCHEMA,
    STATUSES,
    RegistryError,
    load_events,
    model_status,
    promote,
    register,
    reject,
    revert,
    sha256_file,
    sha256_text,
    state,
    validate_registry,
)
from .walkforward import (  # noqa: F401
    WalkForwardError,
    assert_no_leakage,
    crps_from_quantiles,
    folds,
    mae,
    naive_last_forecast,
    rmse,
    run_walk_forward,
)

__all__ = [
    "ACTIVE_MIN_Q", "ALLOWED_TRANSITIONS", "DEFAULT_WEIGHTS", "REGISTRY_SCHEMA", "STATUSES",
    "QualityError", "QualityResult", "RegistryError", "WalkForwardError", "assert_no_leakage",
    "check_hard_constraints", "compare_versions", "crps_from_quantiles", "evaluate_quality",
    "folds", "grade_for", "load_events", "mae", "model_status", "naive_last_forecast", "promote",
    "register", "reject", "revert", "rmse", "run_walk_forward", "sha256_file", "sha256_text",
    "state", "validate_registry",
]
