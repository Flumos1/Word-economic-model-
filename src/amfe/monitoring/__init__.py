"""AMFE monitoring layer (roadmap stages 8-9): degradation monitoring, versioning and rollback.

Modules
-------
versions   versioned models/weights and the append-only hash-chained journal
metrics    rolling MAE/RMSE/CRPS/Brier, EWMA/CUSUM/Page-Hinkley detectors
drift      data-drift and concept-drift detectors
decision   C1/C2-logic monitoring rule and the rollback controller
contours   fast/slow update contours and the observation gate (no update without observations)
loop       end-to-end monitoring loop wiring the stages together

The layer is independent of the forecast models: it accepts numpy arrays and simple observation
objects, so it can run on synthetic data before any candidate is admitted (docs/76: no admitted
candidate yet) and can later be fed from the forward registry.
"""
from .contours import (  # noqa: F401
    ALLOWED_DATA_CLASSES,
    KNOWN_DATA_CLASSES,
    ContourCoordinator,
    ContourUpdate,
    FastContour,
    InverseMSEWeights,
    MonitoringError,
    Observation,
    ObservationGate,
    SlowContour,
    WeightUpdater,
)
from .decision import (  # noqa: F401
    ALPHA,
    DECISION_ACTIONS,
    MIN_OBSERVATIONS,
    DecisionError,
    RollbackController,
    SwitchRule,
    aggregate_loss,
    hln_factor,
    load_threshold_table,
    newey_west_variance,
    one_sided_dm,
    relative_loss,
    strict_threshold,
)
from .drift import (  # noqa: F401
    ConceptDriftReport,
    DriftError,
    DriftMonitor,
    DriftReport,
    FeatureDriftReport,
    detect_concept_drift,
    detect_data_drift,
    ks_p_value,
    ks_statistic,
    ks_test,
    mean_shift_test,
    psi,
    relationship_drift,
    variance_ratio,
)
from .loop import LoopError, MonitoringLoop  # noqa: F401
from .metrics import (  # noqa: F401
    LOG_CLIP,
    QUANTILES,
    DetectionResult,
    MetricError,
    RollingMetrics,
    absolute_error,
    brier,
    crps,
    cusum,
    detect_all,
    ewma,
    ewma_detector,
    log_score,
    long_run_sigma,
    newey_west_variance,
    page_hinkley,
    pinball_matrix,
    rolling,
    rolling_brier,
    rolling_crps,
    rolling_mae,
    rolling_rmse,
    squared_error,
)
from .versions import (  # noqa: F401
    GENESIS_HASH,
    ModelVersion,
    VersionError,
    VersionJournal,
    canonical_json,
    hash_files,
    hash_source,
    make_version,
    sha256_text,
    utc_now,
    weights_hash,
)

__all__ = [
    "ALPHA", "ALLOWED_DATA_CLASSES", "ConceptDriftReport", "ContourCoordinator",
    "ContourUpdate", "DECISION_ACTIONS", "DecisionError", "DetectionResult", "DriftError",
    "DriftMonitor", "DriftReport", "FastContour", "FeatureDriftReport", "GENESIS_HASH",
    "InverseMSEWeights", "KNOWN_DATA_CLASSES", "LOG_CLIP", "LoopError", "MIN_OBSERVATIONS",
    "MetricError", "ModelVersion", "MonitoringError", "MonitoringLoop", "Observation",
    "ObservationGate", "QUANTILES", "RollingMetrics", "RollbackController", "SlowContour",
    "SwitchRule", "VersionError", "VersionJournal", "WeightUpdater", "absolute_error",
    "aggregate_loss", "brier", "canonical_json", "crps", "cusum", "detect_all",
    "detect_concept_drift", "detect_data_drift", "ewma", "ewma_detector", "hash_files",
    "hash_source", "hln_factor", "ks_p_value", "ks_statistic", "ks_test", "load_threshold_table",
    "log_score", "long_run_sigma", "make_version", "mean_shift_test", "newey_west_variance",
    "one_sided_dm", "page_hinkley", "pinball_matrix", "psi", "relationship_drift",
    "relative_loss", "rolling", "rolling_brier", "rolling_crps", "rolling_mae",
    "rolling_rmse", "sha256_text", "squared_error", "strict_threshold", "utc_now",
    "variance_ratio", "weights_hash",
]
