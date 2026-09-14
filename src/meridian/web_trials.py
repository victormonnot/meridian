"""Validate recorded full-run RMSE values for repeated simulation trials."""

import math


METHODS = ("gyro", "complementary", "kalman", "ekf")
PAIRED_METRICS = {
    "gyro": "gyro_angle_rmse_deg",
    "complementary": "complementary_angle_rmse_deg",
    "kalman": "angle_kalman_angle_rmse_deg",
    "ekf": "ekf_angle_rmse_deg",
}
EXPERIMENTS = {"paired": "vector_ekf_comparison", "controlled": "controlled_scenarios"}
# Match the existing display adapter's full-resolution metric checks.
AGGREGATE_ABS_TOL = 1e-10
AGGREGATE_REL_TOL = 1e-10


def _mapping(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"missing or invalid repeated-trial {label}")
    return value


def _seed(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**53 - 1:
        raise ValueError("repeated-trial seeds must be nonnegative JavaScript-safe integers")
    return value


def _nonnegative(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"repeated-trial {label} must be a finite nonnegative number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < 0:
        raise ValueError(f"repeated-trial {label} must be a finite nonnegative number")
    return value


def build_repeated_trials(summary: dict, name: str, *, source: str) -> dict:
    """Extract one case's saved endpoint RMSE, without evaluating any estimator.

    Trial order must match the declared seed order. Source aggregates are checked
    for coherence; this does not reconstruct metrics from unavailable repeated
    trajectories or establish statistical confidence bounds.
    """
    if source not in EXPERIMENTS:
        raise ValueError("unsupported repeated-trial source")
    summary = _mapping(summary, "summary")
    if (summary.get("schema_version") != 1 or summary.get("data_source") != "simulation"
            or summary.get("experiment") != EXPERIMENTS[source]):
        raise ValueError("unsupported repeated-trial experiment")
    validation = _mapping(summary.get("validation"), "validation")
    seeds, trials = validation.get("seeds"), validation.get("trials")
    if not isinstance(seeds, list) or not seeds:
        raise ValueError("repeated-trial seeds must be a nonempty list")
    seeds = [_seed(seed) for seed in seeds]
    if len(set(seeds)) != len(seeds):
        raise ValueError("repeated-trial seeds must be unique")
    if not isinstance(trials, list) or len(trials) != len(seeds):
        raise ValueError("repeated-trial count must match declared seeds")

    rmse = {method: [] for method in METHODS}
    for seed, trial in zip(seeds, trials):
        trial = _mapping(trial, "record")
        if _seed(trial.get("seed")) != seed:
            raise ValueError("repeated-trial order must match declared seeds")
        cases = trial if source == "paired" else _mapping(trial.get("scenarios"), "scenarios")
        case = _mapping(cases.get(name), f"case {name}")
        metrics = case if source == "paired" else _mapping(case.get("metrics"), f"{name} metrics")
        for method in METHODS:
            value = (metrics.get(PAIRED_METRICS[method]) if source == "paired" else
                     _mapping(metrics.get(method), f"{name}/{method} metrics").get("angle_rmse_deg"))
            rmse[method].append(_nonnegative(value, f"{name}/{method} RMSE"))

    aggregates = _mapping(validation.get("aggregates"), "aggregates")
    aggregates = _mapping(aggregates.get(name), f"{name} aggregates")
    for method, values in rmse.items():
        aggregate = _mapping(aggregates.get(PAIRED_METRICS[method] if source == "paired" else method),
                             f"{name}/{method} aggregate")
        if source == "controlled":
            aggregate = _mapping(aggregate.get("angle_rmse_deg"), f"{name}/{method} RMSE aggregate")
        # Dividing first keeps the mean finite for finite nonnegative inputs.
        expected = {"mean": math.fsum(value / len(values) for value in values), "worst": max(values)}
        for key, computed in expected.items():
            recorded = _nonnegative(aggregate.get(key), f"{name}/{method} aggregate {key}")
            if not math.isclose(recorded, computed, abs_tol=AGGREGATE_ABS_TOL, rel_tol=AGGREGATE_REL_TOL):
                raise ValueError(f"inconsistent repeated-trial {name}/{method} aggregate {key}")
    return {"seeds": seeds, "rmse_deg": rmse}
